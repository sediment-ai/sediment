# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI boundary checks for the JSON-only lifecycle report."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sediment_api.reports import lifecycle_report
from sediment_api.services.operational_reports import LifecycleReportEnvelope


def test_lifecycle_report_requires_json() -> None:
    with pytest.raises(SystemExit) as exc_info:
        lifecycle_report.build_parser().parse_args(["--org", "acme"])
    assert exc_info.value.code == 2


def test_lifecycle_report_accepts_json() -> None:
    args = lifecycle_report.build_parser().parse_args(["--org", "acme", "--json"])
    assert args.org == "acme"
    assert args.json is True


@dataclass(frozen=True)
class _Report:
    org_id: str


def test_lifecycle_report_prints_one_json_object(monkeypatch, capsys) -> None:
    @contextmanager
    def store(*args, **kwargs):
        yield SimpleNamespace()

    monkeypatch.setattr(lifecycle_report, "one_shot_fact_store", store)
    monkeypatch.setattr(lifecycle_report, "MirrorManager", lambda path: object())
    envelope = LifecycleReportEnvelope(None, _Report("acme"))
    monkeypatch.setattr(
        lifecycle_report,
        "generate_lifecycle_report",
        lambda *args, **kwargs: envelope,
    )

    assert lifecycle_report.main(["--org", "acme", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{envelope.to_json()}\n"
    assert captured.err == ""


def test_lifecycle_report_sends_generation_error_to_stderr(monkeypatch, capsys) -> None:
    @contextmanager
    def store(*args, **kwargs):
        yield SimpleNamespace()

    monkeypatch.setattr(lifecycle_report, "one_shot_fact_store", store)
    monkeypatch.setattr(lifecycle_report, "MirrorManager", lambda path: object())

    def fail(*args, **kwargs):
        raise ValueError("broken evidence")

    monkeypatch.setattr(lifecycle_report, "generate_lifecycle_report", fail)

    assert lifecycle_report.main(["--org", "acme", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "broken evidence" in captured.err
