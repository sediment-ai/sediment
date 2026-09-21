# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application-service checks for operational reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sediment_derive import Provenance
from sediment_export import OperationalReportScope

from sediment_api.services import operational_reports


@dataclass(frozen=True)
class _Report:
    org_id: str
    provenance: Provenance


def test_lifecycle_service_preserves_scope_report_and_provenance(monkeypatch) -> None:
    store = object()
    mirrors = object()
    scope = OperationalReportScope(
        cohort_start=datetime(2026, 8, 1, tzinfo=UTC),
        cohort_end=datetime(2026, 9, 1, tzinfo=UTC),
        as_of=datetime(2026, 9, 2, tzinfo=UTC),
    )
    report = _Report("acme", Provenance("lifecycle-v1", 7))
    invocations: list[tuple[object, object, str, object]] = []

    def generate(store_arg, mirrors_arg, org_id, *, scope):
        invocations.append((store_arg, mirrors_arg, org_id, scope))
        return report

    monkeypatch.setattr(
        operational_reports, "generate_accepted_work_lifecycle_report", generate
    )

    request = operational_reports.LifecycleReportRequest("acme", scope)
    envelope = operational_reports.generate_lifecycle_report(store, mirrors, request)

    assert invocations == [(store, mirrors, "acme", scope)]
    assert envelope.scope is scope
    assert envelope.report is report
    assert json.loads(envelope.to_json()) == {
        "org_id": "acme",
        "provenance": {
            "policy_version": "lifecycle-v1",
            "quarantine_revision": 7,
            "policy_digest": None,
        },
    }


def test_lifecycle_service_preserves_all_history_request(monkeypatch) -> None:
    report = _Report("acme", Provenance("lifecycle-v1", 0))
    scopes: list[object] = []

    def generate(*args, scope):
        scopes.append(scope)
        return report

    monkeypatch.setattr(
        operational_reports, "generate_accepted_work_lifecycle_report", generate
    )

    request = operational_reports.LifecycleReportRequest("acme")
    envelope = operational_reports.generate_lifecycle_report(
        object(), object(), request
    )

    assert scopes == [None]
    assert envelope.scope is None
