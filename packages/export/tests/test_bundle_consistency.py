# SPDX-License-Identifier: AGPL-3.0-or-later
"""Imported training evidence must agree with its declared source population."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from sediment_core import InferenceMessage, TextPart, ToolCallPart, GatewayProvider
from sediment_core.store import InferenceCallIdentity
from sediment_derive import model_call_ids, project_session_turns
from sediment_export import (
    BundleValidationError,
    read_derived_bundle,
    write_derived_bundle,
    validate_derived_bundle,
    export_rlvr_from_bundle,
)
from test_derived_bundle_io import _bundle, _decision, _rewrite_record, _tree_bytes


def _identity(call):
    return InferenceCallIdentity(
        inference_call_id=call.inference_call_id,
        org_id=call.org_id,
        session_id=call.session_id,
        observed_at=call.observed_at,
        call_ids=tuple(sorted(model_call_ids(call))),
    )


def _calls_bundle(calls, decisions=()):
    bundle = _bundle()
    segments, _, fragmented = project_session_turns(list(calls), list(decisions))
    return replace(
        bundle,
        inference_calls=tuple(calls),
        inference_call_identities=tuple(_identity(call) for call in calls),
        as_of=max(call.observed_at for call in calls),
        rollouts=(replace(bundle.rollouts[0], segments=segments),),
        fragmented=dict(fragmented),
    )


@pytest.mark.parametrize("boundary", ["write", "read"])
@pytest.mark.parametrize("call_id", ["unrelated-call", None])
def test_bundle_rejects_unmatched_decision_before_training(tmp_path, boundary, call_id):
    bundle = _bundle()
    decision = _decision(call_id=call_id, accepted=True, explicit=True)
    destination = tmp_path / "bundle"
    if boundary == "write":
        bundle = replace(
            bundle,
            attributed_completions=(
                replace(bundle.attributed_completions[0], decisions=[decision]),
            ),
        )
        with pytest.raises(BundleValidationError, match="attachment"):
            write_derived_bundle(bundle, destination)
        assert not destination.exists()
    else:
        write_derived_bundle(bundle, destination)
        _rewrite_record(
            destination,
            "attributed_completions",
            lambda row: row.update(decisions=[decision.model_dump(mode="json")]),
        )
        with pytest.raises(BundleValidationError, match="attachment"):
            read_derived_bundle(destination)


@pytest.mark.parametrize("boundary", ["write", "read"])
@pytest.mark.parametrize(
    "field,value", [("completion", "contradiction"), ("new_messages", [])]
)
def test_bundle_rejects_substituted_turn_content(tmp_path, boundary, field, value):
    bundle = _bundle()
    destination = tmp_path / "bundle"
    if boundary == "write":
        rollout = bundle.rollouts[0]
        bundle = replace(
            bundle,
            rollouts=(
                replace(
                    rollout,
                    segments=[[replace(rollout.segments[0][0], **{field: value})]],
                ),
            ),
        )
        with pytest.raises(BundleValidationError, match="Turn projection"):
            write_derived_bundle(bundle, destination)
        assert not destination.exists()
    else:
        write_derived_bundle(bundle, destination)
        _rewrite_record(
            destination,
            "rollouts",
            lambda row: row["segments"][0][0].update({field: value}),
        )
        with pytest.raises(BundleValidationError, match="Turn projection"):
            read_derived_bundle(destination)


def test_direct_cli_bundle_export_validates_before_projection(tmp_path, monkeypatch):
    from sediment_cli import cli

    bundle = _bundle()
    bundle = replace(
        bundle,
        attributed_completions=(
            replace(
                bundle.attributed_completions[0],
                decisions=[
                    _decision(call_id="unrelated-call", accepted=True, explicit=True)
                ],
            ),
        ),
    )

    @contextmanager
    def built_bundle(*args, **kwargs):
        yield bundle

    monkeypatch.setattr(cli, "build_derived_bundle_context", built_bundle)
    with pytest.raises(BundleValidationError, match="attachment"):
        cli._attributed_completion_pipeline(
            object(),
            object(),
            bundle.org_id,
            str(tmp_path),
            "sft",
            label="samples",
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("same_session", [True, False])
@pytest.mark.parametrize(
    "collision", ["provider-provider", "tool-tool", "tool-provider"]
)
def test_complete_identity_population_keeps_outside_cohort_ambiguity(
    tmp_path, same_session, collision
):
    bundle = _bundle()
    call = bundle.inference_calls[0]
    if collision.startswith("tool"):
        call = call.model_copy(
            update={
                "output_messages": [
                    InferenceMessage(
                        role="assistant",
                        parts=[
                            TextPart(content="answer"),
                            ToolCallPart(id="call-1", name="Edit", arguments={}),
                            ToolCallPart(id="call-1", name="Edit", arguments={}),
                        ],
                    )
                ]
            }
        )
    hidden = call.model_copy(
        update={
            "inference_call_id": "outside",
            "observed_at": call.observed_at - timedelta(days=60),
            "session_id": call.session_id if same_session else "outside-session",
            "gateway_provider": GatewayProvider.PORTKEY,
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[ToolCallPart(id="call-1", name="Edit", arguments={})],
                )
            ]
            if collision == "tool-tool"
            else [],
            "model_call_id": "other-provider" if collision == "tool-tool" else "call-1",
        }
    )
    row = replace(bundle.attributed_completions[0], decisions=[_decision()])
    partial = replace(
        bundle,
        rollouts=(),
        inference_calls=(call,),
        attributed_completions=(row,),
        inference_call_identities=(_identity(call), _identity(hidden)),
    )
    with pytest.raises(BundleValidationError, match="attachment"):
        write_derived_bundle(partial, tmp_path / "ambiguous")
    # Explicit partial artifacts still retain complete source identity evidence.
    unique = replace(partial, attributed_completions=(replace(row, decisions=[]),))
    write_derived_bundle(unique, tmp_path / "partial")
    restored = read_derived_bundle(tmp_path / "partial")
    assert len(restored.inference_call_identities) == 2
    assert len(restored.inference_calls) == 1
    _rewrite_record(
        tmp_path / "partial",
        "attributed_completions",
        lambda record: record.update(decisions=[_decision().model_dump(mode="json")]),
    )
    with pytest.raises(BundleValidationError, match="attachment"):
        read_derived_bundle(tmp_path / "partial")
    # Repeated provider/tool aliases within one Fact don't invent ambiguity.
    validate_derived_bundle(
        replace(partial, inference_call_identities=(_identity(call),))
    )


def test_rollout_attachment_retains_its_session_scope(tmp_path):
    bundle = _calls_bundle(_bundle().inference_calls, [_decision()])
    outside = replace(
        bundle.inference_call_identities[0],
        inference_call_id="outside",
        session_id="other-session",
    )
    bundle = replace(
        bundle,
        attributed_completions=(),
        inference_call_identities=(*bundle.inference_call_identities, outside),
    )
    write_derived_bundle(bundle, tmp_path / "rollout")
    [turn] = read_derived_bundle(tmp_path / "rollout").rollouts[0].segments[0]
    assert [decision.call_id for decision in turn.decisions] == ["call-1"]


def test_unique_attachment_to_another_call_cannot_supply_this_artifacts_label():
    bundle = _calls_bundle(_continued_calls())
    row = replace(
        bundle.attributed_completions[0], decisions=[_decision(call_id="call-2")]
    )
    with pytest.raises(BundleValidationError, match="attachment"):
        validate_derived_bundle(replace(bundle, attributed_completions=(row,)))
    validate_derived_bundle(
        replace(bundle, attributed_completions=(replace(row, decisions=[_decision()]),))
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("call_ids", ("wrong",)),
        ("session_id", "wrong-session"),
        ("inference_call_id", "missing"),
        ("org_id", "other-org"),
    ],
)
@pytest.mark.parametrize("boundary", ["write", "read"])
def test_identity_evidence_must_agree_with_every_full_call(
    tmp_path, field, value, boundary
):
    bundle = _bundle()
    destination = tmp_path / "bundle"
    if boundary == "write":
        bundle = replace(
            bundle,
            inference_call_identities=(
                replace(bundle.inference_call_identities[0], **{field: value}),
            ),
        )
        with pytest.raises(BundleValidationError, match="identity|org_id"):
            write_derived_bundle(bundle, destination)
    else:
        write_derived_bundle(bundle, destination)
        _rewrite_record(
            destination,
            "inference_call_identities",
            lambda row: row.update({field: value}),
        )
        with pytest.raises(BundleValidationError, match="identity|org_id"):
            read_derived_bundle(destination)


def test_identity_population_is_explicit_bounded_and_historical(tmp_path, monkeypatch):
    from sediment_export import derived_bundle as owner

    bundle = _bundle()
    witness = bundle.inference_call_identities[0]
    for invalid in (
        None,
        (),
        (witness, witness),
        (replace(witness, observed_at=bundle.as_of + timedelta(microseconds=1)),),
    ):
        with pytest.raises(BundleValidationError, match="identity|as_of"):
            validate_derived_bundle(replace(bundle, inference_call_identities=invalid))
    with pytest.raises(BundleValidationError, match="as_of"):
        validate_derived_bundle(replace(bundle, as_of=None))
    monkeypatch.setattr(owner, "_IDENTITY_LIMIT", 0)
    with pytest.raises(BundleValidationError, match="row limit"):
        write_derived_bundle(bundle, tmp_path / "over-limit")
    empty = replace(
        bundle,
        attributed_completions=(),
        rollouts=(),
        inference_calls=(),
        inference_call_identities=(),
        as_of=None,
    )
    write_derived_bundle(empty, tmp_path / "empty")
    assert read_derived_bundle(tmp_path / "empty") == empty
    manifest = json.loads((tmp_path / "empty" / "manifest.json").read_text())
    assert manifest["counts"]["inference_call_identities"] == 0
    assert (tmp_path / "empty" / "inference_call_identities.jsonl").read_bytes() == b""


def _continued_calls():
    first = _bundle().inference_calls[0]
    second = first.model_copy(
        update={
            "inference_call_id": "inference-2",
            "model_call_id": "call-2",
            "observed_at": first.observed_at + timedelta(seconds=1),
            "input_messages": first.input_messages
            + first.output_messages
            + [InferenceMessage(role="user", parts=[TextPart(content="continue")])],
            "output_messages": [
                InferenceMessage(role="assistant", parts=[TextPart(content="next")])
            ],
        }
    )
    return first, second


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "reversed", "boundary", "tool"]
)
def test_rollout_requires_complete_canonical_session_turns(tmp_path, change):
    bundle = _calls_bundle(_continued_calls())
    rollout = bundle.rollouts[0]
    first, second = rollout.segments[0]
    segments = {
        "missing": [[first]],
        "duplicate": [[first, first, second]],
        "reversed": [[second, first]],
        "boundary": [[first], [second]],
        "tool": [
            [
                replace(
                    first,
                    tool_calls=(
                        ToolCallPart(id="invented", name="Edit", arguments={}),
                    ),
                ),
                second,
            ]
        ],
    }[change]
    if change == "missing":
        bundle = replace(bundle, inference_calls=(bundle.inference_calls[0],))
    with pytest.raises(BundleValidationError, match="Turn"):
        write_derived_bundle(
            replace(bundle, rollouts=(replace(rollout, segments=segments),)),
            tmp_path / "invalid",
        )


def test_identity_order_does_not_change_published_bytes(tmp_path):
    bundle = _calls_bundle(_continued_calls())
    write_derived_bundle(bundle, tmp_path / "first")
    write_derived_bundle(
        replace(
            bundle,
            inference_call_identities=tuple(reversed(bundle.inference_call_identities)),
        ),
        tmp_path / "second",
    )
    assert _tree_bytes(tmp_path / "first") == _tree_bytes(tmp_path / "second")


def test_in_memory_rlvr_bundle_export_validates_and_has_positive_control(tmp_path):
    bundle = _bundle()
    changed = replace(
        bundle.rollouts[0],
        segments=[
            [replace(bundle.rollouts[0].segments[0][0], completion="substituted")]
        ],
    )
    with pytest.raises(BundleValidationError, match="Turn projection"):
        export_rlvr_from_bundle(
            replace(bundle, rollouts=(changed,)),
            None,
            tmp_path / "bad",
            target="nemo-gym",
        )
    assert not (tmp_path / "bad").exists()
    summary = export_rlvr_from_bundle(
        bundle, None, tmp_path / "good", target="nemo-gym"
    )
    assert summary["rollout_rows"] == 1
    assert sum(summary["written"].values()) == 1


@pytest.mark.parametrize(
    "before,after", [(1, 1.0), (0.0, -0.0), (float("nan"), float("nan"))]
)
def test_turn_verification_preserves_existing_numeric_replay_semantics(
    tmp_path, before, after
):
    first, second = _continued_calls()
    output = InferenceMessage(
        role="assistant",
        parts=[ToolCallPart(id="tool", name="Edit", arguments={"n": before})],
    )
    replay = InferenceMessage(
        role="assistant",
        parts=[ToolCallPart(id="tool", name="Edit", arguments={"n": after})],
    )
    first = first.model_copy(update={"output_messages": [output]})
    second = second.model_copy(
        update={"input_messages": first.input_messages + [replay]}
    )
    bundle = _calls_bundle((first, second))
    assert len(bundle.rollouts[0].segments) == 1
    write_derived_bundle(bundle, tmp_path / "numeric")
    restored = read_derived_bundle(tmp_path / "numeric")
    assert len(restored.rollouts[0].segments) == 1
    write_derived_bundle(restored, tmp_path / "again")
    assert _tree_bytes(tmp_path / "numeric") == _tree_bytes(tmp_path / "again")


def test_subsumed_codex_claim_is_checked_across_carried_attributed_rows(tmp_path):
    from sediment_core import AgentHarness

    bundle = _bundle()
    pathless = _decision(
        decision_id="pathless", agent_harness=AgentHarness.CODEX, file_path=""
    )
    keyed = pathless.model_copy(update={"decision_id": "keyed", "file_path": "math.py"})
    row = bundle.attributed_completions[0]
    changed = replace(
        bundle,
        attributed_completions=(
            replace(row, decisions=[pathless]),
            replace(row, file_path="other.py", decisions=[keyed]),
        ),
    )
    with pytest.raises(BundleValidationError, match="attachment"):
        write_derived_bundle(changed, tmp_path / "subsumed")
    validate_derived_bundle(
        replace(changed, attributed_completions=(replace(row, decisions=[keyed]),))
    )


@pytest.mark.parametrize(
    "change", ["population", "missing-file", "old-version", "duplicate-alias"]
)
def test_reader_requires_versioned_complete_identity_contract(tmp_path, change):
    destination = tmp_path / "bundle"
    write_derived_bundle(_bundle(), destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "population":
        manifest.pop("identity_population")
    elif change == "missing-file":
        (destination / "inference_call_identities.jsonl").unlink()
    elif change == "old-version":
        manifest["bundle_schema_version"] = 2
    else:
        _rewrite_record(
            destination,
            "inference_call_identities",
            lambda row: row.update(call_ids=["call-1", "call-1"]),
        )
    if change != "duplicate-alias":
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BundleValidationError):
        read_derived_bundle(destination)


@pytest.mark.parametrize("fold", [0, 1])
def test_identity_agreement_compares_dst_fold_instants_without_changing_facts(
    tmp_path, fold
):
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    at = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=fold)
    call = _bundle().inference_calls[0].model_copy(update={"observed_at": at})
    bundle = _calls_bundle((call,))
    bundle = replace(
        bundle,
        inference_call_identities=(
            replace(
                bundle.inference_call_identities[0], observed_at=at.astimezone(UTC)
            ),
        ),
    )
    write_derived_bundle(bundle, tmp_path / "fold")
    assert call.observed_at is at
    assert read_derived_bundle(tmp_path / "fold").inference_calls[
        0
    ].observed_at.astimezone(UTC) == at.astimezone(UTC)


def test_identity_writer_orders_dst_folds_by_instant(tmp_path):
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    first = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0)
    second = first.replace(fold=1)
    bundle = _bundle()
    witness = bundle.inference_call_identities[0]
    bundle = replace(
        bundle,
        inference_calls=(),
        attributed_completions=(),
        rollouts=(),
        as_of=second.astimezone(UTC),
        inference_call_identities=(
            replace(witness, inference_call_id="a-later", observed_at=second),
            replace(witness, inference_call_id="z-earlier", observed_at=first),
        ),
    )
    write_derived_bundle(bundle, tmp_path / "order")
    assert [
        item.inference_call_id
        for item in read_derived_bundle(tmp_path / "order").inference_call_identities
    ] == ["z-earlier", "a-later"]


@pytest.mark.parametrize("evidence", ["calls", "decisions", "codex_redelivery"])
def test_repeated_hour_bundle_roundtrip_preserves_chronology_and_export(
    tmp_path, evidence
):
    from datetime import UTC, datetime
    from itertools import permutations
    from zoneinfo import ZoneInfo

    from sediment_core import AgentHarness

    early = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0)
    late = early.replace(fold=1)
    calls = list(_continued_calls())
    decisions = []
    if evidence == "calls":
        calls = [
            calls[0].model_copy(
                update={"inference_call_id": "z-earlier", "observed_at": early}
            ),
            calls[1].model_copy(
                update={"inference_call_id": "a-later", "observed_at": late}
            ),
        ]
    else:
        calls = [calls[0].model_copy(update={"observed_at": early.astimezone(UTC)})]
        decisions = [
            _decision(decision_id="z-earlier", occurred_at=early, accepted=False),
            _decision(decision_id="a-later", occurred_at=late, accepted=True),
        ]
        if evidence == "codex_redelivery":
            decisions = [
                _decision(
                    decision_id="empty",
                    agent_harness=AgentHarness.CODEX,
                    occurred_at=early,
                    file_path="",
                ),
                _decision(
                    decision_id="keyed",
                    agent_harness=AgentHarness.CODEX,
                    occurred_at=early.astimezone(UTC),
                    file_path="a.py",
                ),
            ]

    published = []
    exports = []
    for index, (call_order, decision_order) in enumerate(
        (c, d) for c in permutations(calls) for d in permutations(decisions)
    ):
        bundle = replace(
            _calls_bundle(call_order, decision_order),
            attributed_completions=(),
            as_of=late.astimezone(UTC),
        )
        destination = tmp_path / f"bundle-{index}"
        write_derived_bundle(bundle, destination)
        restored = read_derived_bundle(destination)
        turns = [t for segment in restored.rollouts[0].segments for t in segment]
        if evidence == "calls":
            assert [t.inference_call_id for t in turns] == ["z-earlier", "a-later"]
            assert [len(s) for s in restored.rollouts[0].segments] == [2]
            assert bundle.fragmented == {}
        else:
            expected = (
                ["keyed"]
                if evidence == "codex_redelivery"
                else ["z-earlier", "a-later"]
            )
            assert [d.decision_id for d in turns[0].decisions] == expected
        published.append((destination / "rollouts.jsonl").read_bytes())
        for source_index, source in enumerate((bundle, restored)):
            output = tmp_path / f"export-{index}-{source_index}"
            summary = export_rlvr_from_bundle(source, None, output, target="nemo-gym")
            assert summary["rollout_rows"] == 1
            exports.append(_tree_bytes(output))
    assert all(value == published[0] for value in published)
    assert all(value == exports[0] for value in exports)
