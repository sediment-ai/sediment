# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for the shared decision/completion join core (ADR 0004,
``sediment_derive.attachment``) — in particular the Codex decision-collapse
rule: a Codex decision-only redelivery whose ``tool_result`` straddled
an export batch can store one ``file_path=""`` row next to N per-file
fan-out rows sharing the same ``(org_id, agent_harness, call_id)``. That ``""`` row
is subsumed by its keyed siblings and must not be surfaced as a distinct
decision at join time — while a genuine no-path decision (no keyed sibling,
or a non-Codex agent_harness) must pass through untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    TextPart,
    ToolCallPart,
)
from sediment_derive.attachment import (
    DECISION_ATTACHMENT_SKIP_REASONS,
    join_decisions_by_call_id,
    join_decisions_by_call_id_result,
)

ORG = "acme-corp"
AT = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _inference_call(
    session_id: str,
    call_id: str,
    tool_calls: list[ToolCallPart] | None = None,
    *,
    org_id: str = ORG,
) -> InferenceCall:
    return InferenceCall(
        org_id=org_id,
        session_id=session_id,
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[InferenceMessage(role="user", parts=[TextPart(content="hi")])],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[TextPart(content="ok"), *(tool_calls or [])],
            )
        ],
        input_tokens=10,
        output_tokens=5,
        duration_ms=50,
        model_call_id=call_id,
        observed_at=AT,
    )


def _tool_call(call_id: str) -> ToolCallPart:
    return ToolCallPart(id=call_id, name="Edit", arguments={"file_path": "a.py"})


def _decision(
    call_id: str | None,
    *,
    file_path: str,
    agent_harness: AgentHarness = AgentHarness.CODEX,
    accepted: bool = True,
    occurred_at: datetime = AT,
    org_id: str = ORG,
    session_id: str = "s1",
) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=org_id,
        session_id=session_id,
        user_id="dev",
        agent_harness=agent_harness,
        file_path=file_path,
        accepted=accepted,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id=call_id,
        occurred_at=occurred_at,
    )


def _ordered(*decisions: DeveloperDecision) -> list[DeveloperDecision]:
    return sorted(
        decisions, key=lambda decision: (decision.occurred_at, decision.decision_id)
    )


def test_codex_empty_path_row_dropped_when_keyed_siblings_present() -> None:
    """A Codex redelivery's file_path="" row is subsumed by its keyed
    per-file siblings and dropped; the siblings themselves are kept."""
    completion = _inference_call("s1", "call-1")
    empty = _decision("call-1", file_path="")
    sib_a = _decision("call-1", file_path="a.py")
    sib_b = _decision("call-1", file_path="b.py")

    decisions_for = join_decisions_by_call_id([completion], [empty, sib_a, sib_b])

    assert decisions_for[completion.inference_call_id] == _ordered(sib_a, sib_b)


def test_codex_empty_path_row_kept_when_no_keyed_siblings() -> None:
    """A genuine no-path Codex decision (no keyed sibling for its call_id)
    is a real decision, not a subsumed redelivery artifact — it passes
    through the join untouched."""
    completion = _inference_call("s1", "call-2")
    empty = _decision("call-2", file_path="")

    decisions_for = join_decisions_by_call_id([completion], [empty])

    assert decisions_for[completion.inference_call_id] == [empty]


def test_non_codex_empty_path_row_not_collapsed() -> None:
    """The collapse is scoped to AgentHarness.CODEX: Codex's redelivery
    straddling is what produces the subsumed-empty-row shape, so a
    non-Codex agent_harness's file_path="" row must survive alongside keyed
    siblings sharing its call_id."""
    completion = _inference_call("s1", "call-3")
    empty = _decision("call-3", file_path="", agent_harness=AgentHarness.CLAUDE_CODE)
    sib = _decision("call-3", file_path="a.py", agent_harness=AgentHarness.CLAUDE_CODE)

    decisions_for = join_decisions_by_call_id([completion], [empty, sib])

    assert decisions_for[completion.inference_call_id] == _ordered(empty, sib)


def test_codex_keyed_siblings_stay_distinct_from_each_other() -> None:
    """The collapse is asymmetric: only the empty-path row is subsumed.
    Per-file keyed rows never collapse into each other, even though they
    share the same (org_id, agent_harness, call_id) group."""
    completion = _inference_call("s1", "call-4")
    accept_a = _decision("call-4", file_path="a.py", accepted=True)
    reject_b = _decision("call-4", file_path="b.py", accepted=False)

    decisions_for = join_decisions_by_call_id([completion], [accept_a, reject_b])

    assert decisions_for[completion.inference_call_id] == _ordered(accept_a, reject_b)


def test_codex_empty_path_collapse_is_scoped_per_call_id() -> None:
    """Two different call_ids under the same org/agent_harness do not bleed into
    each other's collapse decision: call-5's empty row has a keyed sibling
    and is dropped; call-6's empty row has none and survives."""
    completion_5 = _inference_call("s1", "call-5")
    completion_6 = _inference_call("s1", "call-6")
    empty_5 = _decision("call-5", file_path="")
    sib_5 = _decision("call-5", file_path="a.py")
    empty_6 = _decision("call-6", file_path="")

    decisions_for = join_decisions_by_call_id(
        [completion_5, completion_6], [empty_5, sib_5, empty_6]
    )

    assert decisions_for[completion_5.inference_call_id] == [sib_5]
    assert decisions_for[completion_6.inference_call_id] == [empty_6]


def test_codex_distinct_verdict_on_same_call_id_is_never_collapsed() -> None:
    """A genuine deny precedes an approved fan-out on the same call_id (the
    hook/policy re-prompt path): the reject's file_path="" row differs from
    the keyed accept siblings in accepted/occurred_at, so it is a distinct
    decision — collapsing it would flip the attributed completion's label from rejected to
    accept-only. Only a true redelivery (identical natural key minus
    file_path) is subsumed."""
    completion = _inference_call("s1", "call-7")
    reject = _decision(
        "call-7",
        file_path="",
        accepted=False,
        occurred_at=AT,
    )
    accept_a = _decision(
        "call-7",
        file_path="a.py",
        accepted=True,
        occurred_at=AT + timedelta(seconds=30),
    )

    decisions_for = join_decisions_by_call_id([completion], [reject, accept_a])

    assert decisions_for[completion.inference_call_id] == _ordered(reject, accept_a)


@pytest.mark.parametrize("same_instant", [False, True])
def test_codex_subsumption_uses_instants_in_repeated_hour(same_instant):
    from zoneinfo import ZoneInfo

    early = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0)
    other = early.astimezone(UTC) if same_instant else early.replace(fold=1)
    call = _inference_call("s1", "call-fold")
    empty = _decision("call-fold", file_path="", occurred_at=early)
    keyed = _decision("call-fold", file_path="a.py", occurred_at=other)
    for inputs in permutations((empty, keyed)):
        result = join_decisions_by_call_id_result([call], inputs)
        assert result.skipped == {}
        assert result.decisions_by_completion[call.inference_call_id] == (
            [keyed] if same_instant else [empty, keyed]
        )


# ── tool-call-id join ─────────────────────────────────────────────────────
# On real gateway traffic the completion's call_id is the gateway's own id
# (a LiteLLM UUID) while the decision carries the agent's tool-use id
# (toolu_…). These identifiers have different namespaces. The
# completion is therefore also reachable under its response tool-call ids.


def test_decision_joins_via_response_tool_call_id() -> None:
    completion = _inference_call(
        "s1", "litellm-uuid-1", tool_calls=[_tool_call("toolu_1")]
    )
    decision = _decision("toolu_1", file_path="a.py")

    decisions_for = join_decisions_by_call_id([completion], [decision])

    assert decisions_for[completion.inference_call_id] == [decision]


def test_gateway_call_id_and_tool_call_id_join_the_same_inference_call() -> None:
    completion = _inference_call(
        "s1", "litellm-uuid-2", tool_calls=[_tool_call("toolu_2")]
    )
    via_gateway = _decision("litellm-uuid-2", file_path="a.py")
    via_tool = _decision("toolu_2", file_path="b.py")

    decisions_for = join_decisions_by_call_id([completion], [via_gateway, via_tool])

    assert decisions_for[completion.inference_call_id] == _ordered(
        via_gateway, via_tool
    )


def test_model_call_id_and_structured_tool_call_id_join_the_same_fact() -> None:
    call = InferenceCall(
        inference_call_id="inference-1",
        org_id=ORG,
        session_id="s1",
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    ToolCallPart(id="toolu-v2", name="Edit", arguments={"path": "a.py"})
                ],
            )
        ],
        model_call_id="model-v2",
        observed_at=AT,
    )
    via_model = _decision("model-v2", file_path="a.py")
    via_tool = _decision("toolu-v2", file_path="b.py")

    decisions_for = join_decisions_by_call_id([call], [via_model, via_tool])

    assert decisions_for[call.inference_call_id] == _ordered(via_model, via_tool)


def test_tool_call_id_shared_by_two_completions_is_ambiguous() -> None:
    """Unique-or-drop applies to tool-call ids exactly as to gateway ids: an
    id resolving to two completions drops the decision (zero-poisoning)."""
    first = _inference_call("s1", "uuid-a", tool_calls=[_tool_call("toolu_dup")])
    second = _inference_call("s1", "uuid-b", tool_calls=[_tool_call("toolu_dup")])
    decision = _decision("toolu_dup", file_path="a.py")

    decisions_for = join_decisions_by_call_id([first, second], [decision])

    assert decisions_for == {}


def test_tool_call_id_equal_to_own_call_id_is_not_self_ambiguous() -> None:
    """A completion indexed under the same id twice (its gateway call_id and
    a tool-call id happen to match) is one candidate, not two — the decision
    must still join."""
    completion = _inference_call(
        "s1", "shared-id", tool_calls=[_tool_call("shared-id")]
    )
    decision = _decision("shared-id", file_path="a.py")

    decisions_for = join_decisions_by_call_id([completion], [decision])

    assert decisions_for[completion.inference_call_id] == [decision]


def test_duplicate_tool_call_ids_within_one_completion_still_join() -> None:
    """The same tool-use id repeated inside one completion's tool_calls is
    one join candidate — within-completion repetition is not cross-completion
    ambiguity."""
    completion = _inference_call(
        "s1", "uuid-c", tool_calls=[_tool_call("toolu_3"), _tool_call("toolu_3")]
    )
    decision = _decision("toolu_3", file_path="a.py")

    decisions_for = join_decisions_by_call_id([completion], [decision])

    assert decisions_for[completion.inference_call_id] == [decision]


def test_attachment_result_counts_each_unattached_decision_once() -> None:
    completion = _inference_call("s1", "matched")
    attached = _decision("matched", file_path="a.py")
    missing = _decision(None, file_path="b.py")
    unmatched = _decision("absent", file_path="c.py")
    ambiguous = _decision("duplicate", file_path="d.py")
    duplicate_a = _inference_call("s1", "duplicate")
    duplicate_b = _inference_call("s2", "duplicate")

    result = join_decisions_by_call_id_result(
        [completion, duplicate_a, duplicate_b],
        [attached, missing, unmatched, ambiguous],
    )

    assert result.decisions_by_completion == {completion.inference_call_id: [attached]}
    assert result.skipped == {
        "missing_decision_call_id": 1,
        "unmatched_decision_call_id": 1,
        "ambiguous_decision_call_id": 1,
    }


def test_attachment_result_is_deterministic_under_shuffled_input() -> None:
    first = _inference_call("s1", "first")
    second = _inference_call("s2", "second")
    earlier = _decision(
        "first", file_path="a.py", occurred_at=AT - timedelta(seconds=1)
    )
    later = _decision("first", file_path="b.py")
    second_decision = _decision("second", file_path="c.py", session_id="s2")
    missing = _decision(None, file_path="d.py")
    unmatched = _decision("absent", file_path="e.py")

    ordered = join_decisions_by_call_id_result(
        [first, second], [earlier, later, second_decision, missing, unmatched]
    )
    shuffled = join_decisions_by_call_id_result(
        [second, first], [unmatched, second_decision, later, missing, earlier]
    )

    assert shuffled == ordered


@pytest.mark.parametrize("identity", ["gateway", "tool"])
@pytest.mark.parametrize(
    ("org_id", "session_id", "expected_reason"),
    [
        (ORG, "s1", None),
        (ORG, "s2", "decision_session_mismatch"),
        ("other-org", "s1", "decision_org_mismatch"),
        ("other-org", "s2", "decision_org_mismatch"),
    ],
)
def test_unique_decision_attachment_requires_matching_scope(
    identity, org_id, session_id, expected_reason, caplog
) -> None:
    call = _inference_call("s1", "gateway", [_tool_call("tool")])
    decision = _decision(
        identity, file_path="private-body.py", org_id=org_id, session_id=session_id
    )

    result = join_decisions_by_call_id_result([call], [decision])

    assert result.decisions_by_completion == (
        {call.inference_call_id: [decision]} if expected_reason is None else {}
    )
    assert result.skipped == ({expected_reason: 1} if expected_reason else {})
    if expected_reason:
        assert expected_reason in caplog.text
        assert decision.decision_id in caplog.text
        assert "private-body.py" not in caplog.text


@pytest.mark.parametrize("other_org", [ORG, "other-org"])
def test_attachment_checks_supplied_population_before_scope_and_is_deterministic(
    other_org: str,
) -> None:
    calls = [
        _inference_call("s1", "shared"),
        _inference_call("s2", "shared", org_id=other_org),
        _inference_call("s1", "unique"),
    ]
    decisions = [
        _decision("shared", file_path="a.py"),
        _decision("shared", file_path="b.py", org_id="other-org"),
        _decision("unique", file_path="c.py", session_id="s2"),
        _decision("unique", file_path="d.py", org_id="other-org"),
        _decision(None, file_path="e.py", org_id="other-org"),
        _decision("absent", file_path="f.py", org_id="other-org"),
        _decision("unique", file_path="g.py"),
    ]
    expected_skips = {
        "ambiguous_decision_call_id": 2,
        "decision_session_mismatch": 1,
        "decision_org_mismatch": 1,
        "missing_decision_call_id": 1,
        "unmatched_decision_call_id": 1,
    }
    expected = join_decisions_by_call_id_result(calls, decisions)
    assert expected.decisions_by_completion == {
        calls[2].inference_call_id: [decisions[-1]]
    }
    assert expected.skipped == expected_skips
    assert set(expected.skipped) == set(DECISION_ATTACHMENT_SKIP_REASONS)
    assert join_decisions_by_call_id_result(calls, decisions) == expected
    for candidates in permutations(calls):
        assert (
            join_decisions_by_call_id_result(candidates, reversed(decisions))
            == expected
        )

    # A Session-scoped consumer intentionally supplies a narrower population.
    scoped = join_decisions_by_call_id_result(calls[:1], decisions[:1])
    assert scoped.decisions_by_completion == {calls[0].inference_call_id: decisions[:1]}
    assert scoped.skipped == {}
