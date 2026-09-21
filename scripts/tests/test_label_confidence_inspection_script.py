# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sediment_api/reports/label_confidence_inspection.py: the CLI wraps
``sediment_export.generate_label_confidence_inspection`` — real git fixtures prove the
wiring end to end (store -> mirrors -> attributed_completions -> stratified sample ->
stdout); the aggregation/stratification logic itself is covered by
``packages/export/tests/test_label_confidence_inspection.py`` against the pure
functions.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sediment_api.reports import label_confidence_inspection
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)
from sediment_derive import MirrorManager

ORG = "acme-corp"
REPO = "acme-corp/backend-service"


main = label_confidence_inspection.main


def _report(database_url: str, mirror_path: str, *extra: str) -> int:
    return main(
        [
            "--org",
            ORG,
            "--database-url",
            database_url,
            "--mirror-path",
            mirror_path,
            *extra,
        ]
    )


def _empty_database(postgres_store_factory) -> str:
    database_url, _ = postgres_store_factory()
    return database_url


# Real-git helpers, kept inline — same convention as test_model_report.py.


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _work_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    return work


def _commit(work: Path, filename: str, content: str, message: str) -> str:
    (work / filename).write_text(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD").strip()


def _note(work: Path, session_id: str, commit_sha: str) -> None:
    payload = json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": session_id,
                    "stamped_at": "2026-07-15T00:00:00+00:00",
                }
            ],
        }
    )
    _git(work, "notes", "--ref=sediment", "add", "-m", payload, commit_sha)


def _seed_stratified_dataset(postgres_store_factory, tmp_path: Path) -> tuple[str, str]:
    """Seeds one repo with several commits spanning multiple real ladder
    branches: an explicit accept w/ CI pass, an explicit reject, an
    implicit accept, and a survival-only (no decision, CI pass) attributed_completion.

    Each commit is pushed (and mirrored) individually with its own
    ``before_sha``/``after_sha`` pair: ``list_push_commits`` degrades a
    zero/unknown ``before_sha`` to head-only (branch-creation semantics,
    ``mirror.py``), so a single push spanning all 4 commits would only ever
    attribute the last one. Returns ``(database_url, mirror_path)``."""
    work = _work_repo(tmp_path)
    database_url, store = postgres_store_factory()
    mirror_path = str(tmp_path / "mirrors")
    mirrors = MirrorManager(mirror_path)

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))

    heads: list[str] = []
    prev_head = "0" * 40

    def _seed_one(
        *,
        idx: int,
        session_id: str,
        decision: tuple[bool, bool] | None,  # (accepted, explicit)
        ci_result: CIResult | None,
    ) -> None:
        nonlocal prev_head
        filename = f"file_{idx}.py"
        content = f"def f_{idx}():\n    return {idx}\n"
        head = _commit(work, filename, content, f"commit {idx}")
        _note(work, session_id, head)
        heads.append(head)

        _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
        _git(work, "push", "-q", "-f", str(remote), "refs/notes/*:refs/notes/*")
        push = Push(
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            clone_url=str(remote),
            ref="refs/heads/main",
            before_sha=prev_head,
            after_sha=head,
        )
        mirrors.ensure(push)
        store.store_push(push)
        prev_head = head

        inference_call = InferenceCall(
            org_id=ORG,
            session_id=session_id,
            user_id="dev",
            gateway_provider=GatewayProvider.LITELLM,
            model="claude-sonnet-5",
            input_messages=[
                InferenceMessage(
                    role="user", parts=[TextPart(content=f"write f_{idx}")]
                )
            ],
            output_messages=[
                InferenceMessage(role="assistant", parts=[TextPart(content=content)])
            ],
            input_tokens=10,
            output_tokens=20,
            duration_ms=50,
            model_call_id=f"call-{idx}",
            observed_at=datetime.now(UTC),
        )
        store.store_inference_call(inference_call)

        if decision is not None:
            accepted, explicit = decision
            store.store_decision(
                DeveloperDecision(
                    org_id=ORG,
                    session_id=session_id,
                    user_id="dev",
                    agent_harness=AgentHarness.CLAUDE_CODE,
                    file_path=filename,
                    accepted=accepted,
                    explicit=explicit,
                    interaction_mode=InteractionMode.AGENT,
                    call_id=f"call-{idx}",
                    occurred_at=datetime.now(UTC),
                )
            )
        if ci_result is not None:
            store.store_ci_outcome(
                CIOutcome(
                    org_id=ORG,
                    provider=CIProvider.GITHUB_ACTIONS,
                    run_id=f"run/{idx}",
                    repo=REPO,
                    commit_sha=head,
                    branch="main",
                    result=ci_result,
                    run_url=f"run/{idx}",
                )
            )

    _seed_one(
        idx=1, session_id="sess-1", decision=(True, True), ci_result=CIResult.PASSED
    )
    _seed_one(idx=2, session_id="sess-2", decision=(False, True), ci_result=None)
    _seed_one(idx=3, session_id="sess-3", decision=(True, False), ci_result=None)
    _seed_one(idx=4, session_id="sess-4", decision=None, ci_result=CIResult.PASSED)

    return database_url, mirror_path


def test_json_output_is_valid_json_with_breakdown_fields(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_stratified_dataset(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path, "--json") == 0

    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 4
    for row in rows:
        assert row["recipe_id"] == "sft_curated"
        assert row["recipe_version"] == 1
        assert row["eligibility_source"] in {
            None,
            "explicit_accept",
            "edit_retention",
        }
        assert row["human_judgment"] is None
        assert "decision_branch" in row
        assert "ci_bucket" in row
        if row["confidence"] is not None:
            assert set(row["confidence"]) == {
                "decision_factor",
                "ci_factor",
                "ci_reliability",
                "similarity_discount",
                "final",
            }


def test_stratified_sample_covers_multiple_real_branches(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_stratified_dataset(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path, "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    branches = {row["decision_branch"] for row in rows}
    # explicit_accept, explicit_reject, implicit_accept, and no_decision
    # (survival-only) were all seeded — n (default 50) comfortably covers
    # every one of the 4 seeded attributed completions.
    assert branches == {
        "explicit_accept",
        "explicit_reject",
        "implicit_accept",
        "no_decision",
    }


def test_n_limits_the_sample_size(tmp_path, postgres_store_factory, capsys) -> None:
    database_url, mirror_path = _seed_stratified_dataset(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path, "--n", "2", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2


def test_sensitivity_json_output_reports_sweep_rows(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_stratified_dataset(
        postgres_store_factory, tmp_path
    )

    assert (
        _report(
            database_url,
            mirror_path,
            "--sensitivity",
            "--knob",
            "implicit_accept_multiplier",
            "--values",
            "1.0,1.1",
            "--json",
        )
        == 0
    )

    rows = json.loads(capsys.readouterr().out)
    assert {row["value"] for row in rows} == {1.0, 1.1}
    assert len(rows) == 4
    assert all(row["knob"] == "implicit_accept_multiplier" for row in rows)
    by_source = {}
    for row in rows:
        by_source.setdefault(row["eligibility_source"], []).append(row)
    assert {row["affected_attributed_completions"] for row in by_source[None]} == {1}
    assert {
        row["affected_attributed_completions"] for row in by_source["explicit_accept"]
    } == {0}
    assert all("sft_eligible_count" in row for row in rows)
    assert all("all_confidences" in row for row in rows)
    assert all(row["recipe_id"] == "sft_curated" for row in rows)
    assert all(row["recipe_version"] == 1 for row in rows)


def test_verified_recipe_requires_explicit_selection(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_stratified_dataset(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path, "--recipe", "sft_verified", "--json") == 0

    rows = json.loads(capsys.readouterr().out)
    assert rows
    assert {row["recipe_id"] for row in rows} == {"sft_verified"}
    assert "resolved_ci_pass" in {row["eligibility_source"] for row in rows}
    assert {row["eligibility_source"] for row in rows} <= {
        None,
        "resolved_ci_pass",
    }


def test_latency_buckets_json_outputs_decision_latency_report(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_stratified_dataset(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path, "--latency-buckets", "2", "--json") == 0

    report = json.loads(capsys.readouterr().out)
    assert report["buckets_requested"] == 2
    assert report["included_decisions"] == 3
    assert report["skipped_decisions"]["no_decision"] == 1
    assert len(report["buckets"]) == report["buckets_returned"]
    assert {"accept_rate", "mean_confidence", "mean_latency_ms"} <= set(
        report["buckets"][0]
    )


@pytest.mark.parametrize(
    ("flags", "expected_error"),
    [(["--n", "0"], "--n"), (["--latency-buckets", "0"], "--latency-buckets")],
)
def test_non_positive_counts_are_rejected(
    tmp_path, postgres_store_factory, capsys, flags, expected_error
) -> None:
    assert (
        _report(
            _empty_database(postgres_store_factory), str(tmp_path / "mirrors"), *flags
        )
        == 2
    )
    assert expected_error in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flags", "expected_error"),
    [
        (["--knob", "ci_pass_multiplier"], "--knob requires --sensitivity"),
        (["--min-confidence", "0.9"], "--min-confidence requires --sensitivity"),
        (
            ["--sensitivity", "--latency-buckets", "4"],
            "use --sensitivity or --latency-buckets, not both",
        ),
        (["--sensitivity", "--n", "10"], "--n does not apply to --sensitivity"),
        (["--latency-buckets", "4", "--n", "10"], "--n does not apply"),
        (
            ["--latency-buckets", "4", "--recipe", "sft_curated"],
            "--recipe does not apply to --latency-buckets",
        ),
    ],
)
def test_mode_specific_flags_are_not_silently_ignored(
    tmp_path, postgres_store_factory, capsys, flags, expected_error
) -> None:
    assert (
        _report(
            _empty_database(postgres_store_factory), str(tmp_path / "mirrors"), *flags
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected_error in captured.err


def test_table_output_shows_human_judgment_slot_and_breakdown(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_stratified_dataset(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path) == 0
    out = capsys.readouterr().out
    assert "human_judgment:  None" in out
    assert "confidence:" in out
    assert "decision_branch:" in out


def test_empty_org_prints_no_data_and_exits_zero(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url = _empty_database(postgres_store_factory)
    assert _report(database_url, str(tmp_path / "mirrors")) == 0
    out = capsys.readouterr().out
    assert "no data" in out


def test_empty_org_json_is_an_empty_list(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url = _empty_database(postgres_store_factory)
    assert _report(database_url, str(tmp_path / "mirrors"), "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_invalid_org_id_is_a_clean_error_not_a_traceback(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url = _empty_database(postgres_store_factory)
    code = main(
        [
            "--org",
            "not an org id!",
            "--database-url",
            database_url,
            "--mirror-path",
            str(tmp_path / "mirrors"),
        ]
    )
    assert code == 2
    assert "error:" in capsys.readouterr().err
