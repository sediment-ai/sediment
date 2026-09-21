# SPDX-License-Identifier: AGPL-3.0-or-later
"""Group 1 sim scenarios green in CI.

One shared scenario run per module (the scenarios carry their own asserts —
a red scenario fails the fixture); the tests here pin the run's externally
visible contract: the manifest and the precision floors.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sediment_core import FactStore
from sediment_derive import RepositoryIdentity

HERE = Path(__file__).resolve().parent


def _load(name: str):
    if name in sys.modules:  # share one instance with the sibling loaders
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE.parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # before exec: dataclasses needs it registered
    spec.loader.exec_module(module)
    return module


scenarios = _load("scenarios")
precision_report = _load("precision_report")

MANIFEST_KEYS = {
    "scenario",
    "inference_call_id",
    "expected_commit",
    "expected_file",
    "expected_attribution",
    "true_link",
    "expected_reward_min",
    "notes",
}


@pytest.fixture(scope="module")
def sim_run(tmp_path_factory, postgres_database_factory):
    return scenarios.run_all(
        tmp_path_factory.mktemp("sim"), postgres_database_factory()
    )


def test_manifest_shape_and_totals(sim_run):
    # One row per completion, LITERALLY: the manifest's ids are exactly the
    # store's, so a duplicate row can never mask a forgotten one (a bare
    # count equality could — review-closeout finding).
    from sediment_core.postgres_engine import create_postgres_engine

    engine = create_postgres_engine(sim_run.database_url)
    store = FactStore(engine)
    try:
        stored_ids = {
            call.inference_call_id for call in store.read_inference_calls(scenarios.ORG)
        }
    finally:
        engine.dispose()
    assert {row["inference_call_id"] for row in sim_run.rows} == stored_ids
    assert len(sim_run.rows) == scenarios.EXPECTED_TOTALS["inference_calls"]
    assert all(set(row) == MANIFEST_KEYS for row in sim_run.rows)
    on_disk = [
        json.loads(line) for line in sim_run.manifest_path.read_text().splitlines()
    ]
    assert on_disk == sim_run.rows
    # A positive expectation always names its commit and file; a negative
    # negative-control row never produces an attribution.
    for row in sim_run.rows:
        if row["expected_attribution"] is not None:
            assert row["expected_commit"] and row["expected_file"], row
        # true_link rows carry a target unless they are pure negatives.
        if row["true_link"]:
            assert row["expected_commit"] and row["expected_file"], row


def test_precision_floors_hold(sim_run):
    report = precision_report.compute_report(sim_run)
    assert report["failures"] == [], report["failures"]
    # The sweep is the tradeoff curve: recall must not DECREASE as the
    # threshold loosens, and the gradient rows make it strictly grow
    # somewhere between 0.7 and 0.4.
    recalls = [report["sweep"][t].recall for t in sorted(report["sweep"], reverse=True)]
    assert recalls == sorted(recalls), recalls
    assert report["sweep"][0.4].recall > report["sweep"][0.7].recall


def test_precision_report_main_is_green(
    sim_run, tmp_path, postgres_database_factory, monkeypatch, capsys
):
    # The CLI contract: green run → exit 0 and a printed report. Reuses a
    # fresh workdir (main() runs its own scenario pass) only if cheap enough
    # — it is: the whole run is a few seconds.
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", postgres_database_factory())
    rc = precision_report.main(["--workdir", str(tmp_path / "cli")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "behavior axis" in out and "sweep" in out


def test_synthetic_workflow_identity_survives_capture_and_redelivery(
    tmp_path, postgres_database_factory
):
    identities = []
    for index, workflows in enumerate((("test", "lint"), ("lint", "test"))):
        with scenarios.SimWorld(
            tmp_path / str(index), postgres_database_factory()
        ) as world:
            head = world.head()
            for workflow in workflows:
                world.fire_ci(
                    head,
                    workflow=workflow,
                    conclusion="failure",
                    run_id=101 if workflow == "test" else 102,
                    run_attempt=1,
                )
            world.fire_ci(
                head, workflow="test", conclusion="success", run_id=101, run_attempt=2
            )
            world.fire_ci(
                head,
                workflow="test",
                conclusion="success",
                run_id=101,
                run_attempt=2,
                expect_stored=False,
            )
            with world.store() as store:
                outcomes = store.read_ci_outcomes(scenarios.ORG)
            assert len(outcomes) == 3
            by_workflow = {}
            for outcome in outcomes:
                assert outcome.workflow_id == str(outcome.raw["workflow_id"])
                by_workflow.setdefault(outcome.workflow_name, set()).add(
                    outcome.workflow_id
                )
            assert all(len(ids) == 1 for ids in by_workflow.values())
            assert by_workflow["test"].isdisjoint(by_workflow["lint"])
            identities.append(by_workflow)
    assert identities[0] == identities[1]


def test_sim_repository_identity_survives_capture_and_scoped_derivation(
    tmp_path, postgres_database_factory
):
    with scenarios.SimWorld(tmp_path, postgres_database_factory()) as world:
        session = "sess-sim-identity"
        call_id = world.fire_gateway(
            session, scenarios._XPC_REMINDERS, prompt="add dunning reminders"
        )
        before = world.head()
        after = world.write_commit(
            "app/billing/reminders.py", scenarios._XPC_REMINDERS, "Add reminders"
        )
        world.stamp_notes([session])
        push_id = world.fire_push(before, after)
        assert world.fire_push(before, after, expect_stored=False) == push_id
        ci_id = world.fire_ci(after, workflow="test", conclusion="success", run_id=991)
        assert (
            world.fire_ci(
                after,
                workflow="test",
                conclusion="success",
                run_id=991,
                expect_stored=False,
            )
            == ci_id
        )
        with world.store() as store:
            [push] = store.read_pushes(scenarios.ORG)
            [outcome] = store.read_ci_outcomes(scenarios.ORG)
        identity = RepositoryIdentity("github", "git.simcorp.example", "90000001")
        assert (
            push.repository_provider,
            push.repository_host,
            push.repository_id,
        ) == (identity.provider, identity.host, identity.repository_id)
        assert (
            outcome.repository_provider,
            outcome.repository_host,
            outcome.repository_id,
        ) == (identity.provider, identity.host, identity.repository_id)
        [attribution] = world.derive_for(after)
        assert attribution.inference_call_id == call_id
        assert attribution.source_push_id == push_id
        assert attribution.repository_identity == identity
        assert world.derive() == [attribution]
        assert world.derive(pushes=[push, push]) == [attribution]
        assert (
            world.derive(pushes=[push.model_copy(update={"repository_id": "90000002"})])
            == []
        )
        # The existing post-Push grace also includes a call observed after the
        # last repository Fact. The harness boundary cannot expire that call.
        late_commit = world.write_commit(
            "app/billing/notices.py", scenarios._XPC_NOTICES, "Add notices"
        )
        world.stamp_notes([session])
        world.fire_push(after, late_commit)
        late_call_id = world.fire_gateway(
            session, scenarios._XPC_NOTICES, prompt="add dunning notices"
        )
        [late_attribution] = world.derive_for(late_commit)
        assert late_attribution.inference_call_id == late_call_id
        assert late_attribution.repository_identity == identity


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_sim_uses_its_capture_credential_and_restores_preimported_settings(
    tmp_path, postgres_database_factory, monkeypatch
):
    database_url = postgres_database_factory()
    monkeypatch.setenv("SEDIMENT_ORG_ID", "simcorp")
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", database_url)
    from pydantic import SecretStr
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "operator_token", SecretStr("sim-test-operator"))
    monkeypatch.setattr(settings, "ingest_tokens", {"client": SecretStr("capture")})
    monkeypatch.setattr(settings, "github_webhook_secret", "sim-test-webhook")
    monkeypatch.setattr(settings, "api_bearer_token", "")
    with scenarios.SimWorld(tmp_path / "separate-credential", database_url) as world:
        response = world.client.get("/v1/me", headers=world._auth())
        assert response.status_code == 200
        assert response.json()["authority"] == "ingest"
    assert settings.api_bearer_token == ""
