# SPDX-License-Identifier: AGPL-3.0-or-later
"""Complete bounded identity witnesses without hydrating unrelated content."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sediment_core import (
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    OperationalReportLimitExceeded,
    TextPart,
    ToolCallPart,
)

BOUNDARY = datetime(2026, 9, 7, tzinfo=UTC)


def _call(identity, *, org_id="acme", observed_at=BOUNDARY):
    return InferenceCall(
        inference_call_id=identity,
        org_id=org_id,
        session_id=f"session-{identity}",
        gateway_provider=GatewayProvider.LITELLM,
        model_call_id=f"provider-{identity}",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="x" * 100_000)])
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    TextPart(content="opaque\x00\ud800"),
                    ToolCallPart(
                        id="shared",
                        name="Edit",
                        arguments={
                            "nan": float("nan"),
                            "inf": float("inf"),
                            "text": "\ud800\x00",
                        },
                    ),
                    ToolCallPart(id="shared", name="Edit", arguments={}),
                    ToolCallPart(id=f"provider-{identity}", name="Edit", arguments={}),
                ],
            )
        ],
        observed_at=observed_at,
        raw={"payload": "y" * 100_000},
    )


def test_identity_read_preserves_complete_aliases_and_historical_visibility(
    postgres_store, postgres_engine
):
    facts = [
        _call("old", observed_at=BOUNDARY - timedelta(days=100)),
        _call("boundary"),
        _call("future", observed_at=BOUNDARY + timedelta(microseconds=1)),
        _call("foreign", org_id="other"),
        _call("quarantined"),
    ]
    for fact in reversed(facts):
        postgres_store.store_inference_call(fact)
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "quarantined", reason="control"
    )
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        identities = postgres_store.read_inference_call_identities(
            "acme", observed_through=BOUNDARY, limit=2
        )
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)

    assert [item.inference_call_id for item in identities] == ["old", "boundary"]
    assert [item.observed_at for item in identities] == [
        BOUNDARY - timedelta(days=100),
        BOUNDARY,
    ]
    assert [item.call_ids for item in identities] == [
        ("provider-old", "shared"),
        ("provider-boundary", "shared"),
    ]
    assert all(item.org_id == "acme" for item in identities)
    assert all(not hasattr(item, "output_messages") for item in identities)
    assert all(
        "input_messages" not in statement and ".raw" not in statement
        for statement in statements
    )
    assert all("jsonb" not in statement.lower() for statement in statements)
    with postgres_store.read_snapshot() as snapshot:
        assert (
            snapshot.read_inference_call_identities(
                "acme", observed_through=BOUNDARY, limit=2
            )
            == identities
        )
        with pytest.raises(OperationalReportLimitExceeded, match="identity.*1"):
            snapshot.read_inference_call_identities(
                "acme", observed_through=BOUNDARY, limit=1
            )
        # A failed completeness check never caches a partial result.
        assert (
            snapshot.read_inference_call_identities(
                "acme", observed_through=BOUNDARY, limit=2
            )
            == identities
        )


@pytest.mark.parametrize("limit", [0, -1, True])
def test_identity_read_requires_positive_row_budget(postgres_store, limit):
    with pytest.raises(ValueError, match="limit"):
        postgres_store.read_inference_call_identities(
            "acme", observed_through=BOUNDARY, limit=limit
        )


def test_identity_read_requires_aware_boundary(postgres_store):
    with pytest.raises(ValueError, match="aware"):
        postgres_store.read_inference_call_identities(
            "acme", observed_through=datetime(2026, 9, 7), limit=1
        )
