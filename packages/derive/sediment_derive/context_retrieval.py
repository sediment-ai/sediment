# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure keyword selection of exact evidence from one bounded Session source."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field
from sediment_core import (
    ContextCommitAnchor,
    ContextCommitMatch,
    ContextDiscoverySource,
    EvidenceContextSource,
    EvidenceReadItem,
    NonEmptyId,
)
from sediment_core.evidence import (
    EvidenceIndex,
    EvidenceReadError,
    EvidenceSchemaVersion,
    encode_evidence_json,
    validate_evidence_numbers,
)

from .similarity import tokenize

CONTEXT_QUERY_BYTES_LIMIT = 2_048
CONTEXT_MIN_RESPONSE_BYTES = 4_096
CONTEXT_MAX_RESPONSE_BYTES = 65_536
CONTEXT_DEFAULT_RESPONSE_BYTES = 16_384
CONTEXT_ENVELOPE_BYTES = 2_048
CONTEXT_ITEM_LIMIT = 8
CONTEXT_SKIP_REASONS = (
    "reasoning_part",
    "non_finite_number",
    "no_match",
    "repeated_content",
    "item_limit",
    "response_budget",
)
CONTEXT_DISCOVERY_SKIP_REASONS = (
    "reasoning_part",
    "non_finite_number",
    "unmatched_part",
    "unmatched_session",
    "candidate_limit",
    "response_budget",
)
_QUERY_STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how i in is it of on or "
    "that the this to was were what when where which who why with".split()
)


@dataclass(frozen=True)
class ContextRetrievalPolicy:
    """Version 1 pins the token, ranking, exclusion, and packing rules."""

    policy_version: EvidenceSchemaVersion = 1

    def __post_init__(self) -> None:
        if type(self.policy_version) is not int or self.policy_version != 1:
            raise ValueError("unsupported context retrieval policy")


@dataclass(frozen=True)
class ContextRetrievalCoverage:
    __pydantic_config__ = ConfigDict(extra="forbid")

    visible_inference_calls: EvidenceIndex
    quarantined_inference_calls: EvidenceIndex
    scanned_parts: EvidenceIndex
    complete_visible_scan: Literal[True]


@dataclass(frozen=True)
class ContextRetrievalSkipped:
    __pydantic_config__ = ConfigDict(extra="forbid")

    reasoning_part: EvidenceIndex
    non_finite_number: EvidenceIndex
    no_match: EvidenceIndex
    repeated_content: EvidenceIndex
    item_limit: EvidenceIndex
    response_budget: EvidenceIndex


@dataclass(frozen=True)
class ContextRetrievalItem:
    __pydantic_config__ = ConfigDict(extra="forbid")

    score: Annotated[int, Field(strict=True, ge=1)]
    evidence: EvidenceReadItem


@dataclass(frozen=True, kw_only=True)
class ContextRetrievalResult:
    __pydantic_config__ = ConfigDict(extra="forbid")

    schema_version: EvidenceSchemaVersion
    policy_version: EvidenceSchemaVersion
    source_session_id: NonEmptyId
    quarantine_revision: EvidenceIndex
    status: Literal["matched", "no_match", "budget_exhausted"]
    capture_completeness: Literal["unknown"]
    coverage: ContextRetrievalCoverage
    skipped: ContextRetrievalSkipped
    items: Annotated[
        tuple[ContextRetrievalItem, ...], Field(max_length=CONTEXT_ITEM_LIMIT)
    ]


@dataclass(frozen=True)
class ContextDiscoveryPolicy:
    """Version 1 ranks direct commit witnesses and distinct keyword overlap."""

    policy_version: EvidenceSchemaVersion = 1

    def __post_init__(self) -> None:
        if type(self.policy_version) is not int or self.policy_version != 1:
            raise ValueError("unsupported context discovery policy")


@dataclass(frozen=True)
class ContextDiscoveryCoverage:
    __pydantic_config__ = ConfigDict(extra="forbid")

    authorized_sessions: EvidenceIndex
    found_sessions: EvidenceIndex
    visible_inference_calls: EvidenceIndex
    quarantined_inference_calls: EvidenceIndex
    scanned_parts: EvidenceIndex
    matched_parts: EvidenceIndex
    complete_visible_scan: Literal[True]


@dataclass(frozen=True)
class ContextDiscoverySkipped:
    __pydantic_config__ = ConfigDict(extra="forbid")

    reasoning_part: EvidenceIndex
    non_finite_number: EvidenceIndex
    unmatched_part: EvidenceIndex
    unmatched_session: EvidenceIndex
    candidate_limit: EvidenceIndex
    response_budget: EvidenceIndex


@dataclass(frozen=True)
class ContextDiscoveryItem:
    __pydantic_config__ = ConfigDict(extra="forbid")

    session_id: NonEmptyId
    score: EvidenceIndex
    matched_parts: EvidenceIndex
    preview: EvidenceReadItem | None
    commit_match: ContextCommitMatch | None


@dataclass(frozen=True, kw_only=True)
class ContextDiscoveryResult:
    __pydantic_config__ = ConfigDict(extra="forbid")

    schema_version: EvidenceSchemaVersion
    policy_version: EvidenceSchemaVersion
    quarantine_revision: EvidenceIndex
    capture_completeness: Literal["unknown"]
    commit: ContextCommitAnchor | None
    status: Literal["matched", "no_match", "budget_exhausted"]
    coverage: ContextDiscoveryCoverage
    skipped: ContextDiscoverySkipped
    items: Annotated[
        tuple[ContextDiscoveryItem, ...], Field(max_length=CONTEXT_ITEM_LIMIT)
    ]


def context_query_tokens(query: str) -> set[str]:
    """Validate a question without echoing content; return meaningful tokens."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("invalid context query")
    try:
        encoded = query.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid context query") from None
    if len(encoded) > CONTEXT_QUERY_BYTES_LIMIT:
        raise ValueError("invalid context query")
    tokens = tokenize(query) - _QUERY_STOPWORDS
    if not tokens:
        raise ValueError("invalid context query")
    return tokens


def _strict_json(value: object) -> str:
    validate_evidence_numbers(value)
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _search_text(item: EvidenceReadItem) -> str:
    part = item.part
    if part.type == "text":
        return part.content
    if part.type == "tool_call":
        return part.name + " " + _strict_json(part.arguments)
    if part.type == "tool_call_response":
        return _strict_json(part.result)
    raise ValueError("reasoning has no retrieval text")


def _rank_key(item: ContextRetrievalItem) -> tuple:
    reference = item.evidence.reference
    # A timedelta preserves microsecond ties across the entire datetime range;
    # floating-point Unix timestamps can collapse distinct observed instants.
    newest_first = datetime.max.replace(
        tzinfo=UTC
    ) - item.evidence.observed_at.astimezone(UTC)
    return (
        -item.score,
        newest_first,
        reference.inference_call_id,
        0 if reference.side == "input" else 1,
        reference.message_index,
        reference.part_index,
    )


def retrieve_context(
    source: EvidenceContextSource,
    query: str,
    max_bytes: int = CONTEXT_DEFAULT_RESPONSE_BYTES,
    policy: ContextRetrievalPolicy = ContextRetrievalPolicy(),
) -> ContextRetrievalResult:
    """Select complete original parts; never persist or synthesize evidence."""
    query_tokens = context_query_tokens(query)
    if (
        type(max_bytes) is not int
        or not CONTEXT_MIN_RESPONSE_BYTES <= max_bytes <= CONTEXT_MAX_RESPONSE_BYTES
    ):
        raise ValueError("invalid context response budget")
    skipped = Counter({reason: 0 for reason in CONTEXT_SKIP_REASONS})
    candidates = []
    for item in source.items:
        if item.part.type == "reasoning":
            skipped["reasoning_part"] += 1
            continue
        try:
            score = len(query_tokens & tokenize(_search_text(item)))
        except EvidenceReadError:
            skipped["non_finite_number"] += 1
            continue
        if not score:
            skipped["no_match"] += 1
            continue
        candidates.append(ContextRetrievalItem(score, item))

    selected = []
    seen = set()
    remaining = max_bytes - CONTEXT_ENVELOPE_BYTES
    for candidate in sorted(candidates, key=_rank_key):
        item = candidate.evidence
        key = _strict_json(
            {
                "role": item.role,
                "finish_reason": item.finish_reason,
                "part": item.part.model_dump(mode="python"),
            }
        )
        if key in seen:
            skipped["repeated_content"] += 1
            continue
        seen.add(key)
        if len(selected) == CONTEXT_ITEM_LIMIT:
            skipped["item_limit"] += 1
            continue
        comma_bytes = int(bool(selected))
        try:
            encoded = encode_evidence_json(
                candidate, ContextRetrievalItem, max_bytes=remaining - comma_bytes
            )
        except EvidenceReadError as exc:
            if exc.detail["reason"] != "evidence_response_limit":
                raise
            skipped["response_budget"] += 1
            continue
        remaining -= len(encoded) + comma_bytes
        selected.append(candidate)

    result = ContextRetrievalResult(
        schema_version=1,
        policy_version=policy.policy_version,
        source_session_id=source.session_id,
        quarantine_revision=source.quarantine_revision,
        status="matched"
        if selected
        else "budget_exhausted"
        if candidates
        else "no_match",
        capture_completeness="unknown",
        coverage=ContextRetrievalCoverage(
            visible_inference_calls=source.visible_inference_calls,
            quarantined_inference_calls=source.quarantined_inference_calls,
            scanned_parts=len(source.items),
            complete_visible_scan=True,
        ),
        skipped=ContextRetrievalSkipped(**skipped),
        items=tuple(selected),
    )
    encode_evidence_json(
        replace(result, items=()),
        ContextRetrievalResult,
        max_bytes=CONTEXT_ENVELOPE_BYTES,
    )
    encode_evidence_json(result, ContextRetrievalResult, max_bytes=max_bytes)
    return result


def discover_context(
    source: ContextDiscoverySource,
    query: str,
    max_bytes: int = CONTEXT_DEFAULT_RESPONSE_BYTES,
    policy: ContextDiscoveryPolicy = ContextDiscoveryPolicy(),
) -> ContextDiscoveryResult:
    """Rank authorized Sessions without inventing previews or repository scope."""
    query_tokens = context_query_tokens(query)
    if (
        type(max_bytes) is not int
        or not CONTEXT_MIN_RESPONSE_BYTES <= max_bytes <= CONTEXT_MAX_RESPONSE_BYTES
    ):
        raise ValueError("invalid context response budget")
    skipped = Counter({reason: 0 for reason in CONTEXT_DISCOVERY_SKIP_REASONS})
    candidates = []
    scanned_parts = matched_parts = 0
    for session in source.sessions:
        matches = []
        for item in session.items:
            scanned_parts += 1
            if item.part.type == "reasoning":
                skipped["reasoning_part"] += 1
                continue
            try:
                score = len(query_tokens & tokenize(_search_text(item)))
            except EvidenceReadError:
                skipped["non_finite_number"] += 1
                continue
            if score:
                matches.append(ContextRetrievalItem(score, item))
            else:
                skipped["unmatched_part"] += 1
        matched_parts += len(matches)
        if not matches and session.commit_match is None:
            skipped["unmatched_session"] += 1
            continue
        best = min(matches, key=_rank_key) if matches else None
        candidates.append(
            ContextDiscoveryItem(
                session.session_id,
                best.score if best else 0,
                len(matches),
                best.evidence if best else None,
                session.commit_match,
            )
        )

    selected = []
    remaining = max_bytes - CONTEXT_ENVELOPE_BYTES
    for candidate in sorted(
        candidates,
        key=lambda item: (
            item.commit_match is None,
            -item.score,
            item.session_id,
        ),
    ):
        if len(selected) == CONTEXT_ITEM_LIMIT:
            skipped["candidate_limit"] += 1
            continue
        comma_bytes = int(bool(selected))
        try:
            encoded = encode_evidence_json(
                candidate, ContextDiscoveryItem, max_bytes=remaining - comma_bytes
            )
        except EvidenceReadError as exc:
            if exc.detail["reason"] != "evidence_response_limit":
                raise
            skipped["response_budget"] += 1
            continue
        remaining -= len(encoded) + comma_bytes
        selected.append(candidate)

    result = ContextDiscoveryResult(
        schema_version=1,
        policy_version=policy.policy_version,
        quarantine_revision=source.quarantine_revision,
        capture_completeness="unknown",
        commit=source.commit,
        status="matched"
        if selected
        else "budget_exhausted"
        if candidates
        else "no_match",
        coverage=ContextDiscoveryCoverage(
            source.authorized_sessions,
            len(source.sessions),
            source.visible_inference_calls,
            source.quarantined_inference_calls,
            scanned_parts,
            matched_parts,
            True,
        ),
        skipped=ContextDiscoverySkipped(**skipped),
        items=tuple(selected),
    )
    encode_evidence_json(
        replace(result, items=()),
        ContextDiscoveryResult,
        max_bytes=CONTEXT_ENVELOPE_BYTES,
    )
    encode_evidence_json(result, ContextDiscoveryResult, max_bytes=max_bytes)
    return result
