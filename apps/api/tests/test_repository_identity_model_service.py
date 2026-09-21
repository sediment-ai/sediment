# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scoped model service preloads exact renamed repository evidence."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "packages" / "derive" / "tests")
)

from gitfixtures import FIB, commit_all, make_remote, make_work_repo, run_git
from sediment_core import (
    CIOutcome,
    InferenceCall,
    InferenceMessage,
    Push,
    SessionCommitObservation,
    TextPart,
)
from sediment_derive import MirrorManager, read_repository_context
from sediment_export import OperationalReportScope
from sediment_api.services.operational_reports import generate_operational_model_report

ORG = "identity-model-service"
AT = datetime(2026, 9, 12, tzinfo=UTC)
BOUNDARY = AT + timedelta(hours=1)
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="101"
)


def test_scoped_model_service_keeps_inherited_observation_and_renamed_ci(
    tmp_path, postgres_store, capsys, monkeypatch
):
    work = make_work_repo(tmp_path)
    (work / "code.py").write_text(FIB)
    sha = commit_all(work, "synthetic fibonacci")
    run_git(
        work,
        "notes",
        "--ref=sediment",
        "add",
        "-m",
        json.dumps(
            {
                "v": 1,
                "sessions": [
                    {
                        "tool": "codex",
                        "session_id": "session",
                        "stamped_at": AT.isoformat(),
                    }
                ],
            }
        ),
        sha,
    )
    remote = make_remote(tmp_path, work)
    call = InferenceCall(
        inference_call_id="call",
        org_id=ORG,
        session_id="session",
        user_id="developer",
        gateway_provider="litellm",
        model="model",
        observed_at=AT - timedelta(minutes=1),
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="write fibonacci")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=FIB)])
        ],
    )
    push = Push(
        push_id="push",
        org_id=ORG,
        provider="github",
        repo="acme/old",
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=sha,
        captured_at=AT,
        **IDENTITY,
    )
    observation = SessionCommitObservation(
        observation_id="observation",
        org_id=ORG,
        repo=push.repo,
        commit_sha=sha,
        session_id=call.session_id,
        source_push_id=push.push_id,
        captured_at=AT,
    )
    ci = CIOutcome(
        outcome_id="ci",
        org_id=ORG,
        provider="github_actions",
        repo="acme/new",
        commit_sha=sha,
        run_id="run",
        branch="main",
        result="passed",
        captured_at=BOUNDARY,
        **IDENTITY,
    )
    postgres_store.store_inference_call(call)
    postgres_store.store_push(push)
    postgres_store.store_session_commit_observation(observation)
    postgres_store.store_ci_outcome(ci)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    with postgres_store.read_snapshot() as snapshot:
        context = read_repository_context(snapshot, ORG, as_of=BOUNDARY)
    mirrors.ensure(push, repository_context=context)
    scope = OperationalReportScope(
        AT - timedelta(hours=1), AT + timedelta(minutes=1), BOUNDARY
    )
    report = generate_operational_model_report(
        postgres_store, mirrors, ORG, scope, include_trends=True
    )
    assert (
        report.result.rows[0].completions
        == report.result.rows[0].attributed_inference_calls
        == 1
    )
    assert report.result.rows[0].ci_linked == report.result.rows[0].ci_passed == 1
    assert report.attributed_completions[0].session_commit_observations == (
        observation,
    )
    assert report.attributed_completions[0].ci_outcomes == [ci]
    assert report.result.attribution_share[0].git_notes_attributed == 1
    from sediment_export import generate_model_report_result, CIGrain
    from sediment_api.reports.model_report import _ci_outcomes_by_model

    direct = generate_model_report_result(
        postgres_store, mirrors, ORG, now=BOUNDARY, include_trends=True
    )
    assert direct.rows[0].ci_linked == direct.rows[0].ci_passed == 1
    assert direct.attribution_share[0].git_notes_attributed == 1
    assert _ci_outcomes_by_model(
        report.attributed_completions,
        report.completions,
        None,
        BOUNDARY,
        CIGrain.COMMIT,
        repository_context=report.repository_context,
    ) == {"model": [True]}
    # A sibling on another commit is outside the cohort read, but belongs to
    # the same provider/host run. It invalidates the otherwise passing trial.
    postgres_store.store_ci_outcome(
        CIOutcome.model_validate(
            {
                **ci.model_dump(),
                "outcome_id": "contradictory",
                "run_attempt": 2,
                "repository_id": "202",
                "commit_sha": "c" * 40,
                "result": "failed",
            }
        )
    )
    contradicted = generate_operational_model_report(
        postgres_store, mirrors, ORG, scope
    )
    assert contradicted.result.rows[0].completions == 1
    assert (
        contradicted.result.rows[0].ci_linked
        == contradicted.result.rows[0].ci_passed
        == 0
    )

    assert contradicted.result.ci_skipped["runs"]["conflicting_run_identity"] == 1
    assert (
        generate_model_report_result(postgres_store, mirrors, ORG, now=BOUNDARY)
        .rows[0]
        .ci_passed
        == 0
    )
    assert (
        _ci_outcomes_by_model(
            contradicted.attributed_completions,
            contradicted.completions,
            None,
            BOUNDARY,
            CIGrain.COMMIT,
            repository_context=contradicted.repository_context,
            ci_population=contradicted.ci_population,
        )
        == {}
    )

    from sediment_api.reports import model_report as cli

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return BOUNDARY if tz is None else BOUNDARY.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    for window in ([], ["--since-days", "30"]):
        assert (
            cli.main(
                [
                    "--org",
                    ORG,
                    "--database-url",
                    postgres_store._engine.url.render_as_string(hide_password=False),
                    "--mirror-path",
                    str(tmp_path / "mirrors"),
                    "--json",
                    "--trend",
                    "--compare",
                    "model",
                    "absent",
                    "--effect-decay",
                    *window,
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["rows"][0]["completions"] == 1
        assert payload["rows"][0]["ci_linked"] == payload["rows"][0]["ci_passed"] == 0
        assert payload["ci_skipped"]["runs"]["conflicting_run_identity"] == 1
