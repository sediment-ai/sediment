# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compiled contract reuse never reuses a caller's evidence values."""

from dataclasses import dataclass
from typing import Annotated

import pytest
from pydantic import ConfigDict, Field, ValidationError

from sediment_core import evidence


@dataclass(frozen=True)
class CountPacket:
    __pydantic_config__ = ConfigDict(extra="forbid")
    count: Annotated[int, Field(strict=True)]


def test_encoder_builds_class_contract_once_but_validates_every_value(monkeypatch):
    original = evidence.TypeAdapter
    built = []

    def record(contract):
        built.append(contract)
        return original(contract)

    monkeypatch.setattr(evidence, "TypeAdapter", record)
    assert evidence.encode_evidence_json(CountPacket(1), CountPacket) == b'{"count":1}'
    assert evidence.encode_evidence_json(CountPacket(2), CountPacket) == b'{"count":2}'
    with pytest.raises(ValidationError):
        evidence.encode_evidence_json({"count": "3"}, CountPacket)
    with pytest.raises(ValidationError):
        evidence.encode_evidence_json({"count": 3, "extra": True}, CountPacket)
    assert built.count(CountPacket) == 1


def test_encoder_retains_nonclass_contracts_with_unhashable_metadata():
    contract = Annotated[int, {"description": "nonclass contract"}]
    assert evidence.encode_evidence_json(7, contract) == b"7"
    with pytest.raises(ValidationError):
        evidence.encode_evidence_json("invalid", contract)
