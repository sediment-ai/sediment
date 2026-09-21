# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from sediment_core import (
    AgentHarness,
    CIOutcome,
    CIProvider,
    CIResult,
    DeveloperDecision,
    EditObservation,
    InteractionMode,
    ForgeProvider,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    QuarantineAction,
    RejectedEdit,
    RepositoryRename,
    RetryLinkage,
    SessionCommitObservation,
    TextPart,
    ToolCallPart,
    FactStore,
)
from sediment_core.postgres_engine import DatabaseOperationError
from sediment_core.postgres_schema import developer_decisions
from sediment_core.redaction import REDACTION_MARKER
from sediment_core import store as store_module

T0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def test_composite_filter_key_limit_allows_maximum_and_rejects_before_execute() -> None:
    class Rows:
        def mappings(self):
            return self

        def all(self):
            return []

    class Connection:
        def __init__(self) -> None:
            self.executions = 0

        def execute(self, statement):
            self.executions += 1
            return Rows()

    connection = Connection()
    maximum = {
        (f"acme/repo-{index}", f"{index:040x}")
        for index in range(store_module.COMPOSITE_FILTER_KEY_LIMIT)
    }

    assert (
        store_module._read_ci_outcomes(
            connection,
            "acme",
            False,
            repo_commits=maximum,
            limit=1,
        )
        == []
    )
    assert connection.executions == 1

    with pytest.raises(ValueError, match="composite filter exceeds"):
        store_module._read_ci_outcomes(
            connection,
            "acme",
            False,
            repo_commits=maximum | {("acme/overflow", "f" * 40)},
            limit=1,
        )
    assert connection.executions == 1


def _session_commit_observation(**overrides) -> SessionCommitObservation:
    values = {
        "observation_id": "session-commit-observation-1",
        "org_id": "acme",
        "repo": "acme/service",
        "commit_sha": "a" * 40,
        "session_id": "session-from-note",
        "source_push_id": "push-1",
        "captured_at": T0,
    }
    values.update(overrides)
    return SessionCommitObservation(**values)


def test_postgres_session_commit_observation_round_trip_and_session_upsert(
    postgres_store,
) -> None:
    observation = _session_commit_observation()

    assert postgres_store.store_session_commit_observation(observation) is True
    assert postgres_store.read_session_commit_observations("acme") == [observation]
    session = next(
        row
        for row in postgres_store.read_sessions("acme")
        if row.session_id == observation.session_id
    )
    assert session.first_observed_at == T0
    assert session.last_observed_at == T0


def test_postgres_session_commit_observation_preserves_first_natural_key_row(
    postgres_store,
) -> None:
    first = _session_commit_observation()
    duplicate = _session_commit_observation(
        observation_id="session-commit-observation-2",
        source_push_id="push-2",
        captured_at=T0 + timedelta(hours=1),
    )

    assert postgres_store.store_session_commit_observation(first) is True
    assert postgres_store.store_session_commit_observation(duplicate) is False
    assert postgres_store.read_session_commit_observations("acme") == [first]


def test_postgres_session_commit_observation_historical_boundary_is_inclusive(
    postgres_store,
) -> None:
    first = _session_commit_observation()
    second = _session_commit_observation(
        observation_id="session-commit-observation-2",
        commit_sha="b" * 40,
        captured_at=T0 + timedelta(seconds=1),
    )
    for observation in (second, first):
        postgres_store.store_session_commit_observation(observation)

    assert postgres_store.read_session_commit_observations("acme", as_of=T0) == [first]
    assert postgres_store.read_session_commit_observations(
        "acme", as_of=T0 + timedelta(seconds=1)
    ) == [first, second]


def test_postgres_session_commit_observation_is_quarantine_aware(
    postgres_store,
) -> None:
    observation = _session_commit_observation()
    postgres_store.store_session_commit_observation(observation)

    postgres_store.quarantine_fact(
        "acme",
        FactTable.SESSION_COMMIT_OBSERVATIONS,
        observation.observation_id,
        reason="incorrect note",
    )

    assert postgres_store.read_session_commit_observations("acme") == []
    assert postgres_store.read_session_commit_observations(
        "acme", include_quarantined=True
    ) == [observation]


def test_postgres_session_commit_observation_order_is_insertion_independent(
    postgres_store_factory,
) -> None:
    _, store_a = postgres_store_factory()
    _, store_b = postgres_store_factory()
    first = _session_commit_observation(
        observation_id="a-observation", commit_sha="a" * 40
    )
    second = _session_commit_observation(
        observation_id="b-observation", commit_sha="b" * 40
    )

    for store, observations in (
        (store_a, (first, second)),
        (store_b, (second, first)),
    ):
        for observation in observations:
            store.store_session_commit_observation(observation)

    assert store_a.read_session_commit_observations("acme") == [first, second]
    assert store_b.read_session_commit_observations("acme") == [first, second]


def test_postgres_session_commit_observation_ties_order_by_edge_identity(
    postgres_store,
) -> None:
    first = _session_commit_observation(
        observation_id="z-observation",
        session_id="session-1",
    )
    second = _session_commit_observation(
        observation_id="a-observation",
        session_id="session-2",
    )
    for observation in (second, first):
        postgres_store.store_session_commit_observation(observation)

    assert postgres_store.read_session_commit_observations("acme") == [first, second]


def test_postgres_session_commit_observation_rejects_naive_boundary(
    postgres_store,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        postgres_store.read_session_commit_observations(
            "acme", as_of=datetime(2026, 8, 22, 12, 0)
        )


def test_postgres_session_commit_observations_bound_edges_and_cardinality(
    postgres_store,
) -> None:
    selected = _session_commit_observation()
    unrelated = _session_commit_observation(
        observation_id="session-commit-observation-2",
        repo="acme/other",
        commit_sha="b" * 40,
    )
    postgres_store.store_session_commit_observation(selected)
    postgres_store.store_session_commit_observation(unrelated)

    assert postgres_store.read_session_commit_observations(
        "acme", repo_commits={(selected.repo, selected.commit_sha)}, limit=1
    ) == [selected]
    with pytest.raises(ValueError, match="exceeds 1"):
        postgres_store.read_session_commit_observations("acme", limit=1)


def test_postgres_session_commit_observations_filter_sessions(
    postgres_store,
) -> None:
    selected = _session_commit_observation()
    unrelated = _session_commit_observation(
        observation_id="session-commit-observation-other",
        commit_sha="b" * 40,
        session_id="session-other",
    )
    postgres_store.store_session_commit_observation(unrelated)
    postgres_store.store_session_commit_observation(selected)

    assert postgres_store.read_session_commit_observations(
        "acme", session_ids={selected.session_id}, limit=1
    ) == [selected]


def test_postgres_supporting_fact_reads_apply_scope_boundary_and_exact_keys(
    postgres_store,
) -> None:
    included_decision = _decision(
        decision_id="decision-included",
        session_id="session-included",
        call_id="call-included",
        captured_at=T0,
    )
    later_decision = _decision(
        decision_id="decision-later",
        session_id="session-included",
        call_id="call-later",
        captured_at=T0 + timedelta(seconds=1),
    )
    other_decision = _decision(
        decision_id="decision-other",
        session_id="session-other",
        call_id="call-included",
        captured_at=T0,
    )
    for decision in (later_decision, other_decision, included_decision):
        postgres_store.store_decision(decision)

    included_observation = _observation(
        observation_id="observation-included",
        session_id="session-included",
        call_id="call-included",
        captured_at=T0,
    )
    later_observation = _observation(
        observation_id="observation-later",
        session_id="session-included",
        call_id="call-later",
        captured_at=T0 + timedelta(seconds=1),
    )
    for observation in (later_observation, included_observation):
        postgres_store.store_edit_observation(observation)

    included_retry = _retry_linkage(
        retry_linkage_id="retry-included",
        session_id="session-included",
        captured_at=T0,
    )
    later_retry = _retry_linkage(
        retry_linkage_id="retry-later",
        session_id="session-included",
        rejected_call_id="reject-later",
        accepted_call_id="accept-later",
        captured_at=T0 + timedelta(seconds=1),
    )
    for retry in (later_retry, included_retry):
        postgres_store.store_retry_linkage(retry)

    included_ci = _ci_outcome(
        outcome_id="ci-included",
        repo="acme/service",
        commit_sha="a" * 40,
        captured_at=T0,
    )
    later_ci = _ci_outcome(
        outcome_id="ci-later",
        run_id="run-later",
        repo="acme/service",
        commit_sha="a" * 40,
        captured_at=T0 + timedelta(seconds=1),
    )
    other_ci = _ci_outcome(
        outcome_id="ci-other",
        run_id="run-other",
        repo="acme/other",
        commit_sha="b" * 40,
        captured_at=T0,
    )
    for outcome in (later_ci, other_ci, included_ci):
        postgres_store.store_ci_outcome(outcome)

    with postgres_store.read_snapshot() as snapshot:
        assert snapshot.read_decisions(
            "acme",
            captured_through=T0,
            session_ids={"session-included"},
            call_ids={"call-included"},
            limit=1,
        ) == [included_decision]
        assert snapshot.read_edit_observations(
            "acme",
            captured_through=T0,
            session_ids={"session-included"},
            call_ids={"call-included"},
            limit=1,
        ) == [included_observation]
        assert snapshot.read_retry_linkages(
            "acme",
            captured_through=T0,
            session_ids={"session-included"},
            limit=1,
        ) == [included_retry]
        assert [
            outcome.outcome_id
            for outcome in snapshot.read_ci_outcomes(
                "acme",
                captured_through=T0,
                repo_commits={("acme/service", "a" * 40)},
                limit=1,
            )
        ] == [included_ci.outcome_id]


def test_postgres_supporting_fact_reads_reject_overflow_and_naive_boundary(
    postgres_store,
) -> None:
    postgres_store.store_decision(_decision(decision_id="decision-1"))
    postgres_store.store_decision(_decision(decision_id="decision-2", call_id="call-2"))
    postgres_store.store_edit_observation(_observation(observation_id="edit-1"))
    postgres_store.store_edit_observation(
        _observation(observation_id="edit-2", call_id="edit-call-2")
    )
    postgres_store.store_retry_linkage(_retry_linkage(retry_linkage_id="retry-1"))
    postgres_store.store_retry_linkage(
        _retry_linkage(
            retry_linkage_id="retry-2",
            rejected_call_id="reject-2",
            accepted_call_id="accept-2",
        )
    )
    postgres_store.store_ci_outcome(_ci_outcome(outcome_id="ci-1"))
    postgres_store.store_ci_outcome(_ci_outcome(outcome_id="ci-2", run_id="run-2"))

    with postgres_store.read_snapshot() as snapshot:
        for read in (
            snapshot.read_decisions,
            snapshot.read_edit_observations,
            snapshot.read_retry_linkages,
            snapshot.read_ci_outcomes,
        ):
            with pytest.raises(ValueError, match="exceeds 1"):
                read("acme", limit=1)
            with pytest.raises(ValueError, match="timezone-aware"):
                read("acme", captured_through=datetime(2026, 8, 22, 12, 0))


def test_postgres_bounded_supporting_fact_reads_preserve_quarantine_visibility(
    postgres_store,
) -> None:
    decision = _decision(decision_id="bounded-quarantined")
    postgres_store.store_decision(decision)
    postgres_store.quarantine_fact(
        "acme",
        FactTable.DEVELOPER_DECISIONS,
        decision.decision_id,
        reason="bad bounded evidence",
    )

    with postgres_store.read_snapshot() as snapshot:
        assert (
            snapshot.read_decisions(
                "acme", captured_through=T0, session_ids={decision.session_id}, limit=1
            )
            == []
        )
        assert snapshot.read_decisions(
            "acme",
            captured_through=T0,
            session_ids={decision.session_id},
            limit=1,
            include_quarantined=True,
        ) == [decision]


def _inference_call(**overrides) -> InferenceCall:
    values = {
        "inference_call_id": "inference-1",
        "org_id": "acme",
        "session_id": "session-inference",
        "user_id": "developer-1",
        "gateway_provider": GatewayProvider.LITELLM,
        "model_provider": "anthropic",
        "model": "claude-sonnet",
        "input_messages": [
            InferenceMessage(role="user", parts=[TextPart(content="Fix storage")])
        ],
        "output_messages": [
            InferenceMessage(role="assistant", parts=[TextPart(content="Done")])
        ],
        "input_tokens": 4,
        "output_tokens": 1,
        "duration_ms": 50,
        "model_call_id": "model-call-1",
        "observed_at": T0,
        "raw": {"source": "contract-test"},
    }
    values.update(overrides)
    return InferenceCall(**values)


def _decision(**overrides) -> DeveloperDecision:
    values = {
        "decision_id": "decision-1",
        "org_id": "acme",
        "session_id": "session-1",
        "user_id": "developer-1",
        "agent_harness": AgentHarness.CLAUDE_CODE,
        "file_path": "src/store.py",
        "accepted": True,
        "explicit": True,
        "interaction_mode": InteractionMode.AGENT,
        "commit_sha": "a" * 40,
        "call_id": "tool-call-1",
        "edit_retention_score": 0.75,
        "observation_delay_ms": 5000,
        "occurred_at": T0,
        "captured_at": T0,
        "raw": {"source": "contract-test"},
    }
    values.update(overrides)
    return DeveloperDecision(**values)


def _observation(**overrides) -> EditObservation:
    values = {
        "observation_id": "observation-1",
        "org_id": "acme",
        "session_id": "session-observation",
        "user_id": None,
        "agent_harness": AgentHarness.CODEX,
        "file_path": "src/store.py",
        "call_id": "edit-call-1",
        "applied_text": "lone=\ud800 null=\x00",
        "observed_file_text": "lone=\ud800 null=\x00\nkept=True",
        "external_lines_added": None,
        "external_lines_removed": 0,
        "occurred_at": T0,
        "captured_at": T0,
        "raw": {"source": "contract-test"},
    }
    values.update(overrides)
    return EditObservation(**values)


def _rejected_edit(**overrides) -> RejectedEdit:
    values = {
        "rejection_id": "rejection-1",
        "org_id": "acme",
        "session_id": "session-rejection",
        "user_id": "developer-1",
        "agent_harness": AgentHarness.PI,
        "file_path": "src/store.py",
        "call_id": "reject-call-1",
        "proposed": "lone=\ud800 null=\x00",
        "occurred_at": T0,
        "captured_at": T0,
        "raw": {"source": "contract-test"},
    }
    values.update(overrides)
    return RejectedEdit(**values)


def _retry_linkage(**overrides) -> RetryLinkage:
    values = {
        "retry_linkage_id": "retry-linkage-1",
        "org_id": "acme",
        "session_id": "session-retry",
        "user_id": "developer-1",
        "agent_harness": AgentHarness.CLAUDE_CODE,
        "file_path": "src/store.py",
        "tool_name": "Edit",
        "rejected_call_id": "reject-call-1",
        "accepted_call_id": "accept-call-2",
        "occurred_at": T0,
        "captured_at": T0,
        "raw": {"source": "session-end"},
    }
    values.update(overrides)
    return RetryLinkage(**values)


def _ci_outcome(**overrides) -> CIOutcome:
    values = {
        "outcome_id": "outcome-1",
        "org_id": "acme",
        "provider": CIProvider.GITHUB_ACTIONS,
        "run_id": "run-1",
        "run_attempt": None,
        "repo": "acme/backend",
        "commit_sha": "b" * 40,
        "branch": "main",
        "result": CIResult.ERROR,
        "workflow_name": "CI",
        "workflow_id": "workflow-1",
        "workflow_path": ".github/workflows/ci.yml",
        "run_url": "https://example.test/runs/1",
        "provider_result": "action_required",
        "error_type": "runner_unavailable",
        "reason": "runner pool unavailable",
        "source_event_type": "github.workflow_run.completed",
        "source_spec_version": "1",
        "source_event_id": "delivery-1",
        "pr_number": 7,
        "captured_at": T0,
        "raw": {"lone": "\ud800", "null": "\x00", "nan": float("nan")},
    }
    values.update(overrides)
    return CIOutcome(**values)


def _push(**overrides) -> Push:
    values = {
        "push_id": "push-1",
        "org_id": "acme",
        "provider": ForgeProvider.GITHUB,
        "repo": "acme/backend",
        "clone_url": "https://example.test/acme/backend.git",
        "ref": "refs/heads/main",
        "before_sha": "a" * 40,
        "after_sha": "b" * 40,
        "forced": True,
        "captured_at": T0,
    }
    values.update(overrides)
    return Push(**values)


def _repository_rename(**overrides) -> RepositoryRename:
    values = {
        "rename_id": "rename-1",
        "org_id": "acme",
        "repository_provider": ForgeProvider.GITHUB,
        "repository_host": "github.com",
        "repository_id": "101",
        "old_repo": "acme/backend",
        "new_repo": "acme/renamed",
        "captured_at": T0,
    }
    values.update(overrides)
    return RepositoryRename(**values)


def _pull_request_merge(**overrides) -> PullRequestMerge:
    values = {
        "merge_id": "merge-1",
        "org_id": "acme",
        "provider": ForgeProvider.GITHUB,
        "repo": "acme/backend",
        "pr_number": 7,
        "head_repo": "acme/backend",
        "head_ref": "feature/query",
        "head_sha": "a" * 40,
        "base_ref": "main",
        "base_sha": "b" * 40,
        "merge_commit_sha": "c" * 40,
        "merged_at": T0,
        "source_event_id": "delivery-1",
        "captured_at": T0,
    }
    values.update(overrides)
    return PullRequestMerge(**values)


def _pull_request_revision(**overrides) -> PullRequestRevision:
    values = {
        "revision_id": "revision-1",
        "org_id": "acme",
        "provider": ForgeProvider.GITHUB,
        "repo": "acme/backend",
        "pr_number": 7,
        "head_repo": "acme/backend",
        "head_ref": "feature/query",
        "head_sha": "d" * 40,
        "base_ref": "main",
        "base_sha": "b" * 40,
        "previous_head_sha": "a" * 40,
        "source_event_id": "delivery-2",
        "captured_at": T0,
    }
    values.update(overrides)
    return PullRequestRevision(**values)


def test_postgres_decision_round_trip_and_session_read(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    decision = _decision()

    assert store.store_decision(decision) is True
    assert store.read_decisions("acme") == [decision]
    [session] = store.read_sessions("acme")
    assert session.org_id == "acme"
    assert session.session_id == "session-1"
    assert session.user_id == "developer-1"
    assert session.user_id_conflict is False
    assert session.first_observed_at == T0
    assert session.last_observed_at == T0


def test_postgres_edit_observation_round_trip_and_session_upsert(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    observation = _observation()

    assert store.store_edit_observation(observation) is True
    assert store.read_edit_observations("acme") == [observation]
    [session] = store.read_sessions("acme")
    assert session.session_id == "session-observation"
    assert session.user_id is None


def test_postgres_rejected_edit_round_trip_and_first_write_wins(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    rejected = _rejected_edit()

    assert store.store_rejected_edit(rejected) is True
    assert (
        store.store_rejected_edit(
            rejected.model_copy(
                update={"rejection_id": "rejection-2", "proposed": "changed"}
            )
        )
        is False
    )
    assert store.read_rejected_edits("acme") == [rejected]
    assert store.read_sessions("acme")[0].session_id == "session-rejection"


def test_postgres_retry_linkage_round_trip_and_redelivery(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    linkage = _retry_linkage()

    assert store.store_retry_linkage(linkage) is True
    assert (
        store.store_retry_linkage(
            linkage.model_copy(update={"retry_linkage_id": "retry-linkage-2"})
        )
        is False
    )
    assert store.read_retry_linkages("acme") == [linkage]
    assert store.read_sessions("acme")[0].session_id == "session-retry"


def test_postgres_retry_linkage_quarantine_hides_fact_but_keeps_dedup(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    linkage = _retry_linkage()
    assert store.store_retry_linkage(linkage) is True

    store.quarantine_fact(
        "acme",
        FactTable.RETRY_LINKAGES,
        linkage.retry_linkage_id,
        reason="bad transcript linkage",
    )
    assert store.read_retry_linkages("acme") == []
    assert store.read_retry_linkages("acme", include_quarantined=True) == [linkage]
    assert (
        store.store_retry_linkage(
            linkage.model_copy(update={"retry_linkage_id": "retry-linkage-redelivery"})
        )
        is False
    )


def test_postgres_ci_outcome_round_trip_and_expression_dedup(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    outcome = _ci_outcome()

    assert store.store_ci_outcome(outcome) is True
    assert (
        store.store_ci_outcome(
            outcome.model_copy(update={"outcome_id": "outcome-redelivery"})
        )
        is False
    )
    [stored] = store.read_ci_outcomes("acme")
    assert stored.model_dump(exclude={"raw"}) == outcome.model_dump(exclude={"raw"})
    assert stored.raw["lone"] == "\ud800"
    assert stored.raw["null"] == "\x00"
    assert math.isnan(stored.raw["nan"])


def test_postgres_ci_outcome_exact_run_lookup_distinguishes_attempts(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    first = _ci_outcome(outcome_id="outcome-attempt-1", run_attempt=1)
    second = _ci_outcome(outcome_id="outcome-attempt-2", run_attempt=2)
    store.store_ci_outcome(first)
    store.store_ci_outcome(second)

    found = store.read_ci_outcome_by_run(
        "acme", CIProvider.GITHUB_ACTIONS, "run-1", run_attempt=2
    )

    assert found is not None
    assert found.outcome_id == "outcome-attempt-2"
    assert found.run_attempt == 2
    assert found.provider_result == "action_required"
    assert not hasattr(found, "raw")


def test_postgres_ci_failure_search_is_bounded_and_keyset_paginated(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    for index, result in enumerate(
        [CIResult.FAILED, CIResult.PASSED, CIResult.FAILED], start=1
    ):
        store.store_ci_outcome(
            _ci_outcome(
                outcome_id=f"outcome-{index}",
                run_id=f"run-{index}",
                run_attempt=1,
                result=result,
                captured_at=T0 + timedelta(minutes=index),
            )
        )
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="other-org",
            org_id="other",
            run_id="other-run",
            run_attempt=1,
            result=CIResult.FAILED,
            captured_at=T0 + timedelta(minutes=4),
        )
    )

    first_page = store.read_ci_outcome_summaries(
        "acme",
        repo="acme/backend",
        result=CIResult.FAILED,
        captured_between=(T0, T0 + timedelta(hours=1)),
        limit=1,
    )
    second_page = store.read_ci_outcome_summaries(
        "acme",
        repo="acme/backend",
        result=CIResult.FAILED,
        captured_between=(T0, T0 + timedelta(hours=1)),
        before=(first_page[0].captured_at, first_page[0].outcome_id),
        limit=1,
    )

    assert [row.outcome_id for row in first_page] == ["outcome-3"]
    assert [row.outcome_id for row in second_page] == ["outcome-1"]


def test_postgres_ci_outcome_reason_redaction_overflow_stores_and_dedups(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    reason = "a" * 4082 + '"api_key": "x"'
    assert len(reason) == 4096
    outcome = _ci_outcome(
        run_id="run-overflow",
        run_attempt=1,
        result=CIResult.FAILED,
        reason=reason,
        raw={},
    )

    assert store.store_ci_outcome(outcome) is True
    assert (
        store.store_ci_outcome(
            outcome.model_copy(update={"outcome_id": "outcome-redelivery"})
        )
        is False
    )
    [stored] = store.read_ci_outcomes("acme")
    assert len(stored.reason) <= 4096
    assert stored.reason.endswith(REDACTION_MARKER)
    assert '"x"' not in stored.reason


def test_postgres_push_round_trip_and_natural_key_dedup(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    push = _push()

    assert store.store_push(push) is True
    assert (
        store.store_push(push.model_copy(update={"push_id": "push-redelivery"}))
        is False
    )
    assert store.read_pushes("acme") == [push]


def test_postgres_pull_request_merge_round_trip_and_natural_key_dedup(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    merge = _pull_request_merge()

    assert store.store_pull_request_merge(merge) is True
    assert (
        store.store_pull_request_merge(
            merge.model_copy(update={"merge_id": "merge-redelivery"})
        )
        is False
    )
    assert store.read_pull_request_merges("acme") == [merge]
    assert store.count_facts("acme", FactTable.PULL_REQUEST_MERGES) == 1


def test_postgres_pull_request_merge_quarantine_visibility(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    merge = _pull_request_merge()
    store.store_pull_request_merge(merge)

    store.quarantine_fact(
        "acme", FactTable.PULL_REQUEST_MERGES, merge.merge_id, reason="bad boundary"
    )

    assert store.read_pull_request_merges("acme") == []
    assert store.read_pull_request_merges("acme", include_quarantined=True) == [merge]


def test_postgres_pull_request_revision_round_trip_and_natural_key_dedup(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    revision = _pull_request_revision()

    assert store.store_pull_request_revision(revision) is True
    assert (
        store.store_pull_request_revision(
            revision.model_copy(
                update={
                    "revision_id": "revision-redelivery",
                    "previous_head_sha": "f" * 40,
                }
            )
        )
        is False
    )
    assert store.read_pull_request_revisions("acme") == [revision]
    assert store.count_facts("acme", FactTable.PULL_REQUEST_REVISIONS) == 1


def test_postgres_pull_request_revision_quarantine_visibility(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    revision = _pull_request_revision()
    store.store_pull_request_revision(revision)

    store.quarantine_fact(
        "acme",
        FactTable.PULL_REQUEST_REVISIONS,
        revision.revision_id,
        reason="bad boundary",
    )

    assert store.read_pull_request_revisions("acme") == []
    assert store.read_pull_request_revisions("acme", include_quarantined=True) == [
        revision
    ]


def test_postgres_pull_request_reads_apply_boundaries_and_exact_pr_keys(
    postgres_store,
) -> None:
    selected_merge = _pull_request_merge(merge_id="merge-selected")
    late_capture_merge = _pull_request_merge(
        merge_id="merge-late-capture",
        pr_number=8,
        captured_at=T0 + timedelta(seconds=1),
    )
    late_event_merge = _pull_request_merge(
        merge_id="merge-late-event",
        pr_number=9,
        merged_at=T0 + timedelta(seconds=1),
    )
    for merge in (late_capture_merge, late_event_merge, selected_merge):
        postgres_store.store_pull_request_merge(merge)

    selected_revision = _pull_request_revision(revision_id="revision-selected")
    late_revision = _pull_request_revision(
        revision_id="revision-late",
        pr_number=8,
        head_sha="e" * 40,
        captured_at=T0 + timedelta(seconds=1),
    )
    for revision in (late_revision, selected_revision):
        postgres_store.store_pull_request_revision(revision)

    with postgres_store.read_snapshot() as snapshot:
        assert snapshot.read_pull_request_merges(
            "acme",
            captured_through=T0,
            merged_through=T0,
            repo_prs={("acme/backend", 7)},
            limit=1,
        ) == [selected_merge]
        assert snapshot.read_pull_request_revisions(
            "acme",
            captured_through=T0,
            repo_prs={("acme/backend", 7)},
            limit=1,
        ) == [selected_revision]


def test_postgres_pull_request_reads_empty_filters_overflow_and_validation(
    postgres_store,
) -> None:
    postgres_store.store_pull_request_merge(_pull_request_merge(merge_id="merge-1"))
    postgres_store.store_pull_request_merge(
        _pull_request_merge(merge_id="merge-2", pr_number=8)
    )
    postgres_store.store_pull_request_revision(
        _pull_request_revision(revision_id="revision-1")
    )
    postgres_store.store_pull_request_revision(
        _pull_request_revision(revision_id="revision-2", pr_number=8, head_sha="e" * 40)
    )

    with postgres_store.read_snapshot() as snapshot:
        assert snapshot.read_pull_request_merges("acme", repo_prs=set()) == []
        assert snapshot.read_pull_request_revisions("acme", repo_prs=set()) == []
        for read in (
            snapshot.read_pull_request_merges,
            snapshot.read_pull_request_revisions,
        ):
            with pytest.raises(ValueError, match="exceeds 1"):
                read("acme", limit=1)
            with pytest.raises(ValueError, match="timezone-aware"):
                read("acme", captured_through=datetime(2026, 8, 22, 12, 0))


def test_postgres_bounded_pull_request_reads_preserve_quarantine_and_order(
    postgres_store_factory,
) -> None:
    _, store_a = postgres_store_factory()
    _, store_b = postgres_store_factory()
    first = _pull_request_revision(
        revision_id="z-revision", pr_number=7, head_sha="a" * 40
    )
    second = _pull_request_revision(
        revision_id="a-revision", pr_number=8, head_sha="b" * 40
    )
    for store, revisions in ((store_a, (first, second)), (store_b, (second, first))):
        for revision in revisions:
            store.store_pull_request_revision(revision)

    assert store_a.read_pull_request_revisions("acme", limit=2) == [first, second]
    assert store_b.read_pull_request_revisions("acme", limit=2) == [first, second]
    store_a.quarantine_fact(
        "acme",
        FactTable.PULL_REQUEST_REVISIONS,
        first.revision_id,
        reason="bad bounded revision",
    )
    assert store_a.read_pull_request_revisions("acme", limit=1) == [second]
    assert store_a.read_pull_request_revisions(
        "acme", limit=2, include_quarantined=True
    ) == [first, second]


def test_postgres_health_and_fact_counts(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    store.store_decision(_decision())
    store.store_edit_observation(_observation())
    store.store_retry_linkage(_retry_linkage())
    store.store_push(_push())

    assert store.health_check() is True
    assert store.count_sessions("acme") == 3
    assert store.count_facts("acme", FactTable.DEVELOPER_DECISIONS) == 1
    assert store.count_facts("acme", FactTable.EDIT_OBSERVATIONS) == 1
    assert store.count_facts("acme", FactTable.RETRY_LINKAGES) == 1
    assert store.count_facts("acme", FactTable.PUSHES) == 1
    assert (
        store.count_session_facts("acme", FactTable.DEVELOPER_DECISIONS, "session-1")
        == 1
    )
    assert (
        store.count_session_facts("acme", FactTable.RETRY_LINKAGES, "session-retry")
        == 1
    )

    try:
        store.count_session_facts("acme", FactTable.PUSHES, "session-1")
    except ValueError as exc:
        assert str(exc) == "pushes is not a session-scoped fact table"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("repository-scoped table was accepted")


def test_available_fact_tables_tracks_physical_schema_without_mutation(
    postgres_database_factory,
) -> None:
    from sqlalchemy import inspect
    from sediment_core.postgres_migrations import upgrade_database

    engine, url = _behind_engine(postgres_database_factory)
    try:
        store = FactStore(engine)
        before = inspect(engine).get_table_names()
        expected = set(FactTable) - {FactTable.REPOSITORY_RENAMES}
        assert set(store.available_fact_tables()) == expected
        assert inspect(engine).get_table_names() == before
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0008_session_commit_observations"
            )
        # Absent tables remain errors for callers explicitly requesting a count.
        with pytest.raises(DBAPIError):
            store.count_facts("acme", FactTable.REPOSITORY_RENAMES)
        upgrade_database(url)
        assert set(store.available_fact_tables()) == set(FactTable)
        assert store.count_facts("acme", FactTable.REPOSITORY_RENAMES) == 0
    finally:
        engine.dispose()


def test_postgres_decision_batch_is_atomic_and_preserves_input_flags(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    first = _decision(decision_id="decision-a", user_id=None)
    duplicate = first.model_copy(update={"decision_id": "decision-duplicate"})
    second = _decision(
        decision_id="decision-b",
        session_id="session-2",
        call_id="tool-call-2",
        occurred_at=T0.replace(minute=1),
        captured_at=T0.replace(minute=2),
    )

    assert store.store_decisions([first, duplicate, second]) == [True, False, True]
    assert store.read_decisions("acme") == [first, second]
    assert [session.session_id for session in store.read_sessions("acme")] == [
        "session-1",
        "session-2",
    ]


def _large_decision_batch(size: int) -> list[DeveloperDecision]:
    return [
        _decision(decision_id=f"batch-{index}", call_id=f"call-{index}")
        for index in range(size)
    ]


@pytest.mark.parametrize("extra_rows", [0, 1, 4_001])
def test_postgres_decision_statements_respect_real_parameter_budget(
    postgres_engine, extra_rows
) -> None:
    # Inspect the emitted driver parameters so added columns or statement-level
    # bindings cannot silently exceed the conservative PostgreSQL budget.
    row_parameters = len(developer_decisions.columns)
    row_budget = 60_000 // row_parameters
    decisions = _large_decision_batch(row_budget + extra_rows)
    writes = []

    def inspect_statement(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO developer_decisions"):
            assert not many
            assert len(parameters) <= 60_000
            writes.append((id(connection), len(parameters)))

    event.listen(postgres_engine, "before_cursor_execute", inspect_statement)
    try:
        store = FactStore(postgres_engine)
        assert store.store_decisions(decisions) == [True] * len(decisions)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", inspect_statement)
    assert [count for _, count in writes] == [
        min(row_budget, len(decisions) - offset) * row_parameters
        for offset in range(0, len(decisions), row_budget)
    ]
    assert len({connection for connection, _ in writes}) == 1
    assert {row.decision_id for row in store.read_decisions("acme")} == {
        row.decision_id for row in decisions
    }


@pytest.mark.parametrize("harness", [AgentHarness.CLAUDE_CODE, AgentHarness.CODEX])
def test_postgres_chunked_decisions_preserve_conflict_winners_and_order(
    postgres_store, harness
) -> None:
    row_budget = 60_000 // len(developer_decisions.columns)
    existing = _decision(
        decision_id="existing", call_id="existing-call", agent_harness=harness
    )
    assert postgres_store.store_decision(existing)
    first = _decision(decision_id="first", call_id="first", agent_harness=harness)
    natural_duplicate = first.model_copy(update={"decision_id": "natural-duplicate"})
    primary_duplicate = first.model_copy(
        update={"session_id": "never-inserted", "call_id": "primary-duplicate"}
    )
    filler = _large_decision_batch(row_budget - 4)
    # The last input in the first chunk loses its natural key. Its primary key
    # remains available for the first input in the next chunk.
    loser = existing.model_copy(update={"decision_id": "shared-id"})
    winner = _decision(
        decision_id="shared-id",
        session_id="winner-session",
        call_id="winner-call",
        agent_harness=harness,
    )
    candidates = [
        first,
        natural_duplicate,
        primary_duplicate,
        *filler,
        loser,
        winner,
        winner.model_copy(update={"decision_id": "winner-natural-duplicate"}),
        winner.model_copy(update={"session_id": "never-inserted"}),
        first,
    ]
    assert postgres_store.store_decisions(candidates) == [
        True,
        False,
        False,
        *([True] * len(filler)),
        False,
        True,
        False,
        False,
        False,
    ]
    retained = postgres_store.read_decisions("acme")
    assert {row.decision_id: row for row in retained} == {
        row.decision_id: row for row in [existing, first, *filler, winner]
    }
    assert {row.session_id for row in postgres_store.read_sessions("acme")} == {
        "session-1",
        "winner-session",
    }
    assert postgres_store.store_decisions(candidates) == [False] * len(candidates)


@pytest.mark.parametrize("reverse", [False, True])
def test_postgres_chunked_session_metadata_uses_all_inserted_utc_instants(
    postgres_store, reverse
) -> None:
    row_budget = 60_000 // len(developer_decisions.columns)
    local = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"))
    early = _decision(
        decision_id="early",
        call_id="early",
        user_id="first-user",
        occurred_at=local,
        captured_at=local,
    )
    equivalent = early.model_copy(
        update={
            "decision_id": "equivalent",
            "occurred_at": local.astimezone(UTC),
            "captured_at": local.astimezone(UTC),
        }
    )
    late = _decision(
        decision_id="late",
        call_id="late",
        user_id="second-user",
        occurred_at=local.replace(fold=1),
        captured_at=local.replace(fold=1),
    )
    candidates = [early, *([equivalent] * (row_budget - 1)), late]
    if reverse:
        candidates.reverse()
    inserted = postgres_store.store_decisions(candidates)
    assert sum(inserted) == 2
    [session] = postgres_store.read_sessions("acme")
    assert session.first_observed_at == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert session.last_observed_at == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert session.user_id is None
    assert session.user_id_conflict is True
    assert postgres_store.store_decision(
        early.model_copy(
            update={
                "decision_id": "later-call",
                "call_id": "later-call",
            }
        )
    )
    assert postgres_store.read_sessions("acme") == [session]


@pytest.mark.parametrize("failure", ["later_chunk", "session_upsert"])
def test_postgres_chunked_decision_failure_rolls_back_and_retries_once(
    postgres_engine, caplog, failure
) -> None:
    store = FactStore(postgres_engine)
    seed = _decision(decision_id="seed", session_id="a-existing", call_id="seed")
    assert store.store_decision(seed)
    before_sessions = store.read_sessions("acme")
    row_budget = 60_000 // len(developer_decisions.columns)
    candidates = [
        row.model_copy(
            update={
                "session_id": "a-existing",
                "user_id": "another-user",
                "captured_at": T0 + timedelta(hours=1),
                "raw": {"authorization": "Bearer secret-test-credential"},
            }
        )
        for row in _large_decision_batch(row_budget)
    ]
    candidates.append(
        _decision(
            decision_id="last",
            call_id="fail-chunk",
            session_id="z-failure",
        )
    )
    table, condition = (
        ("developer_decisions", "call_id <> 'fail-chunk'")
        if failure == "later_chunk"
        else ("sessions", "session_id <> 'z-failure'")
    )
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            f"ALTER TABLE {table} ADD CONSTRAINT reject_batch CHECK ({condition})"
        )
    completed_chunks = []

    def observe_success(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO developer_decisions"):
            completed_chunks.append(len(parameters) // len(developer_decisions.columns))

    event.listen(postgres_engine, "after_cursor_execute", observe_success)
    caplog.set_level("INFO", logger="sediment_core.store")
    caplog.clear()
    try:
        with pytest.raises(IntegrityError):
            store.store_decisions(candidates)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", observe_success)
        with postgres_engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE {table} DROP CONSTRAINT reject_batch"
            )
    assert completed_chunks == (
        [row_budget] if failure == "later_chunk" else [row_budget, 1]
    )
    assert store.read_decisions("acme") == [seed]
    assert store.read_sessions("acme") == before_sessions
    assert not any(record.name == "sediment_core.store" for record in caplog.records)
    assert store.store_decisions(candidates) == [True] * len(candidates)
    retained = store.read_decisions("acme")
    assert len(retained) == len(candidates) + 1
    assert all(
        row.raw == {"authorization": f"Bearer {REDACTION_MARKER}"}
        for row in retained
        if row.decision_id.startswith("batch-")
    )
    assert store.store_decisions(candidates) == [False] * len(candidates)
    assert store.read_decisions("acme") == retained


@pytest.mark.parametrize("fold", [None, 0, 1])
def test_postgres_decision_batch_matches_returned_row_to_its_input(
    postgres_engine,
    fold,
) -> None:
    store = FactStore(postgres_engine)
    existing = _decision(decision_id="decision-existing")
    loser = existing.model_copy(
        update={"decision_id": "decision-shared", "file_path": "other.py"}
    )
    winner_time = (
        T0 + timedelta(minutes=1)
        if fold is None
        else datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=fold)
    )
    winner = _decision(
        decision_id="decision-shared",
        session_id="session-winner",
        call_id="tool-call-winner",
        occurred_at=winner_time,
        captured_at=winner_time,
    )
    assert store.store_decision(existing) is True

    assert store.store_decisions([loser, winner]) == [False, True]
    assert {session.session_id for session in store.read_sessions("acme")} == {
        "session-1",
        "session-winner",
    }
    stored = {
        decision.decision_id: decision for decision in store.read_decisions("acme")
    }
    assert stored["decision-shared"] == winner.model_copy(
        update={
            "occurred_at": winner_time.astimezone(UTC),
            "captured_at": winner_time.astimezone(UTC),
        }
    )


@pytest.mark.parametrize("field", ["occurred_at", "captured_at"])
@pytest.mark.parametrize("fold", [0, 1])
@pytest.mark.parametrize("batch", [False, True])
def test_postgres_decision_receipt_matches_ambiguous_local_time(
    postgres_store,
    field,
    fold,
    batch,
) -> None:
    local_time = datetime(
        2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=fold
    )
    decision = _decision(**{field: local_time})

    if batch:
        assert postgres_store.store_decisions([decision]) == [True]
    else:
        assert postgres_store.store_decision(decision) is True

    [stored] = postgres_store.read_decisions("acme")
    assert stored == decision.model_copy(update={field: local_time.astimezone(UTC)})
    [session] = postgres_store.read_sessions("acme")
    assert session.session_id == decision.session_id
    assert session.user_id == decision.user_id
    assert session.first_observed_at == decision.captured_at.astimezone(UTC)
    assert session.last_observed_at == decision.captured_at.astimezone(UTC)


def test_postgres_decision_natural_redelivery_matches_same_instant(
    postgres_store,
) -> None:
    first_time = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"))
    capture_time = first_time.replace(fold=1)
    first = _decision(
        agent_harness=AgentHarness.CODEX,
        call_id=None,
        occurred_at=first_time,
        captured_at=capture_time,
    )
    duplicate = _decision(
        decision_id="redelivered",
        agent_harness=AgentHarness.CODEX,
        call_id=None,
        occurred_at=first_time.astimezone(UTC),
        captured_at=capture_time.astimezone(UTC) + timedelta(hours=1),
    )

    assert postgres_store.store_decisions([first, duplicate]) == [True, False]
    assert len(postgres_store.read_decisions("acme")) == 1
    [session] = postgres_store.read_sessions("acme")
    assert session.first_observed_at == capture_time.astimezone(UTC)
    assert session.last_observed_at == capture_time.astimezone(UTC)


@pytest.mark.parametrize("reverse", [False, True])
def test_postgres_decision_batch_preserves_both_fold_instants(
    postgres_store, reverse
) -> None:
    decisions = [
        _decision(
            decision_id=f"decision-fold-{fold}",
            agent_harness=AgentHarness.CODEX,
            call_id=None,
            occurred_at=datetime(
                2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=fold
            ),
            captured_at=datetime(
                2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=fold
            ),
        )
        for fold in (0, 1)
    ]

    assert postgres_store.store_decisions(
        list(reversed(decisions)) if reverse else decisions
    ) == [True, True]
    assert [d.occurred_at for d in postgres_store.read_decisions("acme")] == [
        datetime(2026, 10, 25, hour, 30, tzinfo=UTC) for hour in (0, 1)
    ]
    [session] = postgres_store.read_sessions("acme")
    assert session.first_observed_at == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert session.last_observed_at == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_postgres_decision_batch_rolls_back_facts_and_sessions(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    invalid = _decision(decision_id="decision-invalid").model_copy(
        update={"org_id": "   "}
    )

    with pytest.raises(IntegrityError):
        store.store_decisions([_decision(), invalid])

    assert store.count_facts("acme", FactTable.DEVELOPER_DECISIONS) == 0
    assert store.count_sessions("acme") == 0


def test_postgres_concurrent_duplicate_writers_store_one_fact(postgres_engine) -> None:
    barrier = Barrier(2)

    def write(fact_id: str) -> bool:
        barrier.wait()
        return FactStore(postgres_engine).store_inference_call(
            _inference_call(inference_call_id=fact_id)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ("inference-a", "inference-b")))

    assert sorted(results) == [False, True]
    assert (
        FactStore(postgres_engine).count_facts("acme", FactTable.INFERENCE_CALLS) == 1
    )


def test_postgres_quarantine_excludes_by_revision_and_audit_can_include(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    decision = _decision()
    store.store_decision(decision)

    assert store.quarantine_revision("acme") == 0
    store.quarantine_fact(
        "acme",
        FactTable.DEVELOPER_DECISIONS,
        decision.decision_id,
        reason="forged decision",
    )
    assert store.quarantine_revision("acme") == 1
    assert store.read_decisions("acme") == []
    assert store.read_decisions("acme", include_quarantined=True) == [decision]
    assert store.count_facts("acme", FactTable.DEVELOPER_DECISIONS) == 0
    assert (
        store.count_facts(
            "acme", FactTable.DEVELOPER_DECISIONS, include_quarantined=True
        )
        == 1
    )

    store.release_fact(
        "acme",
        FactTable.DEVELOPER_DECISIONS,
        decision.decision_id,
        reason="verified delivery",
    )
    assert store.quarantine_revision("acme") == 2
    assert store.read_decisions("acme") == [decision]
    assert [record.action for record in store.read_quarantine_log("acme")] == [
        QuarantineAction.QUARANTINE,
        QuarantineAction.RELEASE,
    ]


def test_postgres_bulk_quarantine_filters_and_dry_run_are_atomic(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    matching = _inference_call(
        inference_call_id="inference-match",
        model_call_id="model-match",
        session_id="session-target",
        observed_at=T0,
    )
    other_session = _inference_call(
        inference_call_id="inference-other",
        model_call_id="model-other",
        session_id="session-other",
        observed_at=T0,
    )
    outside_window = _inference_call(
        inference_call_id="inference-late",
        model_call_id="model-late",
        session_id="session-target",
        observed_at=T0 + timedelta(days=2),
    )
    for call in (outside_window, other_session, matching):
        store.store_inference_call(call)

    filters = {
        "captured_between": (T0 - timedelta(hours=1), T0 + timedelta(hours=1)),
        "session_id": "session-target",
        "provider": matching.gateway_provider,
        "reason": "incident-559",
    }
    assert store.quarantine_inference_calls_where("acme", dry_run=True, **filters) == 1
    assert store.read_quarantine_log("acme") == []
    assert store.quarantine_inference_calls_where("acme", **filters) == 1
    assert store.read_inference_calls("acme") == [other_session, outside_window]

    try:
        store.quarantine_inference_calls_where(
            "acme",
            captured_between=(datetime(2026, 8, 22), T0),
            reason="invalid window",
        )
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("naive time bound was accepted")


def test_postgres_reconciliation_read_projects_and_bounds_one_session(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    target = _inference_call(
        inference_call_id="inference-target",
        model_call_id="model-target",
        session_id="session-target",
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[ToolCallPart(id="tool-target", name="Edit")],
            )
        ],
    )
    store.store_inference_call(target)

    [row] = store.read_inference_call_reconciliation("acme", "session-target", limit=1)

    assert row.inference_call_id == "inference-target"
    assert row.model_call_id == "model-target"
    assert row.model == target.model
    [join_row] = store.read_compatibility_inference_evidence(
        "acme", "session-target", limit=1
    )
    assert join_row.tool_call_ids == ("tool-target",)
    with pytest.raises(ValueError, match="exceeds the reconciliation limit"):
        store.read_inference_call_reconciliation("acme", "session-target", limit=0)
    with pytest.raises(ValueError, match="exceeds the compatibility limit"):
        store.read_compatibility_inference_evidence("acme", "session-target", limit=0)


def test_postgres_compatibility_evidence_projects_join_fields_for_one_session(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    store.store_decision(_decision(session_id="session-target", call_id="tool-1"))
    store.store_decision(
        _decision(
            decision_id="decision-other",
            session_id="session-other",
            call_id="tool-other",
        )
    )
    store.store_edit_observation(
        _observation(session_id="session-target", call_id="tool-1")
    )

    decisions, observations = store.read_compatibility_evidence(
        "acme", "session-target", limit=1
    )

    assert len(decisions) == 1
    assert decisions[0].call_id == "tool-1"
    assert decisions[0].accepted is True
    assert decisions[0].explicit is True
    assert len(observations) == 1
    assert observations[0].call_id == "tool-1"
    with pytest.raises(ValueError, match="exceeds the compatibility limit"):
        store.read_compatibility_evidence("acme", "session-target", limit=0)


def test_postgres_bulk_quarantine_rolls_back_on_database_failure(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    for fact_id in ("inference-a", "inference-b"):
        store.store_inference_call(
            _inference_call(inference_call_id=fact_id, model_call_id=fact_id)
        )
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION fail_second_quarantine() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "IF NEW.fact_id = 'inference-b' THEN "
                "RAISE EXCEPTION 'forced quarantine failure'; END IF; "
                "RETURN NEW; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER fail_second_quarantine BEFORE INSERT "
                "ON fact_quarantine FOR EACH ROW "
                "EXECUTE FUNCTION fail_second_quarantine()"
            )
        )
    try:
        with pytest.raises(DBAPIError, match="forced quarantine failure"):
            store.quarantine_inference_calls_where("acme", reason="incident")
        assert store.read_quarantine_log("acme") == []
        assert len(store.read_inference_calls("acme")) == 2
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER fail_second_quarantine ON fact_quarantine")
            )
            connection.execute(text("DROP FUNCTION fail_second_quarantine()"))


def test_postgres_content_fact_writes_apply_basic_redaction(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    credential = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    store.store_decision(_decision(raw={"authorization": f"Bearer {credential}"}))
    store.store_edit_observation(
        _observation(
            applied_text=f"Authorization: Bearer {credential}",
            raw={"token": credential},
        )
    )
    store.store_rejected_edit(
        _rejected_edit(
            proposed=f"Authorization: Bearer {credential}",
            raw={"token": credential},
        )
    )
    store.store_retry_linkage(_retry_linkage(raw={"token": credential}))
    store.store_ci_outcome(_ci_outcome(raw={"token": credential}))

    serialized = repr(
        (
            store.read_decisions("acme")[0].model_dump(),
            store.read_edit_observations("acme")[0].model_dump(),
            store.read_rejected_edits("acme")[0].model_dump(),
            store.read_retry_linkages("acme")[0].model_dump(),
            store.read_ci_outcomes("acme")[0].model_dump(),
        )
    )
    assert credential not in serialized
    assert REDACTION_MARKER in serialized


def test_postgres_concurrent_quarantine_release_visibility_follows_revision(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    call = _inference_call()
    store.store_inference_call(call)
    barrier = Barrier(2)

    def append(action: QuarantineAction) -> None:
        barrier.wait()
        worker = FactStore(postgres_engine)
        if action is QuarantineAction.QUARANTINE:
            worker.quarantine_fact(
                "acme", FactTable.INFERENCE_CALLS, call.inference_call_id, reason="q"
            )
        else:
            worker.release_fact(
                "acme", FactTable.INFERENCE_CALLS, call.inference_call_id, reason="r"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                append, (QuarantineAction.QUARANTINE, QuarantineAction.RELEASE)
            )
        )

    log = store.read_quarantine_log("acme")
    assert store.quarantine_revision("acme") == 2
    expected_visible = log[-1].action is QuarantineAction.RELEASE
    assert bool(store.read_inference_calls("acme")) is expected_visible


def test_postgres_read_snapshot_is_read_only_repeatable_and_reentrant(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    first = _inference_call(inference_call_id="inference-first", model_call_id="first")
    second = _inference_call(
        inference_call_id="inference-second", model_call_id="second"
    )
    store.store_inference_call(first)
    checked_out_before = postgres_engine.pool.checkedout()

    with store.read_snapshot() as outer:
        assert postgres_engine.pool.checkedout() == checked_out_before + 1
        assert outer.read_inference_calls("acme") == [first]
        assert outer.quarantine_revision("acme") == 0
        with outer.read_snapshot() as inner:
            assert inner is outer
            store.store_inference_call(second)
            store.quarantine_fact(
                "acme",
                FactTable.INFERENCE_CALLS,
                first.inference_call_id,
                reason="concurrent incident",
            )
            assert inner.read_inference_calls("acme") == [first]
            assert inner.quarantine_revision("acme") == 0
        with pytest.raises(DBAPIError):
            outer._connection.execute(text("INSERT INTO sessions DEFAULT VALUES"))

    assert postgres_engine.pool.checkedout() == checked_out_before
    assert store.read_inference_calls("acme") == [second]
    assert store.quarantine_revision("acme") == 1


def test_postgres_read_snapshot_releases_connection_after_caller_failure(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    checked_out_before = postgres_engine.pool.checkedout()

    with pytest.raises(RuntimeError, match="caller failed"):
        with store.read_snapshot():
            raise RuntimeError("caller failed")

    assert postgres_engine.pool.checkedout() == checked_out_before


def test_postgres_snapshot_projections_do_not_hydrate_omitted_text(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    call = _inference_call()
    store.store_inference_call(call)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE inference_calls SET raw = '{broken', "
                "input_messages = '{also broken'"
            )
        )

    with store.read_snapshot() as snapshot:
        [summary] = snapshot.read_inference_call_summaries("acme")
        [candidate] = snapshot.read_attribution_candidates(
            "acme", observed_between=(T0 - timedelta(minutes=1), T0)
        )

    assert summary.inference_call_id == call.inference_call_id
    assert summary.model == call.model
    assert not hasattr(summary, "raw")
    assert not hasattr(summary, "input_messages")
    assert candidate.output_messages == call.output_messages
    assert not hasattr(candidate, "raw")
    assert not hasattr(candidate, "input_messages")


def test_postgres_attribution_candidates_reject_limit_overflow(
    postgres_store,
) -> None:
    postgres_store.store_inference_call(_inference_call(inference_call_id="first"))
    postgres_store.store_inference_call(
        _inference_call(inference_call_id="second", model_call_id="model-second")
    )

    with postgres_store.read_snapshot() as snapshot:
        with pytest.raises(ValueError, match="Attribution candidate cohort exceeds 1"):
            snapshot.read_attribution_candidates(
                "acme",
                observed_between=(T0 - timedelta(minutes=1), T0),
                limit=1,
            )


def test_postgres_inference_summaries_enforce_cohort_bounds_and_limit(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    for call_id, observed_at in (
        ("before", T0 - timedelta(days=2)),
        ("inside-a", T0),
        ("inside-b", T0 + timedelta(hours=1)),
        ("after", T0 + timedelta(days=2)),
    ):
        store.store_inference_call(
            _inference_call(
                inference_call_id=call_id,
                model_call_id=call_id,
                observed_at=observed_at,
            )
        )

    with store.read_snapshot() as snapshot:
        rows = snapshot.read_inference_call_summaries(
            "acme",
            observed_between=(T0, T0 + timedelta(days=1)),
            limit=2,
        )
        with pytest.raises(ValueError, match="Inference call cohort exceeds 1"):
            snapshot.read_inference_call_summaries(
                "acme",
                observed_between=(T0, T0 + timedelta(days=1)),
                limit=1,
            )

    assert [row.inference_call_id for row in rows] == ["inside-a", "inside-b"]


def test_postgres_snapshot_exact_id_read_hydrates_only_requested_facts(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    first = _inference_call(inference_call_id="inference-a", model_call_id="a")
    second = _inference_call(inference_call_id="inference-b", model_call_id="b")
    store.store_inference_call(second)
    store.store_inference_call(first)

    with store.read_snapshot() as snapshot:
        assert snapshot.read_inference_calls_by_ids(
            "acme", {first.inference_call_id}
        ) == [first]
        assert snapshot.read_inference_calls_by_ids("acme", set()) == []


def test_postgres_snapshot_derivation_projections_omit_unused_raw(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    call = _inference_call()
    decision = _decision()
    observation = _observation()
    outcome = _ci_outcome()
    store.store_inference_call(call)
    store.store_decision(decision)
    store.store_edit_observation(observation)
    store.store_ci_outcome(outcome)
    with postgres_engine.begin() as connection:
        for table in (
            "inference_calls",
            "developer_decisions",
            "edit_observations",
            "ci_outcomes",
        ):
            connection.execute(text(f"UPDATE {table} SET raw = '{{broken'"))

    with store.read_snapshot() as snapshot:
        [call_summary] = snapshot.read_inference_call_summaries("acme")
        [rollout_call] = snapshot.read_rollout_inference_calls("acme")
        [projected_decision] = snapshot.read_decision_projections("acme")
        [projected_observation] = snapshot.read_edit_observation_projections("acme")
        [projected_outcome] = snapshot.read_ci_outcome_projections("acme")

    assert call_summary.gateway_provider is GatewayProvider.LITELLM
    assert rollout_call.input_messages == call.input_messages
    assert rollout_call.output_messages == call.output_messages
    assert rollout_call.gateway_provider is GatewayProvider.LITELLM
    assert projected_decision.decision_id == decision.decision_id
    assert projected_decision.agent_harness is AgentHarness.CLAUDE_CODE
    assert projected_decision.interaction_mode is InteractionMode.AGENT
    assert projected_observation.applied_text == observation.applied_text
    assert projected_observation.agent_harness is AgentHarness.CODEX
    assert projected_outcome.outcome_id == outcome.outcome_id
    assert projected_outcome.provider is CIProvider.GITHUB_ACTIONS
    assert projected_outcome.result is CIResult.ERROR
    assert all(
        not hasattr(row, "raw")
        for row in (
            rollout_call,
            projected_decision,
            projected_observation,
            projected_outcome,
        )
    )


def test_postgres_snapshot_exposes_complete_fact_reads(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    decision = _decision()
    observation = _observation()
    rejected = _rejected_edit()
    retry_linkage = _retry_linkage()
    outcome = _ci_outcome()
    push = _push()
    merge = _pull_request_merge()
    revision = _pull_request_revision()
    for write, fact in (
        (store.store_decision, decision),
        (store.store_edit_observation, observation),
        (store.store_rejected_edit, rejected),
        (store.store_retry_linkage, retry_linkage),
        (store.store_ci_outcome, outcome),
        (store.store_push, push),
        (store.store_pull_request_merge, merge),
        (store.store_pull_request_revision, revision),
    ):
        assert write(fact) is True

    with store.read_snapshot() as snapshot:
        assert snapshot.read_decisions("acme") == [decision]
        assert snapshot.read_edit_observations("acme") == [observation]
        assert snapshot.read_rejected_edits("acme") == [rejected]
        assert snapshot.read_retry_linkages("acme") == [retry_linkage]
        [stored_outcome] = snapshot.read_ci_outcomes("acme")
        assert stored_outcome.model_dump(exclude={"raw"}) == outcome.model_dump(
            exclude={"raw"}
        )
        assert math.isnan(stored_outcome.raw["nan"])
        assert snapshot.read_pushes("acme") == [push]
        assert snapshot.read_pull_request_merges("acme") == [merge]
        assert snapshot.read_pull_request_revisions("acme") == [revision]


def test_postgres_null_dedup_keys_follow_each_fact_types_contract(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    keyless = _inference_call(model_call_id=None)
    replay = keyless.model_copy(update={"inference_call_id": "inference-2"})

    # The inference dedup index is partial (model_call_id IS NOT NULL), so a
    # keyless call is never a redelivery of another keyless call.
    assert store.store_inference_call(keyless) is True
    assert store.store_inference_call(replay) is True
    assert store.read_inference_calls("acme") == [keyless, replay]

    # Decisions coalesce a null call_id into the dedup key, so an otherwise
    # identical keyless redelivery with its own decision_id still collapses.
    assert store.store_decision(_decision(call_id=None)) is True
    assert (
        store.store_decision(_decision(decision_id="decision-2", call_id=None)) is False
    )


def test_postgres_reads_sessions_and_counts_are_org_scoped(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    assert store.store_inference_call(_inference_call()) is True
    assert (
        store.store_inference_call(
            _inference_call(
                inference_call_id="inference-other",
                org_id="other",
                model_call_id="model-call-2",
            )
        )
        is True
    )
    assert store.store_rejected_edit(_rejected_edit()) is True
    assert (
        store.store_rejected_edit(
            _rejected_edit(rejection_id="rejection-other", org_id="other")
        )
        is True
    )

    assert store.read_inference_calls("other-org") == []
    assert [call.org_id for call in store.read_inference_calls("acme")] == ["acme"]
    assert [call.org_id for call in store.read_inference_calls("other")] == ["other"]
    assert len(store.read_rejected_edits("acme")) == 1
    assert len(store.read_rejected_edits("other")) == 1
    assert store.count_sessions("acme") == 2
    assert store.count_sessions("other") == 2
    assert {session.org_id for session in store.read_sessions("acme")} == {"acme"}


def test_postgres_push_reads_bound_capture_window_and_cardinality(
    postgres_store,
) -> None:
    before = _push(push_id="before", captured_at=T0 - timedelta(seconds=1))
    inside = _push(
        push_id="inside",
        ref="refs/heads/inside",
        after_sha="b" * 40,
        captured_at=T0,
    )
    upper = _push(
        push_id="upper",
        ref="refs/heads/upper",
        after_sha="c" * 40,
        captured_at=T0 + timedelta(hours=1),
    )
    for push in (upper, before, inside):
        postgres_store.store_push(push)

    assert postgres_store.read_pushes(
        "acme",
        captured_between=(T0, T0 + timedelta(hours=1)),
        limit=1,
    ) == [inside]
    with pytest.raises(ValueError, match="exceeds 1"):
        postgres_store.read_pushes("acme", limit=1)


def test_postgres_push_gc_iteration_is_keyset_bounded_and_quarantine_aware(
    postgres_engine,
) -> None:
    store = FactStore(postgres_engine)
    pushes = [
        _push(
            push_id=f"push-{index}",
            ref=f"refs/heads/branch-{index}",
            after_sha=f"{index + 1:x}" * 40,
            captured_at=T0 + timedelta(minutes=index),
        )
        for index in range(5)
    ]
    for push in reversed(pushes):
        store.store_push(push)
    store.quarantine_fact(
        "acme", FactTable.PUSHES, pushes[2].push_id, reason="exclude from GC"
    )

    with store.read_snapshot() as snapshot:
        rows = list(snapshot.iter_push_gc_rows("acme", batch_size=2))

    assert [(row.repo, row.captured_at) for row in rows] == [
        (push.repo, push.captured_at) for push in pushes if push is not pushes[2]
    ]
    assert all(
        not hasattr(row, "raw") and not hasattr(row, "clone_url") for row in rows
    )
    with store.read_snapshot() as snapshot:
        with pytest.raises(ValueError, match="batch_size"):
            list(snapshot.iter_push_gc_rows("acme", batch_size=0))


def _behind_engine(postgres_database_factory):
    """Create a database migrated to 0008 (one revision below head) and engine.

    The live FactStore writers encode ``_SerializedText`` values on bind; the
    0009 migration re-encodes existing rows. A pre-0009 database is the
    double-encoding window, so tests of the writer-side AT_HEAD gate build
    their store against this behind schema.
    """
    from alembic import command

    from sediment_core.postgres_migrations import (
        RevisionState,
        _alembic_config,
        inspect_revision,
    )

    url = postgres_database_factory(migrated=False)
    engine = create_engine(url)
    with engine.connect() as connection:
        config = _alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0008_session_commit_observations")
    assert inspect_revision(url).state is RevisionState.BEHIND
    return engine, url


@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("quarantine_fact", QuarantineAction.QUARANTINE),
        ("release_fact", QuarantineAction.RELEASE),
    ],
)
def test_postgres_quarantine_writers_refuse_pre_0009_database(
    postgres_database_factory, method: str, action: QuarantineAction
) -> None:
    """The single-fact quarantine verbs refuse a behind database.

    Regression for the CLI one-shot write path: ``_append_quarantine`` writes
    ``fact_quarantine.reason`` through ``_SerializedText`` before the 0009
    migration re-encodes every existing row, so a pre-upgrade write would be
    double-encoded. The writer-side AT_HEAD gate refuses the write instead
    of letting the migration corrupt the audit value.
    """
    engine, _url = _behind_engine(postgres_database_factory)
    try:
        store = FactStore(engine)
        with pytest.raises(DatabaseOperationError) as exc:
            getattr(store, method)(
                "acme",
                FactTable.INFERENCE_CALLS,
                "call-1",
                reason="review",
            )
        assert action.value in str(exc.value)
        assert "run `sediment db upgrade`" in str(exc.value)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM fact_quarantine"
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_postgres_bulk_quarantine_apply_refuses_pre_0009_database(
    postgres_database_factory,
) -> None:
    """The bulk quarantine ``--apply`` verb refuses a behind database."""
    engine, _url = _behind_engine(postgres_database_factory)
    try:
        store = FactStore(engine)
        store.store_inference_call(_inference_call(inference_call_id="call-1"))
        with pytest.raises(DatabaseOperationError, match="quarantine-inference-calls"):
            store.quarantine_inference_calls_where("acme", reason="incident")
        with pytest.raises(DatabaseOperationError, match="quarantine-inference-calls"):
            store.quarantine_inference_calls_where(
                "acme",
                session_id="session-inference",
                reason="incident",
            )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM fact_quarantine"
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_postgres_bulk_quarantine_dry_run_still_works_against_pre_0009_database(
    postgres_database_factory,
) -> None:
    """The bulk quarantine dry-run is a read and stays usable pre-upgrade.

    A staged rollout may preview how many facts would be quarantined before
    running ``sediment db upgrade``. The gate scopes to the write path; the
    dry-run branch only reads ``inference_calls`` and writes nothing, so it
    keeps working against a behind database.
    """
    engine, _url = _behind_engine(postgres_database_factory)
    try:
        store = FactStore(engine)
        store.store_inference_call(_inference_call(inference_call_id="call-1"))
        assert (
            store.quarantine_inference_calls_where(
                "acme",
                session_id="session-inference",
                reason="incident",
                dry_run=True,
            )
            == 1
        )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM fact_quarantine"
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_postgres_quarantine_pre_0009_write_blocked_prevents_double_encoding(
    postgres_database_factory,
) -> None:
    """End-to-end regression for the double-encoding corruption.

    Before the writer-side AT_HEAD gate, a pre-upgrade ``quarantine`` write
    encoded ``reason`` once through ``_SerializedText`` and the 0009
    migration encoded it again, so ``read_quarantine_log`` returned the
    literal value with extra quote characters. With the gate, the
    pre-upgrade write is refused, nothing is left for the migration to
    re-encode, and the post-upgrade write lands exactly once.
    """
    from alembic import command

    from sediment_core.postgres_migrations import (
        RevisionState,
        _alembic_config,
        inspect_revision,
        upgrade_database,
    )

    url = postgres_database_factory(migrated=False)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            config = _alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0008_session_commit_observations")
        assert inspect_revision(url).state is RevisionState.BEHIND
        store = FactStore(engine)

        # The pre-upgrade write is refused; no audit row is persisted.
        with pytest.raises(DatabaseOperationError, match="run `sediment db upgrade`"):
            store.quarantine_fact(
                "acme",
                FactTable.INFERENCE_CALLS,
                "call-1",
                reason="review",
            )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM fact_quarantine"
                ).scalar_one()
                == 0
            )

        upgrade_database(url)
        assert inspect_revision(url).state is RevisionState.AT_HEAD

        # The post-upgrade write lands exactly once and reads back unchanged.
        store.quarantine_fact(
            "acme",
            FactTable.INFERENCE_CALLS,
            "call-1",
            reason="review",
        )
        records = store.read_quarantine_log("acme")
        assert len(records) == 1
        assert records[0].reason == "review"
        with engine.connect() as connection:
            raw = connection.execute(
                text("SELECT reason FROM fact_quarantine")
            ).scalar_one()
        # The physical bytes are the single-encoded form, not double-encoded.
        assert raw == '"review"'
        assert raw != '"\\"review\\""'
    finally:
        engine.dispose()


def test_postgres_quarantine_writers_succeed_against_head_database(
    postgres_database_factory,
) -> None:
    """The AT_HEAD gate never blocks a fully upgraded database.

    A fresh database migrated to head accepts every quarantine write verb;
    this guards against the gate over-firing on a healthy deployment.
    """
    url = postgres_database_factory(migrated=True)
    engine = create_engine(url)
    try:
        store = FactStore(engine)
        store.store_inference_call(_inference_call(inference_call_id="call-1"))
        store.quarantine_fact(
            "acme",
            FactTable.INFERENCE_CALLS,
            "call-1",
            reason="review",
        )
        store.release_fact(
            "acme",
            FactTable.INFERENCE_CALLS,
            "call-1",
            reason="verified",
        )
        assert (
            store.quarantine_inference_calls_where(
                "acme", session_id="session-inference", reason="incident"
            )
            == 1
        )
        records = store.read_quarantine_log("acme")
        assert [record.reason for record in records] == [
            "review",
            "verified",
            "incident",
        ]
    finally:
        engine.dispose()
