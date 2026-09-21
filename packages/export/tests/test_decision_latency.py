# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision-latency diagnostic tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    TextPart,
)
from sediment_derive import AttributionSource, Provenance, SessionAbandonment
from sediment_export import (
    DecisionLatencyReport,
    AttributedCompletion,
    build_decision_latency_report,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _inference_call(
    inference_call_id: str, *, observed_at: datetime = NOW
) -> InferenceCall:
    return InferenceCall(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id="sess-1",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="done")])
        ],
        observed_at=observed_at,
    )


def _decision(
    decision_id: str,
    *,
    accepted: bool,
    captured_at: datetime = NOW,
    occurred_at: datetime | None = None,
) -> DeveloperDecision:
    # occurred_at is client-stamped and required by the model but is no
    # longer what the latency diagnostic reads -- default
    # it to something obviously different from captured_at so a test that
    # accidentally reads the wrong field would fail loudly.
    d = DeveloperDecision(
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="a.py",
        accepted=accepted,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        occurred_at=occurred_at or (NOW - timedelta(days=1)),
    )
    return d.model_copy(update={"decision_id": decision_id, "captured_at": captured_at})


def _attributed_completion(
    inference_call_id: str,
    decision: DeveloperDecision | None,
    *,
    commit_sha: str,
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=REPO,
        commit_sha=commit_sha,
        file_path="a.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=[decision] if decision is not None else [],
        ci_outcomes=[],
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )


def _abandoned_attributed_completion(
    inference_call_id: str, decision: DeveloperDecision
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[decision],
        ci_outcomes=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="sess-1",
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=NOW,
            as_of=NOW,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


def test_latency_accepts_version_2_observation_time() -> None:
    decision = _decision(
        "d-v2", accepted=True, captured_at=NOW + timedelta(milliseconds=250)
    )
    labeled = _attributed_completion("v2-call", decision, commit_sha="sha-v2")

    report = build_decision_latency_report(
        [_inference_call("v2-call")], [labeled], latency_buckets=1
    )

    assert report.included_decisions == 1
    assert report.buckets[0].min_latency_ms == 250


def test_mixed_evidence_variants_have_deterministic_latency_order() -> None:
    correlated_decision = _decision(
        "d-attributed", accepted=True, captured_at=NOW + timedelta(seconds=1)
    )
    abandoned_decision = _decision(
        "d-abandoned", accepted=True, captured_at=NOW + timedelta(seconds=2)
    )
    attributed = _attributed_completion(
        "c-attributed", correlated_decision, commit_sha="sha-attributed"
    )
    abandoned = _abandoned_attributed_completion("c-abandoned", abandoned_decision)
    completions = [_inference_call("c-attributed"), _inference_call("c-abandoned")]

    first = build_decision_latency_report(completions, [abandoned, attributed])
    second = build_decision_latency_report(completions, [attributed, abandoned])

    assert first == second
    assert first.included_decisions == 2


def _case(
    latencies_s: list[int], accepts: list[bool]
) -> tuple[list[InferenceCall], list[AttributedCompletion]]:
    completions: list[InferenceCall] = []
    attributed_completions: list[AttributedCompletion] = []
    for index, (latency_s, accepted) in enumerate(zip(latencies_s, accepts)):
        inference_call_id = f"c-{index}"
        decision = _decision(
            f"d-{index}",
            accepted=accepted,
            captured_at=NOW + timedelta(seconds=latency_s),
        )
        completions.append(_inference_call(inference_call_id))
        attributed_completions.append(
            _attributed_completion(
                inference_call_id, decision, commit_sha=f"sha-{index}"
            )
        )
    return completions, attributed_completions


def _accept_rates(report: DecisionLatencyReport) -> list[float]:
    return [bucket.accept_rate for bucket in report.buckets]


def test_fast_rejects_slow_accepts_bucket_rates_match_fixture() -> None:
    completions, attributed_completions = _case(
        latencies_s=[1, 2, 3, 4, 5, 6, 7, 8],
        accepts=[False, False, False, True, True, True, True, True],
    )

    report = build_decision_latency_report(
        completions, attributed_completions, latency_buckets=4
    )

    assert report.included_decisions == 8
    assert report.buckets_returned == 4
    assert _accept_rates(report) == [0.0, 0.5, 1.0, 1.0]
    assert report.accept_rate_delta == 1.0
    assert [bucket.decisions for bucket in report.buckets] == [2, 2, 2, 2]


def test_flat_accept_rate_across_latency_buckets_reports_no_pattern() -> None:
    completions, attributed_completions = _case(
        latencies_s=[1, 2, 3, 4, 5, 6, 7, 8],
        accepts=[False, True, False, True, False, True, False, True],
    )

    report = build_decision_latency_report(
        completions, attributed_completions, latency_buckets=4
    )

    assert _accept_rates(report) == [0.5, 0.5, 0.5, 0.5]
    assert report.accept_rate_delta == 0.0


def test_all_decisions_at_same_latency_collapse_to_one_bucket() -> None:
    completions, attributed_completions = _case(
        latencies_s=[5, 5, 5],
        accepts=[True, False, True],
    )

    report = build_decision_latency_report(
        completions, attributed_completions, latency_buckets=4
    )

    assert report.buckets_requested == 4
    assert report.buckets_returned == 1
    assert report.buckets[0].bucket == "all"
    assert report.buckets[0].accept_rate == 2 / 3


def test_missing_or_unusable_latency_inputs_are_skipped_and_counted() -> None:
    valid = _decision("valid", accepted=True, captured_at=NOW + timedelta(seconds=1))
    missing_inference_call = _decision(
        "missing-completion", accepted=False, captured_at=NOW + timedelta(seconds=2)
    )
    null_timestamp = _decision(
        "null-timestamp", accepted=True, captured_at=NOW + timedelta(seconds=3)
    )
    negative_latency = _decision(
        "negative-latency", accepted=True, captured_at=NOW - timedelta(seconds=1)
    )

    completions = [
        _inference_call("valid"),
        _inference_call("null").model_copy(update={"observed_at": None}),
        _inference_call("negative"),
    ]
    attributed_completions = [
        _attributed_completion("valid", valid, commit_sha="sha-valid"),
        _attributed_completion(
            "absent", missing_inference_call, commit_sha="sha-absent"
        ),
        _attributed_completion("null", null_timestamp, commit_sha="sha-null"),
        _attributed_completion("negative", negative_latency, commit_sha="sha-negative"),
        _attributed_completion("no-decision", None, commit_sha="sha-none"),
    ]

    report = build_decision_latency_report(completions, attributed_completions)

    assert report.included_decisions == 1
    assert report.total_decisions == 4
    assert report.skipped_decisions["missing_inference_call"] == 1
    assert report.skipped_decisions["missing_timestamp"] == 1
    assert report.skipped_decisions["negative_latency"] == 1
    assert report.skipped_decisions["no_decision"] == 1
    assert report.buckets[0].accept_rate == 1.0


def test_latency_uses_server_captured_at_not_client_occurred_at() -> None:
    # A badly skewed client clock (occurred_at) must not
    # corrupt the latency reading -- captured_at (server ingestion time,
    # never client-supplied) drives the calculation.
    decision = _decision(
        "skewed",
        accepted=True,
        captured_at=NOW + timedelta(seconds=5),
        occurred_at=NOW - timedelta(days=30),
    )
    completions = [_inference_call("c-skewed")]
    attributed_completions = [
        _attributed_completion("c-skewed", decision, commit_sha="sha-skewed")
    ]

    report = build_decision_latency_report(
        completions, attributed_completions, latency_buckets=1
    )

    assert report.included_decisions == 1
    assert report.skipped_decisions["negative_latency"] == 0
    assert report.buckets[0].mean_latency_ms == 5000.0


def test_decision_seen_on_multiple_attributed_completions_resolves_once_regardless_of_order() -> (
    None
):
    # Previously, if the first attributed completion (in sort order) for a
    # decision_id had a missing completion, that decision was counted as
    # skipped even though a later attributed completion for the same decision_id had a
    # valid completion and got included -- double-counting the decision as
    # both skipped and included, with the verdict depending on sort order.
    shared = _decision("shared", accepted=True, captured_at=NOW + timedelta(seconds=5))
    completions = [_inference_call("has-completion")]
    # "missing" has no matching InferenceCall; sha-a sorts before sha-b so the
    # missing-completion attributed completion is processed first.
    attributed_completions = [
        _attributed_completion("missing", shared, commit_sha="sha-a"),
        _attributed_completion("has-completion", shared, commit_sha="sha-b"),
    ]

    report = build_decision_latency_report(completions, attributed_completions)

    assert report.included_decisions == 1
    assert report.skipped_decisions["missing_inference_call"] == 0
    assert report.total_decisions == 1

    # Order independence: reversing which attributed completion is seen first must not
    # change the verdict.
    reversed_report = build_decision_latency_report(
        completions, list(reversed(attributed_completions))
    )
    assert reversed_report.included_decisions == 1
    assert reversed_report.skipped_decisions["missing_inference_call"] == 0


def test_report_is_deterministic_under_shuffled_inputs() -> None:
    completions, attributed_completions = _case(
        latencies_s=[8, 1, 7, 2, 6, 3, 5, 4],
        accepts=[True, False, True, False, True, False, True, False],
    )

    first = build_decision_latency_report(completions, attributed_completions)
    second = build_decision_latency_report(
        list(reversed(completions)), list(reversed(attributed_completions))
    )

    assert second == first


def test_vendor_edit_retention_decisions_are_excluded_and_counted() -> None:
    # An explicit accept ingested +2 s and a Copilot
    # edit-retention implicit accept whose record was emitted after its 5 m
    # observation delay — its ingest time inherits that delay, so including it
    # would enrich the slow bucket with a fabricated slow accept.
    completions = [_inference_call("c-1")]
    explicit = _decision(
        "d-explicit", accepted=True, captured_at=NOW + timedelta(seconds=2)
    )
    retention_observation = _decision(
        "d-retention", accepted=True, captured_at=NOW + timedelta(seconds=301)
    ).model_copy(
        update={
            "agent_harness": AgentHarness.COPILOT,
            "explicit": False,
            "edit_retention_score": 0.9,
            "observation_delay_ms": 300_000,
        }
    )
    attributed_completions = [
        _attributed_completion("c-1", explicit, commit_sha="sha-a"),
        _attributed_completion("c-1", retention_observation, commit_sha="sha-b"),
    ]

    report = build_decision_latency_report(completions, attributed_completions)

    assert report.included_decisions == 1
    assert report.skipped_decisions["observation_delay"] == 1
    assert report.total_decisions == 2
    # The buckets contain only the honest latency — no 301 s pollution.
    assert report.buckets_returned == 1
    assert report.buckets[0].max_latency_ms == 2000.0

    # The verdict is intrinsic to the decision: it holds even when the
    # edit-retention decision's inference call is missing entirely.
    orphaned = [
        _attributed_completion("c-gone", retention_observation, commit_sha="sha-c")
    ]
    orphan_report = build_decision_latency_report([], orphaned)
    assert orphan_report.skipped_decisions["observation_delay"] == 1
    assert orphan_report.skipped_decisions["missing_inference_call"] == 0
