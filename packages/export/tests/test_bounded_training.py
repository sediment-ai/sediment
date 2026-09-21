# SPDX-License-Identifier: AGPL-3.0-or-later
"""Complete evidence groups preserve projector results with bounded payloads."""

from dataclasses import replace
from datetime import timedelta
import json
import tracemalloc

import pytest

from sediment_core import InferenceMessage
from sediment_core.store import InferenceCallIdentity
from sediment_derive import (
    AbandonmentPolicy,
    Provenance,
    SessionAbandonment,
    model_call_ids,
    split_of,
)
from sediment_export import dpo, sft, diff_sft
from sediment_export.derived_bundle import (
    BundleRecordStore,
    BundleCapacityError,
    validate_derived_bundle,
)
from sediment_export.jsonl import write_jsonl
from test_derived_bundle_io import _bundle, _decision
from export_factories import message, tool_call


def _wire(rows):
    return [
        (row.split, json.dumps(row.body, ensure_ascii=True, allow_nan=False))
        for row in rows
    ]


def _training_bundle(prompts=("z", "a"), *, copies=1, source_bytes=0):
    base = _bundle()
    calls, members = [], []
    for bucket, prompt in enumerate(prompts):
        for side in ("chosen", "rejected"):
            identifier = f"{bucket}-{side}"
            session = f"session-{identifier}"
            call = base.inference_calls[0].model_copy(
                update={
                    "inference_call_id": identifier,
                    "model_call_id": f"call-{identifier}",
                    "session_id": session,
                    "observed_at": base.as_of - timedelta(seconds=len(calls)),
                    "raw": {"ignored": "x" * source_bytes},
                    "input_messages": [message("user", prompt)]
                    if isinstance(prompt, str)
                    else prompt,
                    "output_messages": [message("assistant", side)],
                }
            )
            calls.append(call)
            decision = _decision(
                decision_id=f"decision-{identifier}",
                session_id=session,
                call_id=call.model_call_id,
                accepted=side == "chosen",
            )
            member = replace(
                base.attributed_completions[0],
                inference_call_id=identifier,
                session_id=session,
                decisions=[decision],
                split=split_of(session, base.policy.eval_fraction),
            )
            members.extend([member] * copies)
    identities = tuple(
        InferenceCallIdentity(
            call.inference_call_id,
            call.org_id,
            call.session_id,
            call.observed_at,
            tuple(sorted(model_call_ids(call))),
        )
        for call in calls
    )
    return replace(
        base,
        inference_calls=tuple(calls),
        attributed_completions=tuple(members),
        inference_call_identities=identities,
        rollouts=(),
    )


def _expected(bundle, objective, policy=None, mirrors=None):
    context = validate_derived_bundle(bundle)
    calls = {call.inference_call_id: call for call in bundle.inference_calls}
    owner = {"sft": sft, "dpo": dpo, "diff-sft": diff_sft}[objective]
    args = (mirrors, policy) if objective == "diff-sft" else (policy,)
    projection = getattr(owner, f"project_{objective.replace('-', '_')}")(
        bundle.attributed_completions, calls, *args, repository_context=context
    )
    return owner.to_export_rows(projection.rows), projection.skipped


@pytest.mark.parametrize("objective", ["sft", "dpo"])
@pytest.mark.parametrize("reverse", [False, True])
def test_bounded_training_matches_complete_projector_bytes_and_counts(
    tmp_path, objective, reverse
):
    from sediment_export.bounded_training import project_training_bundle

    bundle = _training_bundle(copies=2)
    if reverse:
        bundle = replace(
            bundle,
            inference_calls=tuple(reversed(bundle.inference_calls)),
            attributed_completions=tuple(reversed(bundle.attributed_completions)),
        )
    expected, skipped = _expected(bundle, objective)
    assert expected
    write_jsonl(expected, tmp_path / "expected.jsonl", split_enabled=False)
    with project_training_bundle(
        bundle, objective=objective, temporary_parent=tmp_path
    ) as result:
        assert result.skipped == skipped
        assert _wire(result.rows) == _wire(expected)
        assert result.remaining_bytes == 8 * 1024**3 - result.rows.encoded_bytes
        write_jsonl(result.rows, tmp_path / "actual.jsonl", split_enabled=False)
    assert (tmp_path / "actual.jsonl").read_bytes() == (
        tmp_path / "expected.jsonl"
    ).read_bytes()


def test_dpo_exact_collision_resolution_and_structural_order(tmp_path, monkeypatch):
    from sediment_export import bounded_training

    bundle = _training_bundle(prompts=("z", "a", "é", "\ud800", "😀", "\ud83d\ude00"))
    expected, skipped = _expected(bundle, "dpo")
    monkeypatch.setattr(bounded_training, "_bucket_digest", lambda key: b"collision")
    with bounded_training.project_training_bundle(
        bundle, objective="dpo", temporary_parent=tmp_path
    ) as result:
        assert _wire(result.rows) == _wire(expected)
        assert result.skipped == skipped


def test_dpo_representation_failure_remains_before_pair_cap(tmp_path):
    from sediment_export.bounded_training import project_training_bundle

    bundle = _training_bundle(prompts=("same", "same", "same"))
    bad = bundle.inference_calls[1].model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[tool_call("tool", "run", {"bad": float("nan")})],
                )
            ]
        }
    )
    calls = (bundle.inference_calls[0], bad, *bundle.inference_calls[2:])
    identities = tuple(
        InferenceCallIdentity(
            call.inference_call_id,
            call.org_id,
            call.session_id,
            call.observed_at,
            tuple(sorted(model_call_ids(call))),
        )
        for call in calls
    )
    bundle = replace(
        bundle, inference_calls=calls, inference_call_identities=identities
    )
    policy = dpo.DPOPolicy(max_pairs_per_bucket=2)
    expected, skipped = _expected(bundle, "dpo", policy)
    assert skipped.get("non_finite_number")
    with project_training_bundle(
        bundle, objective="dpo", policy=policy, temporary_parent=tmp_path
    ) as result:
        assert _wire(result.rows) == _wire(expected)
        assert result.skipped == skipped


def test_training_group_capacity_fails_before_projector_hydration(
    tmp_path, monkeypatch
):
    from sediment_export import bounded_training

    bundle = _training_bundle(prompts=("same", "same"), source_bytes=128 * 1024)

    def should_not_project(*args, **kwargs):
        pytest.fail("oversized group reached the projector")

    monkeypatch.setattr(bounded_training.dpo, "project_dpo", should_not_project)
    with pytest.raises(BundleCapacityError, match="group"):
        with bounded_training.project_training_bundle(
            bundle,
            objective="dpo",
            max_group_bytes=256 * 1024,
            temporary_parent=tmp_path,
        ):
            pytest.fail("capacity refusal was hidden")
    assert list(tmp_path.iterdir()) == []


def test_training_memory_does_not_retain_all_source_raw(tmp_path):
    from sediment_export.bounded_training import project_training_bundle

    bundle = _training_bundle(
        prompts=tuple(str(index) for index in range(32)), source_bytes=256 * 1024
    )
    with BundleRecordStore(temporary_parent=tmp_path) as stage:
        calls = stage.records("inference_calls")
        calls.extend(bundle.inference_calls)
        calls.seal()
        bundle = replace(bundle, inference_calls=calls)
        tracemalloc.start()
        try:
            with project_training_bundle(
                bundle, objective="sft", temporary_parent=tmp_path
            ) as result:
                assert len(result.rows) == 32
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


@pytest.mark.parametrize("abandoned", [False, True])
def test_diff_sft_processes_complete_commits_then_restores_global_order(
    tmp_path, abandoned
):
    from sediment_export.bounded_training import project_training_bundle
    from test_diff_sft import _work_repo, _commit, _mirror_manager

    work = _work_repo(tmp_path)
    (work / "one.py").write_text("one = 1\n")
    (work / "two.py").write_text("two = 2\n")
    (work / "binary.py").write_bytes(b"binary\x00one")
    first = _commit(work, "first")
    (work / "one.py").write_text("one = 3\n")
    (work / "two.py").write_text("two = 4\n")
    (work / "binary.py").write_bytes(b"binary\x00two")
    second = _commit(work, "second")
    mirrors = _mirror_manager(tmp_path, work, second)
    bundle = _training_bundle(prompts=("change both files",))
    members = tuple(
        replace(
            member,
            commit_sha=commit,
            file_path=path,
            decisions=[member.decisions[0].model_copy(update={"accepted": True})],
        )
        for member in reversed(bundle.attributed_completions)
        for commit in (second, first)
        for path in ("two.py", "one.py")
    )
    if abandoned:
        member = members[0]
        members += (
            replace(
                member,
                repo=None,
                commit_sha=None,
                file_path=None,
                similarity_score=None,
                attribution_source=None,
                decisions=[
                    member.decisions[0].model_copy(
                        update={
                            "decision_id": "abandoned-decision",
                            "explicit": True,
                        }
                    )
                ],
                abandonment=SessionAbandonment(
                    org_id=member.org_id,
                    session_id=member.session_id,
                    accepted_decisions=1,
                    explicit_accepted_decisions=1,
                    last_decision_at=bundle.as_of,
                    as_of=bundle.as_of,
                    provenance=Provenance(
                        AbandonmentPolicy().policy_version,
                        bundle.quarantine_revision,
                        bundle.policy.digest,
                    ),
                ),
            ),
        )
    bundle = replace(bundle, attributed_completions=members)
    expected, skipped = _expected(bundle, "diff-sft", mirrors=mirrors)
    assert len(expected) == 4
    assert skipped == {
        "unsupported_diff_section": 2,
        **({"abandoned": 1} if abandoned else {}),
    }
    write_jsonl(expected, tmp_path / "expected.jsonl", split_enabled=False)
    with project_training_bundle(
        bundle, objective="diff-sft", mirrors=mirrors, temporary_parent=tmp_path
    ) as result:
        assert _wire(result.rows) == _wire(expected)
        assert result.skipped == skipped
        write_jsonl(result.rows, tmp_path / "actual.jsonl", split_enabled=False)
    assert (tmp_path / "actual.jsonl").read_bytes() == (
        tmp_path / "expected.jsonl"
    ).read_bytes()


def test_dpo_membership_exclusions_count_each_artifact(tmp_path):
    from sediment_export.bounded_training import project_training_bundle

    bundle = _training_bundle(prompts=("empty", "absent", "valid"), copies=3)
    calls = tuple(
        call.model_copy(update={"input_messages": []})
        if call.inference_call_id.startswith("0-")
        else call.model_copy(update={"model": None})
        if call.inference_call_id.startswith("1-")
        else call
        for call in bundle.inference_calls
    )
    bundle = replace(bundle, inference_calls=calls)
    expected, skipped = _expected(bundle, "dpo")
    assert skipped == {"promptless": 6, "model_absent": 6}
    with project_training_bundle(
        bundle, objective="dpo", temporary_parent=tmp_path
    ) as result:
        assert result.skipped == skipped
        assert _wire(result.rows) == _wire(expected)


def test_training_validation_failure_precedes_staging_and_projection(
    tmp_path, monkeypatch
):
    from sediment_export import bounded_training
    from sediment_export.derived_bundle import BundleValidationError

    bundle = _training_bundle()
    bad = replace(bundle, inference_calls=bundle.inference_calls[:-1])
    monkeypatch.setattr(
        bounded_training.sft,
        "project_sft",
        lambda *args, **kwargs: pytest.fail("invalid bundle reached projector"),
    )
    with pytest.raises(BundleValidationError):
        with bounded_training.project_training_bundle(
            bad, objective="sft", temporary_parent=tmp_path
        ):
            pytest.fail("invalid bundle exposed training rows")
    assert list(tmp_path.iterdir()) == []
