# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sediment_api/reports/model_report.py: the CLI wraps
``sediment_export.generate_model_report`` — one real git fixture proves the
wiring end to end (store -> mirrors -> attributed_completions -> report -> stdout); the rest
of the aggregation logic is covered by
``packages/export/tests/test_outcome_report.py`` against the pure function.
"""

from __future__ import annotations

import json
import math
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sediment_api.reports import model_report
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    FactStore,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
    ToolCallPart,
    SessionCommitObservation,
)
from sediment_derive import MirrorManager, Provenance
from sediment_export import (
    CIGrain,
    ModelOutcomeReport,
    ModelOutcomeReportResult,
    compare_all_models,
    compare_models,
    derive_model_report_attribution_share,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
FIB = "def fibonacci(n):\n    return n if n <= 1 else fibonacci(n - 1)\n"
MODELS = ("claude-sonnet-5", "claude-opus-4-8")
COMPARE = ("--compare", *MODELS)
COMPARE_ALL = ("--compare-all", *MODELS)


main = model_report.main


def test_bounded_model_report_sends_generation_error_to_stderr(
    monkeypatch, capsys
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def store(*args, **kwargs):
        yield object()

    monkeypatch.setattr(model_report, "one_shot_fact_store", store)
    monkeypatch.setattr(model_report, "MirrorManager", lambda path: object())

    def fail(*args, **kwargs):
        raise ValueError("bounded evidence overflow")

    monkeypatch.setattr(model_report, "generate_operational_model_report", fail)

    assert main(["--org", ORG, "--json", "--since-days", "30"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "couldn't generate model report" in captured.err
    assert "bounded evidence overflow" in captured.err


def test_model_report_table_prints_fate_counts_and_global_skips_once(capsys) -> None:
    row = ModelOutcomeReport(
        model="model-a",
        since_days=None,
        completions=1,
        attributed_inference_calls=1,
        attribution_rate=1.0,
        attribution_rate_ci=(1.0, 1.0),
        ci_linked=0,
        ci_passed=0,
        ci_pass_rate=0.0,
        ci_pass_rate_ci=(0.0, 0.0),
        explicit_accepts=1,
        explicit_rejects=0,
        mean_similarity=1.0,
        provenance=Provenance(policy_version="2", quarantine_revision=4),
        grain=CIGrain.COMMIT,
        fates={"deleted": 2},
        explicit_accept_fates={"deleted": 1},
        fates_with_external_changes={"deleted": 1},
    )
    result = ModelOutcomeReportResult(
        rows=[row],
        stratification=[],
        fate_skipped={"invalid_score": 3},
        fate_provenance=Provenance(policy_version="1", quarantine_revision=4),
    )

    model_report._print_table(result.rows)
    model_report._print_fate_diagnostics(result)

    output = capsys.readouterr().out
    assert "fates: deleted=2" in output
    assert "human-explicit accept fates: deleted=1" in output
    assert "fates with external changes: deleted=1" in output
    assert output.count("Fate diagnostic") == 1
    assert "derivation_skipped: invalid_score=3" in output
    assert "provenance: policy_version=1 quarantine_revision=4" in output


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


@pytest.mark.parametrize("window", [[], ["--since-days", "30"]])
def test_model_report_cli_keeps_uncommitted_direct_accept(
    postgres_store_factory, tmp_path, capsys, window
):
    database_url, store = postgres_store_factory()
    occurred = datetime.now(UTC) - timedelta(days=20)
    call = InferenceCall(
        org_id=ORG,
        session_id="direct",
        gateway_provider=GatewayProvider.LITELLM,
        model="model-a",
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[ToolCallPart(id="tool", name="Edit", arguments={})],
            )
        ],
        observed_at=occurred,
    )
    store.store_inference_call(call)
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id=call.session_id,
            call_id="tool",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="a.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            occurred_at=occurred,
            captured_at=occurred,
        )
    )

    assert _report(database_url, str(tmp_path / "mirrors"), "--json", *window) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["explicit_accepts"] == 1
    assert payload["rows"][0]["ci_linked"] == 0
    assert payload["abandonment"]["negative_completions"] == 0


# Real-git helpers, kept inline — same convention as test_attributed_completions.py.


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


def _commit(work: Path, message: str) -> str:
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD").strip()


def _note(session_id: str) -> str:
    return json.dumps(
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


def _make_remote(tmp_path: Path, work: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    return remote


def _store_push(
    store: FactStore, mirrors: MirrorManager, remote: Path, head: str
) -> None:
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
    )
    mirrors.ensure(push)
    store.store_push(push)


def _store_inference_call(
    store: FactStore,
    session_id: str,
    model: str,
    call_id: str,
    text: str,
    *,
    observed_at: datetime | None = None,
) -> None:
    store.store_inference_call(
        InferenceCall(
            org_id=ORG,
            session_id=session_id,
            user_id="dev",
            gateway_provider=GatewayProvider.LITELLM,
            model=model,
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content="write it")])
            ],
            output_messages=[
                InferenceMessage(role="assistant", parts=[TextPart(content=text)])
            ],
            input_tokens=10,
            output_tokens=20,
            duration_ms=50,
            model_call_id=call_id,
            observed_at=observed_at or datetime.now(UTC),
        )
    )


def _store_decision(
    store: FactStore, session_id: str, call_id: str, file_path: str, accepted: bool
) -> None:
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id=session_id,
            user_id="dev",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path=file_path,
            accepted=accepted,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id=call_id,
            occurred_at=datetime.now(UTC),
        )
    )


def _store_ci(store: FactStore, head: str, result: CIResult, run_url: str) -> None:
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id=run_url,
            repo=REPO,
            commit_sha=head,
            branch="main",
            result=result,
            run_url=run_url,
        )
    )


def _store_observed_edge(store: FactStore, session_id: str, commit_sha: str) -> None:
    store.store_session_commit_observation(
        SessionCommitObservation(
            org_id=ORG,
            repo=REPO,
            commit_sha=commit_sha,
            session_id=session_id,
            source_push_id=store.read_pushes(ORG)[0].push_id,
        )
    )


def _seed_one_full_attributed_completion(
    postgres_store_factory, tmp_path: Path
) -> tuple[str, str]:
    """Builds one notes-stamped commit + completion + explicit accept +
    passing CI, wired through a real store and real mirrors. Returns
    ``(database_url, mirror_path)``."""
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-1"), head)
    remote = _make_remote(tmp_path, work)

    database_url, store = postgres_store_factory()
    mirror_path = str(tmp_path / "mirrors")
    _store_push(store, MirrorManager(mirror_path), remote, head)
    _store_inference_call(store, "sess-1", "claude-sonnet-5", "call-1", FIB)
    _store_decision(store, "sess-1", "call-1", "math_utils.py", accepted=True)
    _store_ci(store, head, CIResult.PASSED, "run/1")
    _store_observed_edge(store, "sess-1", head)
    return database_url, mirror_path


def _seed_jaccard_only_completion(
    postgres_store_factory,
    tmp_path: Path,
    *,
    observed_at: datetime,
) -> tuple[str, str, FactStore]:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    remote = _make_remote(tmp_path, work)

    database_url, store = postgres_store_factory()
    mirror_path = str(tmp_path / "mirrors")
    _store_inference_call(
        store,
        "sess-1",
        "claude-sonnet-5",
        "call-1",
        FIB,
        observed_at=observed_at,
    )
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        captured_at=observed_at + timedelta(minutes=1),
    )
    mirrors = MirrorManager(mirror_path)
    mirrors.ensure(push)
    store.store_push(push)
    return database_url, mirror_path, store


def test_json_output_reports_the_seeded_model(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_one_full_attributed_completion(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path, "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    rows = payload["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "claude-sonnet-5"
    assert row["completions"] == 1
    assert row["attributed_inference_calls"] == 1
    assert row["attribution_rate"] == 1.0
    assert row["attribution_rate_ci"][0] < 1.0
    assert row["attribution_rate_ci"][1] == 1.0
    assert "survival_rate" not in row
    assert "survival_rate_ci" not in row
    assert row["ci_linked"] == 1
    assert row["ci_passed"] == 1
    assert row["ci_pass_rate"] == 1.0
    assert row["ci_pass_rate_ci"][0] < 1.0
    assert row["ci_pass_rate_ci"][1] == 1.0
    assert row["explicit_accepts"] == 1
    assert row["explicit_rejects"] == 0
    funnel = payload["signal_funnel"]
    assert len(funnel) == 1
    assert funnel[0]["model"] == "claude-sonnet-5"
    assert funnel[0]["completions_total"] == 1
    assert funnel[0]["attributed"] == 1
    assert funnel[0]["ci_linked"] == 1
    assert funnel[0]["has_decision"] == 1
    assert funnel[0]["training_row_eligible"] == 1
    assert funnel[0]["training_row_eligible_retention_rate"] == 1.0
    assert payload["stratification"][0]["metric"] == "ci_pass_rate"
    assert payload["stratification"][0]["status"] == "not_enough_models"
    assert payload["stratification"][1]["metric"] == "attribution_rate"
    assert payload["stratification"][1]["status"] == "not_checkable"
    assert payload["abandonment"] == {
        "abandoned_sessions": 0,
        "grade_eligible_sessions": 0,
        "implicit_only_sessions": 0,
        "negative_completions": 0,
        "explicit_accepts_unjoined": 0,
        "derivation_skipped": {"reached_a_commit": 1},
        "provenance": {
            "policy_version": "6",
            "quarantine_revision": 0,
            "policy_digest": None,
        },
    }
    assert len(payload["attribution_share"]) == 1
    assert payload["attribution_share"][0]["repo"] == REPO
    assert payload["attribution_share"][0]["git_notes_share"] == 1.0
    assert payload["attribution_alerts"] == []
    assert row["fates"] == {}
    assert row["explicit_accept_fates"] == {}
    assert row["fates_with_external_changes"] == {}
    assert payload["fate_skipped"] == {}
    assert payload["fate_provenance"] == {
        "policy_version": "1",
        "quarantine_revision": 0,
        "policy_digest": None,
    }


def test_json_output_carries_zero_share_alert_for_jaccard_only_repo(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path, _ = _seed_jaccard_only_completion(
        postgres_store_factory,
        tmp_path,
        observed_at=datetime.now(UTC) - timedelta(minutes=2),
    )

    report_started_at = datetime.now(UTC)
    assert _report(database_url, mirror_path, "--since-days", "30", "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    [share] = payload["attribution_share"]
    assert share["repo"] == REPO
    assert share["agent_plausible_commits"] == 1
    assert share["git_notes_attributed"] == 0
    assert share["jaccard_attributed"] == 1
    window_start = datetime.fromisoformat(share["window_start"])
    window_end = datetime.fromisoformat(share["window_end"])
    assert report_started_at <= window_end <= datetime.now(UTC)
    assert window_end - window_start == timedelta(days=30)
    [alert] = payload["attribution_alerts"]
    assert alert["repo"] == REPO
    assert alert["kind"] == "zero_share_nonzero_activity"
    assert alert["baseline"] is None


def test_attribution_share_uses_the_model_report_window(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path, _ = _seed_jaccard_only_completion(
        postgres_store_factory,
        tmp_path,
        observed_at=datetime.now(UTC) - timedelta(days=40),
    )

    assert _report(database_url, mirror_path, "--since-days", "30", "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["attribution_share"] == []
    assert payload["attribution_alerts"] == []


def test_attribution_share_section_is_deterministic_for_fixed_now(
    tmp_path, postgres_store_factory
) -> None:
    now = datetime.now(UTC)
    _, mirror_path, store = _seed_jaccard_only_completion(
        postgres_store_factory,
        tmp_path,
        observed_at=now - timedelta(minutes=2),
    )
    mirrors = MirrorManager(mirror_path)

    first = derive_model_report_attribution_share(store, mirrors, ORG, 30, now=now)
    second = derive_model_report_attribution_share(store, mirrors, ORG, 30, now=now)

    assert second == first


def test_json_trend_output_serializes_temporal_results(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_one_full_attributed_completion(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path, "--json", "--trend") == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "rows",
        "stratification",
        "signal_funnel",
        "abandonment",
        "attribution_share",
        "attribution_alerts",
        "fate_skipped",
        "fate_provenance",
        "repository_skipped",
        "ci_skipped",
        "trends",
    }
    trends = payload["trends"]
    assert {trend["metric"] for trend in trends} == {
        "attribution_rate",
        "ci_pass_rate",
    }
    attribution = next(t for t in trends if t["metric"] == "attribution_rate")
    assert attribution["model"] == "claude-sonnet-5"
    assert attribution["windows"][0]["rate"] == 1.0
    assert isinstance(attribution["windows"][0]["window_start"], str)
    assert attribution["test"]["status"] == "insufficient_data"


def test_table_output_prints_a_header_and_the_model_row(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_one_full_attributed_completion(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path) == 0

    out = capsys.readouterr().out
    assert "attribution share" in out
    assert (
        "100.0% of agent-plausible commits in this window carry a session stamp" in out
    )
    assert out.index("attribution share") < out.index("model")
    assert "model" in out and "attribution_rate" in out
    assert "survival" not in out
    assert "100.0% [" in out
    assert "claude-sonnet-5" in out
    assert "signal funnel" in out
    assert "training_eligible" in out
    assert "abandonment" in out
    assert "abandoned_sessions:        0" in out
    assert "grade_eligible_sessions:   0" in out
    assert "implicit_only_sessions:    0" in out
    assert "negative_completions:      0" in out
    assert "explicit_accepts_unjoined: 0" in out
    assert "derivation_skipped:       reached_a_commit=1" in out
    assert "provenance:                policy_version=6 quarantine_revision=0" in out
    assert "stratification" in out
    assert "ci_pass_rate: not_enough_models" in out


def test_empty_org_prints_no_data_and_exits_zero(
    tmp_path, postgres_store_factory, capsys
) -> None:
    assert (
        _report(_empty_database(postgres_store_factory), str(tmp_path / "mirrors")) == 0
    )
    assert "no data" in capsys.readouterr().out


def test_invalid_org_id_is_a_clean_error_not_a_traceback(
    tmp_path, postgres_store_factory, capsys
) -> None:
    code = main(
        [
            "--org",
            "not an org id!",
            "--database-url",
            _empty_database(postgres_store_factory),
            "--mirror-path",
            str(tmp_path / "mirrors"),
        ]
    )
    assert code == 2
    assert "error:" in capsys.readouterr().err


def _seed_two_models(postgres_store_factory, tmp_path: Path) -> tuple[str, str]:
    """Two models, one commit each, both notes-stamped and CI-linked: model
    A's commit passes CI, model B's fails — enough for a real (if tiny-n)
    two-proportion comparison, not just an insufficient-data one."""
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head_a = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-a"), head_a)

    (work / "other.py").write_text("x = 1\n")
    head_b = _commit(work, "add other")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-b"), head_b)

    remote = _make_remote(tmp_path, work)

    database_url, store = postgres_store_factory()
    mirror_path = str(tmp_path / "mirrors")
    _store_push(store, MirrorManager(mirror_path), remote, head_b)

    _store_inference_call(store, "sess-a", "claude-sonnet-5", "call-a", FIB)
    _store_decision(store, "sess-a", "call-a", "math_utils.py", accepted=True)
    _store_ci(store, head_a, CIResult.PASSED, "run/a")
    _store_observed_edge(store, "sess-a", head_a)

    _store_inference_call(store, "sess-b", "claude-opus-4-8", "call-b", "x = 1\n")
    _store_decision(store, "sess-b", "call-b", "other.py", accepted=False)
    _store_ci(store, head_b, CIResult.FAILED, "run/b")
    _store_observed_edge(store, "sess-b", head_b)

    return database_url, mirror_path


def test_compare_prints_significance_section_alongside_the_table(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, *COMPARE) == 0

    out = capsys.readouterr().out
    # The existing descriptive table is untouched.
    assert "attribution" in out
    assert "claude-sonnet-5" in out
    assert "claude-opus-4-8" in out
    # The new significance section is appended.
    assert "compare: claude-sonnet-5 vs claude-opus-4-8" in out
    assert "ci_pass_rate" in out
    assert "attribution_rate" in out
    assert " h " in out
    assert "p_value" in out
    assert "multiple comparisons: raw alpha=0.05" in out
    assert "Bonferroni-adjusted threshold=0.0250" in out
    assert "repeated-testing caveat" in out
    assert "power analysis: alpha=0.05, power=80%" in out
    assert (
        "attribution_rate: at your current sample size (n_a=1, n_b=1), "
        "this comparison can detect a difference of at least 198.1 "
        "percentage points with 80% power."
    ) in out
    assert "bayesian posterior" in out
    assert "Beta-Binomial posterior with flat Beta(1, 1) prior" in out
    assert "P(A>B) is posterior probability, not a p-value" in out


def test_compare_with_json_includes_comparison_object_without_breaking_rows(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, "--json", *COMPARE) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "rows",
        "stratification",
        "signal_funnel",
        "abandonment",
        "attribution_share",
        "attribution_alerts",
        "fate_skipped",
        "fate_provenance",
        "repository_skipped",
        "ci_skipped",
        "comparison",
    }
    assert {row["model"] for row in payload["rows"]} == {
        "claude-sonnet-5",
        "claude-opus-4-8",
    }
    assert {row["model"] for row in payload["signal_funnel"]} == {
        "claude-sonnet-5",
        "claude-opus-4-8",
    }
    comparison = payload["comparison"]
    assert comparison["model_a"] == "claude-sonnet-5"
    assert comparison["model_b"] == "claude-opus-4-8"
    assert "ci_pass_rate" in comparison
    assert "attribution_rate" in comparison
    assert "p_value" in comparison["ci_pass_rate"]
    assert "cohens_h" in comparison["ci_pass_rate"]
    assert "significant_at_alpha" in comparison["ci_pass_rate"]
    assert "significant_after_bonferroni" in comparison["ci_pass_rate"]
    assert comparison["bonferroni_alpha"] == 0.025
    assert "minimum_detectable_effect" in comparison["ci_pass_rate"]
    assert "insufficient_data" in comparison["ci_pass_rate"]
    assert "bayesian_ci_pass_rate" in comparison
    assert "bayesian_attribution_rate" in comparison
    assert "probability_a_gt_b" in comparison["bayesian_attribution_rate"]
    assert comparison["bayesian_attribution_rate"]["seed"] == 179
    assert comparison["bayesian_attribution_rate"]["sample_count"] == 100000
    assert comparison["bayesian_attribution_rate"]["credible_level"] == 0.95
    assert "credible_interval" not in comparison["bayesian_attribution_rate"]


def test_compare_all_with_two_models_matches_compare_numbers_in_json(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, "--json", *COMPARE) == 0
    compare_payload = json.loads(capsys.readouterr().out)

    assert _report(database_url, mirror_path, "--json", *COMPARE_ALL) == 0
    compare_all_payload = json.loads(capsys.readouterr().out)

    comparison = compare_payload["comparison"]
    comparison_all = compare_all_payload["comparison_all"]
    assert comparison_all["models"] == ["claude-sonnet-5", "claude-opus-4-8"]
    assert comparison_all["comparison_family_size"] == 2
    assert comparison_all["tested_family_size"] == 1
    [round_robin] = comparison_all["comparisons"]
    assert round_robin["model_a"] == comparison["model_a"]
    assert round_robin["model_b"] == comparison["model_b"]
    assert (
        round_robin["ci_pass_rate"]["p_value"] == comparison["ci_pass_rate"]["p_value"]
    )
    assert (
        round_robin["attribution_rate"]["p_value"]
        == comparison["attribution_rate"]["p_value"]
    )
    assert "benjamini_hochberg_alpha" in round_robin["ci_pass_rate"]
    assert "significant_after_benjamini_hochberg" in round_robin["ci_pass_rate"]


def test_compare_all_prints_fdr_section_alongside_the_table(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, *COMPARE_ALL, "no-such-model") == 0

    out = capsys.readouterr().out
    assert "compare-all: claude-sonnet-5, claude-opus-4-8, no-such-model" in out
    assert "bh_alpha" in out
    assert "fdr" in out
    assert "Benjamini-Hochberg FDR threshold=" in out
    assert "6 total pair-metric slots" in out
    assert "insufficient data" in out


def test_compare_with_json_and_peek_fraction_includes_sequential_reading(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(database_url, mirror_path, "--json", *COMPARE, "--peek-fraction", "0.4")
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    comparison = payload["comparison"]
    assert comparison["information_fraction"] == 0.4
    attribution_rate = comparison["attribution_rate"]
    assert "p_value" in attribution_rate
    assert "significant_at_alpha" in attribution_rate
    sequential = attribution_rate["sequential_boundary"]
    assert sequential["information_fraction"] == 0.4
    # Boundary is built from bonferroni_alpha (0.025 for this two-metric
    # family), not the nominal 0.05, so it never disagrees with
    # significant_after_bonferroni for the same row.
    assert sequential["alpha"] == comparison["bonferroni_alpha"]
    assert math.isclose(
        sequential["critical_z"],
        3.5439688864727965,
        rel_tol=1e-12,
    )
    assert math.isclose(
        sequential["alpha_spent"],
        0.00039415175669121894,
        rel_tol=1e-12,
    )
    assert "significant_after_sequential_boundary" in sequential


def test_compare_with_effect_decay_prints_checkpoint_table(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, *COMPARE, "--effect-decay") == 0

    out = capsys.readouterr().out
    assert "effect decay diagnostic" in out
    assert "ci_pass_rate" in out
    assert "attribution_rate" in out
    assert "checkpoint" in out
    assert "decay_detected=no" in out
    assert "small_n=True" in out


def test_compare_with_json_includes_effect_decay_payload(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, "--json", *COMPARE, "--effect-decay") == 0

    payload = json.loads(capsys.readouterr().out)
    effect_decay = payload["effect_decay"]
    assert set(effect_decay) == {"ci_pass_rate", "attribution_rate"}
    ci = effect_decay["ci_pass_rate"]
    assert ci["decay_detected"] is False
    assert ci["insufficient_data"] is True
    assert len(ci["checkpoints"]) == 4
    assert ci["checkpoints"][0]["fraction"] == 0.25
    assert "decay_trend" in ci
    assert "status" in ci["decay_trend"]
    attribution = effect_decay["attribution_rate"]
    assert attribution["small_n"] is True
    assert "cohens_h" in attribution["checkpoints"][0]


def test_compare_all_with_regret_prints_regret_column(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(database_url, mirror_path, *COMPARE_ALL, "no-such-model", "--regret")
        == 0
    )

    out = capsys.readouterr().out
    assert "compare-all: claude-sonnet-5, claude-opus-4-8, no-such-model" in out
    assert "bh_alpha" in out
    assert "fdr" in out
    assert "regret" in out
    assert "Benjamini-Hochberg FDR threshold=" in out
    assert "6 total pair-metric slots" in out
    assert "expected loss from shipping each metric's apparent best" in out
    assert "insufficient data" in out


def test_compare_all_with_regret_json_includes_regret_payload(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, "--json", *COMPARE_ALL, "--regret") == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "rows",
        "stratification",
        "signal_funnel",
        "abandonment",
        "attribution_share",
        "attribution_alerts",
        "fate_skipped",
        "fate_provenance",
        "repository_skipped",
        "ci_skipped",
        "comparison_all",
        "regret",
    }
    regret = payload["regret"]["ci_pass_rate"]
    assert regret["metric_name"] == "ci_pass_rate"
    assert regret["apparent_best_model"] in {"claude-sonnet-5", "claude-opus-4-8"}
    [alternative] = regret["alternatives"]
    assert alternative["model"] in {"claude-sonnet-5", "claude-opus-4-8"}
    assert "expected_regret" in alternative


def test_compare_prints_raw_only_when_p_value_fails_bonferroni(
    capsys,
) -> None:
    report_a = ModelOutcomeReport(
        model="model-a",
        since_days=None,
        completions=100,
        attributed_inference_calls=33,
        attribution_rate=0.33,
        attribution_rate_ci=(0.0, 0.0),
        ci_linked=100,
        ci_passed=33,
        ci_pass_rate=0.33,
        ci_pass_rate_ci=(0.0, 0.0),
        explicit_accepts=0,
        explicit_rejects=0,
        mean_similarity=0.0,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        grain=CIGrain.COMMIT,
    )
    report_b = ModelOutcomeReport(
        model="model-b",
        since_days=None,
        completions=100,
        attributed_inference_calls=20,
        attribution_rate=0.20,
        attribution_rate_ci=(0.0, 0.0),
        ci_linked=100,
        ci_passed=20,
        ci_pass_rate=0.20,
        ci_pass_rate_ci=(0.0, 0.0),
        explicit_accepts=0,
        explicit_rejects=0,
        mean_similarity=0.0,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        grain=CIGrain.COMMIT,
    )

    model_report._print_comparison(compare_models(report_a, report_b))

    out = capsys.readouterr().out
    assert "0.0373" in out
    assert "raw_only" in out
    assert "sig=raw_only clears only raw alpha" in out


def _outcome_row(
    model: str, *, completions: int, attributed: int, ci_linked: int, ci_passed: int
) -> ModelOutcomeReport:
    """Minimal stdlib-only ``ModelOutcomeReport`` for pure-unit compare-all tests.

    ``completions == 0`` or ``ci_linked == 0`` make the corresponding metric
    insufficient data (n=0 in at least one arm) — no Postgres, no git, mirroring
    the convention in ``test_significance.py``.
    """
    return ModelOutcomeReport(
        model=model,
        since_days=None,
        completions=completions,
        attributed_inference_calls=attributed,
        attribution_rate=attributed / completions if completions else 0.0,
        attribution_rate_ci=(0.0, 0.0),
        ci_linked=ci_linked,
        ci_passed=ci_passed,
        ci_pass_rate=ci_passed / ci_linked if ci_linked else 0.0,
        ci_pass_rate_ci=(0.0, 0.0),
        explicit_accepts=0,
        explicit_rejects=0,
        mean_similarity=0.0,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        grain=CIGrain.COMMIT,
    )


def test_bh_threshold_for_display_reads_real_alpha_when_first_pair_ci_pass_rate_is_insufficient() -> (
    None
):
    # model-a is listed FIRST and has ci_linked=0 -> its ci_pass_rate is
    # insufficient data; model-b/model-c have full data and reject on the
    # attribution_rate slots. The first `combinations` pair is (a, b), whose
    # ci_pass_rate is the insufficient 0.0 placeholder the buggy `is not None`
    # guard returned.
    a = _outcome_row(
        "model-a", completions=100, attributed=80, ci_linked=0, ci_passed=0
    )
    b = _outcome_row(
        "model-b", completions=100, attributed=50, ci_linked=100, ci_passed=50
    )
    c = _outcome_row(
        "model-c", completions=100, attributed=60, ci_linked=100, ci_passed=60
    )
    comparison = compare_all_models([a, b, c])

    sufficient_alphas = {
        pc.benjamini_hochberg_alpha
        for pair in comparison.comparisons
        for pc in (pair.ci_pass_rate, pair.attribution_rate)
        if not pc.insufficient_data
    }
    assert sufficient_alphas == {0.025}  # rejected_count=2, m=4, alpha=0.05

    assert model_report._bh_threshold_for_display(comparison) == 0.025
    # The first pair's ci_pass_rate is the insufficient-data placeholder the
    # bug returned; it must NOT be what the helper reports.
    assert (
        comparison.comparisons[0].ci_pass_rate.benjamini_hochberg_alpha == 0.0
        and comparison.comparisons[0].ci_pass_rate.insufficient_data
    )
    assert model_report._bh_threshold_for_display(comparison) != 0.0


def test_print_compare_all_summary_threshold_matches_fdr_rows_when_first_model_is_insufficient(
    capsys,
) -> None:
    a = _outcome_row(
        "model-a", completions=100, attributed=80, ci_linked=0, ci_passed=0
    )
    b = _outcome_row(
        "model-b", completions=100, attributed=50, ci_linked=100, ci_passed=50
    )
    c = _outcome_row(
        "model-c", completions=100, attributed=60, ci_linked=100, ci_passed=60
    )
    comparison = compare_all_models([a, b, c])

    model_report._print_compare_all(comparison)
    out = capsys.readouterr().out

    # The summary line must print the shared global BH cutoff actually shown
    # on the fdr=yes rows of this same block -- 0.0250 -- not the 0.0000 the
    # buggy first-slot read produced.
    assert "Benjamini-Hochberg FDR threshold=0.0250" in out
    assert "over 4 tested p-values" in out
    assert "6 total pair-metric slots" in out
    assert "fdr=yes" in out
    assert "insufficient data (n=0 in at least one group)" in out
    assert "Benjamini-Hochberg FDR threshold=0.0000 over" not in out


def test_compare_with_target_mde_prints_required_n_per_arm(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert _report(database_url, mirror_path, *COMPARE, "--target-mde", "0.05") == 0

    out = capsys.readouterr().out
    assert "target difference of 5.0 percentage points" in out
    assert "plan for n=1570 per arm" in out
    assert "estimated duration" not in out


def test_compare_with_target_mde_and_traffic_prints_required_days(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(
            database_url,
            mirror_path,
            *COMPARE,
            "--target-mde",
            "0.05",
            "--completions-per-day",
            "100",
            "80",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "plan for n=1570 per arm" in out
    assert (
        "attribution_rate: at 100/80 completions/day for model A/model B, "
        "estimated duration is 19.62 days."
    ) in out


def test_compare_with_target_mde_and_zero_traffic_omits_required_days(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(
            database_url,
            mirror_path,
            *COMPARE,
            "--target-mde",
            "0.05",
            "--completions-per-day",
            "0",
            "80",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "plan for n=1570 per arm" in out
    assert "estimated duration" not in out


def test_compare_with_non_inferiority_margin_prints_tost_section(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(database_url, mirror_path, *COMPARE, "--non-inferiority-margin", "0.05")
        == 0
    )

    out = capsys.readouterr().out
    assert "non-inferiority:" in out
    assert "baseline=claude-sonnet-5; candidate=claude-opus-4-8" in out
    assert "margin=5.0 percentage points" in out
    assert "estimate is candidate minus baseline (p_b - p_a)" in out
    assert "equivalent two-sided 90% CI" in out
    assert "non-inferior means lower_bound > -margin" in out
    assert "non_inferior" in out
    assert "small n, less reliable" in out


def test_compare_with_json_includes_target_mde_payload(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(database_url, mirror_path, "--json", *COMPARE, "--target-mde", "0.05")
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    target = payload["target_mde"]
    assert target["target_mde"] == 0.05
    assert target["power"] == 0.8
    assert target["alpha"] == 0.05
    assert target["required_n_per_arm"]["ci_pass_rate"] is None
    assert target["required_n_per_arm"]["attribution_rate"] == 1570
    assert "required_days" not in target


def test_compare_with_json_includes_target_mde_duration_payload(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(
            database_url,
            mirror_path,
            "--json",
            *COMPARE,
            "--target-mde",
            "0.05",
            "--completions-per-day",
            "100",
            "80",
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    target = payload["target_mde"]
    assert target["completions_per_day"] == {"model_a": 100.0, "model_b": 80.0}
    assert target["required_days"]["ci_pass_rate"] is None
    assert target["required_days"]["attribution_rate"] == 19.625


def _all_pass_ci_report(
    model: str,
    *,
    completions: int,
    attributed: int,
    ci_linked: int,
) -> ModelOutcomeReport:
    """Both arms all-pass on CI: ``ci_passed == ci_linked``. Attribution is
    left interior so the comparison mixes a degenerate CI-pass metric with a
    normal attribution metric — the boundary guard must scope to the
    degenerate metric only, not blanket-suppress the whole comparison."""
    return ModelOutcomeReport(
        model=model,
        since_days=None,
        completions=completions,
        attributed_inference_calls=attributed,
        attribution_rate=attributed / completions,
        attribution_rate_ci=(0.0, 0.0),
        ci_linked=ci_linked,
        ci_passed=ci_linked,
        ci_pass_rate=1.0,
        ci_pass_rate_ci=(0.0, 0.0),
        explicit_accepts=0,
        explicit_rejects=0,
        mean_similarity=0.0,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        grain=CIGrain.COMMIT,
    )


def test_compare_power_row_boundary_outcome_prints_unavailable(capsys) -> None:
    # Both arms all-pass at large n on CI pass rate; attribution stays interior.
    report_a = _all_pass_ci_report(
        "model-a", completions=100, attributed=20, ci_linked=100
    )
    report_b = _all_pass_ci_report(
        "model-b", completions=100, attributed=30, ci_linked=100
    )
    comparison = compare_models(report_a, report_b)

    assert comparison.ci_pass_rate.degenerate_se is True
    assert comparison.ci_pass_rate.minimum_detectable_effect == 0.0
    assert comparison.attribution_rate.degenerate_se is False

    model_report._print_comparison(comparison, target_mde=0.05)

    out = capsys.readouterr().out
    # The degenerate CI-pass metric prints the boundary caveat, not its MDE.
    assert (
        "ci_pass_rate: MDE unavailable because both arms shared a boundary "
        "outcome (all-pass or all-fail)"
    ) in out
    assert "ci_pass_rate: at your current sample size" not in out
    # The degenerate MDE/n=0 must never reach the operator.
    assert "0.0 percentage points" not in out
    assert "plan for n=0 per arm" not in out
    # The interior attribution metric is unaffected: still gets its MDE line
    # and a real (non-zero) per-arm plan.
    assert "attribution_rate: at your current sample size" in out
    # baseline_p = 50/200 = 0.25 ->
    #   ceil(2 * 0.25 * 0.75 * (z_alpha/2 + z_beta)^2 / 0.05^2) = 1178
    assert "plan for n=1178 per arm" in out


def test_target_mde_payload_boundary_outcome_omits_required_n_and_days(capsys) -> None:
    report_a = _all_pass_ci_report(
        "model-a", completions=100, attributed=20, ci_linked=100
    )
    report_b = _all_pass_ci_report(
        "model-b", completions=100, attributed=30, ci_linked=100
    )
    comparison = compare_models(report_a, report_b)

    payload = model_report._target_mde_payload(comparison, 0.05, (100.0, 80.0))
    assert payload is not None
    # Degenerate CI-pass metric carries None for both required n and days.
    assert payload["required_n_per_arm"]["ci_pass_rate"] is None
    assert payload["required_days"]["ci_pass_rate"] is None
    # Interior attribution metric is unaffected.
    assert payload["required_n_per_arm"]["attribution_rate"] == 1178
    assert payload["required_days"]["attribution_rate"] == 14.725


def test_compare_with_bootstrap_check_prints_interval_diagnostic(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(
            database_url,
            mirror_path,
            *COMPARE,
            "--bootstrap-check",
            "--bootstrap-iterations",
            "200",
            "--bootstrap-seed",
            "42",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "bootstrap interval diagnostic: B=200, seed=42, confidence=95%" in out
    assert "analytic_ci" in out
    assert "bootstrap_ci" in out
    assert "point_in" in out
    assert "ci_pass_rate" in out
    assert "attribution_rate" in out


def test_compare_with_json_includes_bootstrap_interval_diagnostic(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(
            database_url,
            mirror_path,
            "--json",
            *COMPARE,
            "--bootstrap-check",
            "--bootstrap-iterations",
            "200",
            "--bootstrap-seed",
            "42",
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    bootstrap = payload["bootstrap_interval_diagnostic"]
    assert set(bootstrap) == {"ci_pass_rate", "attribution_rate"}
    assert bootstrap["attribution_rate"]["bootstrap_iterations"] == 200
    assert bootstrap["attribution_rate"]["seed"] == 42
    assert "analytic" in bootstrap["attribution_rate"]
    assert "bootstrap_ci_low" in bootstrap["attribution_rate"]
    assert "bootstrap_ci_high" in bootstrap["attribution_rate"]


def test_compare_with_json_includes_non_inferiority_payload(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(
            database_url,
            mirror_path,
            "--json",
            *COMPARE,
            "--non-inferiority-margin",
            "0.05",
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    non_inferiority = payload["non_inferiority"]
    assert set(non_inferiority) == {"ci_pass_rate", "attribution_rate"}
    assert non_inferiority["ci_pass_rate"]["margin"] == 0.05
    assert non_inferiority["ci_pass_rate"]["insufficient_data"] is True
    assert non_inferiority["ci_pass_rate"]["point_estimate"] == 0.0
    assert non_inferiority["ci_pass_rate"]["is_non_inferior"] is False
    assert non_inferiority["attribution_rate"]["equivalent_two_sided_confidence"] == 0.9
    assert non_inferiority["attribution_rate"]["small_n"] is True


def test_compare_against_a_model_with_no_data_is_insufficient_not_a_crash(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_one_full_attributed_completion(
        postgres_store_factory, tmp_path
    )

    assert (
        _report(
            database_url, mirror_path, "--compare", "claude-sonnet-5", "no-such-model"
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "compare: claude-sonnet-5 vs no-such-model" in out
    assert "insufficient data" in out
    assert "MDE unavailable" in out
    assert "bayesian posterior" in out


def test_compare_without_the_flag_leaves_plain_output_unchanged(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_one_full_attributed_completion(
        postgres_store_factory, tmp_path
    )

    assert _report(database_url, mirror_path) == 0

    out = capsys.readouterr().out
    assert "compare:" not in out


@pytest.mark.parametrize(
    ("flags", "expected_error"),
    [
        pytest.param(["--since-days", "0"], "since-days", id="since-days-positive"),
        pytest.param(
            ["--target-mde", "0.05"],
            "--target-mde requires --compare",
            id="target-mde-needs-compare",
        ),
        pytest.param(
            ["--compare-all", "a"],
            "--compare-all requires at least two models",
            id="compare-all-needs-two",
        ),
        pytest.param(
            ["--compare", "a", "b", "--compare-all", "a", "b"],
            "use --compare or --compare-all",
            id="compare-and-compare-all-exclusive",
        ),
        pytest.param(
            ["--effect-decay"],
            "--effect-decay requires --compare",
            id="effect-decay-needs-compare",
        ),
        pytest.param(
            ["--regret"],
            "--regret requires --compare-all",
            id="regret-needs-compare-all",
        ),
        pytest.param(
            ["--compare", "a", "b", "--target-mde", "0"],
            "--target-mde",
            id="target-mde-proportion",
        ),
        pytest.param(
            ["--bootstrap-check"],
            "--bootstrap-check requires --compare",
            id="bootstrap-check-needs-compare",
        ),
        pytest.param(
            ["--compare", "a", "b", "--bootstrap-check", "--bootstrap-iterations", "1"],
            "--bootstrap-iterations",
            id="bootstrap-iterations-support-percentile-ci",
        ),
        pytest.param(
            ["--bootstrap-iterations", "200"],
            "--bootstrap-iterations requires --bootstrap-check",
            id="bootstrap-iterations-needs-bootstrap-check",
        ),
        pytest.param(
            ["--bootstrap-seed", "42"],
            "--bootstrap-seed requires --bootstrap-check",
            id="bootstrap-seed-needs-bootstrap-check",
        ),
        pytest.param(
            ["--compare", "same-model", "same-model"],
            "--compare requires two different model names",
            id="compare-needs-different-models",
        ),
        pytest.param(
            ["--non-inferiority-margin", "0.05"],
            "--non-inferiority-margin requires --compare",
            id="non-inferiority-needs-compare",
        ),
        pytest.param(
            ["--compare", "a", "b", "--non-inferiority-margin", "0"],
            "--non-inferiority-margin",
            id="non-inferiority-proportion",
        ),
        pytest.param(
            ["--peek-fraction", "0.4"],
            "--peek-fraction requires --compare",
            id="peek-fraction-needs-compare",
        ),
        pytest.param(
            ["--compare", "a", "b", "--peek-fraction", "1.1"],
            "--peek-fraction",
            id="peek-fraction-range",
        ),
    ],
)
def test_rejected_flag_combinations_exit_two(
    tmp_path, postgres_store_factory, capsys, flags, expected_error
) -> None:
    database_url = _empty_database(postgres_store_factory)

    assert _report(database_url, str(tmp_path / "mirrors"), *flags) == 2
    assert expected_error in capsys.readouterr().err


def test_peek_fraction_table_stays_aligned_when_a_metric_is_insufficient_data(
    tmp_path, postgres_store_factory, capsys
) -> None:
    # With --peek-fraction the header always carries the seq_z/seq_alpha/seq_sig
    # columns; an insufficient-data metric once printed a bare sentence with no
    # column padding at all.
    database_url, mirror_path = _seed_two_models(postgres_store_factory, tmp_path)

    assert (
        _report(
            database_url,
            mirror_path,
            "--compare",
            "claude-sonnet-5",
            "no-such-model",
            "--peek-fraction",
            "0.4",
        )
        == 0
    )

    out = capsys.readouterr().out
    compare_section = out.split("compare: claude-sonnet-5 vs no-such-model")[1]
    compare_lines = compare_section.splitlines()
    header = next(line for line in compare_lines if line.startswith("metric"))
    insufficient_line = next(
        line for line in compare_lines if line.startswith("attribution_rate")
    )
    assert "insufficient data" in insufficient_line
    # Both lines must carry the same number of whitespace-separated fields
    # up through n_a/n_b as the header, so the table columns stay aligned
    # instead of the insufficient row collapsing to a short, unpadded string.
    header_fields = header.split()
    row_fields = insufficient_line.split()
    n_a_index = header_fields.index("n_a")
    assert row_fields[:n_a_index] == ["attribution_rate"] + ["--"] * (n_a_index - 1)


@pytest.mark.parametrize(
    "case",
    [
        "matching",
        "absent",
        "late",
        "wrong_org",
        "wrong_repo",
        "wrong_commit",
        "wrong_session",
    ],
)
@pytest.mark.parametrize("grain", list(CIGrain))
def test_effect_decay_uses_only_qualified_observed_population(case, grain):
    from dataclasses import replace
    from sediment_core import SessionCommitObservation
    from sediment_export import AttributedCompletion
    from sediment_derive import AttributionSource

    now = datetime(2026, 9, 6, tzinfo=UTC)
    call = InferenceCall(
        inference_call_id="call",
        org_id=ORG,
        session_id="session",
        model="model",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[],
        observed_at=now,
    )
    outcome = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run",
        repo=REPO,
        commit_sha="a" * 40,
        branch="main",
        result=CIResult.PASSED,
        workflow_name="tests",
        captured_at=now,
    )
    row = AttributedCompletion(
        org_id=ORG,
        session_id="session",
        inference_call_id="call",
        repo=REPO,
        commit_sha="a" * 40,
        file_path="file.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.JACCARD,
        decisions=[],
        ci_outcomes=[outcome],
        provenance=Provenance("1", 0),
        split="train",
    )
    changes = {
        "late": {"captured_at": now + timedelta(microseconds=1)},
        "wrong_org": {"org_id": "other"},
        "wrong_repo": {"repo": "other/repo"},
        "wrong_commit": {"commit_sha": "b" * 40},
        "wrong_session": {"session_id": "other"},
    }
    fact = SessionCommitObservation(
        observation_id="edge",
        org_id=ORG,
        repo=REPO,
        commit_sha="a" * 40,
        session_id="session",
        source_push_id="push",
        captured_at=now,
    ).model_copy(update=changes.get(case, {}))
    row = replace(row, session_commit_observations=() if case == "absent" else (fact,))
    rows = [row, replace(row, file_path="second.py")]
    expected = (
        {"model": [True] * (1 if grain == CIGrain.COMMIT else 2)}
        if case == "matching"
        else {}
    )
    for population in (rows, list(reversed(rows))):
        assert (
            model_report._ci_outcomes_by_model(population, [call], None, now, grain)
            == expected
        )


@pytest.mark.parametrize("grain", list(CIGrain))
@pytest.mark.parametrize(
    "offset, included",
    [
        (timedelta(microseconds=-1), True),
        (timedelta(0), True),
        (timedelta(microseconds=1), False),
    ],
)
def test_effect_decay_completion_observed_at_boundary_drops_future_inference_call(
    offset, included, grain
):
    from dataclasses import replace

    from sediment_core import SessionCommitObservation
    from sediment_derive import AttributionSource, Provenance
    from sediment_export import AttributedCompletion

    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
    call = InferenceCall(
        inference_call_id="call",
        org_id=ORG,
        session_id="session",
        model="model",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[],
        observed_at=now + offset,
    )
    observation = SessionCommitObservation(
        observation_id="obs",
        org_id=ORG,
        repo=REPO,
        commit_sha="a" * 40,
        session_id="session",
        source_push_id="push",
        captured_at=now,
    )
    row = AttributedCompletion(
        org_id=ORG,
        session_id="session",
        inference_call_id="call",
        repo=REPO,
        commit_sha="a" * 40,
        file_path="file.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.JACCARD,
        decisions=[],
        ci_outcomes=[
            CIOutcome(
                org_id=ORG,
                provider=CIProvider.GITHUB_ACTIONS,
                run_id="run",
                repo=REPO,
                commit_sha="a" * 40,
                branch="main",
                result=CIResult.PASSED,
                workflow_name="tests",
                captured_at=now,
            )
        ],
        provenance=Provenance("1", 0),
        split="train",
    )
    row = replace(row, session_commit_observations=(observation,))

    expected = {"model": [True]} if included else {}
    assert (
        model_report._attribution_outcomes_by_model([call], [row], None, now)
        == expected
    )
    assert (
        model_report._ci_outcomes_by_model([row], [call], None, now, grain) == expected
    )


def test_effect_decay_cohorts_share_one_evidence_boundary_with_headline():
    from dataclasses import replace

    from sediment_core import SessionCommitObservation
    from sediment_derive import AttributionSource, Provenance
    from sediment_export import AttributedCompletion
    from sediment_export.outcome_report import model_report_inputs

    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

    def _call(call_id, model, observed_at):
        return InferenceCall(
            inference_call_id=call_id,
            org_id=ORG,
            session_id="session",
            model=model,
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[],
            output_messages=[],
            observed_at=observed_at,
        )

    def _row(call_id, model, observed_at, commit):
        observation = SessionCommitObservation(
            observation_id="obs-" + commit,
            org_id=ORG,
            repo=REPO,
            commit_sha=commit,
            session_id="session",
            source_push_id="push",
            captured_at=now,
        )
        row = AttributedCompletion(
            org_id=ORG,
            session_id="session",
            inference_call_id=call_id,
            repo=REPO,
            commit_sha=commit,
            file_path="file.py",
            similarity_score=1.0,
            attribution_source=AttributionSource.JACCARD,
            decisions=[],
            ci_outcomes=[
                CIOutcome(
                    org_id=ORG,
                    provider=CIProvider.GITHUB_ACTIONS,
                    run_id="run-" + commit,
                    repo=REPO,
                    commit_sha=commit,
                    branch="main",
                    result=CIResult.PASSED,
                    workflow_name="tests",
                    captured_at=now,
                )
            ],
            provenance=Provenance("1", 0),
            split="train",
        )
        return replace(row, session_commit_observations=(observation,))

    # model_a: one unattributed on-time call, two attributed on-time calls,
    # and one attributed LATE call (observed_at > now) -- the clock-skew
    # Scenario for the Attribution decay diagnostic.
    completions = [
        _call("a0", "model_a", now - timedelta(microseconds=3)),
        _call("a1", "model_a", now - timedelta(microseconds=2)),
        _call("a2", "model_a", now - timedelta(microseconds=1)),
        _call("a3", "model_a", now + timedelta(microseconds=1)),
        _call("b0", "model_b", now - timedelta(microseconds=4)),
        _call("b1", "model_b", now - timedelta(microseconds=3)),
        _call("b2", "model_b", now - timedelta(microseconds=2)),
        _call("b3", "model_b", now - timedelta(microseconds=1)),
    ]
    attributed = [
        _row("a1", "model_a", now - timedelta(microseconds=2), "a" * 40),
        _row("a2", "model_a", now - timedelta(microseconds=1), "b" * 40),
        _row("a3", "model_a", now + timedelta(microseconds=1), "c" * 40),
        _row("b0", "model_b", now - timedelta(microseconds=4), "0" * 40),
        _row("b1", "model_b", now - timedelta(microseconds=3), "1" * 40),
        _row("b2", "model_b", now - timedelta(microseconds=2), "2" * 40),
        _row("b3", "model_b", now - timedelta(microseconds=1), "3" * 40),
    ]

    attribution = model_report._attribution_outcomes_by_model(
        completions, attributed, None, now
    )
    ci = model_report._ci_outcomes_by_model(
        attributed, completions, None, now, CIGrain.COMMIT
    )
    windowed_calls, windowed_rows = model_report_inputs(
        completions, attributed, None, now
    )

    # The late attributed call a3 is dropped from both decay diagnostics.
    assert attribution["model_a"] == [False, True, True]
    assert ci["model_a"] == [True, True]
    assert attribution["model_b"] == [True, True, True, True]
    assert ci["model_b"] == [True, True, True, True]

    # The attribution decay cohort equals the headline model_report_inputs
    # completion cohort: same per-model counts, same late-call exclusion.
    windowed_by_model: dict[str, int] = {}
    for call in windowed_calls:
        windowed_by_model[call.model] = windowed_by_model.get(call.model, 0) + 1
    assert windowed_by_model == {"model_a": 3, "model_b": 4}
    assert {
        model: len(rows) for model, rows in attribution.items()
    } == windowed_by_model

    # The attribution decay successes equal the headline attribution_rate
    # numerator (non-abandoned rows in model_report_inputs).
    headline_attracted_by_model: dict[str, int] = {}
    for row in windowed_rows:
        if row.abandonment is not None:
            continue
        model = next(
            c.model
            for c in windowed_calls
            if c.inference_call_id == row.inference_call_id
        )
        headline_attracted_by_model[model] = (
            headline_attracted_by_model.get(model, 0) + 1
        )
    assert headline_attracted_by_model == {"model_a": 2, "model_b": 4}
    assert {
        model: sum(rows) for model, rows in attribution.items()
    } == headline_attracted_by_model
