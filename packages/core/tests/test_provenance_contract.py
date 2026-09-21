# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical Session and Edit observation Provenance contract."""

from __future__ import annotations

import itertools
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    InteractionMode,
    RejectedEdit,
    RetryLinkage,
    TextPart,
)
from sediment_core.store import FactStore

OBSERVED = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def test_developer_side_fact_models_use_canonical_provenance_names() -> None:
    decision = DeveloperDecision(
        org_id="acme",
        session_id="session-1",
        user_id=None,
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id="tool-1",
        edit_retention_score=0.75,
        observation_delay_ms=5_000,
        occurred_at=OBSERVED,
        captured_at=OBSERVED,
    )
    observation = EditObservation(
        org_id="acme",
        session_id="session-1",
        user_id=None,
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app.py",
        call_id="tool-1",
        applied_text="before",
        observed_file_text="after",
        occurred_at=OBSERVED,
        captured_at=OBSERVED,
    )
    rejection = RejectedEdit(
        org_id="acme",
        session_id="session-1",
        user_id=None,
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app.py",
        call_id="tool-2",
        proposed="candidate",
        occurred_at=OBSERVED,
        captured_at=OBSERVED,
    )

    assert decision.edit_retention_score == 0.75
    assert decision.observation_delay_ms == 5_000
    assert observation.applied_text == "before"
    assert observation.observed_file_text == "after"
    assert rejection.user_id is None
    assert "source" not in DeveloperDecision.model_fields
    assert "surface" not in DeveloperDecision.model_fields
    assert "original" not in EditObservation.model_fields
    assert "final" not in EditObservation.model_fields


def test_interaction_mode_is_a_closed_two_value_vocabulary() -> None:
    assert {mode.value for mode in InteractionMode} == {"agent", "inline"}


def _inference_call(
    *, user_id: str | None, observed_at: datetime, model_call_id: str
) -> InferenceCall:
    return InferenceCall(
        org_id="acme",
        session_id="session-1",
        user_id=user_id,
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="change app.py")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="done")])
        ],
        model_call_id=model_call_id,
        observed_at=observed_at,
    )


def _session(store: FactStore) -> dict[str, object]:
    [session] = store.read_sessions("acme")
    return asdict(session)


def test_fact_store_has_no_precanonical_migration_readers() -> None:
    assert not hasattr(FactStore, "_migrate_sessions")
    assert not hasattr(FactStore, "_migrate_developer_facts")


@pytest.mark.parametrize(
    ("identities", "expected_user_id", "expected_conflict"),
    [
        ([None, None], None, 0),
        ([None, "dev-1"], "dev-1", 0),
        (["dev-1", None], "dev-1", 0),
        (["dev-1", "dev-1"], "dev-1", 0),
        (["dev-1", "dev-2"], None, 1),
        (["dev-1", "dev-2", "dev-1"], None, 1),
    ],
)
def test_session_user_identity_merge_is_deterministic_and_sticky(
    postgres_store,
    identities: list[str | None],
    expected_user_id: str | None,
    expected_conflict: bool,
) -> None:
    store = postgres_store
    for index, user_id in enumerate(identities):
        store.store_inference_call(
            _inference_call(
                user_id=user_id,
                observed_at=OBSERVED + timedelta(minutes=index),
                model_call_id=f"model-{index}",
            )
        )

    session = _session(store)
    assert session["user_id"] == expected_user_id
    assert session["user_id_conflict"] == expected_conflict


def _session_facts() -> list[tuple[str, object]]:
    return [
        (
            "inference",
            _inference_call(
                user_id=None,
                observed_at=OBSERVED + timedelta(minutes=10),
                model_call_id="model-1",
            ),
        ),
        (
            "decision",
            DeveloperDecision(
                org_id="acme",
                session_id="session-1",
                user_id="dev-1",
                agent_harness=AgentHarness.CLAUDE_CODE,
                file_path="app.py",
                accepted=True,
                explicit=True,
                interaction_mode=InteractionMode.AGENT,
                call_id="tool-1",
                occurred_at=OBSERVED - timedelta(days=10),
                captured_at=OBSERVED + timedelta(minutes=20),
            ),
        ),
        (
            "observation",
            EditObservation(
                org_id="acme",
                session_id="session-1",
                user_id="dev-2",
                agent_harness=AgentHarness.CLAUDE_CODE,
                file_path="app.py",
                call_id="tool-2",
                applied_text="before",
                observed_file_text="after",
                occurred_at=OBSERVED + timedelta(days=10),
                captured_at=OBSERVED,
            ),
        ),
        (
            "rejection",
            RejectedEdit(
                org_id="acme",
                session_id="session-1",
                user_id=None,
                agent_harness=AgentHarness.CLAUDE_CODE,
                file_path="app.py",
                call_id="tool-3",
                proposed="candidate",
                occurred_at=OBSERVED - timedelta(days=20),
                captured_at=OBSERVED + timedelta(minutes=30),
            ),
        ),
    ]


def test_session_metadata_is_identical_for_every_fact_arrival_order(
    postgres_store_factory,
) -> None:
    observed_rows: list[dict[str, object]] = []
    for ordered_facts in itertools.permutations(_session_facts()):
        _, store = postgres_store_factory()
        for fact_type, fact in ordered_facts:
            if fact_type == "inference":
                store.store_inference_call(fact)
            elif fact_type == "decision":
                store.store_decision(fact)
            elif fact_type == "observation":
                store.store_edit_observation(fact)
            else:
                store.store_rejected_edit(fact)
        observed_rows.append(_session(store))

    assert all(row == observed_rows[0] for row in observed_rows)
    assert observed_rows[0] == {
        "org_id": "acme",
        "session_id": "session-1",
        "user_id": None,
        "user_id_conflict": True,
        "first_observed_at": OBSERVED,
        "last_observed_at": OBSERVED + timedelta(minutes=30),
    }


def test_retry_linkage_session_metadata_is_arrival_order_independent(
    postgres_store_factory,
) -> None:
    inference = _inference_call(
        user_id=None,
        observed_at=OBSERVED + timedelta(minutes=10),
        model_call_id="model-retry",
    )
    linkage = RetryLinkage(
        org_id="acme",
        session_id="session-1",
        user_id="dev-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app.py",
        tool_name="Edit",
        rejected_call_id="tool-rejected",
        accepted_call_id="tool-accepted",
        occurred_at=OBSERVED - timedelta(days=1),
        captured_at=OBSERVED + timedelta(minutes=20),
    )
    rows = []
    for retry_first in (False, True):
        _, store = postgres_store_factory()
        if retry_first:
            store.store_retry_linkage(linkage)
            store.store_inference_call(inference)
        else:
            store.store_inference_call(inference)
            store.store_retry_linkage(linkage)
        rows.append(_session(store))

    assert rows[0] == rows[1]
    assert rows[0]["user_id"] == "dev-1"
    assert rows[0]["first_observed_at"] == inference.observed_at
    assert rows[0]["last_observed_at"] == linkage.captured_at
