# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator rendering for the merge-retention diagnostic."""

from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace

from sediment_api.reports import merge_retention_report as cli
from sediment_derive import (
    AttributionSource,
    MergeRetention,
    MergeRetentionResult,
    Provenance,
)
from sediment_export import MergeRetentionReportResult, build_merge_retention_report


def test_merge_retention_table_reports_empty_data_and_provenance(capsys) -> None:
    provenance = Provenance(policy_version="1", quarantine_revision=2)
    report = build_merge_retention_report(
        "acme",
        MergeRetentionResult(
            provenance=provenance,
            attributed_candidates=4,
            membership=Counter({"attribution_without_merge": 4}),
        ),
        explicit_accepted_inference_call_ids=set(),
        attribution_skipped=Counter(),
        decision_attachment_skipped=Counter({"ambiguous_decision_call_id": 1}),
        attribution_provenance=provenance,
    )

    cli._print_table(report)

    output = capsys.readouterr().out
    assert "attributed file candidates: 0" in output
    assert "scores: no data" in output
    assert "membership: none" in output
    assert "decision attachment skips: ambiguous_decision_call_id=1" in output
    assert "policy_version=1 quarantine_revision=2" in output


def _row() -> MergeRetention:
    return MergeRetention(
        org_id="acme",
        repo="acme/backend",
        pr_number=41,
        merge_id="merge-41",
        inference_call_id="inference-1",
        session_id="session-1",
        source_commit_sha="a" * 40,
        source_file_path="service.py",
        head_commit_sha="b" * 40,
        head_file_path="service.py",
        merge_commit_sha="c" * 40,
        merge_file_path="service.py",
        head_retention_score=1.0,
        merge_retention_score=0.9,
        attribution_source=AttributionSource.GIT_NOTES,
        attribution_similarity_score=1.0,
        provenance=Provenance(policy_version="1", quarantine_revision=2),
    )


def _result(rows: tuple[MergeRetention, ...]) -> MergeRetentionReportResult:
    provenance = Provenance(policy_version="1", quarantine_revision=2)
    retention_result = MergeRetentionResult(provenance=provenance, rows=list(rows))
    return MergeRetentionReportResult(
        report=build_merge_retention_report(
            "acme",
            retention_result,
            explicit_accepted_inference_call_ids=set(),
            attribution_skipped=Counter(),
            decision_attachment_skipped=Counter(),
            attribution_provenance=provenance,
        ),
        rows=rows,
    )


def _patch_generation(monkeypatch, result: MergeRetentionReportResult) -> None:
    @contextmanager
    def store(*args, **kwargs):
        yield SimpleNamespace()

    monkeypatch.setattr(cli, "one_shot_fact_store", store)
    monkeypatch.setattr(
        cli, "generate_merge_retention_report_result", lambda *args, **kwargs: result
    )
    monkeypatch.setattr(cli, "MirrorManager", lambda path: SimpleNamespace())


def test_rows_out_preserves_json_stdout_and_reports_destination(
    tmp_path, monkeypatch, capsys
) -> None:
    result = _result((_row(),))
    _patch_generation(monkeypatch, result)
    destination = tmp_path / "rows.jsonl"
    common_args = [
        "--org",
        "acme",
        "--database-url",
        "postgresql://unused",
        "--json",
    ]

    assert cli.main(common_args) == 0
    aggregate_only = capsys.readouterr()

    assert cli.main([*common_args, "--rows-out", str(destination)]) == 0

    output = capsys.readouterr()
    assert output.out == aggregate_only.out
    assert output.err != aggregate_only.err
    assert "wrote 1 merge-retention rows" in output.err
    assert str(destination) in output.err
    assert destination.read_text().count("\n") == 1


def test_empty_rows_out_leaves_existing_destination_untouched(
    tmp_path, monkeypatch, capsys
) -> None:
    _patch_generation(monkeypatch, _result(()))
    destination = tmp_path / "rows.jsonl"
    destination.write_text("earlier artifact\n")

    assert (
        cli.main(
            [
                "--org",
                "acme",
                "--database-url",
                "postgresql://unused",
                "--rows-out",
                str(destination),
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert "scores: no data" in output.out
    assert "wrote 0 merge-retention rows" in output.err
    assert destination.read_text() == "earlier artifact\n"


def test_rows_out_write_failure_preserves_existing_destination(
    tmp_path, monkeypatch, capsys
) -> None:
    _patch_generation(monkeypatch, _result((_row(),)))
    destination = tmp_path / "rows.jsonl"
    destination.write_text("earlier artifact\n")
    monkeypatch.setattr(
        cli,
        "merge_retention_to_export_rows",
        lambda rows: [SimpleNamespace(split="train", body={"bad": object()})],
    )

    assert (
        cli.main(
            [
                "--org",
                "acme",
                "--database-url",
                "postgresql://unused",
                "--rows-out",
                str(destination),
            ]
        )
        == 1
    )

    output = capsys.readouterr()
    assert "couldn't write merge-retention rows" in output.err
    assert destination.read_text() == "earlier artifact\n"
