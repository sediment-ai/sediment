# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyword selection keeps exact evidence, deterministic order, and byte bounds."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
import itertools
import json
import random

import pytest
from pydantic import TypeAdapter, ValidationError

from sediment_core import (
    EvidenceContextSource,
    EvidenceReadItem,
    EvidenceReference,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_core.evidence import EvidenceReadError, encode_evidence_json
from sediment_derive.context_retrieval import (
    CONTEXT_SKIP_REASONS,
    ContextRetrievalItem,
    ContextRetrievalPolicy,
    ContextRetrievalResult,
    context_query_tokens,
    retrieve_context,
)

T0 = datetime(2026, 9, 21, tzinfo=UTC)


def item(
    part,
    identifier="a",
    *,
    side="input",
    message=0,
    index=0,
    observed_at=T0,
    role="user",
    finish=None,
):
    return EvidenceReadItem(
        EvidenceReference(identifier, side, message, index),
        observed_at,
        role,
        finish,
        part,
    )


def source(*items, session="session"):
    return EvidenceContextSource(
        session_id=session,
        quarantine_revision=3,
        visible_inference_calls=len({i.reference.inference_call_id for i in items}),
        quarantined_inference_calls=1,
        items=items,
    )


def wire(result, max_bytes=16384):
    return encode_evidence_json(result, ContextRetrievalResult, max_bytes=max_bytes)


def assert_accounting(result):
    counts = asdict(result.skipped)
    assert tuple(counts) == CONTEXT_SKIP_REASONS
    assert sum(counts.values()) + len(result.items) == result.coverage.scanned_parts
    assert all(value >= 0 for value in counts.values())


def test_selection_is_deterministic_under_permutations_and_fact_order():
    items = (
        item(
            TextPart(content="parser failure"),
            "older",
            observed_at=T0 - timedelta(days=1),
        ),
        item(TextPart(content="parser failure"), "b"),
        item(TextPart(content="parser failure"), "a"),
        item(TextPart(content="parser other"), "single"),
        item(TextPart(content="unrelated timeout"), "distractor"),
    )
    expected = retrieve_context(
        source(*items), "what parser failure did the task encounter"
    )
    assert [i.evidence.reference.inference_call_id for i in expected.items] == [
        "a",
        "single",
    ]
    assert [i.score for i in expected.items] == [2, 1]
    assert expected.skipped.repeated_content == 2
    assert expected.skipped.no_match == 1
    assert_accounting(expected)
    for permutation in itertools.permutations(items):
        assert wire(
            retrieve_context(
                source(*permutation), "what parser failure did the task encounter"
            )
        ) == wire(expected)


def test_order_uses_exact_instant_then_id_side_and_indices():
    specs = [
        ("z", "input", 0, 0, T0 + timedelta(microseconds=1)),
        ("a", "input", 0, 0, T0),
        ("a", "input", 0, 1, T0),
        ("a", "input", 1, 0, T0),
        ("a", "output", 0, 0, T0.astimezone(timezone(timedelta(hours=2)))),
        ("b", "input", 0, 0, T0),
    ]
    inputs = [
        item(
            TextPart(content=f"parser {n}"),
            identifier,
            side=side,
            message=message,
            index=index,
            observed_at=when,
        )
        for n, (identifier, side, message, index, when) in enumerate(specs)
    ]
    expected = [i.reference for i in inputs]
    random.Random(27).shuffle(inputs)
    result = retrieve_context(source(*inputs), "parser")
    assert [i.evidence.reference for i in result.items] == expected
    assert_accounting(result)


def test_searchable_fields_exclusions_and_exact_scalar_values():
    original = ToolCallPart(
        id="unsearchable_alias",
        name="RunParser",
        arguments={"z": "parser\x00\ud800😀", "n": 2**100, "float": -0.0},
    )
    inputs = [
        item(ReasoningPart(content="parser"), "reasoning"),
        item(ToolCallResponsePart(id="nan", result={"parser": float("nan")}), "nan"),
        item(
            ToolCallPart(id="inf", name="parser", arguments={"value": [float("inf")]}),
            "inf",
        ),
        item(original, "tool", role="assistant\x00\ud800", finish="stop\ud800"),
        item(
            ToolCallResponsePart(
                id="result", result={"failure": "parser", "n": 2**100}
            ),
            "result",
        ),
        item(TextPart(content="constraint parser stays offline"), "text"),
        item(TextPart(content="other"), "parser"),
    ]
    result = retrieve_context(source(*inputs), "parser")
    assert result.skipped.reasoning_part == 1
    assert result.skipped.non_finite_number == 2
    assert result.skipped.no_match == 1
    assert len(result.items) == 3
    text = wire(result).decode("ascii")
    payload = json.loads(text)
    original_payload = next(
        i["evidence"]
        for i in payload["items"]
        if i["evidence"]["reference"]["inference_call_id"] == "tool"
    )
    assert original_payload["part"] == original.model_dump(mode="python")
    assert original_payload["role"] == "assistant\x00\ud800"
    assert original_payload["finish_reason"] == "stop\ud800"
    assert original_payload["part"]["arguments"]["n"] == 2**100
    assert '"float":-0.0' in text
    assert (
        retrieve_context(source(inputs[3]), "unsearchable_alias").status == "no_match"
    )
    assert retrieve_context(source(inputs[3]), "runparser").status == "matched"
    assert_accounting(result)


def test_duplicates_compare_sorted_keys_and_historical_metadata():
    a = item(ToolCallResponsePart(id="same", result={"a": "parser", "b": 1}), "a")
    b = item(ToolCallResponsePart(id="same", result={"b": 1, "a": "parser"}), "b")
    result = retrieve_context(
        source(a, b, replace(b, role="assistant"), replace(b, finish_reason="end")),
        "parser",
    )
    assert result.skipped.repeated_content == 1
    assert len(result.items) == 3
    assert_accounting(result)


def test_empty_no_match_and_query_validation():
    result = retrieve_context(source(), "parser")
    assert result.status == "no_match" and result.items == ()
    assert result.capture_completeness == "unknown"
    assert result.coverage.complete_visible_scan is True
    assert_accounting(result)
    assert (
        retrieve_context(source(item(TextPart(content="garden"))), "parser").status
        == "no_match"
    )
    assert context_query_tokens("WHERE is Parser_42 and 123?") == {"parser_42", "123"}
    for query in (
        "",
        "  ",
        "the and what",
        "!!!",
        "漢字",
        "x\ud800",
        "x" * 2049,
        "x" + "é" * 1024,
        7,
    ):
        with pytest.raises(ValueError, match="invalid context query"):
            context_query_tokens(query)
    assert context_query_tokens("x" * 2048)
    for budget in (True, 4096.0, "4096", 4095, 65537):
        with pytest.raises(ValueError, match="invalid context response budget"):
            retrieve_context(source(), "parser", budget)
    for version in (True, "1", 2):
        with pytest.raises(ValueError):
            ContextRetrievalPolicy(version)


def test_whole_part_budget_and_duplicate_precedence_keep_later_small_match():
    huge = "parser " + "x" * 5000
    result = retrieve_context(
        source(
            item(TextPart(content=huge), "a"),
            item(TextPart(content=huge), "b"),
            item(TextPart(content="parser small"), "c"),
        ),
        "parser",
        4096,
    )
    assert [i.evidence.reference.inference_call_id for i in result.items] == ["c"]
    assert result.skipped.response_budget == 1
    assert result.skipped.repeated_content == 1
    assert len(wire(result, 4096)) <= 4096
    assert_accounting(result)
    exhausted = retrieve_context(source(item(TextPart(content=huge))), "parser", 4096)
    assert exhausted.status == "budget_exhausted" and exhausted.items == ()
    assert_accounting(exhausted)


def test_item_limit_duplicate_precedence_and_all_skip_counts():
    inputs = [item(TextPart(content=f"parser {i}"), f"{i:02}") for i in range(10)]
    inputs += [item(TextPart(content="parser 0"), "duplicate")]
    result = retrieve_context(source(*inputs), "parser", 65536)
    assert len(result.items) == 8 and result.skipped.item_limit == 2
    assert result.skipped.repeated_content == 1
    assert_accounting(result)


def test_exact_item_byte_fit_and_comma_budget():
    first = item(TextPart(content="parser " + "x" * 1400), "a")
    second = item(TextPart(content="parser " + "y" * 1400), "b")
    size = len(
        encode_evidence_json(ContextRetrievalItem(1, first), ContextRetrievalItem)
    )
    assert size == len(
        encode_evidence_json(ContextRetrievalItem(1, second), ContextRetrievalItem)
    )
    budget = 2048 + size * 2 + 1
    assert len(retrieve_context(source(first, second), "parser", budget).items) == 2
    result = retrieve_context(source(first, second), "parser", budget - 1)
    assert len(result.items) == 1 and result.skipped.response_budget == 1
    assert_accounting(result)


def test_metadata_reserve_refuses_oversized_session_identity():
    with pytest.raises(EvidenceReadError) as error:
        retrieve_context(source(session="s" * 2048), "parser", 65536)
    assert error.value.detail == {"reason": "evidence_response_limit", "limit": 2048}


def test_response_contract_rejects_extra_fields_and_invalid_counts():
    payload = json.loads(wire(retrieve_context(source(), "parser")))
    adapter = TypeAdapter(ContextRetrievalResult)
    for field in payload:
        with pytest.raises(ValidationError):
            adapter.validate_python({k: v for k, v in payload.items() if k != field})
    payload["skipped"]["invented"] = 0
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


def test_fact_insertion_order_does_not_change_response_bytes(postgres_store_factory):
    from sediment_core import GatewayProvider, InferenceCall, InferenceMessage

    facts = [
        InferenceCall(
            inference_call_id=identifier,
            org_id="acme",
            session_id="session",
            gateway_provider=GatewayProvider.LITELLM,
            observed_at=T0,
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content=text)])
            ],
            output_messages=[],
        )
        for identifier, text in (
            ("b", "parser failure"),
            ("a", "parser failure"),
            ("c", "parser constraint"),
        )
    ]
    results = []
    for population in (facts, list(reversed(facts))):
        _, store = postgres_store_factory()
        for fact in population:
            store.store_inference_call(fact)
        with store.read_snapshot() as snapshot:
            results.append(
                wire(
                    retrieve_context(
                        snapshot.read_context_source("acme", "session"),
                        "parser failure constraint",
                    )
                )
            )
    assert results[0] == results[1]
