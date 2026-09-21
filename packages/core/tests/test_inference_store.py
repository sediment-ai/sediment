# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import itertools
import json
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text

from sediment_core import GatewayProvider, InferenceCall, InferenceMessage, TextPart
from sediment_core import FactStore
from sediment_core.redaction import REDACTION_MARKER

T0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _call(**overrides) -> InferenceCall:
    values = {
        "inference_call_id": "inference-1",
        "org_id": "acme",
        "session_id": "session-1",
        "user_id": "developer-1",
        "gateway_provider": GatewayProvider.LITELLM,
        "model_provider": "anthropic",
        "model": "claude-sonnet",
        "input_messages": [
            InferenceMessage(
                role="user",
                parts=[TextPart(content="Update the storage seam")],
            )
        ],
        "output_messages": [
            InferenceMessage(
                role="assistant",
                parts=[TextPart(content="I will update it.")],
            )
        ],
        "input_tokens": 12,
        "output_tokens": 8,
        "duration_ms": 40,
        "model_call_id": "model-call-1",
        "observed_at": T0,
        "raw": {"provider": {"request_id": "model-call-1"}},
    }
    values.update(overrides)
    return InferenceCall(**values)


def test_postgres_inference_call_round_trip_and_database_dedup(postgres_engine) -> None:
    store = FactStore(postgres_engine)
    original = _call()
    redelivery = original.model_copy(update={"inference_call_id": "inference-2"})

    assert store.store_inference_call(original) is True
    assert store.store_inference_call(redelivery) is False
    assert store.read_inference_calls("acme") == [original]

    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM inference_calls")
            ).scalar_one()
            == 1
        )
        session = (
            connection.execute(
                text(
                    "SELECT session_id, user_id, user_id_conflict, "
                    "first_observed_at, last_observed_at FROM sessions"
                )
            )
            .mappings()
            .one()
        )
    assert dict(session) == {
        "session_id": "session-1",
        "user_id": "developer-1",
        "user_id_conflict": False,
        "first_observed_at": T0,
        "last_observed_at": T0,
    }


def test_postgres_inference_call_redacts_before_persistence(postgres_engine) -> None:
    credential = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    original = _call(
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[TextPart(content=f"Authorization: Bearer {credential}")],
            )
        ],
        raw={"authorization": f"Bearer {credential}"},
    )
    store = FactStore(postgres_engine)

    assert store.store_inference_call(original) is True
    [stored] = store.read_inference_calls("acme")
    assert stored.output_messages[0].parts[0].content == (
        f"Authorization: Bearer {REDACTION_MARKER}"
    )
    assert stored.raw == {"authorization": f"Bearer {REDACTION_MARKER}"}
    assert original.raw == {"authorization": f"Bearer {credential}"}

    with postgres_engine.connect() as connection:
        output_messages, raw = connection.execute(
            text("SELECT output_messages, raw FROM inference_calls")
        ).one()
    assert credential not in output_messages
    assert credential not in raw


def test_postgres_serialized_text_preserves_jsonb_incompatible_values(
    postgres_engine,
) -> None:
    difficult_text = "lone=\ud800 null=\x00"
    huge_number = 10**100
    original = _call(
        input_messages=[
            InferenceMessage(
                role="user",
                parts=[TextPart(content=difficult_text)],
            )
        ],
        raw={
            "lone_surrogate": "\ud800",
            "null_character": "\x00",
            "not_a_number": float("nan"),
            "huge_number": huge_number,
        },
    )
    store = FactStore(postgres_engine)

    assert store.store_inference_call(original) is True
    [stored] = store.read_inference_calls("acme")
    assert stored.input_messages[0].parts[0].content == difficult_text
    assert stored.raw["lone_surrogate"] == "\ud800"
    assert stored.raw["null_character"] == "\x00"
    assert math.isnan(stored.raw["not_a_number"])
    assert stored.raw["huge_number"] == huge_number

    with postgres_engine.connect() as connection:
        raw, raw_type, messages_type = connection.execute(
            text(
                "SELECT raw, pg_typeof(raw)::text, "
                "pg_typeof(input_messages)::text FROM inference_calls"
            )
        ).one()
    assert raw_type == "text"
    assert messages_type == "text"
    assert "\\ud800" in raw
    assert "\\u0000" in raw
    assert "NaN" in raw
    assert str(huge_number) in raw
    assert json.loads(raw)["huge_number"] == huge_number


def test_postgres_session_merge_and_redelivery_are_order_independent(
    postgres_database_factory,
) -> None:
    facts = [
        _call(
            inference_call_id="inference-a",
            model_call_id="model-a",
            user_id=None,
            observed_at=T0 + timedelta(minutes=2),
        ),
        _call(
            inference_call_id="inference-b",
            model_call_id="model-b",
            user_id="developer-1",
            observed_at=T0,
        ),
        _call(
            inference_call_id="inference-c",
            model_call_id="model-c",
            user_id="developer-2",
            observed_at=T0 + timedelta(minutes=1),
        ),
    ]
    populations = []
    for order in itertools.permutations(facts):
        redeliveries = {
            fact.inference_call_id: fact.model_copy(
                update={"inference_call_id": f"{fact.inference_call_id}-redelivery"}
            )
            for fact in facts
        }
        deliveries = (
            order[0],
            order[1],
            redeliveries[order[0].inference_call_id],
            order[2],
            redeliveries[order[2].inference_call_id],
            redeliveries[order[1].inference_call_id],
        )
        engine = create_engine(postgres_database_factory())
        try:
            store = FactStore(engine)
            for index, fact in enumerate(deliveries):
                expected_stored = index in {0, 1, 3}
                assert store.store_inference_call(fact) is expected_stored
            with engine.connect() as connection:
                session = dict(
                    connection.execute(
                        text(
                            "SELECT session_id, user_id, user_id_conflict, "
                            "first_observed_at, last_observed_at FROM sessions"
                        )
                    )
                    .mappings()
                    .one()
                )
                ids = tuple(
                    connection.execute(
                        text(
                            "SELECT inference_call_id FROM inference_calls "
                            "ORDER BY inference_call_id"
                        )
                    ).scalars()
                )
            populations.append((session, ids))
        finally:
            engine.dispose()

    assert all(population == populations[0] for population in populations)
    assert populations[0] == (
        {
            "session_id": "session-1",
            "user_id": None,
            "user_id_conflict": True,
            "first_observed_at": T0,
            "last_observed_at": T0 + timedelta(minutes=2),
        },
        ("inference-a", "inference-b", "inference-c"),
    )
