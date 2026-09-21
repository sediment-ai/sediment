# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test: the ``/query/ci/failures`` cursor fingerprint must be
built over the datetime *instants* of the request bounds, not their UTC-offset
*spelling*.

``_ci_filter_key`` serializes ``captured_after``/``captured_before`` into the
opaque cursor's embedded fingerprint, and ``_decode_ci_cursor`` rejects a
continuation request whose re-derived fingerprint doesn't match the one the
cursor was minted with. Two requests that denote the same window in different
offsets (e.g. ``+00:00`` and ``-06:00``) are the same query scope -- the
endpoint already treats the bounds as instants for its ``>=`` check and for
the SQL predicates -- so the fingerprint must treat them as identical too,
matching how ``RepoSlug``/``WorkflowName`` are already canonicalized before
reaching it.

This seeds two CI outcomes, mints a page-1 cursor with the bounds expressed in
UTC, then continues with the same instants re-spelled at ``-06:00`` and asserts
the continuation is accepted (200) and yields the expected second page.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from sediment_core import CIOutcome, CIProvider, CIResult  # noqa: E402
from sediment_api.main import app  # noqa: E402

ORG = "testorg"
REPO = "testorg/test-repo"
AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}


def _ci_outcome(**overrides) -> CIOutcome:
    values = {
        "outcome_id": "outcome-repro",
        "org_id": ORG,
        "provider": CIProvider.GITHUB_ACTIONS,
        "run_id": "run/42",
        "run_attempt": 2,
        "repo": REPO,
        "commit_sha": "c" * 40,
        "branch": "main",
        "result": CIResult.FAILED,
        "workflow_name": "CI",
        "captured_at": "2026-09-05T12:00:00+00:00",
        "raw": {"logs": "sensitive"},
    }
    values.update(overrides)
    return CIOutcome(**values)


def test_ci_cursor_accepts_equivalent_window_re_spelled_at_a_different_offset(
    client: TestClient,
) -> None:
    store = app.state.fact_store
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="repro-1",
            run_id="run/repro-1",
            captured_at="2026-09-05T12:01:00+00:00",
        )
    )
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="repro-2",
            run_id="run/repro-2",
            captured_at="2026-09-05T12:02:00+00:00",
        )
    )

    # Same query window, expressed twice in different offsets.
    params_page1 = {
        "repo": REPO,
        "captured_after": "2026-09-05T11:00:00+00:00",
        "captured_before": "2026-09-05T13:00:00+00:00",
        "limit": 1,
    }
    params_page2 = {
        "repo": REPO,
        "captured_after": "2026-09-05T05:00:00-06:00",
        "captured_before": "2026-09-05T07:00:00-06:00",
        "limit": 1,
    }

    first = client.get("/query/ci/failures", params=params_page1, headers=AUTH)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    assert [row["outcome_id"] for row in first.json()["outcomes"]] == ["repro-2"]

    second = client.get(
        "/query/ci/failures",
        params={**params_page2, "cursor": cursor},
        headers=AUTH,
    )

    # Keep the diagnostic prints the original reproduction used so ``-s`` output
    # stays comparable across the fix; the assertion is the corrected behavior.
    print(f"PAGE2_STATUS: {second.status_code}")
    print(f"PAGE2_BODY: {second.json()}")
    assert second.status_code == 200
    assert [row["outcome_id"] for row in second.json()["outcomes"]] == ["repro-1"]
    assert second.json()["next_cursor"] is None


def test_ci_cursor_still_rejects_a_genuinely_changed_window(
    client: TestClient,
) -> None:
    """Normalization is over the instant, not a free pass: a cursor minted on
    one window must still 422 when the continuation request moves the window
    (a real semantic change), so the fix doesn't weaken the binding."""
    store = app.state.fact_store
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="reject-1",
            run_id="run/reject-1",
            captured_at="2026-09-05T12:01:00+00:00",
        )
    )
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="reject-2",
            run_id="run/reject-2",
            captured_at="2026-09-05T12:02:00+00:00",
        )
    )

    page1 = {
        "repo": REPO,
        "captured_after": "2026-09-05T11:00:00+00:00",
        "captured_before": "2026-09-05T13:00:00+00:00",
        "limit": 1,
    }
    first = client.get("/query/ci/failures", params=page1, headers=AUTH)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    # A genuinely different window (not an offset re-spelling) stays bound to
    # the original cursor's fingerprint and is rejected.
    moved = {
        "repo": REPO,
        "captured_after": "2026-09-05T11:30:00+00:00",
        "captured_before": "2026-09-05T13:30:00+00:00",
        "limit": 1,
        "cursor": cursor,
    }
    moved_resp = client.get("/query/ci/failures", params=moved, headers=AUTH)
    assert moved_resp.status_code == 422
    assert moved_resp.json() == {"detail": "CI outcome cursor doesn't match filters"}
