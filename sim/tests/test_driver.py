# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sim driver contract.

CI cannot drive live agents — that is API spend and a network dependency — so
the suite covers everything around the invocation and injects a stub in its
place. What is actually pinned here is the deterministic half:

- the task list's shape, including both members of every retry pair;
- outcome verdicts, checked against the known gold patches (gold → green,
  untouched base → red);
- the funnel self-check's **red** path, which is the whole reason the runner
  exists (six of eight first-window gaps failed silently);
- the drift report in both directions — identical input finds nothing, a
  mutated wire shape produces a named finding.

The live half (`claude -p`, `codex exec`, a real forge) is exercised through
`sim/driver/run.py`, not here.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parents[1] / "driver"


def _load(name: str, directory: Path = DRIVER):
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tasks_mod = _load("tasks")
run_mod = _load("run")
drift = _load("drift_report")
stub_mod = _load("stub_agent")
gen_repo = _load("gen_repo", DRIVER.parent)


def _bare_task(**overrides: str) -> tasks_mod.Task:
    """A Task with every field blank but the named ones — enough to exercise
    invocation without generating the corpus."""
    fields = {field.name: "" for field in dataclasses.fields(tasks_mod.Task)}
    return tasks_mod.Task(**{**fields, **overrides})


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> tuple[dict, Path]:
    """One generated repo for the whole module: generation is the slow part
    and it is byte-identical anyway."""
    out = tmp_path_factory.mktemp("gen")
    return gen_repo.generate(out), out


@pytest.fixture(scope="module")
def manifest(generated) -> dict:
    return generated[0]


def test_task_ids_are_stable_and_unique(manifest) -> None:
    first = tasks_mod.build_tasks(manifest)
    second = tasks_mod.build_tasks(manifest)
    ids = [t.task_id for t in first]
    assert ids == [t.task_id for t in second]  # stable across builds
    assert len(ids) == len(set(ids))  # and joinable, so unique
    assert all(t.agent in tasks_mod.AGENTS for t in first)


def test_every_pair_gets_a_fix_task_per_agent(manifest) -> None:
    tasks = tasks_mod.build_tasks(manifest)
    fixes = [t for t in tasks if t.family == "fix"]
    expected = len(manifest["executable_pairs"]) * len(tasks_mod.AGENTS)
    assert len(fixes) == expected
    assert {t.module for t in fixes} == {
        p["module"] for p in manifest["executable_pairs"]
    }


def test_both_members_of_every_retry_pair_are_present(manifest) -> None:
    # The retry family is the driver's only source of same-bucket DPO
    # material. A run that emits the rejected half and drops the accepted one
    # produces nothing pairable, and would look like ordinary task output.
    tasks = tasks_mod.build_tasks(manifest)
    pairs = tasks_mod.retry_pairs(tasks)
    # Rejects only exist for REJECT_CAPABLE agents (codex has no deny
    # path that emits a decision in the supported integration).
    assert len(pairs) == len(tasks_mod.RETRY_PAIR_STEMS) * len(tasks_mod.REJECT_CAPABLE)
    for rejected, accepted in pairs:
        assert rejected.disposition == "reject"
        assert accepted.disposition == "accept"
        assert accepted.retry_of == rejected.task_id
        assert rejected.agent == accepted.agent


def test_retry_pair_prompts_are_byte_identical(manifest) -> None:
    # DPO pairs bucket by (prompt, model). Steering the retry through the
    # prompt text would split the bucket and silently produce zero pairs —
    # the failure would look like "the model just did not retry".
    for rejected, accepted in tasks_mod.retry_pairs(tasks_mod.build_tasks(manifest)):
        assert rejected.prompt == accepted.prompt
        assert rejected.module == accepted.module


def test_retry_pairs_drops_a_half_pair(manifest) -> None:
    tasks = tasks_mod.build_tasks(manifest)
    orphan = [t for t in tasks if t.retry_of is not None][0]
    assert tasks_mod.retry_pairs([orphan]) == []


def _clone_at(tmp_path: Path, manifest_dir: Path, sha: str) -> Path:
    """A clone of the generated repo checked out at ``sha``."""
    source = manifest_dir / gen_repo.REPO_NAME
    target = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(source), str(target)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    subprocess.run(
        ["git", "-C", str(target), "checkout", "-q", sha],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return target


def test_outcome_is_red_at_base_and_green_at_gold(tmp_path, generated) -> None:
    # The gold patch does not define a correct answer — it proves a passing
    # verdict is reachable, which is what makes a red result the agent's
    # fault rather than a broken task.
    manifest, out = generated
    task = tasks_mod.build_tasks(manifest)[0]

    at_base = _clone_at(tmp_path / "base", out, task.base_sha)
    red = tasks_mod.check_outcome(at_base, task)
    assert red.passed is False
    assert red.changed_module is False  # nothing edited it

    at_gold = _clone_at(tmp_path / "gold", out, task.gold_sha)
    green = tasks_mod.check_outcome(at_gold, task)
    assert green.passed is True
    assert green.changed_module is True  # gold differs from base


def test_outcome_reports_a_missing_module_instead_of_crashing(
    tmp_path, generated
) -> None:
    manifest, out = generated
    task = tasks_mod.build_tasks(manifest)[0]
    at_base = _clone_at(tmp_path / "gone", out, task.base_sha)
    (at_base / task.module).unlink()
    outcome = tasks_mod.check_outcome(at_base, task)
    assert outcome.passed is False
    assert "missing from the clone" in outcome.detail


_FULL = {
    "inference_calls": 10,
    "developer_decisions": 10,
    "pushes": 10,
    "ci_outcomes": 10,
    "edit_observations": 0,
}


def _grown(**overrides: int) -> dict[str, int]:
    return {
        **{k: v + 1 for k, v in _FULL.items() if k != "edit_observations"},
        **overrides,
    }


def test_funnel_passes_when_every_layer_grows() -> None:
    check = run_mod.audit_funnel(_FULL, _grown())
    assert check.empty_layers == []


@pytest.mark.parametrize(
    ("stalled", "layer"),
    [
        ("inference_calls", "gateway"),
        ("developer_decisions", "gateway"),
        ("pushes", "push"),
        ("ci_outcomes", "ci"),
    ],
)
def test_funnel_names_the_layer_that_did_not_grow(stalled: str, layer: str) -> None:
    check = run_mod.audit_funnel(_FULL, _grown(**{stalled: _FULL[stalled]}))
    assert check.empty_layers == [layer]
    assert any(line.startswith("EMPTY") and layer in line for line in check.report())


def test_gateway_layer_needs_decisions_too() -> None:
    # Every driver task requires an edit, so inference calls without any
    # decisions indicate incomplete capture and must fail the self-check.
    stalled = _grown(developer_decisions=_FULL["developer_decisions"])
    check = run_mod.audit_funnel(_FULL, stalled)
    assert check.delta("inference_calls") == 1  # inference calls did grow
    assert "gateway" in check.empty_layers  # and the layer still fails


def test_run_exits_nonzero_on_an_induced_empty_layer(tmp_path, generated) -> None:
    """The red path end to end, through the runner's own main()."""
    manifest, out = generated
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "sim_repo_manifest.json").write_text(json.dumps(manifest))
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            str(out / gen_repo.REPO_NAME),
            str(workdir / "clone"),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )

    # An agent that does nothing: no edits, so nothing is pushed and no
    # layer can grow. This is exactly the shape of a broken driver.
    def dead_agent(task, clone, **_):
        return run_mod.AgentRun(task.task_id, 0, "", "")

    result = run_mod.run(
        workdir,
        database_url="unused",
        api="http://127.0.0.1:0",
        webhook_secret="s",
        per_agent=1,
        invoke=dead_agent,
        count=lambda _db, _org: dict(_FULL),
    )
    assert result.pushed == []
    assert set(result.funnel.empty_layers) == {"gateway", "push", "ci"}


def test_key_paths_collapses_lists_and_walks_nesting() -> None:
    paths = drift.key_paths({"a": {"b": 1}, "c": [{"d": 2}, {"e": 3}]})
    assert paths == {"a", "a.b", "c", "c[].d", "c[].e"}


def test_identical_shapes_produce_no_findings() -> None:
    baseline = drift.baseline_shapes()
    assert baseline, "the frozen fixtures must yield at least one source"
    assert drift.compare(baseline, {k: set(v) for k, v in baseline.items()}) == []


def test_a_dropped_wire_path_is_a_named_missing_finding() -> None:
    # The dangerous direction: translators read these paths, and when one
    # disappears upstream the translator degrades fail-soft and the facts get
    # quietly thinner. The report has to name the path.
    baseline = drift.baseline_shapes()
    source = sorted(baseline)[0]
    dropped = sorted(baseline[source])[0]
    observed = {k: set(v) for k, v in baseline.items()}
    observed[source].discard(dropped)

    findings = drift.compare(baseline, observed)
    missing = [f for f in findings if f.kind == "missing"]
    assert len(missing) == 1
    assert missing[0].source == source
    assert dropped in missing[0].paths
    assert dropped in missing[0].line()


def test_a_new_wire_path_is_added_not_missing() -> None:
    baseline = drift.baseline_shapes()
    source = sorted(baseline)[0]
    observed = {k: set(v) for k, v in baseline.items()}
    observed[source].add("brand.new.upstream.field")

    findings = drift.compare(baseline, observed)
    assert [f.kind for f in findings] == ["added"]
    assert findings[0].paths == ["brand.new.upstream.field"]


def test_a_source_with_no_traffic_is_unobserved_not_drift() -> None:
    # No traffic is not changed traffic. Reporting it as drift would fire on
    # every run that simply did not use that agent.
    baseline = drift.baseline_shapes()
    source = sorted(baseline)[0]
    observed = {k: set(v) for k, v in baseline.items() if k != source}
    findings = drift.compare(baseline, observed)
    assert [(f.source, f.kind) for f in findings] == [(source, "unobserved")]


def test_baseline_covers_the_agents_tier_b_drives() -> None:
    # If a translator stops producing facts from its own frozen fixture, the
    # drift report silently loses that source's coverage — and would then
    # report "no drift" forever.
    baseline = drift.baseline_shapes()
    assert "litellm" in baseline
    assert "claude-code" in baseline
    assert "codex" in baseline


def test_staging_puts_the_pair_back_into_its_red_state(tmp_path, generated) -> None:
    # The clone sits at main, after both commits of every pair, so the prompt
    # describes a bug that is already fixed on disk. Without staging the agent has
    # nothing to change, nothing is pushed, and two funnel layers stay empty.
    manifest, out = generated
    task = tasks_mod.build_tasks(manifest)[0]
    clone = _clone_at(tmp_path / "main", out, "main")

    assert tasks_mod.check_outcome(clone, task).passed is True  # green at main
    assert run_mod.stage_regression(clone, task) is True
    assert tasks_mod.check_outcome(clone, task).passed is False  # red again
    # Committed, not just dirty: the agent's later fix has to be a real diff
    # against a real parent.
    assert not run_mod._git(clone, "status", "--porcelain")


def test_staging_is_a_noop_when_already_red(tmp_path, generated) -> None:
    # The retry member's case: the rejected attempt left the bug in place, so
    # there is nothing to reintroduce and nothing to commit.
    manifest, out = generated
    task = tasks_mod.build_tasks(manifest)[0]
    clone = _clone_at(tmp_path / "red", out, "main")
    run_mod.stage_regression(clone, task)
    head = run_mod._git(clone, "rev-parse", "HEAD")
    assert run_mod.stage_regression(clone, task) is False
    assert run_mod._git(clone, "rev-parse", "HEAD") == head  # no second commit


def test_stub_applies_gold_from_history_not_a_second_copy(tmp_path, generated) -> None:
    manifest, out = generated
    task = tasks_mod.build_tasks(manifest)[0]
    clone = _clone_at(tmp_path / "stub", out, "main")
    run_mod.stage_regression(clone, task)

    assert stub_mod.apply_reference_patch(clone, task) is True
    gold = subprocess.run(
        ["git", "-C", str(clone), "show", f"{task.gold_sha}:{task.module}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    assert (clone / task.module).read_text() == gold


def test_stub_has_no_reject_fixture_for_codex(generated) -> None:
    # An honest gap, pinned so it cannot be quietly papered over with an
    # invented payload: codex ships accept fixtures only. build_tasks no
    # longer produces codex rejects at all (REJECT_CAPABLE), so the task is
    # hand-built — the stub pin stays as the second guard behind the gate.
    manifest, _ = generated
    codex_reject = _bare_task(
        task_id="cx", agent="codex", prompt="p", disposition="reject"
    )
    claude_reject = next(
        t
        for t in tasks_mod.build_tasks(manifest)
        if t.agent == "claude-code" and t.disposition == "reject"
    )
    assert stub_mod.decision_payload(codex_reject, "s", "toolu-1") is None
    assert stub_mod.decision_payload(claude_reject, "s", "toolu-1") is not None


def test_stub_decision_carries_the_completions_call_id(generated) -> None:
    # The join: decision.call_id must equal the completion's tool-use id,
    # or the pair can never become a attributed completion. A dry run that skipped this would
    # report a funnel the real pipeline cannot reproduce.
    from sediment_capture import parse_otlp_decisions

    manifest, _ = generated
    task = next(
        t
        for t in tasks_mod.build_tasks(manifest)
        if t.agent == "claude-code" and t.disposition == "accept"
    )
    payload = stub_mod.decision_payload(task, "sess-x", "toolu-driver-x")
    decisions = parse_otlp_decisions(payload, org_id="simcorp")
    assert decisions and all(d.call_id == "toolu-driver-x" for d in decisions)

    gateway = stub_mod.gateway_payload(task, "sess-x", "toolu-driver-x", "code")
    assert gateway["litellm_call_id"] == "toolu-driver-x"
    assert gateway["messages"] == [{"role": "user", "content": task.prompt}]


def test_seeder_requires_a_directory_for_every_git_call() -> None:
    # Force-push operations must target an explicit repository. Keep cwd
    # positional and required, independent of the process's working directory.
    import inspect

    seed_remote = _load("seed_remote")
    params = list(inspect.signature(seed_remote.git).parameters.values())
    assert params[0].name == "cwd"
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[0].default is inspect.Parameter.empty


def test_commits_do_not_need_an_ambient_git_identity(
    tmp_path, generated, monkeypatch
) -> None:
    # Reproduces the CI runner exactly: no global or system git config, so no
    # user.name/user.email anywhere. This landed red in CI while passing on a
    # laptop that happened to have a global identity — the driver must carry
    # the sim corpus's own identity rather than inherit the host's.
    manifest, out = generated
    task = tasks_mod.build_tasks(manifest)[0]
    clone = _clone_at(tmp_path / "noident", out, "main")
    subprocess.run(
        ["git", "-C", str(clone), "config", "--unset-all", "user.name"],
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "--unset-all", "user.email"],
        capture_output=True,
        timeout=60,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-gitconfig"))

    assert run_mod.stage_regression(clone, task) is True
    author = run_mod._git(clone, "log", "-1", "--format=%an <%ae>")
    assert author == gen_repo.AUTHOR  # the corpus's identity, not the host's

    # Asserting the author rather than "the commit did not crash", because the
    # crash is not reproducible off CI: with no configured identity git falls
    # back to a guess from the OS (user@hostname) and commits anyway, and only
    # a host whose guess is empty — a CI runner — fails outright. The author
    # check catches the same bug everywhere, since without the fix this commit
    # would carry whatever identity the host supplied.


def test_stub_prefix_pinned_between_stub_and_drift_report() -> None:
    # drift_report excludes stub facts by this prefix; if the two modules
    # ever disagree, one dry run silently blinds the drift report.
    assert drift.STUB_SESSION_PREFIX == stub_mod.SESSION_PREFIX


def test_otlp_attribute_lists_flatten_to_semantic_paths() -> None:
    raw = {
        "decision": {
            "attributes": [
                {"key": "tool_name", "value": {"stringValue": "Edit"}},
                {"key": "tool_use_id", "value": {"stringValue": "toolu-1"}},
            ],
            "body": {"stringValue": "tool_decision"},
        }
    }
    paths = drift._shape(raw)
    assert "decision.attributes.tool_name" in paths
    assert "decision.attributes.tool_use_id" in paths
    # The envelope's own structure no longer swallows the names.
    assert "decision.attributes[].key" not in paths

    # A dropped attribute is now a visible difference between two shapes.
    without = {
        "decision": {
            "attributes": [
                {"key": "tool_name", "value": {"stringValue": "Edit"}},
            ],
            "body": {"stringValue": "tool_decision"},
        }
    }
    missing = drift._shape(raw) - drift._shape(without)
    assert any(p.startswith("decision.attributes.tool_use_id") for p in missing)


def test_observed_shapes_excludes_dry_run_stub_facts(
    postgres_engine, postgres_database_url
) -> None:
    from datetime import UTC, datetime

    from sediment_core import (
        FactStore,
        GatewayProvider,
        InferenceCall,
        InferenceMessage,
        TextPart,
    )

    store = FactStore(postgres_engine)
    try:
        base = dict(
            org_id=drift.ORG,
            user_id="dev-1",
            gateway_provider=GatewayProvider.LITELLM,
            model="m",
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content="x")])
            ],
            output_messages=[
                InferenceMessage(role="assistant", parts=[TextPart(content="y")])
            ],
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        store.store_inference_call(
            InferenceCall(
                session_id=f"{stub_mod.SESSION_PREFIX}fix-late-fee",
                model_call_id="toolu-driver-x",
                raw={"stub_only_path": True},
                **base,
            )
        )
        store.store_inference_call(
            InferenceCall(
                session_id="sess-live-1",
                model_call_id="call-live",
                raw={"live_path": True},
                **base,
            )
        )
    finally:
        pass

    shapes = drift.observed_shapes(postgres_database_url)
    got = shapes.get(str(GatewayProvider.LITELLM), set())
    assert "live_path" in got
    assert "stub_only_path" not in got


def test_reject_disposition_uses_deny_hook_not_plan_mode(monkeypatch) -> None:
    # Plan mode yields a mislabeled Write/accept on
    # the plan file instead of the Edit/reject the retry family needs. The
    # reject arm must run acceptEdits with the deny-hook settings.
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    reject_task = _bare_task(
        task_id="t-reject",
        agent="claude-code",
        prompt="fix it",
        disposition="reject",
    )
    run_mod.invoke_agent(reject_task, Path("."))
    argv = captured["argv"]
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert "plan" not in argv
    i = argv.index("--settings")
    assert argv[i + 1].endswith("reject_settings.json")

    accept_task = _bare_task(
        task_id="t-accept", agent="claude-code", prompt="fix it", disposition="accept"
    )
    run_mod.invoke_agent(accept_task, Path("."))
    assert "--settings" not in captured["argv"]


def test_reject_settings_hook_denies_every_edit_tool() -> None:
    settings = json.loads((DRIVER / "reject_settings.json").read_text())
    (rule,) = settings["hooks"]["PreToolUse"]
    assert rule["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    (hook,) = rule["hooks"]
    assert '"permissionDecision":"deny"' in hook["command"].replace(" ", "")


def test_reject_tasks_exist_only_for_reject_capable_agents() -> None:
    # Headless codex has no deny path that emits
    # a decision, so its retry pairs would capture nothing (or worse). The
    # retry family is gated on REJECT_CAPABLE.
    manifest = {
        "executable_pairs": [
            {
                "module": "billing/proration.py",
                "test": "tests/test_proration.py",
                "base_sha": "a" * 40,
                "gold_sha": "b" * 40,
                "verification_command": "pytest tests/test_proration.py",
            }
        ]
    }
    tasks = tasks_mod.build_tasks(manifest)
    rejects = [t for t in tasks if t.disposition == "reject"]
    assert rejects and all(t.agent in tasks_mod.REJECT_CAPABLE for t in rejects)
    assert all(t.agent != "codex" for t in rejects)
    # codex still gets its plain fix task.
    assert any(t.agent == "codex" and t.family == "fix" for t in tasks)


def test_codex_reject_invocation_is_guarded() -> None:
    task = _bare_task(task_id="t", agent="codex", prompt="p", disposition="reject")
    with pytest.raises(ValueError, match="REJECT_CAPABLE"):
        run_mod.invoke_agent(task, Path("."))
