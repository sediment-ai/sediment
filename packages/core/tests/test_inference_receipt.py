# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inference-call receipts name the immutable row PostgreSQL retains."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

import sediment_core
from sediment_core import FactStore, GatewayProvider, InferenceCall

BOUNDARY = datetime(2026, 9, 11, tzinfo=UTC)


def _call(identity="first", **overrides):
    return InferenceCall(
        **{
            "inference_call_id": identity,
            "org_id": "acme",
            "session_id": "session",
            "gateway_provider": GatewayProvider.LITELLM,
            "model_call_id": "provider-call",
            "observed_at": BOUNDARY,
            "input_messages": [],
            "output_messages": [],
            **overrides,
        }
    )


@pytest.mark.parametrize("keyed", [False, True])
def test_receipt_returns_retained_identity_without_changing_facts_or_session(
    postgres_store, keyed
):
    first = _call(model_call_id="provider-call" if keyed else None)
    receipt = postgres_store.store_inference_call_receipt(first)
    assert isinstance(receipt, sediment_core.InferenceCallReceipt)
    assert (receipt.fact_id, receipt.stored) == (first.inference_call_id, True)
    sessions = postgres_store.read_sessions("acme")
    duplicate = _call(
        "attempted" if keyed else "first",
        model_call_id=first.model_call_id,
        session_id="later-session",
        observed_at=BOUNDARY + timedelta(days=1),
    )
    receipt = postgres_store.store_inference_call_receipt(duplicate)
    assert (receipt.fact_id, receipt.stored) == (first.inference_call_id, False)
    assert postgres_store.store_inference_call(duplicate) is False
    assert postgres_store.read_inference_calls("acme") == [first]
    assert postgres_store.read_sessions("acme") == sessions


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("second_match", [False, True])
def test_receipt_declines_conflicting_primary_and_natural_identity(
    postgres_store, foreign, second_match
):
    first = _call(org_id="foreign" if foreign else "acme")
    assert postgres_store.store_inference_call(first)
    if second_match:
        assert postgres_store.store_inference_call(
            _call("second", model_call_id="other-provider-call")
        )
    before = {
        org: (
            postgres_store.read_inference_calls(org),
            postgres_store.read_sessions(org),
        )
        for org in ("acme", "foreign")
    }
    attempted = _call(model_call_id="other-provider-call")
    with pytest.raises(sediment_core.InferenceCallIdentityConflict) as raised:
        postgres_store.store_inference_call_receipt(attempted)
    assert first.inference_call_id not in str(raised.value)
    assert "foreign" not in str(raised.value)
    assert before == {
        org: (
            postgres_store.read_inference_calls(org),
            postgres_store.read_sessions(org),
        )
        for org in ("acme", "foreign")
    }


def test_receipt_names_quarantined_duplicate_without_releasing_it(postgres_store):
    first = _call()
    assert postgres_store.store_inference_call(first)
    postgres_store.quarantine_fact("acme", "inference_calls", "first", reason="test")
    receipt = postgres_store.store_inference_call_receipt(_call("attempted"))
    assert (receipt.fact_id, receipt.stored) == ("first", False)
    assert postgres_store.read_inference_calls("acme") == []


@pytest.mark.parametrize("keyed", [False, True])
def test_concurrent_receipts_converge_on_one_retained_fact(postgres_engine, keyed):
    barrier = Barrier(2)

    def deliver(index):
        call = _call(
            str(index) if keyed else "shared", model_call_id="p" if keyed else None
        )
        barrier.wait(timeout=5)
        return FactStore(postgres_engine).store_inference_call_receipt(call)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(deliver, range(2)))
    [retained] = FactStore(postgres_engine).read_inference_calls("acme")
    assert sum(receipt.stored for receipt in receipts) == 1
    assert {receipt.fact_id for receipt in receipts} == {retained.inference_call_id}
