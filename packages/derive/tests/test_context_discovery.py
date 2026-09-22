# SPDX-License-Identifier: AGPL-3.0-or-later
"""Candidate selection keeps exact previews, evidence counts, and stable bytes."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
import itertools
import json

import pytest

import sediment_core as core
from sediment_core.evidence import encode_evidence_json
from sediment_derive import context_retrieval as retrieval

T0 = datetime(2026, 9, 22, tzinfo=UTC)


def item(content, identifier="call"):
    part = core.TextPart(content=content) if isinstance(content, str) else content
    return core.EvidenceReadItem(
        core.EvidenceReference(identifier, "input", 0, 0), T0, "user", None, part
    )


def session(identifier, *items, commit=False):
    match = core.ContextCommitMatch("observation", "push", T0) if commit else None
    return core.ContextDiscoverySession(identifier, items, match)


def source(*sessions):
    return core.ContextDiscoverySource(
        authorized_sessions=len(sessions) + 1,
        quarantine_revision=3,
        visible_inference_calls=sum(len(s.items) for s in sessions),
        quarantined_inference_calls=1,
        sessions=sessions,
        commit=core.ContextCommitAnchor("github", "github.com", "42", "a" * 40),
    )


def wire(result, budget=16384):
    return encode_evidence_json(
        result, retrieval.ContextDiscoveryResult, max_bytes=budget
    )


def assert_counts(result):
    c, s = result.coverage, result.skipped
    assert (
        c.scanned_parts
        == c.matched_parts + s.reasoning_part + s.non_finite_number + s.unmatched_part
    )
    assert (
        c.found_sessions
        == len(result.items)
        + s.unmatched_session
        + s.candidate_limit
        + s.response_budget
    )
    assert all(v >= 0 for v in asdict(s).values())


def test_discovery_ranks_observed_edges_and_best_occurrence_deterministically():
    discover = getattr(retrieval, "discover_context", None)
    assert callable(discover), "pure candidate discovery must be implemented"
    sessions = (
        session("text", item("parser failure", "z"), item("parser failure", "a")),
        session("commit-only", commit=True),
        session("anchored", item("parser"), commit=True),
        session("unmatched", item("distractor")),
    )
    result = discover(source(*sessions), "parser failure")
    assert [s.session_id for s in result.items] == ["anchored", "commit-only", "text"]
    assert result.items[1].score == 0 and result.items[1].preview is None
    assert result.items[2].preview.reference.inference_call_id == "a"
    assert result.items[2].matched_parts == 2 and result.items[2].score == 2
    assert result.coverage.matched_parts == 3
    assert result.skipped.unmatched_session == 1
    assert_counts(result)
    for permutation in itertools.permutations(sessions):
        shuffled = tuple(
            replace(s, items=tuple(reversed(s.items))) for s in permutation
        )
        assert wire(discover(source(*shuffled), "parser failure")) == wire(result)


def test_discovery_exclusions_and_exact_scalar_preview():
    original = core.ToolCallPart(
        id="alias",
        name="parser",
        arguments={"n": 2**100, "f": -0.0, "s": "\x00\ud800😀"},
    )
    result = retrieval.discover_context(
        source(
            session(
                "s",
                item(core.ReasoningPart(content="parser")),
                item(
                    core.ToolCallResponsePart(id="nan", result={"parser": float("nan")})
                ),
                item(original),
                item("other"),
            )
        ),
        "parser",
    )
    assert (
        result.skipped.reasoning_part
        == result.skipped.non_finite_number
        == result.skipped.unmatched_part
        == 1
    )
    assert json.loads(wire(result))["items"][0]["preview"][
        "part"
    ] == original.model_dump(mode="python")
    assert b'"f":-0.0' in wire(result)
    assert_counts(result)


def test_whole_candidate_packing_continues_and_reports_limits():
    oversized = session("a", item("parser " * 2000))
    small = tuple(session(f"b{i}", item("parser")) for i in range(10))
    result = retrieval.discover_context(source(oversized, *small), "parser", 65536)
    assert len(result.items) == 8 and result.skipped.candidate_limit == 3
    limited = retrieval.discover_context(source(oversized, small[0]), "parser", 4096)
    assert [s.session_id for s in limited.items] == ["b0"]
    assert limited.skipped.response_budget == 1
    assert len(wire(limited, 4096)) <= 4096
    exhausted = retrieval.discover_context(source(oversized), "parser", 4096)
    assert exhausted.status == "budget_exhausted" and not exhausted.items
    no_match = retrieval.discover_context(source(session("empty")), "parser")
    assert no_match.status == "no_match" and no_match.skipped.unmatched_session == 1
    for result in (result, limited, exhausted, no_match):
        assert_counts(result)


@pytest.mark.parametrize("budget", [True, 0, 4095, 65537, 4096.0])
def test_discovery_rejects_invalid_budget(budget):
    with pytest.raises(ValueError, match="invalid context response budget"):
        retrieval.discover_context(source(), "parser", budget)


def test_discovery_policy_and_closed_wire_contract():
    from pydantic import TypeAdapter, ValidationError

    for version in (0, 2, True, 1.0):
        with pytest.raises(ValueError, match="unsupported context discovery policy"):
            retrieval.ContextDiscoveryPolicy(version)
    result = retrieval.discover_context(source(session("s", item("parser"))), "parser")
    payload = json.loads(wire(result))
    assert set(payload) == {
        "schema_version",
        "policy_version",
        "quarantine_revision",
        "capture_completeness",
        "commit",
        "status",
        "coverage",
        "skipped",
        "items",
    }
    assert set(payload["items"][0]) == {
        "session_id",
        "score",
        "matched_parts",
        "preview",
        "commit_match",
    }
    assert tuple(payload["skipped"]) == retrieval.CONTEXT_DISCOVERY_SKIP_REASONS
    with pytest.raises(ValidationError):
        TypeAdapter(retrieval.ContextDiscoveryResult).validate_python(
            {**payload, "query": "private"}
        )
    with pytest.raises(ValidationError):
        TypeAdapter(retrieval.ContextDiscoveryResult).validate_python(
            {**payload, "schema_version": True}
        )


def test_reserved_envelope_is_validated_independently(monkeypatch):
    monkeypatch.setattr(retrieval, "CONTEXT_ENVELOPE_BYTES", 64)
    with pytest.raises(core.EvidenceReadError) as caught:
        retrieval.discover_context(source(), "parser", 65536)
    assert caught.value.detail == {"reason": "evidence_response_limit", "limit": 64}


def test_real_postgres_ingest_and_scope_order_preserve_discovery_bytes(
    postgres_store_factory,
):
    facts = [
        core.InferenceCall(
            inference_call_id=identifier,
            org_id="acme",
            session_id=session_id,
            gateway_provider="litellm",
            observed_at=T0,
            input_messages=[
                core.InferenceMessage(role="user", parts=[core.TextPart(content=text)])
            ],
            output_messages=[],
        )
        for identifier, session_id, text in (
            ("b", "first", "parser constraint"),
            ("a", "first", "parser constraint"),
            ("c", "second", "parser failure"),
            ("d", "distractor", "another task"),
        )
    ]
    results = []
    for population, grant in (
        (facts, ["second", "first", "distractor"]),
        (list(reversed(facts)), ["distractor", "first", "second"]),
    ):
        _, store = postgres_store_factory()
        for fact in population:
            store.store_inference_call(fact)
        with store.read_snapshot() as snapshot:
            results.append(
                wire(
                    retrieval.discover_context(
                        snapshot.read_context_discovery_source("acme", grant),
                        "parser constraint failure",
                    )
                )
            )
    assert results[0] == results[1]
