# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for server-side Session identity extraction.

The header-parser cases share their contract with
litellm/tests/test_sediment_callback.py; the header names remain aligned with
shims/pi/lib/provider.ts and the fleet's models.json renderer. The
identity-string and JSON-blob cases are synthetic inputs matching the
wire-verified shapes in the module docstring — no frozen fixture carries those
forms. The Codex-header and explicit-metadata paths assert against the frozen
fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sediment_capture import SessionIdentity, resolve_identity
from sediment_capture.session_identity import (
    _agent_from_sediment_headers,
    _identity_label,
    _session_from_codex_headers,
    _session_from_identity,
    _session_from_sediment_headers,
)

FIXTURES = Path(__file__).parent / "fixtures"

SESSION = "0190f5e0-0000-7000-8000-000000000001"  # uuidv7 shape (pi)
CC_SESSION = "11111111-2222-4333-8444-555555555555"  # Claude Code
CODEX_SESSION = "019f6f4c-810d-7ab1-afad-7d40838a62cd"  # frozen fixture's

# The wire-verified Claude Code identity string shape.
CC_USER = "user_a1b2c3_account_99999999-8888-4777-8666-555555555555"
CC_IDENTITY = f"{CC_USER}_session_{CC_SESSION}"


def _meta(headers: dict[str, str]) -> dict:
    return {"requester_custom_headers": headers}


# Header-parser behavior shared with the callback's suite.


def test_session_header_round_trips_a_pi_uuid() -> None:
    assert (
        _session_from_sediment_headers(_meta({"x-sediment-session": SESSION}))
        == SESSION
    )


def test_session_survives_in_the_slo_metadata_copy() -> None:
    # The SLO's metadata is checked first by the caller; either copy carries.
    assert (
        _session_from_sediment_headers({}, _meta({"x-sediment-session": SESSION}))
        == SESSION
    )


@pytest.mark.parametrize(
    "value",
    ["", "not-a-uuid", SESSION.upper() + "junk", f"  {SESSION}junk"],
)
def test_junk_session_headers_are_refused(value: str) -> None:
    # ADR 0002: a malformed header must not smuggle in a fake session id.
    assert _session_from_sediment_headers(_meta({"x-sediment-session": value})) == ""


def test_agent_header_labels_the_user() -> None:
    assert (
        _agent_from_sediment_headers(_meta({"x-sediment-agent": "docs"}))
        == "agent:docs"
    )


def test_absent_headers_degrade_to_empty() -> None:
    assert (
        _session_from_sediment_headers(None, {}, {"requester_custom_headers": "junk"})
        == ""
    )
    assert _agent_from_sediment_headers(None, {}) == ""


def test_identity_suffix_extracts_the_trailing_session_uuid() -> None:
    assert _session_from_identity(CC_IDENTITY) == CC_SESSION


def test_first_candidate_carrying_a_suffix_wins() -> None:
    assert _session_from_identity("", None, CC_IDENTITY) == CC_SESSION


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        f"user_x_session_{CC_SESSION}_account_y",  # not trailing — anchored
        f"session_{CC_SESSION[:-1]}",  # truncated uuid
        "dev-1",
    ],
)
def test_identity_without_a_trailing_suffix_yields_nothing(value: Any) -> None:
    assert _session_from_identity(value) == ""


def test_json_blob_identity_condenses_to_a_device_label() -> None:
    blob = json.dumps(
        {"device_id": "d3adbeefcafe1234", "account_uuid": "", "session_id": "s"}
    )
    assert _identity_label(blob) == "device_d3adbeefcafe"


def test_blob_without_a_device_falls_back_to_the_account_field() -> None:
    blob = json.dumps({"device_id": "", "account_uuid": "acct-42", "session_id": "s"})
    assert _identity_label(blob) == "account_acct-42"


def test_blob_with_no_usable_field_yields_empty() -> None:
    # session_id is deliberately never a label field — it changes every
    # session and would never be a stable user key.
    assert _identity_label(json.dumps({"session_id": "s"})) == ""


@pytest.mark.parametrize("value", [CC_IDENTITY, "dev-1", "[1, 2]", ""])
def test_non_blob_identities_are_left_alone(value: str) -> None:
    # The identity-string form must reach the session-suffix strip untouched.
    assert _identity_label(value) == value


def test_codex_header_resolves_from_the_frozen_responses_fixture() -> None:
    payload = json.loads(
        (FIXTURES / "litellm_responses_standard_logging_object.json").read_text()
    )
    assert resolve_identity(payload) == SessionIdentity(
        session_id=CODEX_SESSION, user_id=None, source="codex_header"
    )


def test_explicit_metadata_resolves_from_the_frozen_chat_fixture() -> None:
    # Explicit request metadata survives as the SLO's
    # metadata.requester_metadata — the server-side carrier.
    payload = json.loads(
        (FIXTURES / "litellm_standard_logging_object.json").read_text()
    )
    assert resolve_identity(payload) == SessionIdentity(
        session_id="sess-litellm", user_id="dev-1", source="metadata"
    )


def _payload_with_every_source() -> dict:
    return {
        "end_user": CC_IDENTITY,
        "metadata": {
            "requester_metadata": {"session_id": "sess-explicit", "user_id": "dev-9"},
            "requester_custom_headers": {
                "x-codex-turn-metadata": json.dumps({"session_id": CODEX_SESSION}),
                "x-sediment-session": SESSION,
                "x-sediment-agent": "docs",
            },
        },
    }


def test_each_source_beats_the_ones_below_it() -> None:
    payload = _payload_with_every_source()
    assert resolve_identity(payload).session_id == "sess-explicit"

    del payload["metadata"]["requester_metadata"]["session_id"]
    resolved = resolve_identity(payload)
    assert (resolved.session_id, resolved.source) == (CC_SESSION, "identity_suffix")

    payload["end_user"] = "dev-9"
    resolved = resolve_identity(payload)
    assert (resolved.session_id, resolved.source) == (CODEX_SESSION, "codex_header")

    del payload["metadata"]["requester_custom_headers"]["x-codex-turn-metadata"]
    resolved = resolve_identity(payload)
    assert (resolved.session_id, resolved.source) == (SESSION, "sediment_header")

    del payload["metadata"]["requester_custom_headers"]["x-sediment-session"]
    assert resolve_identity(payload) is None


def test_direct_metadata_keys_win_over_the_requester_copy() -> None:
    payload = {
        "metadata": {
            "session_id": "sess-direct",
            "requester_metadata": {"session_id": "sess-nested"},
        }
    }
    assert resolve_identity(payload).session_id == "sess-direct"


@pytest.mark.parametrize(
    "header",
    [
        json.dumps({"session_id": "not-a-uuid"}),
        json.dumps({"session_id": ""}),
        json.dumps({"session_id": CODEX_SESSION + "junk"}),
        "{not json",
        '"a string"',
        "",
    ],
)
def test_junk_codex_session_ids_are_refused(header: str) -> None:
    assert _session_from_codex_headers(_meta({"x-codex-turn-metadata": header})) == ""


def test_junk_headers_never_yield_an_identity() -> None:
    payload = {
        "metadata": _meta(
            {
                "x-codex-turn-metadata": json.dumps({"session_id": "not-a-uuid"}),
                "x-sediment-session": "also-not-a-uuid",
            }
        )
    }
    assert resolve_identity(payload) is None


@pytest.mark.parametrize("payload", [{}, {"metadata": None}, {"end_user": "dev-1"}])
def test_absent_everything_resolves_to_none(payload: dict) -> None:
    assert resolve_identity(payload) is None


def test_session_suffix_is_stripped_from_the_user_id() -> None:
    # The identity string embeds the per-session uuid; user_id must stay
    # stable for the same developer across sessions.
    resolved = resolve_identity({"end_user": CC_IDENTITY})
    assert resolved.session_id == CC_SESSION
    assert resolved.user_id == CC_USER


def test_bare_session_identity_leaves_user_id_absent() -> None:
    resolved = resolve_identity({"end_user": f"session_{CC_SESSION}"})
    assert resolved.session_id == CC_SESSION
    assert resolved.user_id is None


def test_json_blob_end_user_is_condensed_not_stored_raw() -> None:
    blob = json.dumps(
        {"device_id": "d3adbeefcafe1234", "account_uuid": "", "session_id": "s"}
    )
    payload = {
        "end_user": blob,
        "metadata": _meta({"x-sediment-session": SESSION}),
    }
    assert resolve_identity(payload).user_id == "device_d3adbeefcafe"


def test_json_blob_end_user_resolves_the_api_key_session() -> None:
    blob = json.dumps(
        {
            "device_id": "d3adbeefcafe1234",
            "account_uuid": "",
            "session_id": CC_SESSION,
        }
    )
    assert resolve_identity({"end_user": blob}) == SessionIdentity(
        session_id=CC_SESSION,
        user_id="device_d3adbeefcafe",
        source="identity_json",
    )


def test_metadata_user_id_beats_end_user() -> None:
    payload = {
        "end_user": "dev-enduser",
        "metadata": {
            "requester_metadata": {"session_id": "sess-x", "user_id": "dev-meta"}
        },
    }
    assert resolve_identity(payload).user_id == "dev-meta"


def test_agent_header_fills_a_user_id_every_other_source_left_blank() -> None:
    payload = {
        "metadata": _meta({"x-sediment-session": SESSION, "x-sediment-agent": "docs"})
    }
    assert resolve_identity(payload) == SessionIdentity(
        session_id=SESSION, user_id="agent:docs", source="sediment_header"
    )


def test_whitespace_identity_values_are_absent() -> None:
    """Whitespace-only values fall through the source chain instead of
    surfacing as padded ids that raise at fact construction. Deliberate delta
    from the callback, which returned them raw (they then 422'd at the
    envelope's NonEmptyId — a gate the server-side path no longer passes
    through)."""
    assert resolve_identity({"metadata": {"session_id": "   "}}) is None
    assert (
        resolve_identity({"metadata": {"requester_metadata": {"session_id": " "}}})
        is None
    )
    # Whitespace metadata session falls through to a real lower source.
    got = resolve_identity(
        {
            "metadata": {
                "session_id": "  ",
                "requester_metadata": {"session_id": "sess-real"},
            }
        }
    )
    assert got is not None and got.session_id == "sess-real"
    # Whitespace end_user leaves the user absent.
    got = resolve_identity({"metadata": {"session_id": "sess-1"}, "end_user": "   "})
    assert got is not None and got.user_id is None


@pytest.mark.parametrize("invalid", [{"nested": True}, ["session"], 42, True])
@pytest.mark.parametrize("nested", [False, True])
def test_non_string_metadata_falls_through_to_captured_header(invalid, nested):
    meta = _meta({"x-sediment-session": SESSION, "x-sediment-agent": "docs"})
    target = meta.setdefault("requester_metadata", {}) if nested else meta
    target.update(session_id=invalid, user_id=invalid)
    assert resolve_identity({"metadata": meta, "end_user": invalid}) == SessionIdentity(
        session_id=SESSION, user_id="agent:docs", source="sediment_header"
    )
    meta.pop("requester_custom_headers")
    assert resolve_identity({"metadata": meta, "end_user": invalid}) is None


def test_invalid_direct_metadata_preserves_valid_requester_identity():
    assert resolve_identity(
        {
            "metadata": {
                "session_id": {"nested": True},
                "user_id": ["developer"],
                "requester_metadata": {
                    "session_id": "team-session",
                    "user_id": "developer",
                },
            }
        }
    ) == SessionIdentity("team-session", "developer", "metadata")


def test_non_string_agent_and_device_labels_are_absent():
    meta = _meta({"x-sediment-session": SESSION, "x-sediment-agent": {"name": "docs"}})
    assert resolve_identity({"metadata": meta}).user_id is None
    assert (
        _identity_label(json.dumps({"device_id": ["device"], "account_uuid": "acct"}))
        == "account_acct"
    )
