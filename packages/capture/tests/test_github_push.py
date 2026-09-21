# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub push → Push fact translation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sediment_capture import parse_push
from sediment_core import ForgeProvider

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "github_push.json").read_text())


def test_parse_push_normalizes_payload() -> None:
    push = parse_push(_payload(), org_id="acme-corp")
    assert push is not None
    assert push.org_id == "acme-corp"
    assert push.provider is ForgeProvider.GITHUB
    assert push.repo == "acme-corp/backend-service"
    assert push.clone_url == "https://github.com/acme-corp/backend-service.git"
    assert push.ref == "refs/heads/main"
    assert push.before_sha == "9049f1265b7d61be4a8904a9a27120d2064dab3b"
    assert push.after_sha == "abc123def456abc123def456abc123def456abc1"
    assert push.forced is False


def test_forced_push_flag() -> None:
    payload = _payload()
    payload["forced"] = True
    push = parse_push(payload, org_id="acme-corp")
    assert push is not None
    assert push.forced is True


def test_forced_is_exact_bool_not_truthy() -> None:
    # A crafted "forced": "false" must not invert to True.
    payload = _payload()
    payload["forced"] = "false"
    push = parse_push(payload, org_id="acme-corp")
    assert push is not None
    assert push.forced is False


def test_branch_deletion_returns_none() -> None:
    # All-zeros sha at either hash size (SHA-1, SHA-256)…
    for after in ("0" * 40, "0" * 64):
        payload = _payload()
        payload["after"] = after
        assert parse_push(payload, org_id="acme-corp") is None
    # …and the documented `deleted` flag, independent of the sha.
    payload = _payload()
    payload["deleted"] = True
    assert parse_push(payload, org_id="acme-corp") is None


def test_falsy_or_missing_after_returns_none() -> None:
    for after in (None, "", 0, False):
        payload = _payload()
        payload["after"] = after
        assert parse_push(payload, org_id="acme-corp") is None
    payload = _payload()
    del payload["after"]
    assert parse_push(payload, org_id="acme-corp") is None


def test_non_branch_ref_returns_none() -> None:
    # Tag/other-ref pushes re-point at commits already seen on a branch.
    for ref in ("refs/tags/v1.0.0", "refs/notes/sediment", None):
        payload = _payload()
        payload["ref"] = ref
        assert parse_push(payload, org_id="acme-corp") is None


def test_non_dict_repository_is_coerced_not_rejected() -> None:
    payload = _payload()
    payload["repository"] = "corrupted"
    push = parse_push(payload, org_id="acme-corp")
    assert push is not None
    assert push.repo == ""


def test_junk_shas_skip_and_warn(caplog: pytest.LogCaptureFixture) -> None:
    # A before/after that is not a full-length commit sha skips the push
    # (both are uq_pushes_natural components) instead of raising at
    # construction.
    payload = _payload()
    payload["after"] = 123
    payload["before"] = 456
    with caplog.at_level(logging.WARNING, logger="sediment.capture.github"):
        assert parse_push(payload, org_id="acme-corp") is None
    assert "push_invalid_sha" in caplog.text


def test_parsed_push_stores_and_redelivery_collapses(postgres_store) -> None:
    store = postgres_store
    first = parse_push(_payload(), org_id="acme-corp")
    redelivered = parse_push(_payload(), org_id="acme-corp")
    assert first is not None
    assert redelivered is not None
    assert store.store_push(first) is True
    assert store.store_push(redelivered) is False  # uq_pushes_natural collapse
    assert store.read_pushes("acme-corp") == [first]
