# SPDX-License-Identifier: AGPL-3.0-or-later
"""Driver runner: drive live agents at the sim clone, then audit the funnel.

One run = N scripted tasks per agent against ``<workdir>/clone``, each
committed and pushed, followed by a **funnel self-check that fails loudly**.

The driver counts every traffic layer before and after each run. If a layer
does not grow, the run exits nonzero and names that layer. This catches
configuration failures that otherwise leave partial capture without a
failing agent command.

Layer 1 requires both completions and decisions to grow. Every scripted task
requires an edit, so a run with zero decisions fails the self-check.

Usage:
    # prove the deployment is wired, no API spend (stub_agent.py)
    python sim/driver/run.py --workdir /tmp/sim-driver --tasks 3 \\
        --api http://127.0.0.1:8000 --database-url "$SEDIMENT_DATABASE_URL" --dry-run
    # then the live agents
    python sim/driver/run.py --workdir /tmp/sim-driver --tasks 3 \\
        --api http://127.0.0.1:8000 --database-url "$SEDIMENT_DATABASE_URL"

Agent invocation is injectable (``invoke=``) so the suite can exercise
everything around it without spending API credits; CI never drives a live
agent.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))
from scripts.operator_http import open_request, validate_url  # noqa: E402

FIXTURES = REPO_ROOT / "packages/capture/tests/fixtures"
ORG = "simcorp"


def _load_sibling(name: str, directory: Path = HERE):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tasks_mod = _load_sibling("tasks")
# For the commit identity only — the sim corpus's own, so the driver's
# commits match its history instead of duplicating the literal here.
gen_repo = _load_sibling("gen_repo", HERE.parent)

# The three traffic layers. Each names the fact tables that must grow for
# that layer to have carried anything.

FUNNEL_LAYERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gateway", ("inference_calls", "developer_decisions")),
    ("push", ("pushes",)),
    ("ci", ("ci_outcomes",)),
)


@dataclass
class FunnelCheck:
    """Per-table before/after counts and the verdict they imply."""

    before: dict[str, int]
    after: dict[str, int]
    empty_layers: list[str] = field(default_factory=list)

    def delta(self, table: str) -> int:
        return self.after.get(table, 0) - self.before.get(table, 0)

    def report(self) -> list[str]:
        lines = []
        for layer, tables in FUNNEL_LAYERS:
            parts = [f"{t} +{self.delta(t)}" for t in tables]
            status = "EMPTY" if layer in self.empty_layers else "ok"
            lines.append(f"{status:<5} funnel[{layer}]: {', '.join(parts)}")
        return lines


def count_facts(store, org: str = ORG) -> dict[str, int]:
    """Visible fact counts per table, through ``FactStore`` reads only.

    ADR 0001: reads go through the store's API, never a hand-rolled SELECT —
    the store's count applies the same quarantine exclusion the derivations
    see, so a self-check cannot disagree with what the pipeline will use.
    """
    from sediment_core import FactTable

    return {t.value: store.count_facts(org, t) for t in FactTable}


def audit_funnel(before: dict[str, int], after: dict[str, int]) -> FunnelCheck:
    check = FunnelCheck(before=before, after=after)
    for layer, tables in FUNNEL_LAYERS:
        if any(check.delta(table) <= 0 for table in tables):
            check.empty_layers.append(layer)
    return check


@dataclass
class AgentRun:
    """What one live agent invocation produced, from the driver's side."""

    task_id: str
    returncode: int
    stdout: str
    stderr: str


def invoke_agent(task, clone: Path, *, timeout: int = 900) -> AgentRun:
    """Run one scripted task through the agent's own headless CLI.

    Claude Code takes the prompt positionally; ``disposition="reject"`` is
    expressed as a PreToolUse deny hook (``reject_settings.json``) under
    ``acceptEdits``: the agent genuinely proposes the edit and the surface
    denies it, so the pipeline sees a real rejected decision. Steering the
    rejection through the prompt would split the DPO bucket the retry family
    exists to fill (see ``tasks.py``). Codex runs accept tasks only:
    headless codex has no deny path that emits a decision, so codex is
    outside ``REJECT_CAPABLE``.

    Both CLIs route through the operator's configured gateway; nothing here
    talks to a model provider directly.
    """
    reject = task.disposition == "reject"
    if task.agent == "claude-code":
        argv = ["claude", "-p", task.prompt]
        if reject:
            # Plan mode does not propose the repo edit (no Edit decision to
            # reject) and emits a
            # Write/accept on its own plan file — a mislabeled edit-tool
            # decision, worse than none. The deny hook keeps the agent in
            # acceptEdits so it genuinely proposes the edit, and the surface
            # rejects it: the wire carries tool_decision Edit/reject, which
            # is the fact the retry family exists to produce. The denial
            # reason tells the agent not to work around the refusal; it
            # rides the hook, never the prompt — retry pairs must stay
            # byte-identical (see tasks.py).
            argv += [
                "--permission-mode",
                "acceptEdits",
                "--settings",
                str(HERE / "reject_settings.json"),
            ]
        else:
            argv += ["--permission-mode", "acceptEdits"]
    elif task.agent == "codex":
        if reject:
            # Unreachable while REJECT_CAPABLE excludes codex (tasks.py):
            # headless codex exec has no deny path that emits a decision.
            # Reject unsupported tasks before launching the agent.
            raise ValueError(
                "codex reject tasks are not buildable (tasks.REJECT_CAPABLE)"
            )
        argv = ["codex", "exec", task.prompt, "--sandbox", "workspace-write"]
    else:
        raise ValueError(f"unknown agent {task.agent!r}")
    try:
        result = subprocess.run(
            argv, cwd=clone, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A missing CLI or a hung agent is a run-level fact, not a crash: the
        # funnel check still has to run and report what did land.
        return AgentRun(task.task_id, 127, "", str(exc))
    return AgentRun(task.task_id, result.returncode, result.stdout, result.stderr)


def _git(clone: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(clone), *args], capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_commit(clone: Path, message: str) -> str:
    """Commit with the sim corpus's own identity, never the host's.

    Both commits this driver makes are harness machinery against a fictional
    repo, so they carry ``gen_repo.AUTHOR`` — the identity the rest of
    simcorp-billing's history already has. Passed with ``-c`` rather than
    written to config: the driver must not mutate the operator's clone.

    Without an explicit identity, commits inherit the host's ambient Git
    configuration. Hosts without an identity, such as fresh containers and
    CI runners, fail with "Author identity unknown".
    """
    return _git(
        clone,
        "-c",
        f"user.name={gen_repo.AUTHOR_NAME}",
        "-c",
        f"user.email={gen_repo.AUTHOR_EMAIL}",
        "commit",
        "-m",
        message,
    )


def stage_regression(clone: Path, task) -> bool:
    """Put the pair's module back into its buggy state and commit that.

    Without this there is no task. The clone is at ``main``, which is *after*
    both commits of every executable pair, so the bug the prompt describes is
    already fixed on disk: the agent has nothing to change, nothing is pushed,
    and two of the three funnel layers stay empty. The first dry run failed
    exactly that way.

    Committing the regression rather than leaving it in the working tree is
    what makes the agent's later fix a real diff against a real parent, and it
    is why a *rejected* attempt correctly pushes nothing — the bug simply
    stays until the retry fixes it.

    Returns False when the module is already at its base content (the retry
    member of a pair, where the rejected attempt left the bug in place).
    """
    _git(clone, "checkout", task.base_sha, "--", task.module)
    if not _git(clone, "status", "--porcelain"):
        return False
    _git(clone, "add", "--", task.module)
    _git_commit(clone, f"{task.task_id}: reintroduce {task.module} bug")
    return True


def commit_and_push(clone: Path, task) -> str | None:
    """Commit the agent's work and push it. Returns the new HEAD, or None
    when the agent changed nothing (a rejected attempt, by design)."""
    if not _git(clone, "status", "--porcelain"):
        return None
    _git(clone, "add", "-A")
    _git_commit(clone, f"{task.task_id}: {task.prompt}")
    _git(clone, "push", "origin", "HEAD:refs/heads/main")
    return _git(clone, "rev-parse", "HEAD")


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    url = validate_url(url)
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with open_request(req, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except OSError as exc:
        return 0, str(exc)


def post_synthetic_forge(
    api: str, secret: str, before: str, after: str, clone_url: str
) -> list[str]:
    """Post the push and workflow_run webhooks a real forge would send.

    Needed because the local dry run has no GitHub: the remote is a bare repo
    on disk, so nothing generates webhooks, and without them two of the three
    funnel layers can never grow — the acceptance criterion would be
    unreachable. Against a real forge this is skipped entirely
    (``--forge github``) and the layers fill from actual deliveries.

    The payloads are the frozen wire fixtures with only the commit range,
    repository and ``clone_url`` retargeted, so the synthetic path exercises
    the same shapes the real one does rather than a driver-invented
    approximation. ``clone_url`` has to be the run's own remote: the push
    handler refreshes the mirror from it, and the RLVR export reads commits
    out of that mirror — point it at the fixture's github URL and the export
    silently produces zero task rows.
    """
    results = []
    for name, route, event, retarget in (
        ("github_push.json", "/ingest/github/push", "push", _retarget_push),
        ("github_workflow_run.json", "/ingest/github/ci", "workflow_run", _retarget_ci),
    ):
        payload = retarget(
            json.loads((FIXTURES / name).read_text()), before, after, clone_url
        )
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        status, body = _post(
            f"{api.rstrip('/')}{route}",
            raw,
            {"X-Hub-Signature-256": sig, "X-GitHub-Event": event},
        )
        results.append(f"{event}: status={status} {body[:120]}")
    return results


def _sim_repository(repository: dict, clone_url: str) -> dict:
    return {
        **repository,
        "name": "simcorp-billing",
        "full_name": f"{ORG}/simcorp-billing",
        "owner": {**repository.get("owner", {}), "login": ORG},
        "clone_url": clone_url,
    }


def _retarget_push(payload: dict, before: str, after: str, clone_url: str) -> dict:
    payload["ref"] = "refs/heads/main"
    payload["before"] = before
    payload["after"] = after
    payload["repository"] = _sim_repository(payload.get("repository", {}), clone_url)
    payload["commits"] = [{"id": after, "message": "tier b", "modified": []}]
    payload["head_commit"] = {"id": after, "message": "tier b"}
    return payload


def _retarget_ci(payload: dict, before: str, after: str, clone_url: str) -> dict:
    run = payload.get("workflow_run", {})
    payload["workflow_run"] = {**run, "head_sha": after, "head_branch": "main"}
    payload["repository"] = _sim_repository(payload.get("repository", {}), clone_url)
    return payload


@dataclass
class RunResult:
    outcomes: list  # tasks.Outcome
    agent_runs: list[AgentRun]
    pushed: list[str]
    funnel: FunnelCheck


def run(
    workdir: Path,
    *,
    database_url: str,
    api: str,
    webhook_secret: str,
    per_agent: int,
    forge: str = "synthetic",
    org: str = ORG,
    invoke=invoke_agent,
    count=None,
) -> RunResult:
    """Drive the scripted tasks, then audit. Never raises on agent failure —
    a dead agent must still reach the funnel check, which is the thing that
    reports it.

    ``org`` must match the deployment's ``SEDIMENT_ORG_ID``: tenancy binds
    server-side, so the funnel counts under any other org stay zero and the
    self-check reports a broken pipeline on a correctly wired deployment."""
    manifest = json.loads((workdir / "sim_repo_manifest.json").read_text())
    clone = workdir / "clone"
    every = tasks_mod.build_tasks(manifest)
    selected = [
        task
        for agent in tasks_mod.AGENTS
        for task in [t for t in every if t.agent == agent][:per_agent]
    ]

    # The clone's own origin, so the synthetic push points the mirror at the
    # repository this run actually wrote to.
    clone_url = _git(clone, "remote", "get-url", "origin")

    engine = None
    if count is None:
        from sediment_core import FactStore
        from sediment_core.postgres_engine import create_postgres_engine

        engine = create_postgres_engine(database_url)
        store = FactStore(engine)

        def count(_database_url, tenant):
            return count_facts(store, tenant)

    try:
        before = count(database_url, org)
        agent_runs, outcomes, pushed = [], [], []
        for task in selected:
            # HEAD before the regression, so the push's commit range covers both
            # the staged bug and the agent's fix — one push, as a developer would.
            head_before = _git(clone, "rev-parse", "HEAD")
            stage_regression(clone, task)
            agent_runs.append(invoke(task, clone))
            outcomes.append(tasks_mod.check_outcome(clone, task))
            head = commit_and_push(clone, task)
            if head is None:
                continue
            pushed.append(head)
            if forge == "synthetic":
                post_synthetic_forge(api, webhook_secret, head_before, head, clone_url)
        after = count(database_url, org)
        return RunResult(
            outcomes=outcomes,
            agent_runs=agent_runs,
            pushed=pushed,
            funnel=audit_funnel(before, after),
        )
    finally:
        if engine is not None:
            engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one driver pass")
    parser.add_argument("--workdir", required=True, help="the seeder's workdir")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SEDIMENT_DATABASE_URL"),
        help="PostgreSQL URL (default: SEDIMENT_DATABASE_URL)",
    )
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--tasks", type=int, default=3, help="scripted tasks per agent (default 3)"
    )
    parser.add_argument(
        "--forge",
        choices=("synthetic", "github"),
        default="synthetic",
        help="synthetic posts the push/CI webhooks itself (local compose, no "
        "GitHub); github posts nothing and expects real deliveries — the "
        "funnel audit runs immediately and does NOT wait for them, so use "
        "it only once the operator wiring settles delivery timing",
    )
    parser.add_argument(
        "--org",
        default=ORG,
        help=f"org id the funnel counts under (default: {ORG}). Must match "
        "the deployment's SEDIMENT_ORG_ID — tenancy binds server-side, so "
        "counting any other org reads zero and fails the self-check",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="drive stubbed agents instead of claude/codex: same wire shapes, "
        "no API spend. Use it to prove the deployment is wired before paying "
        "for a live run",
    )
    args = parser.parse_args(argv)
    try:
        args.api = validate_url(args.api, base=True)
    except ValueError as error:
        print(f"error: invalid API URL: {error}", file=sys.stderr)
        return 2

    if not args.database_url:
        print(
            "error: set SEDIMENT_DATABASE_URL or pass --database-url", file=sys.stderr
        )
        return 2

    secret = os.environ.get("SEDIMENT_GITHUB_WEBHOOK_SECRET", "")
    if args.forge == "synthetic" and not secret:
        print(
            "error: --forge synthetic needs SEDIMENT_GITHUB_WEBHOOK_SECRET to "
            "sign the webhooks it posts",
            file=sys.stderr,
        )
        return 2

    invoke = invoke_agent
    if args.dry_run:
        token = os.environ.get("SEDIMENT_API_BEARER_TOKEN", "")
        if not token:
            print(
                "error: --dry-run needs SEDIMENT_API_BEARER_TOKEN to post the "
                "gateway and OTLP traffic a live agent would produce",
                file=sys.stderr,
            )
            return 2
        invoke = _load_sibling("stub_agent").make_stub_agent(args.api, token)

    result = run(
        Path(args.workdir),
        database_url=args.database_url,
        api=args.api,
        webhook_secret=secret,
        per_agent=args.tasks,
        forge=args.forge,
        org=args.org,
        invoke=invoke,
    )

    for outcome in result.outcomes:
        verdict = "green" if outcome.passed else "red"
        edited = "edited" if outcome.changed_module else "no edit"
        print(f"{verdict:<5} {outcome.task_id} ({edited}): {outcome.detail}")
    print(f"pushed {len(result.pushed)} commit(s)")
    for line in result.funnel.report():
        print(line)

    if result.funnel.empty_layers:
        sys.stdout.flush()
        print(
            "\nfunnel self-check FAILED: no new facts in "
            f"{', '.join(result.funnel.empty_layers)}. The run captured "
            "nothing at that layer — treat it as a broken pipeline, not a "
            "quiet run.",
            file=sys.stderr,
        )
        return 1
    print("funnel self-check passed: all three layers grew")
    return 0


if __name__ == "__main__":
    sys.exit(main())
