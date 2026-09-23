# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streamed keyword selection agrees with the complete materialized oracle."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
import itertools
import json
import random

import pytest

import sediment_core as core
from sediment_core.evidence import (
    ContextScanMetadata,
    ContextScanSession,
    EvidenceReadError,
    encode_evidence_json,
)
from sediment_derive import context_retrieval as retrieval

T0 = datetime(2026, 9, 23, tzinfo=UTC)


def item(content, identifier="call", **changes):
    part = core.TextPart(content=content) if isinstance(content, str) else content
    return replace(
        core.EvidenceReadItem(
            core.EvidenceReference(identifier, "input", 0, 0),
            T0,
            "user",
            None,
            part,
        ),
        **changes,
    )


def source(*items):
    return core.EvidenceContextSource(
        session_id="session",
        quarantine_revision=7,
        visible_inference_calls=len({i.reference.inference_call_id for i in items}),
        quarantined_inference_calls=2,
        items=items,
    )


def discovery_source(*sessions):
    return core.ContextDiscoverySource(
        authorized_sessions=len(sessions) + 1,
        quarantine_revision=7,
        visible_inference_calls=sum(len(s.items) for s in sessions),
        quarantined_inference_calls=2,
        sessions=sessions,
        commit=core.ContextCommitAnchor("github", "github.com", "42", "a" * 40),
    )


def scan(value):
    sessions = (
        value.sessions
        if isinstance(value, core.ContextDiscoverySource)
        else (core.ContextDiscoverySession(value.session_id, value.items, None),)
    )
    return (
        ContextScanMetadata(
            authorized_sessions=getattr(value, "authorized_sessions", 1),
            quarantine_revision=value.quarantine_revision,
            visible_inference_calls=value.visible_inference_calls,
            quarantined_inference_calls=value.quarantined_inference_calls,
            sessions=tuple(
                ContextScanSession(s.session_id, s.commit_match) for s in sessions
            ),
            commit=getattr(value, "commit", None),
        ),
        [(s.session_id, i) for s in sessions for i in s.items],
    )


def streamed(value, query="parser", budget=16384, *, order=None):
    name = (
        "discover_context_stream"
        if isinstance(value, core.ContextDiscoverySource)
        else "retrieve_context_stream"
    )
    selector = getattr(retrieval, name, None)
    assert callable(selector), f"{name} must reduce a complete one-shot stream"
    metadata, items = scan(value)
    consumed = []

    def once():
        for occurrence in items if order is None else order:
            consumed.append(occurrence)
            yield occurrence

    result = selector(metadata, once(), query, budget)
    assert len(consumed) == len(items)
    return result


def wire(result, budget=65536):
    return encode_evidence_json(result, type(result), max_bytes=budget)


def assert_parity(value, query="parser", budget=16384, *, order=None):
    oracle = (
        retrieval.discover_context
        if isinstance(value, core.ContextDiscoverySource)
        else retrieval.retrieve_context
    )(value, query, budget)
    result = streamed(value, query, budget, order=order)
    assert wire(result, budget) == wire(oracle, budget)
    skipped, coverage = asdict(result.skipped), result.coverage
    if isinstance(value, core.ContextDiscoverySource):
        assert coverage.scanned_parts == (
            coverage.matched_parts
            + skipped["reasoning_part"]
            + skipped["non_finite_number"]
            + skipped["unmatched_part"]
        )
        assert coverage.found_sessions == (
            len(result.items)
            + skipped["unmatched_session"]
            + skipped["candidate_limit"]
            + skipped["response_budget"]
        )
    else:
        assert coverage.scanned_parts == len(result.items) + sum(skipped.values())
    return result


def test_retrieval_stream_preserves_occurrences_under_every_permutation():
    value = source(
        item("parser failure", "older", observed_at=T0 - timedelta(days=1)),
        item("parser failure", "winner"),
        item("parser different", "other"),
        item(core.ReasoningPart(content="parser"), "reasoning"),
        item("unrelated", "no-match"),
    )
    _, items = scan(value)
    for order in itertools.permutations(items):
        result = assert_parity(value, "parser failure", order=order)
        assert result.items[0].evidence.reference.inference_call_id == "winner"


def test_discovery_stream_counts_repeated_occurrences_and_commit_only_sessions():
    witness = core.ContextCommitMatch("observation", "push", T0)
    value = discovery_source(
        core.ContextDiscoverySession(
            "text", (item("parser failure", "z"), item("parser failure", "a")), None
        ),
        core.ContextDiscoverySession("empty", (), None),
        core.ContextDiscoverySession("commit-only", (), witness),
        core.ContextDiscoverySession("anchored", (item("parser"),), witness),
    )
    _, items = scan(value)
    for order in itertools.permutations(items):
        result = assert_parity(value, "parser failure", order=order)
        assert result.coverage.matched_parts == 3
        assert result.items[-1].matched_parts == 2
        assert result.items[-1].preview.reference.inference_call_id == "a"
        assert result.items[1].preview is None


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_keeps_strict_scalar_bytes_and_full_key_equivalence(discovery):
    original = core.ToolCallPart(
        id="unsearchable_alias",
        name="parser",
        arguments={"z": "\x00\ud800😀", "n": 2**100, "f": -0.0, "a": True},
    )
    reordered = original.model_copy(
        update={"arguments": dict(reversed(list(original.arguments.items())))}
    )
    parts = (
        item(original, "a"),
        item(reordered, "z"),
        item(original.model_copy(update={"id": "different"}), "tool-id"),
        item(original, "role", role="assistant"),
        item(original, "finish", finish_reason="stop"),
        item("parser 😀", "emoji"),
        item("parser \ud83d\ude00", "surrogates"),
        item("parser", "role-scalar", role="😀"),
        item("parser", "role-pair", role="\ud83d\ude00"),
        item("parser", "finish-scalar", finish_reason="😀"),
        item("parser", "finish-pair", finish_reason="\ud83d\ude00"),
        item(core.ToolCallResponsePart(id="nan", result={"q": float("nan")}), "nan"),
        item(
            core.ToolCallPart(id="inf", name="other", arguments={"x": [float("inf")]}),
            "inf",
        ),
        item(core.ReasoningPart(content="parser"), "reasoning"),
    )
    value = (
        discovery_source(core.ContextDiscoverySession("s", parts, None))
        if discovery
        else source(*parts)
    )
    _, occurrences = scan(value)
    for order in (occurrences, list(reversed(occurrences))):
        result = assert_parity(value, budget=65536, order=order)
        assert result.skipped.non_finite_number == 2
        assert result.skipped.reasoning_part == 1
        assert b'"z":' in wire(result)
        assert b'"f":-0.0' in wire(result)
        assert wire(result).index(b'"z":') < wire(result).index(b'"n":')
    assert_parity(value, "unsearchable_alias")


def test_stream_rank_preserves_precise_instants_and_reference_ties():
    latest = datetime.max.replace(tzinfo=UTC) - timedelta(days=1)
    specs = (
        ("z", "input", 0, 0, latest),
        ("a", "input", 0, 0, latest - timedelta(microseconds=1)),
        ("a", "input", 0, 1, T0),
        ("a", "input", 1, 0, T0),
        ("a", "output", 0, 0, T0.astimezone(timezone(timedelta(hours=2)))),
        ("b", "input", 0, 0, T0),
    )
    value = source(
        *(
            item(
                f"parser {n}",
                reference=core.EvidenceReference(identifier, side, message, part),
                observed_at=observed,
            )
            for n, (identifier, side, message, part, observed) in enumerate(specs)
        )
    )
    _, occurrences = scan(value)
    random.Random(101).shuffle(occurrences)
    assert_parity(value, order=occurrences)


def test_stream_keeps_packing_precedence_and_continues_after_oversized_group():
    huge = "parser " + "x" * 5000
    value = source(item(huge, "a"), item(huge, "b"), item("parser small", "c"))
    result = assert_parity(value, budget=4096)
    assert result.skipped.repeated_content == result.skipped.response_budget == 1
    value = source(
        *(item(f"parser {n}", f"{n:02}") for n in range(10)),
        item("parser 0", "duplicate"),
    )
    result = assert_parity(value, budget=65536)
    assert result.skipped.item_limit == 2 and result.skipped.repeated_content == 1
    first, second = item("parser " + "x" * 1400, "a"), item("parser " + "y" * 1400, "b")
    size = len(
        encode_evidence_json(
            retrieval.ContextRetrievalItem(1, first), retrieval.ContextRetrievalItem
        )
    )
    budget = 2048 + 2 * size + 1
    assert len(assert_parity(source(first, second), budget=budget).items) == 2
    assert len(assert_parity(source(first, second), budget=budget - 1).items) == 1


@pytest.mark.parametrize("commit", [False, True])
def test_discovery_keeps_oversized_best_preview_even_with_smaller_match(commit):
    value = discovery_source(
        core.ContextDiscoverySession(
            "s",
            (item("parser failure " + "x" * 5000, "a"), item("parser", "b")),
            core.ContextCommitMatch("observation", "push", T0) if commit else None,
        )
    )
    result = assert_parity(value, "parser failure", budget=4096)
    assert result.status == "budget_exhausted"
    assert result.coverage.matched_parts == 2


@pytest.mark.parametrize("discovery", [False, True])
def test_state_reservation_is_exact_monotone_and_permutation_independent(
    monkeypatch, discovery
):
    small = item("parser", "a")
    large = item("parser", "z" * 4096)
    value = (
        discovery_source(core.ContextDiscoverySession("s", (small, large), None))
        if discovery
        else source(small, large)
    )
    _, occurrences = scan(value)
    if discovery:
        size = len(encode_evidence_json(large, core.EvidenceReadItem)) + 512
    else:
        key = json.dumps(
            {
                "role": large.role,
                "finish_reason": large.finish_reason,
                "part": large.part.model_dump(mode="python"),
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        size = (
            len(key)
            + len(
                encode_evidence_json(
                    retrieval.ContextRetrievalItem(1, large),
                    retrieval.ContextRetrievalItem,
                )
            )
            + 512
        )
    for order in itertools.permutations(occurrences):
        monkeypatch.setattr(retrieval, "CONTEXT_STATE_BYTES_LIMIT", size, raising=False)
        assert_parity(value, order=order)
        monkeypatch.setattr(retrieval, "CONTEXT_STATE_BYTES_LIMIT", size - 1)
        with pytest.raises(EvidenceReadError) as caught:
            streamed(value, order=order)
        assert caught.value.detail == {
            "reason": "retrieval_state_limit",
            "limit_bytes": size - 1,
        }


@pytest.mark.parametrize("field", ["role", "finish_reason", "inference_call_id"])
def test_long_shared_metadata_has_explicit_state_refusal(field):
    # One stored call/message carries this metadata once for its 100 parts.
    long = "r" * (400 * 1024)
    value = source(
        *(
            item(
                f"parser {n}",
                reference=core.EvidenceReference(
                    long if field == "inference_call_id" else "call", "input", 0, n
                ),
                **({field: long} if field != "inference_call_id" else {}),
            )
            for n in range(100)
        )
    )
    eager = retrieval.retrieve_context(value, "parser")
    assert eager.status == "budget_exhausted" and eager.coverage.scanned_parts == 100
    with pytest.raises(EvidenceReadError) as caught:
        streamed(value)
    assert caught.value.detail == {
        "reason": "retrieval_state_limit",
        "limit_bytes": 32 * 1024 * 1024,
    }


@pytest.mark.parametrize("discovery", [False, True])
def test_empty_and_unmatched_streams_preserve_complete_status(discovery):
    for parts in ((), (item("unrelated"),)):
        value = (
            discovery_source(core.ContextDiscoverySession("s", parts, None))
            if discovery
            else source(*parts)
        )
        assert assert_parity(value).status == "no_match"


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_validates_query_and_budget_before_consuming(discovery):
    name = "discover_context_stream" if discovery else "retrieve_context_stream"
    selector = getattr(retrieval, name, None)
    assert callable(selector), f"{name} must validate before scanning"
    metadata, _ = scan(source())

    def forbidden():
        raise AssertionError("invalid request consumed the source")
        yield

    for query, budget in (("the and", 4096), ("parser", True), ("parser", 4095)):
        with pytest.raises(ValueError):
            selector(metadata, forbidden(), query, budget)


def test_discovery_charges_every_found_session_even_without_matches(monkeypatch):
    value = discovery_source(
        *(core.ContextDiscoverySession(str(n), (), None) for n in range(3))
    )
    monkeypatch.setattr(retrieval, "CONTEXT_STATE_BYTES_LIMIT", 3 * 512)
    assert assert_parity(value).skipped.unmatched_session == 3
    monkeypatch.setattr(retrieval, "CONTEXT_STATE_BYTES_LIMIT", 3 * 512 - 1)
    with pytest.raises(EvidenceReadError) as caught:
        streamed(value)
    assert caught.value.detail == {
        "reason": "retrieval_state_limit",
        "limit_bytes": 3 * 512 - 1,
    }


@pytest.mark.parametrize("discovery", [False, True])
def test_state_bounds_the_aggregate_of_distinct_groups_or_sessions(
    monkeypatch, discovery
):
    parts = (item("parser one", "a"), item("parser two", "b"))
    value = (
        discovery_source(
            *(
                core.ContextDiscoverySession(str(n), (part,), None)
                for n, part in enumerate(parts)
            )
        )
        if discovery
        else source(*parts)
    )
    if discovery:
        size = sum(
            len(encode_evidence_json(part, core.EvidenceReadItem)) + 512
            for part in parts
        )
    else:
        size = sum(
            len(
                json.dumps(
                    {
                        "role": part.role,
                        "finish_reason": part.finish_reason,
                        "part": part.part.model_dump(mode="python"),
                    },
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            + len(
                encode_evidence_json(
                    retrieval.ContextRetrievalItem(1, part),
                    retrieval.ContextRetrievalItem,
                )
            )
            + 512
            for part in parts
        )
    _, occurrences = scan(value)
    for order in itertools.permutations(occurrences):
        monkeypatch.setattr(retrieval, "CONTEXT_STATE_BYTES_LIMIT", size)
        assert_parity(value, order=order)
        monkeypatch.setattr(retrieval, "CONTEXT_STATE_BYTES_LIMIT", size - 1)
        with pytest.raises(EvidenceReadError) as caught:
            streamed(value, order=order)
        assert caught.value.detail == {
            "reason": "retrieval_state_limit",
            "limit_bytes": size - 1,
        }


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_declines_an_individually_oversized_state_encoding(
    monkeypatch, discovery
):
    part = item("parser " + "x" * 3000)
    value = (
        discovery_source(core.ContextDiscoverySession("s", (part,), None))
        if discovery
        else source(part)
    )
    monkeypatch.setattr(retrieval, "CONTEXT_STATE_BYTES_LIMIT", 2048)
    with pytest.raises(EvidenceReadError) as caught:
        streamed(value)
    assert caught.value.detail == {
        "reason": "retrieval_state_limit",
        "limit_bytes": 2048,
    }


def test_stream_never_replaces_an_oversized_winner_with_a_smaller_duplicate():
    value = source(
        item("parser", "a" * 4096),
        item("parser", "z"),
        item("parser small", "other"),
    )
    result = assert_parity(value, budget=4096)
    assert [part.evidence.reference.inference_call_id for part in result.items] == [
        "other"
    ]
    assert result.skipped.repeated_content == result.skipped.response_budget == 1


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_propagates_interruption_without_a_partial_result(discovery):
    selector = (
        retrieval.discover_context_stream
        if discovery
        else retrieval.retrieve_context_stream
    )
    metadata, _ = scan(source())

    def interrupted():
        yield "session", item("parser")
        raise RuntimeError("source interrupted")

    with pytest.raises(RuntimeError, match="source interrupted"):
        selector(metadata, interrupted(), "parser")


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_reduces_all_10100_growing_history_occurrences(discovery):
    parts = []
    for call in range(100):
        for message in range(2 * call + 2):
            output = message == 2 * call + 1
            parts.append(
                item(
                    f"parser message{message}",
                    reference=core.EvidenceReference(
                        f"call-{call:03}",
                        "output" if output else "input",
                        0 if output else message,
                        0,
                    ),
                    observed_at=T0 + timedelta(seconds=call),
                    role="assistant" if message % 2 else "user",
                    finish_reason="stop" if output else None,
                )
            )
    value = (
        replace(
            discovery_source(
                core.ContextDiscoverySession("session", tuple(parts), None)
            ),
            visible_inference_calls=100,
        )
        if discovery
        else source(*parts)
    )
    _, occurrences = scan(value)
    random.Random(101).shuffle(occurrences)
    result = assert_parity(value, budget=65536, order=occurrences)
    assert result.coverage.scanned_parts == 10_100
    if discovery:
        assert result.coverage.matched_parts == 10_100
        assert result.items[0].matched_parts == 10_100
    else:
        assert result.skipped.repeated_content == 9801
        assert result.skipped.item_limit == 291


def test_retrieval_stream_bounds_distinct_group_count(monkeypatch):
    monkeypatch.setattr(retrieval, "CONTEXT_SCAN_PART_LIMIT", 1)
    assert_parity(source(item("parser")))
    value = source(item("parser one", "a"), item("parser two", "b"))
    _, occurrences = scan(value)
    for order in itertools.permutations(occurrences):
        with pytest.raises(EvidenceReadError) as caught:
            streamed(value, order=order)
        assert caught.value.detail == {
            "reason": "retrieval_state_limit",
            "limit_bytes": 32 * 1024 * 1024,
        }


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_rejects_occurrences_outside_its_header(discovery):
    selector = (
        retrieval.discover_context_stream
        if discovery
        else retrieval.retrieve_context_stream
    )
    metadata, _ = scan(source())
    with pytest.raises(ValueError, match="scan Session mismatch"):
        selector(metadata, iter((("different", item("parser")),)), "parser")


def test_retrieval_stream_requires_exactly_one_found_session():
    metadata, _ = scan(source())
    with pytest.raises(ValueError, match="requires one found Session"):
        retrieval.retrieve_context_stream(
            replace(metadata, sessions=()), iter(()), "parser"
        )
