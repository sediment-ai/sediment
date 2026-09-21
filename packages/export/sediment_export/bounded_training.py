# SPDX-License-Identifier: AGPL-3.0-or-later
"""Training orchestration over complete evidence groups and private row files."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path
from typing import Literal

from sediment_derive import inference_model, inference_prompt_key
from sediment_derive.repository_identity import commit_sort_key

from . import dpo, sft, diff_sft
from ._record_storage import FileRecords
from .derived_bundle import (
    BundleCapacityError,
    BundleLimits,
    BundleRecordStore,
    DerivedBundle,
    validate_derived_bundle,
)
from .jsonl import ExportRow
from .staged_rows import ExportRowStore


@dataclass(frozen=True)
class TrainingProjection:
    """Repeatable staged trainer rows and complete projector diagnostics."""

    rows: Sequence[ExportRow]
    skipped: Counter[str]
    remaining_bytes: int


class _CallIndex(Mapping):
    """Source IDs point to file offsets; reads never cache complete Facts."""

    def __init__(self, records: FileRecords, max_group_bytes: int):
        self.records = records
        self.indices = {}
        for index in range(len(records)):
            if records.record_bytes(index) > max_group_bytes:
                raise BundleCapacityError("training source exceeds group byte budget")
            call = records[index]
            self.indices[call.inference_call_id] = index

    def __len__(self):
        return len(self.indices)

    def __iter__(self):
        return iter(self.indices)

    def __getitem__(self, identifier):
        return self.records[self.indices[identifier]]


class _OrderedRows(Sequence):
    """A compact final ordering over an owned staged population."""

    def __init__(self, rows, indices):
        self._rows, self._indices = rows, indices

    def __len__(self):
        return len(self._indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return _OrderedRows(self._rows, self._indices[index])
        return self._rows[self._indices[index]]


class _EvidenceGroups:
    """Record references and exact encoded-byte preflight for active groups."""

    def __init__(self, members, calls, max_bytes):
        self.members, self.calls, self.max_bytes = members, calls, max_bytes
        self.call_ids = []
        for index in range(len(members)):
            if members.record_bytes(index) > max_bytes:
                raise BundleCapacityError("training evidence exceeds group byte budget")
            row = members[index]
            self.call_ids.append(row.inference_call_id)

    def preflight(self, indices):
        total = sum(self.members.record_bytes(index) for index in indices)
        for call_id in {self.call_ids[index] for index in indices}:
            position = self.calls.indices.get(call_id)
            if position is not None:
                total += self.calls.records.record_bytes(position)
        if total > self.max_bytes:
            raise BundleCapacityError("complete training group exceeds byte budget")

    def hydrate(self, indices):
        self.preflight(indices)
        return [self.members[index] for index in indices]


def _bucket_digest(key):
    # Unicode code points remain distinct: escaped surrogate pairs must not
    # collapse into the astral characters that JSON can spell the same way.
    encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", "surrogatepass"
    )
    return hashlib.sha256(encoded).digest()


def _dpo_groups(evidence, skipped):
    representatives = []
    buckets = []
    digest_groups = defaultdict(list)

    def key_at(index):
        call = evidence.calls[evidence.call_ids[index]]
        member = evidence.members[index]
        return member.org_id, inference_model(call), inference_prompt_key(call)

    for index in range(len(evidence.members)):
        evidence.preflight([index])
        local_skipped = Counter()
        # The existing owner decides membership and structural skip precedence.
        candidates, conversations, representation = dpo._bucket_candidates(
            [evidence.members[index]], evidence.calls, local_skipped
        )
        del conversations, representation
        if not candidates:
            skipped.update(local_skipped)
            continue
        key = next(iter(candidates))
        digest = _bucket_digest(key)
        group = None
        for candidate in digest_groups[digest]:
            if key_at(representatives[candidate]) == key:
                group = candidate
                break
        if group is None:
            group = len(buckets)
            buckets.append([])
            representatives.append(index)
            digest_groups[digest].append(group)
        buckets[group].append(index)
        del key, candidates

    def compare(left, right):
        left_key, right_key = (
            key_at(representatives[left]),
            key_at(representatives[right]),
        )
        return (left_key > right_key) - (left_key < right_key)

    # A key= function would retain every decoded prompt while sorting.
    for group in sorted(range(len(buckets)), key=cmp_to_key(compare)):
        yield buckets[group]


def _simple_groups(evidence, objective, skipped):
    groups = defaultdict(list)
    for index in range(len(evidence.members)):
        row = evidence.members[index]
        if objective == "sft":
            key = row.inference_call_id
        elif row.abandonment is not None:
            skipped["abandoned"] += 1
            continue
        else:
            # Complete qualified commits preserve one parsed-diff skip count.
            key = diff_sft._group_key(row)[1]
        groups[key].append(index)
    row = None
    key_function = commit_sort_key if objective == "diff-sft" else None
    for key in sorted(groups, key=key_function):
        yield groups[key]


def _records(values, stage, name):
    if isinstance(values, FileRecords):
        return values
    records = stage.records(name)
    records.extend(values)
    return records.seal()


@contextmanager
def project_training_bundle(
    bundle: DerivedBundle,
    *,
    objective: Literal["sft", "dpo", "diff-sft"],
    policy=None,
    mirrors=None,
    limits: BundleLimits | None = None,
    temporary_parent: Path | None = None,
    max_group_bytes: int = 128 * 1024 * 1024,
) -> Iterator[TrainingProjection]:
    """Validate, project complete evidence groups, and yield unpublished rows.

    Callers keep this context open through publication. The group byte budget
    counts every artifact and each distinct full source Fact before hydration.
    Capacity failures abort the export, never become eligibility exclusions.
    Existing pure projectors own all evidence interpretation and skip counts.
    """
    if objective not in {"sft", "dpo", "diff-sft"}:
        raise ValueError("unsupported training objective")
    if type(max_group_bytes) is not int or max_group_bytes <= 0:
        raise ValueError("max_group_bytes must be a positive integer")
    if objective == "diff-sft" and mirrors is None:
        raise ValueError("diff-SFT requires a MirrorManager")
    context = validate_derived_bundle(bundle)
    with (
        BundleRecordStore(
            limits=limits, temporary_parent=temporary_parent, private=True
        ) as inputs,
        ExportRowStore(limits=limits, temporary_parent=temporary_parent) as stage,
    ):
        calls = _CallIndex(
            _records(bundle.inference_calls, inputs, "inference_calls"), max_group_bytes
        )
        members = _records(
            bundle.attributed_completions, inputs, "attributed_completions"
        )
        evidence = _EvidenceGroups(members, calls, max_group_bytes)
        skipped = Counter()
        groups = (
            _dpo_groups(evidence, skipped)
            if objective == "dpo"
            else _simple_groups(evidence, objective, skipped)
        )
        owner = {"sft": sft, "dpo": dpo, "diff-sft": diff_sft}[objective]
        projector = getattr(owner, f"project_{objective.replace('-', '_')}")
        rows = stage.records("rows")
        order = []
        for indices in groups:
            arguments = (mirrors, policy) if objective == "diff-sft" else (policy,)
            projection = projector(
                evidence.hydrate(indices), calls, *arguments, repository_context=context
            )
            skipped.update(projection.skipped)
            for sample in projection.rows:
                if objective == "diff-sft":
                    order.append(
                        (
                            sample.metadata.completion_id,
                            sample.metadata.commit_sha,
                            len(rows),
                        )
                    )
                rows.append(owner.to_export_rows([sample])[0])
            sample = None
            del projection
        rows.seal()
        result_rows = (
            _OrderedRows(rows, [index for _, _, index in sorted(order)])
            if objective == "diff-sft"
            else rows
        )
        yield TrainingProjection(
            result_rows, skipped, stage.limits.max_staging_bytes - rows.encoded_bytes
        )
