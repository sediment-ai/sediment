# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned operator-supplied model prices for operational analysis."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Price = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PRICE_DECIMAL_PATTERN = r"^(?:-0(?:\.0+)?|0(?:\.\d+)?|[1-9]\d*(?:\.\d+)?)$"


class ModelPrice(BaseModel):
    """One model price over a half-open applicability interval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_provider: NonEmptyText
    model: NonEmptyText
    currency: CurrencyCode
    effective_from: AwareDatetime | None
    effective_until: AwareDatetime | None
    input_per_million_tokens: Price
    output_per_million_tokens: Price

    @field_validator(
        "input_per_million_tokens", "output_per_million_tokens", mode="before"
    )
    @classmethod
    def validate_price_input(cls, value: object) -> object:
        """Require the exact decimal-string wire contract or a Decimal value."""

        if isinstance(value, str):
            if re.fullmatch(PRICE_DECIMAL_PATTERN, value) is None:
                raise ValueError("prices must be nonnegative plain decimal strings")
            return value
        if not isinstance(value, Decimal):
            raise ValueError("prices must use decimal strings")
        return value

    @field_validator("input_per_million_tokens", "output_per_million_tokens")
    @classmethod
    def normalize_price(cls, value: Decimal) -> Decimal:
        return Decimal(0) if value == 0 else value.normalize()

    @field_validator("effective_from", "effective_until", mode="before")
    @classmethod
    def validate_time_input(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("time bounds must use RFC 3339 strings")
        return value

    @field_validator("effective_from", "effective_until")
    @classmethod
    def normalize_time(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_interval(self) -> ModelPrice:
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_from >= self.effective_until
        ):
            raise ValueError("effective_until must be later than effective_from")
        return self

    @field_serializer("input_per_million_tokens", "output_per_million_tokens")
    def serialize_price(self, value: Decimal) -> str:
        return format(value, "f")


class PriceManifest(BaseModel):
    """One immutable external price policy used by cost Derivations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    manifest_id: NonEmptyText
    prices: Annotated[tuple[ModelPrice, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def reject_overlapping_prices(self) -> PriceManifest:
        grouped: dict[tuple[str, str, str], list[ModelPrice]] = defaultdict(list)
        for price in self.prices:
            grouped[(price.model_provider, price.model, price.currency)].append(price)

        for key, prices in grouped.items():
            ordered = sorted(
                prices,
                key=lambda price: (
                    price.effective_from is not None,
                    price.effective_from,
                ),
            )
            previous = ordered[0]
            for current in ordered[1:]:
                if previous.effective_until is None or (
                    current.effective_from is None
                    or current.effective_from < previous.effective_until
                ):
                    provider, model, currency = key
                    raise ValueError(
                        f"price intervals overlap for {provider}/{model}/{currency}"
                    )
                previous = current
        return self

    def canonical_json(self) -> str:
        """Return stable JSON independent of entry input order."""

        prices = sorted(
            (price.model_dump(mode="json") for price in self.prices),
            key=lambda price: (
                price["model_provider"],
                price["model"],
                price["currency"],
                price["effective_from"] or "",
                price["effective_until"] or "",
                price["input_per_million_tokens"],
                price["output_per_million_tokens"],
            ),
        )
        return json.dumps(
            {
                "manifest_id": self.manifest_id,
                "prices": prices,
                "version": self.version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def digest(self) -> str:
        """SHA-256 identity of the canonical manifest content."""

        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
