# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery applies one grant and aggregate limits before content transfer."""

from datetime import UTC, datetime
from dataclasses import asdict

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import event, func, select

import sediment_core as core
from sediment_core import evidence
from sediment_core.postgres_schema import inference_calls, session_commit_observations

T0 = datetime(2026, 9, 22, tzinfo=UTC)
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="42"
)


def call(identifier, session, *, org="acme", content="parser constraint"):
    return core.InferenceCall(
        inference_call_id=identifier,
        org_id=org,
        session_id=session,
        gateway_provider="litellm",
        observed_at=T0,
        user_id="private-user",
        raw={"private": "secret"},
        input_messages=[
            core.InferenceMessage(role="user", parts=[core.TextPart(content=content)])
        ],
        output_messages=[],
    )


def anchor(**changes):
    return core.ContextCommitAnchor(**{**IDENTITY, "commit_sha": "a" * 40, **changes})


def observe(store, session, *, identifier="observation", identity=None, **changes):
    identity = IDENTITY if identity is None else identity
    push = core.Push(
        push_id=f"push-{identifier}",
        org_id="acme",
        provider="github",
        repo="acme/old",
        clone_url="https://private.invalid/repo",
        ref=f"refs/heads/{identifier}",
        before_sha="0" * 40,
        after_sha="b" * 40,
        captured_at=T0,
        **identity,
    )
    store.store_push(push)
    fact = core.SessionCommitObservation(
        **{
            "observation_id": identifier,
            "org_id": "acme",
            "repo": "acme/renamed",
            "session_id": session,
            "commit_sha": "a" * 40,
            "source_push_id": push.push_id,
            "captured_at": T0,
            **identity,
            **changes,
        }
    )
    store.store_session_commit_observation(fact)
    return fact


def record_sql(engine):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


def test_discovery_source_scopes_presence_content_and_quarantine(postgres_store):
    reader = getattr(postgres_store, "read_context_discovery_source", None)
    assert callable(reader), "FactStore must expose bounded candidate discovery"
    for fact in (
        call("a", "first"),
        call("b", "second"),
        call("hidden", "second"),
        call("outside", "outside"),
        call("foreign", "foreign", org="other"),
    ):
        postgres_store.store_inference_call(fact)
    postgres_store.quarantine_fact(
        "acme", core.FactTable.INFERENCE_CALLS, "hidden", reason="review"
    )
    source = reader("acme", ["second", "missing", "foreign", "first"])
    assert source.authorized_sessions == 4
    assert [s.session_id for s in source.sessions] == ["first", "second"]
    assert source.visible_inference_calls == 2
    assert source.quarantined_inference_calls == 1
    assert source.quarantine_revision > 0
    assert [
        i.reference.inference_call_id for s in source.sessions for i in s.items
    ] == ["a", "b"]


def test_anchor_requires_exact_visible_source_push_and_identity(postgres_store):
    observed = observe(postgres_store, "matched")
    observe(
        postgres_store,
        "fork",
        identifier="fork",
        identity={**IDENTITY, "repository_id": "99"},
    )
    observe(postgres_store, "legacy", identifier="legacy", identity={})
    observe(postgres_store, "hidden-push", identifier="hidden-push")
    observe(postgres_store, "hidden-observation", identifier="hidden-observation")
    postgres_store.quarantine_fact(
        "acme", core.FactTable.PUSHES, "push-hidden-push", reason="review"
    )
    postgres_store.quarantine_fact(
        "acme",
        core.FactTable.SESSION_COMMIT_OBSERVATIONS,
        "hidden-observation",
        reason="review",
    )
    source = postgres_store.read_context_discovery_source(
        "acme",
        ["matched", "fork", "legacy", "hidden-push", "hidden-observation"],
        anchor(),
    )
    matches = {s.session_id: s.commit_match for s in source.sessions if s.commit_match}
    assert set(matches) == {"matched"}
    assert matches["matched"].observation_id == observed.observation_id
    assert matches["matched"].source_push_id == observed.source_push_id
    assert source.commit == anchor()


def test_aggregate_preflight_includes_session_and_commit_metadata_before_transfer(
    postgres_store, postgres_engine, monkeypatch
):
    for identifier in ("a", "b"):
        postgres_store.store_inference_call(call(identifier, identifier))
    observe(postgres_store, "a")
    with postgres_engine.connect() as connection:
        content_bytes = connection.execute(
            select(
                func.sum(
                    sum(
                        func.coalesce(func.octet_length(inference_calls.c[name]), 0)
                        for name in (
                            "session_id",
                            "inference_call_id",
                            "model_provider",
                            "model",
                            "input_messages",
                            "output_messages",
                        )
                    )
                )
            )
        ).scalar_one()
        commit_bytes = connection.execute(
            select(
                func.sum(
                    sum(
                        func.octet_length(session_commit_observations.c[name])
                        for name in ("session_id", "observation_id", "source_push_id")
                    )
                )
            )
        ).scalar_one()
    exact = int(content_bytes + commit_bytes)
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact)
    assert (
        len(
            postgres_store.read_context_discovery_source(
                "acme", ["a", "b"], anchor()
            ).sessions
        )
        == 2
    )
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact - 1)
    statements, record = record_sql(postgres_engine)
    try:
        with pytest.raises(evidence.EvidenceReadError) as caught:
            postgres_store.read_context_discovery_source("acme", ["a", "b"], anchor())
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert caught.value.detail == {
        "reason": "evidence_source_limit",
        "bytes": exact,
        "limit": exact - 1,
    }
    assert not any(
        sql.startswith(
            ("SELECT inference_calls.", "SELECT session_commit_observations.")
        )
        for sql in statements
    )


@pytest.mark.parametrize(
    "scope", [[], ["a", "a"], [" a ", "a"], [""], [False], ["a"] * 33, "a"]
)
def test_invalid_grant_fails_before_sql(postgres_store, postgres_engine, scope):
    statements, record = record_sql(postgres_engine)
    try:
        with pytest.raises(ValueError):
            postgres_store.read_context_discovery_source("acme", scope)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert not statements


def test_opaque_content_outside_grant_is_not_loaded(postgres_store, postgres_engine):
    postgres_store.store_inference_call(call("visible", "granted"))
    # A retained malformed source outside the grant must not even reach parsing.
    for identifier, org, session in (
        ("other-session", "acme", "outside"),
        ("other-org", "other", "granted"),
    ):
        values = call(identifier, session, org=org).model_dump(mode="python")
        values.update(
            input_messages="invalid JSON",
            output_messages="[]",
            raw="{}",
            call_alias_count=0,
        )
        with postgres_engine.begin() as connection:
            connection.execute(inference_calls.insert().values(values))
    statements, record = record_sql(postgres_engine)
    try:
        result = postgres_store.read_context_discovery_source("acme", {"granted"})
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert result.visible_inference_calls == 1
    content_reads = [
        sql for sql in statements if sql.startswith("SELECT inference_calls.")
    ]
    assert len(content_reads) == 1
    assert "inference_calls.org_id =" in content_reads[0]
    assert "inference_calls.session_id IN" in content_reads[0]
    assert ".raw" not in content_reads[0] and ".user_id" not in content_reads[0]


def test_missing_and_conflicting_push_witnesses_do_not_match(
    postgres_store, postgres_engine
):
    original = observe(postgres_store, "s")
    for identifier, source_push_id, identity in (
        ("missing", "absent", IDENTITY),
        (
            "wrong-identity",
            original.source_push_id,
            {**IDENTITY, "repository_id": "99"},
        ),
        ("foreign-source", "foreign-push", IDENTITY),
    ):
        # Historical corruption is read through real PostgreSQL. The normal
        # write seam refuses these contradictory source relationships.
        fact = core.SessionCommitObservation.model_validate(
            {
                **original.model_dump(mode="python"),
                "observation_id": identifier,
                "session_id": identifier,
                "source_push_id": source_push_id,
                **identity,
            }
        )
        postgres_store.store_inference_call(call(identifier, identifier))
        with postgres_engine.begin() as connection:
            connection.execute(
                session_commit_observations.insert().values(
                    fact.model_dump(mode="python")
                )
            )
    postgres_store.store_push(
        core.Push(
            push_id="foreign-push",
            org_id="other",
            provider="github",
            repo="acme/old",
            clone_url="https://private.invalid/repo",
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha="b" * 40,
            captured_at=T0,
            **IDENTITY,
        )
    )
    result = postgres_store.read_context_discovery_source(
        "acme", ["missing", "foreign-source"], anchor()
    )
    assert all(s.commit_match is None for s in result.sessions)
    conflict = postgres_store.read_context_discovery_source(
        "acme", ["wrong-identity"], anchor(repository_id="99")
    )
    assert conflict.sessions[0].commit_match is None


def test_aggregate_call_limit_refuses_before_content_transfer(
    postgres_store, postgres_engine, monkeypatch
):
    for identifier in ("a", "b"):
        postgres_store.store_inference_call(call(identifier, identifier))
    monkeypatch.setattr(evidence, "EVIDENCE_INVENTORY_LIMIT", 1)
    statements, record = record_sql(postgres_engine)
    try:
        with pytest.raises(evidence.EvidenceReadError) as caught:
            postgres_store.read_context_discovery_source("acme", ["a", "b"])
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert caught.value.detail == {
        "reason": "evidence_inventory_limit",
        "count": 2,
        "limit": 1,
    }
    assert not any(sql.startswith("SELECT inference_calls.") for sql in statements)


def test_part_limit_is_aggregate_and_includes_unsearchable_parts(postgres_store):
    for identifier in ("a", "b"):
        fact = call(identifier, identifier)
        fact.input_messages[0].parts = [
            core.ReasoningPart(content="parser") for _ in range(1024)
        ]
        postgres_store.store_inference_call(fact)
    source = postgres_store.read_context_discovery_source("acme", ["a", "b"])
    assert sum(len(s.items) for s in source.sessions) == 2048
    postgres_store.store_inference_call(call("extra", "b"))
    with pytest.raises(evidence.EvidenceReadError) as caught:
        postgres_store.read_context_discovery_source("acme", ["b", "a"])
    assert caught.value.detail == {
        "reason": "retrieval_part_limit",
        "count": 2049,
        "limit": 2048,
    }


def test_snapshot_keeps_content_and_push_visibility_then_rechecks(
    postgres_store, postgres_engine
):
    postgres_store.store_inference_call(call("a", "s"))
    observe(postgres_store, "s")
    changed = False

    def change(connection, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if (
            not changed
            and "octet_length" in statement
            and "input_messages" in statement
        ):
            changed = True
            postgres_store.quarantine_fact(
                "acme", core.FactTable.PUSHES, "push-observation", reason="review"
            )
            postgres_store.quarantine_fact(
                "acme", core.FactTable.INFERENCE_CALLS, "a", reason="review"
            )

    event.listen(postgres_engine, "after_cursor_execute", change)
    try:
        first = postgres_store.read_context_discovery_source("acme", ["s"], anchor())
    finally:
        event.remove(postgres_engine, "after_cursor_execute", change)
    assert changed and first.quarantine_revision == 0
    assert first.sessions[0].items and first.sessions[0].commit_match
    later = postgres_store.read_context_discovery_source("acme", ["s"], anchor())
    assert later.quarantine_revision > first.quarantine_revision
    assert later.sessions[0].items == () and later.sessions[0].commit_match is None
    assert later.quarantined_inference_calls == 1


def test_anchor_canonical_validation_and_closed_shape():
    normalized = anchor(repository_host="GITHUB.COM", commit_sha="A" * 40)
    assert normalized == anchor()
    adapter = TypeAdapter(core.ContextCommitAnchor)
    for changes in (
        {"repository_id": ""},
        {"repository_id": " 42 "},
        {"commit_sha": "a"},
        {"repository_provider": "unknown"},
        {"repository_host": "https://github.com"},
    ):
        with pytest.raises(ValueError):
            anchor(**changes)
    for value in ({"commit_sha": "a" * 40}, {**asdict(anchor()), "repo": "acme/repo"}):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)
