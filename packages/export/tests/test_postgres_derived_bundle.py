# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sediment_core import (
    AgentHarness,
    CIOutcome,
    CIProvider,
    CIResult,
    DeveloperDecision,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    InteractionMode,
    TextPart,
)
from sediment_core import FactStore
from sediment_derive import MirrorManager, derive_recovery_result
from sediment_export import build_derived_bundle

T0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _call() -> InferenceCall:
    return InferenceCall(
        inference_call_id="inference-1",
        org_id="acme",
        session_id="session-1",
        user_id="developer-1",
        gateway_provider=GatewayProvider.LITELLM,
        model_provider="anthropic",
        model="claude-sonnet",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="Fix storage")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="Done")])
        ],
        model_call_id="model-call-1",
        observed_at=T0,
        raw={"large-provider-field": "x" * 100_000},
    )


def _decision() -> DeveloperDecision:
    return DeveloperDecision(
        decision_id="decision-1",
        org_id="acme",
        session_id="session-1",
        user_id="developer-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="src/store.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id="model-call-1",
        occurred_at=T0 + timedelta(seconds=1),
        captured_at=T0 + timedelta(seconds=2),
        raw={"decision-audit": "kept"},
    )


def test_postgres_bundle_is_deterministic_and_exact_after_shuffled_ingest(
    postgres_database_factory,
    tmp_path,
) -> None:
    populations = []
    for index, facts in enumerate(((_call(), _decision()), (_decision(), _call()))):
        engine = create_engine(postgres_database_factory())
        try:
            store = FactStore(engine)
            for fact in facts:
                if isinstance(fact, InferenceCall):
                    store.store_inference_call(fact)
                else:
                    store.store_decision(fact)
            mirrors = MirrorManager(tmp_path / f"mirrors-{index}")
            first = build_derived_bundle(store, mirrors, "acme")
            repeated = build_derived_bundle(store, mirrors, "acme")
            assert repeated == first
            populations.append(first)
        finally:
            engine.dispose()

    assert populations[0] == populations[1]
    [stored_call] = populations[0].inference_calls
    [rollout] = populations[0].rollouts
    [turn] = rollout.segments[0]
    [stored_decision] = turn.decisions
    assert stored_call.raw == _call().raw
    assert stored_decision.raw == _decision().raw


def test_postgres_recovery_accepts_projected_ci_enums(
    postgres_database_factory,
    tmp_path,
) -> None:
    engine = create_engine(postgres_database_factory())
    try:
        store = FactStore(engine)
        store.store_ci_outcome(
            CIOutcome(
                org_id="acme",
                provider=CIProvider.GITHUB_ACTIONS,
                run_id="run-1",
                repo="acme/project",
                commit_sha="a" * 40,
                branch="main",
                result=CIResult.FAILED,
                workflow_name="tests",
                captured_at=T0,
            )
        )

        result = derive_recovery_result(
            store,
            MirrorManager(tmp_path / "mirrors-recovery"),
            "acme",
        )

        assert result.pairs == []
    finally:
        engine.dispose()
