# SPDX-License-Identifier: AGPL-3.0-or-later
"""Factual Session status over real captured observations and historical boundaries."""

from __future__ import annotations

import json
import random
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitfixtures import FIB, commit_all, make_remote, make_work_repo, run_git
from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)
from sediment_core.store import FactStore
from sediment_derive import (
    AbandonmentPolicy,
    AttributionSource,
    Attribution,
    MirrorManager,
    Provenance,
    derive_abandonment,
)
from sediment_derive.rollout import CommitRef

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
BASE = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def _decision(
    session_id: str,
    *,
    accepted: bool = True,
    explicit: bool = True,
    occurred_at: datetime | None = None,
    call_id: str | None = None,
    user_id: str | None = "dev",
) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id=session_id,
        user_id=user_id,
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="math_utils.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        call_id=call_id,
        occurred_at=occurred_at if occurred_at is not None else _at(0),
        captured_at=occurred_at if occurred_at is not None else _at(0),
    )


def _clock(when: datetime) -> DeveloperDecision:
    """A fact that advances ``as_of`` without joining the candidate pool.

    A reject-only session is out of the population by definition, so this
    moves the horizon without changing what is being measured — using the
    session under test would advance its own ``last_decision_at`` too.
    """
    return _decision("sess-clock", accepted=False, occurred_at=when)


def _inference_call(
    session_id: str, *, observed_at: datetime | None = None
) -> InferenceCall:
    return InferenceCall(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="write fib")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=FIB)])
        ],
        input_tokens=10,
        output_tokens=20,
        duration_ms=50,
        observed_at=observed_at if observed_at is not None else _at(0),
    )


def _attribution(session_id: str, inference_call_id: str) -> Attribution:
    return Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha="a" * 40,
        file_path="math_utils.py",
        inference_call_id=inference_call_id,
        session_id=session_id,
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )


def _mirrors(tmp_path: Path) -> MirrorManager:
    return MirrorManager(str(tmp_path / "mirrors"))


class _BoundaryStore:
    def read_snapshot(self):
        return nullcontext(self)

    def quarantine_revision(self, org_id: str) -> int:
        return 0

    def read_decision_projections(self, org_id: str):
        raise AssertionError("decision fallback read called")

    def read_pushes(self, org_id: str):
        raise AssertionError("push fallback read called")

    def read_inference_call_summaries(self, org_id: str):
        raise AssertionError("completion fallback read called")


def _note(session_id: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": session_id,
                    "stamped_at": "2026-07-15T00:00:00+00:00",
                }
            ],
        }
    )


def _stamped_world(tmp_path: Path, store: FactStore, session_id: str) -> MirrorManager:
    """A real repo whose HEAD carries a notes stamp for ``session_id``, pushed
    and mirrored. This is what a *working* stamper looks like."""
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        captured_at=_at(1),
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    store.store_push(push)
    return mirrors


def _observation(
    session_id: str, *, captured_at: datetime = BASE, observation_id: str = "observed"
):
    from sediment_core import SessionCommitObservation

    return SessionCommitObservation(
        observation_id=observation_id,
        org_id=ORG,
        repo=REPO,
        commit_sha="a" * 40,
        session_id=session_id,
        source_push_id="push",
        captured_at=captured_at,
    )


@pytest.mark.parametrize("source", list(AttributionSource))
def test_perfect_similarity_never_proves_committed_or_abandoned(
    tmp_path, postgres_store, source
):
    from dataclasses import replace

    postgres_store.store_decision(_decision("candidate"))
    result = derive_abandonment(
        postgres_store,
        _mirrors(tmp_path),
        ORG,
        attributions=[
            replace(_attribution("candidate", "call"), attribution_source=source)
        ],
        session_commits={"candidate": [CommitRef(REPO, "a" * 40)]},
        as_of=_at(60 * 24 * 40),
    )
    assert result.sessions == []
    assert [item.status for item in result.outcomes] == ["attribution_unavailable"]
    assert result.skipped == {"session_commit_unobserved": 1}


def test_live_notes_and_other_sessions_cannot_supply_observation(
    tmp_path, postgres_store
):
    postgres_store.store_decisions([_decision("noted"), _decision("unseen")])
    mirrors = _stamped_world(tmp_path, postgres_store, "noted")
    postgres_store.store_session_commit_observation(_observation("other"))
    result = derive_abandonment(postgres_store, mirrors, ORG, as_of=_at(60 * 24 * 40))
    assert result.sessions == []
    assert [item.status for item in result.outcomes] == ["attribution_unavailable"] * 2
    assert result.skipped == {"session_commit_unobserved": 2}


def test_observed_session_needs_neither_call_nor_mirror(tmp_path, postgres_store):
    postgres_store.store_decisions(
        [
            _decision("candidate"),
            _decision("candidate", explicit=False, occurred_at=_at(1)),
            _decision("rejected", accepted=False),
        ]
    )
    postgres_store.store_session_commit_observation(
        _observation("candidate", captured_at=_at(2))
    )
    result = derive_abandonment(postgres_store, _mirrors(tmp_path), ORG, as_of=_at(2))
    [outcome] = result.outcomes
    assert (
        outcome.status,
        outcome.accepted_decisions,
        outcome.explicit_accepted_decisions,
    ) == ("committed", 2, 1)
    assert outcome.provenance.policy_version == "6"
    assert result.skipped == {"reached_a_commit": 1, "no_accepted_decision": 1}
    assert result.sessions == []


def test_preloaded_empty_observations_are_authoritative(tmp_path, postgres_store):
    postgres_store.store_decision(_decision("candidate"))
    postgres_store.store_session_commit_observation(_observation("candidate"))
    result = derive_abandonment(
        postgres_store,
        _mirrors(tmp_path),
        ORG,
        session_commit_observations=[],
        as_of=_at(2),
    )
    assert result.outcomes[0].status == "attribution_unavailable"
    assert result.skipped == {"session_commit_unobserved": 1}


def test_preloaded_facts_need_no_fallback_reads(tmp_path):
    from sediment_derive import (
        build_repository_context,
        repository_identity_evidence_of,
    )

    observation = _observation("candidate")
    context = build_repository_context(
        [repository_identity_evidence_of(observation)], (), ORG, as_of=_at(2)
    )
    result = derive_abandonment(
        _BoundaryStore(),
        _mirrors(tmp_path),
        ORG,
        decisions=[_decision("candidate")],
        pushes=[],
        completions=[],
        session_commit_observations=[observation],
        repository_context=context,
        as_of=_at(2),
    )
    assert result.outcomes[0].status == "committed"


def test_decisions_captured_after_boundary_are_excluded(tmp_path, postgres_store):
    postgres_store.store_decision(_decision("late", occurred_at=_at(3)))
    result = derive_abandonment(postgres_store, _mirrors(tmp_path), ORG, as_of=_at(2))
    assert result.outcomes == []
    assert result.skipped == {}


def test_no_decisions_has_no_session_population(tmp_path, postgres_store):
    result = derive_abandonment(postgres_store, _mirrors(tmp_path), ORG)
    assert result.outcomes == []
    assert result.sessions == []
    assert result.skipped == {}


def test_shuffled_facts_and_repeated_reads_are_identical(
    tmp_path, postgres_store_factory
):
    decisions = [_decision("a"), _decision("b"), _decision("reject", accepted=False)]
    observations = [
        _observation("a", observation_id="a"),
        _observation("b", observation_id="b", captured_at=_at(3)),
    ]
    results = []
    for index in range(3):
        _, store = postgres_store_factory()
        random.Random(index).shuffle(decisions)
        random.Random(index).shuffle(observations)
        store.store_decisions(decisions)
        for observation in observations:
            store.store_session_commit_observation(observation)
        result = derive_abandonment(store, _mirrors(tmp_path), ORG, as_of=_at(2))
        assert result == derive_abandonment(
            store, _mirrors(tmp_path), ORG, as_of=_at(2)
        )
        results.append(result)
    assert results[0] == results[1] == results[2]
    assert [item.status for item in results[0].outcomes] == [
        "committed",
        "attribution_unavailable",
    ]


def test_legacy_policy_fields_do_not_create_negative_evidence(tmp_path, postgres_store):
    postgres_store.store_decision(_decision("candidate"))
    for horizon in (1, 14, 90):
        result = derive_abandonment(
            postgres_store,
            _mirrors(tmp_path),
            ORG,
            AbandonmentPolicy(grace_horizon_days=horizon),
            as_of=_at(60 * 24 * 100),
        )
        assert result.sessions == []
        assert result.outcomes[0].status == "attribution_unavailable"
    with pytest.raises(ValueError, match="grace_horizon_days"):
        AbandonmentPolicy(grace_horizon_days=0)


def test_unobserved_attribution_cannot_establish_a_factual_session_outcome(
    tmp_path: Path, postgres_store
) -> None:
    store = postgres_store
    decision = _decision("unobserved")
    call = _inference_call("unobserved")
    store.store_decision(decision)
    store.store_inference_call(call)
    result = derive_abandonment(
        store,
        _mirrors(tmp_path),
        ORG,
        attributions=[_attribution("unobserved", call.inference_call_id)],
        as_of=_at(60 * 24 * 30),
    )
    assert result.sessions == []
    assert [item.status for item in result.outcomes] == ["attribution_unavailable"]
    assert result.skipped["session_commit_unobserved"] == 1


@pytest.mark.parametrize(
    "case", ["matching", "late", "other_session", "other_org", "quarantined"]
)
def test_factual_session_status_requires_eligible_observation(
    tmp_path: Path, postgres_store, case: str
) -> None:
    from sediment_core import FactTable, SessionCommitObservation

    store = postgres_store
    store.store_decision(_decision("observed"))
    observation = SessionCommitObservation(
        observation_id="observation",
        org_id="other" if case == "other_org" else ORG,
        repo=REPO,
        commit_sha="a" * 40,
        session_id="other" if case == "other_session" else "observed",
        source_push_id="push",
        captured_at=_at(3 if case == "late" else 2),
    )
    store.store_session_commit_observation(observation)
    if case == "quarantined":
        store.quarantine_fact(
            ORG,
            FactTable.SESSION_COMMIT_OBSERVATIONS,
            observation.observation_id,
            reason="test",
        )
    result = derive_abandonment(store, _mirrors(tmp_path), ORG, as_of=_at(2))
    assert result.sessions == []
    expected = "committed" if case == "matching" else "attribution_unavailable"
    assert [item.status for item in result.outcomes] == [expected]
    assert result.skipped["session_commit_unobserved"] == (case != "matching")


@pytest.mark.parametrize("explicit_boundary", [False, True])
def test_accepted_session_fold_instants_match_stored_boundaries(
    postgres_store, tmp_path, explicit_boundary
):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/London")
    boundary = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=0)
    late_time = datetime(2026, 10, 25, 1, 15, tzinfo=zone, fold=1)
    early = _decision("session", occurred_at=boundary)
    late = _decision("session", occurred_at=late_time)
    for decision in (early, late):
        postgres_store.store_decision(decision)
    kwargs = {"as_of": boundary} if explicit_boundary else {}
    mirrors = _mirrors(tmp_path)
    stored = derive_abandonment(postgres_store, mirrors, ORG, **kwargs)
    [outcome] = stored.outcomes
    expected_time = boundary if explicit_boundary else late_time
    assert outcome.accepted_decisions == (1 if explicit_boundary else 2)
    assert outcome.last_decision_at == expected_time.astimezone(UTC)
    for population in ([early, late], [late, early]):
        result = derive_abandonment(
            postgres_store,
            mirrors,
            ORG,
            decisions=population,
            completions=[],
            pushes=[],
            session_commit_observations=[],
            **kwargs,
        )
        assert result.as_of.astimezone(UTC) == stored.as_of.astimezone(UTC)
        [actual] = result.outcomes
        assert actual.accepted_decisions == outcome.accepted_decisions
        assert actual.last_decision_at.astimezone(UTC) == outcome.last_decision_at
        assert result.skipped == stored.skipped


def test_default_session_boundary_has_one_spelling_for_equal_instants(
    postgres_store, tmp_path
):
    from zoneinfo import ZoneInfo
    from sediment_core import SessionCommitObservation

    local = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0)
    utc = local.astimezone(UTC)
    decision = _decision("session", occurred_at=utc - timedelta(minutes=5))
    observations = [
        SessionCommitObservation(
            observation_id=identifier,
            org_id=ORG,
            repo=REPO,
            commit_sha=sha * 40,
            session_id="session",
            source_push_id="push",
            captured_at=when,
        )
        for identifier, sha, when in (("local", "a", local), ("utc", "b", utc))
    ]
    for facts in (observations, list(reversed(observations))):
        result = derive_abandonment(
            postgres_store,
            _mirrors(tmp_path),
            ORG,
            decisions=[decision],
            completions=[],
            pushes=[],
            session_commit_observations=facts,
        )
        assert result.as_of.isoformat() == utc.isoformat()
        assert result.outcomes[0].as_of.isoformat() == utc.isoformat()
        assert result.skipped == {"reached_a_commit": 1}
