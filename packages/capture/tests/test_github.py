# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub workflow_run → CIOutcome translation and signature verification."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sediment_capture import parse_workflow_run, sign_payload, verify_signature
from sediment_core import CIProvider, CIResult

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "github_workflow_run.json").read_text())


def test_parse_workflow_run_populates_fact_and_check_identity() -> None:
    outcome = parse_workflow_run(
        _payload(), org_id="acme-corp", source_event_id="delivery-1"
    )
    assert outcome is not None
    assert outcome.org_id == "acme-corp"
    assert outcome.provider is CIProvider.GITHUB_ACTIONS
    assert outcome.repo == "acme-corp/backend-service"
    assert outcome.commit_sha == "abc123def456abc123def456abc123def456abc1"
    assert outcome.branch == "main"
    assert outcome.result is CIResult.PASSED
    assert outcome.run_id == "12345678"
    assert outcome.run_attempt == 1
    assert outcome.workflow_id == "161335"
    assert outcome.provider_result == "success"
    assert outcome.source_event_type == "github.workflow_run.completed"
    assert outcome.source_spec_version is None
    assert outcome.source_event_id == "delivery-1"
    assert outcome.run_url == (
        "https://github.com/acme-corp/backend-service/actions/runs/12345678"
    )
    assert outcome.pr_number is None
    # Check identity: fields + the full workflow_run object on raw.
    assert outcome.workflow_name == "CI"
    assert outcome.workflow_path == ".github/workflows/ci.yml"
    assert outcome.raw == _payload()["workflow_run"]


def test_failure_conclusion_maps_to_failed() -> None:
    payload = _payload()
    payload["workflow_run"]["conclusion"] = "failure"
    outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.result is CIResult.FAILED


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("cancelled", CIResult.CANCELLED),
        ("skipped", CIResult.SKIPPED),
        ("timed_out", CIResult.TIMED_OUT),
        ("neutral", CIResult.NEUTRAL),
        ("action_required", CIResult.UNKNOWN),
        ("startup_failure", CIResult.UNKNOWN),
        (None, CIResult.UNKNOWN),
    ],
)
def test_terminal_conclusion_mapping(
    conclusion: str | None, expected: CIResult
) -> None:
    payload = _payload()
    payload["workflow_run"]["conclusion"] = conclusion
    outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.result is expected
    assert outcome.provider_result == conclusion


def test_missing_run_attempt_stays_absent() -> None:
    payload = _payload()
    del payload["workflow_run"]["run_attempt"]
    outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.run_attempt is None


def test_missing_or_malformed_run_id_skips() -> None:
    for run_id in (None, "", "   ", True, {"id": 1}):
        payload = _payload()
        payload["workflow_run"]["id"] = run_id
        assert parse_workflow_run(payload, org_id="acme-corp") is None


def test_non_completed_action_returns_none() -> None:
    payload = _payload()
    payload["action"] = "requested"
    assert parse_workflow_run(payload, org_id="acme-corp") is None


def test_missing_head_sha_returns_none() -> None:
    payload = _payload()
    del payload["workflow_run"]["head_sha"]
    assert parse_workflow_run(payload, org_id="acme-corp") is None


def test_missing_name_and_path_degrade_to_defaults() -> None:
    payload = _payload()
    del payload["workflow_run"]["name"]
    del payload["workflow_run"]["path"]
    outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.workflow_name == ""
    assert outcome.workflow_path is None


def test_missing_repo_still_produces_fact_but_leaves_a_trail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    del payload["repository"]
    with caplog.at_level(logging.WARNING, logger="sediment.capture.github"):
        outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.repo == ""
    assert "workflow_run_missing_repo" in caplog.text


def test_pr_number_extracted_when_present() -> None:
    payload = _payload()
    payload["workflow_run"]["pull_requests"] = [{"number": 41}]
    outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.pr_number == 41


def test_pr_number_malformed_entry_is_none() -> None:
    payload = _payload()
    payload["workflow_run"]["pull_requests"] = ["not-a-dict"]
    outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.pr_number is None


def test_pr_number_bool_or_out_of_range_degrades_to_none() -> None:
    # bool is an int subclass (True → pull request number 1); 2**70 overflows BIGINT
    # at INSERT. Both must degrade, not crash or misattribute.
    for number in (True, False, 2**70, 0, -1):
        payload = _payload()
        payload["workflow_run"]["pull_requests"] = [{"number": number}]
        outcome = parse_workflow_run(payload, org_id="acme-corp")
        assert outcome is not None
        assert outcome.pr_number is None


def test_non_dict_workflow_run_returns_none_not_raises() -> None:
    payload = _payload()
    payload["workflow_run"] = "corrupted"
    assert parse_workflow_run(payload, org_id="acme-corp") is None


def test_non_string_scalar_fields_do_not_raise() -> None:
    # A crafted, signature-valid payload must degrade, never raise.
    # head_sha stays valid here: a junk sha skips the whole fact (covered
    # below), which would mask the per-field coercions.
    payload = _payload()
    run = payload["workflow_run"]
    run["head_branch"] = ["main"]
    run["name"] = 7
    run["path"] = 7
    run["html_url"] = 7
    run["conclusion"] = {"nested": "dict"}
    outcome = parse_workflow_run(payload, org_id="acme-corp")
    assert outcome is not None
    assert outcome.workflow_name == "7"  # str fields coerce
    assert outcome.workflow_path is None  # str-or-None fields drop non-strings
    assert outcome.run_url is None
    assert outcome.result is CIResult.UNKNOWN


def test_junk_head_sha_skips_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    # A head_sha that is not a full-length commit sha skips the fact (it could
    # never join a commit) instead of raising at construction.
    for head_sha in (123, "deadbeef", "g" * 40):
        payload = _payload()
        payload["workflow_run"]["head_sha"] = head_sha
        with caplog.at_level(logging.WARNING, logger="sediment.capture.github"):
            assert parse_workflow_run(payload, org_id="acme-corp") is None
    assert "workflow_run_invalid_sha" in caplog.text


def test_signature_round_trip_and_tamper_detection() -> None:
    body = b'{"action": "completed"}'
    header = sign_payload(body, "webhook-secret")
    assert verify_signature(body, header, "webhook-secret") is True
    assert verify_signature(b'{"action": "other"}', header, "webhook-secret") is False
    assert verify_signature(body, header, "wrong-secret") is False


def test_non_ascii_signature_header_is_rejected_not_crash() -> None:
    # hmac.compare_digest raises TypeError on non-ASCII str args; the
    # bytes-compare must reject, not crash the caller.
    assert verify_signature(b"{}", "sha256=éé", "secret") is False
    assert verify_signature(b"{}", "", "secret") is False


def test_parsed_outcome_stores_and_redelivery_collapses(postgres_store) -> None:
    store = postgres_store
    first = parse_workflow_run(_payload(), org_id="acme-corp")
    redelivered = parse_workflow_run(_payload(), org_id="acme-corp")
    assert first is not None
    assert redelivered is not None
    assert store.store_ci_outcome(first) is True
    assert store.store_ci_outcome(redelivered) is False  # uq_ci_run collapse
    assert store.read_ci_outcomes("acme-corp") == [first]


def test_missing_html_url_keeps_location_absent_and_run_id_still_collapses(
    postgres_store,
) -> None:
    # Without run identity the fact would be keyless under uq_ci_run and a
    # GitHub redelivery would double-insert; the run id fills that gap.
    store = postgres_store

    def degraded() -> dict[str, Any]:
        payload = _payload()
        del payload["workflow_run"]["html_url"]
        return payload

    first = parse_workflow_run(degraded(), org_id="acme-corp")
    redelivered = parse_workflow_run(degraded(), org_id="acme-corp")
    assert first is not None
    assert redelivered is not None
    assert first.run_url is None
    assert store.store_ci_outcome(first) is True
    assert store.store_ci_outcome(redelivered) is False


def test_whitespace_workflow_path_degrades_to_none() -> None:
    # workflow_path's absent form is None, never "" — a stored "" would be
    # a second spelling of absent in the lineage key.
    payload = _payload()
    payload["workflow_run"]["path"] = "   "
    outcome = parse_workflow_run(payload, org_id="acme")
    assert outcome is not None
    assert outcome.workflow_path is None
