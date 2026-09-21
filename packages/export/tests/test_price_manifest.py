# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned operator-supplied model price manifest tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from sediment_export import ModelPrice, PriceManifest
from sediment_export.schema_contracts import CONTRACTS


T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
T2 = datetime(2027, 1, 1, tzinfo=UTC)


def _price(**overrides) -> ModelPrice:
    values = {
        "model_provider": "openai",
        "model": "gpt-5",
        "currency": "USD",
        "effective_from": T0,
        "effective_until": T1,
        "input_per_million_tokens": "1.25",
        "output_per_million_tokens": "10.00",
    }
    values.update(overrides)
    return ModelPrice(**values)


def test_manifest_accepts_adjacent_half_open_ranges() -> None:
    manifest = PriceManifest(
        version=1,
        manifest_id="contract-2026",
        prices=(
            _price(),
            _price(
                effective_from=T1,
                effective_until=T2,
                input_per_million_tokens="1.00",
            ),
        ),
    )

    assert manifest.prices[0].input_per_million_tokens == Decimal("1.25")
    assert manifest.prices[1].effective_from == T1


def test_manifest_accepts_open_ended_ranges() -> None:
    manifest = PriceManifest(
        version=1,
        manifest_id="contract-2026",
        prices=(
            _price(effective_from=None, effective_until=T0),
            _price(effective_from=T0, effective_until=None),
        ),
    )

    assert manifest.prices[0].effective_from is None
    assert manifest.prices[1].effective_until is None


@pytest.mark.parametrize("version", [0, 2, "1"])
def test_manifest_rejects_unknown_or_coerced_versions(version) -> None:
    with pytest.raises(ValidationError):
        PriceManifest(version=version, manifest_id="contract-2026", prices=(_price(),))


def test_manifest_rejects_overlapping_ranges() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        PriceManifest(
            version=1,
            manifest_id="contract-2026",
            prices=(
                _price(effective_until=T2),
                _price(effective_from=T1, effective_until=None),
            ),
        )


def test_manifest_rejects_duplicate_ranges() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        PriceManifest(
            version=1,
            manifest_id="contract-2026",
            prices=(_price(), _price()),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_provider", " "),
        ("model", ""),
        ("currency", "usd"),
        ("currency", "US"),
        ("input_per_million_tokens", "-0.01"),
        ("output_per_million_tokens", "NaN"),
        ("input_per_million_tokens", 1.25),
    ],
)
def test_price_rejects_invalid_identity_currency_or_money(field, value) -> None:
    with pytest.raises(ValidationError):
        _price(**{field: value})


def test_price_rejects_empty_or_reversed_range() -> None:
    with pytest.raises(ValidationError, match="effective_until"):
        _price(effective_from=T1, effective_until=T1)
    with pytest.raises(ValidationError, match="effective_until"):
        _price(effective_from=T2, effective_until=T1)


def test_price_rejects_naive_time_bound() -> None:
    with pytest.raises(ValidationError):
        _price(effective_from=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_per_million_tokens", 1),
        ("effective_from", 1_767_225_600),
    ],
)
def test_manifest_json_rejects_values_outside_the_published_schema(
    field, value
) -> None:
    payload = {
        "version": 1,
        "manifest_id": "contract-2026",
        "prices": [_price().model_dump(mode="json")],
    }
    payload["prices"][0][field] = value

    with pytest.raises(ValidationError):
        PriceManifest.model_validate_json(json.dumps(payload))


def test_manifest_rejects_an_empty_price_list() -> None:
    with pytest.raises(ValidationError):
        PriceManifest(version=1, manifest_id="contract-2026", prices=())


def test_manifest_digest_is_entry_order_independent() -> None:
    first = _price(model="gpt-5")
    second = _price(model="gpt-5-mini")

    left = PriceManifest(
        version=1,
        manifest_id="contract-2026",
        prices=(first, second),
    )
    right = PriceManifest(
        version=1,
        manifest_id="contract-2026",
        prices=(second, first),
    )

    assert left.canonical_json() == right.canonical_json()
    assert left.digest == right.digest
    assert len(left.digest) == 64


def test_manifest_digest_normalizes_signed_zero() -> None:
    positive = PriceManifest(
        version=1,
        manifest_id="contract-2026",
        prices=(_price(input_per_million_tokens="0"),),
    )
    negative = PriceManifest(
        version=1,
        manifest_id="contract-2026",
        prices=(_price(input_per_million_tokens="-0.00"),),
    )

    assert positive.canonical_json() == negative.canonical_json()
    assert positive.digest == negative.digest
    assert '"input_per_million_tokens":"0"' in negative.canonical_json()


def test_manifest_is_closed_and_immutable() -> None:
    with pytest.raises(ValidationError):
        PriceManifest(
            version=1,
            manifest_id="contract-2026",
            prices=(_price(),),
            guessed_discount="0.1",
        )

    manifest = PriceManifest(version=1, manifest_id="contract-2026", prices=(_price(),))
    with pytest.raises(ValidationError):
        manifest.manifest_id = "changed"


def test_manifest_has_a_canonical_external_input_schema() -> None:
    contract = next(
        contract
        for contract in CONTRACTS
        if contract.python_type_object is PriceManifest
    )

    assert contract.artifact_family == "external-inputs"
    assert contract.slug == "price-manifest"
    assert contract.version == 1
