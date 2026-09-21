# SPDX-License-Identifier: AGPL-3.0-or-later
"""Driver task list: scripted fix-the-bug work over the executable pairs.

The five SWE-bench-style pairs ``gen_repo.py`` builds are red at their base
commit and green at their gold commit, so an agent's attempt has a
deterministic verdict — run the pair's tests. That is the whole reason the driver
can score live agents at all without a human in the loop.

Two families:

- ``fix`` — one task per pair, per agent. Ordinary work: read the failing
  test, change the module, commit.
- ``retry`` — the same task issued **twice in one session**, with the first
  proposed edit rejected at the agent's own permission surface. This is the
  driver's only source of same-bucket DPO material: two completions, same
  prompt, same model, one rejected and one accepted.

The retry pair's two members carry a **byte-identical prompt**. That is load
bearing, not tidiness: DPO pairs bucket by (prompt, model), so steering the
retry through the prompt text ("try again, but…") would split the bucket and
produce nothing pairable. The rejection is expressed as a disposition the
driver applies to the agent surface — which is also what a real
reject-then-retry looks like from the pipeline's side.

Task ids are stable across runs (``<agent>-<family>-<module stem>[-retry]``)
so a run's rows can be joined to the previous run's.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Agents the driver runs live. Copilot is absent on purpose: no completion
# capture path exists for it, so it stays scenario fixture replay.
AGENTS = ("claude-code", "codex")

# One intent per executable pair, keyed by module stem. Same terse register as
# the scenario prompts ("add late fees"), phrased as the fix the pair's
# failing test already describes. Deliberately does not name the bug: the
# agent has to read the test, which is what makes the completion worth
# capturing.
_INTENTS = {
    "proration": "fix the prorated charge calculation so partial periods are proportional",
    "tax": "fix VAT rounding so it rounds half up instead of truncating",
    "totals": "fix invoice totals so credit lines reduce the total",
    "currency": "fix decimal-string to minor-unit conversion so it is exact",
    "discounts": "fix percentage discounts so they scale with the amount",
}
_FALLBACK_INTENT = "fix the failing test in {module}"

# Pairs that also get the retry/regenerate treatment. Two is enough to make
# the family measurable and keeps a run's live-agent spend bounded; every
# pair still gets a plain fix task.
RETRY_PAIR_STEMS = ("proration", "tax")

# Agents whose surface can deny a proposed edit AND emit the rejected
# decision — the two halves a reject task needs.
# claude-code: PreToolUse deny hook under acceptEdits puts tool_decision
# Edit/reject on the wire. codex: no reachable deny in headless exec —
# read-only answers in prose (never proposes), workspace-write with
# approval_policy=untrusted silently auto-applies, and an interactive deny
# emits no tool_decision in the supported integration; codex-rs has the
# denied emit upstream but `codex exec` cannot reach it. Copilot has no
# live arm (above). pi is not a driver agent; its reject is
# wire-representable but the shim has no deny gate yet.
REJECT_CAPABLE = ("claude-code",)


@dataclass(frozen=True)
class Task:
    """One scripted unit of agent work.

    ``disposition`` is an instruction to the *driver*, never to the model:
    ``reject`` means deny the agent's first proposed edit at its permission
    surface, so the pipeline sees a real rejected decision.
    """

    task_id: str
    agent: str
    family: str  # "fix" | "retry"
    module: str
    test: str
    base_sha: str
    gold_sha: str
    verification_command: str
    prompt: str
    disposition: str  # "accept" | "reject"
    retry_of: str | None = None


def _intent(module: str) -> str:
    stem = Path(module).stem
    return _INTENTS.get(stem) or _FALLBACK_INTENT.format(module=module)


def build_tasks(manifest: dict, agents: tuple[str, ...] = AGENTS) -> list[Task]:
    """The full scripted task list for one run, from a generator manifest.

    Takes the manifest rather than importing ``gen_repo``: the seeder already
    wrote it, and this keeps the task list a pure function of recorded data
    that a test can hand-build.
    """
    tasks: list[Task] = []
    for agent in agents:
        for pair in manifest["executable_pairs"]:
            stem = Path(pair["module"]).stem
            prompt = _intent(pair["module"])
            common = {
                "agent": agent,
                "module": pair["module"],
                "test": pair["test"],
                "base_sha": pair["base_sha"],
                "gold_sha": pair["gold_sha"],
                "verification_command": pair["verification_command"],
                "prompt": prompt,
            }
            tasks.append(
                Task(
                    task_id=f"{agent}-fix-{stem}",
                    family="fix",
                    disposition="accept",
                    **common,
                )
            )
            if stem in RETRY_PAIR_STEMS and agent in REJECT_CAPABLE:
                first = f"{agent}-retry-{stem}"
                tasks.append(
                    Task(
                        task_id=first,
                        family="retry",
                        disposition="reject",
                        **common,
                    )
                )
                tasks.append(
                    Task(
                        task_id=f"{first}-retry",
                        family="retry",
                        disposition="accept",
                        retry_of=first,
                        **common,
                    )
                )
    return tasks


def retry_pairs(tasks: list[Task]) -> list[tuple[Task, Task]]:
    """(rejected, accepted) task pairs — the DPO-bucket material.

    A pair whose partner is missing is dropped rather than half-reported: a
    lone rejected attempt is not a preference pair, and counting it as one
    would overstate what the run produced.
    """
    by_id = {task.task_id: task for task in tasks}
    return [
        (by_id[task.retry_of], task)
        for task in tasks
        if task.retry_of is not None and task.retry_of in by_id
    ]


@dataclass(frozen=True)
class Outcome:
    """The deterministic verdict on one attempt."""

    task_id: str
    passed: bool
    changed_module: bool
    detail: str


def check_outcome(clone: Path, task: Task) -> Outcome:
    """Run the pair's tests in ``clone`` and report the verdict.

    Verdict is the test result, never a diff against the gold patch: the gold
    patch is one correct answer, and an agent that writes a different correct
    fix has not failed. What the gold patch guarantees is that a passing
    verdict is *reachable*, which is what makes a red result the agent's.

    ``changed_module`` separates "tried and failed" from "did nothing" — both
    are red, and only the second means the driver never got the agent to act.
    """
    if not (clone / task.module).exists():
        return Outcome(
            task_id=task.task_id,
            passed=False,
            changed_module=False,
            detail=f"{task.module} is missing from the clone",
        )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", task.test],
        cwd=clone,
        capture_output=True,
        text=True,
        timeout=300,
        # Stale bytecode can mask an edited module when sizes match; the same
        # guard sim/tests/test_gen_repo.py uses for subprocess pytest.
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    tail = (result.stdout or result.stderr).strip().splitlines()
    return Outcome(
        task_id=task.task_id,
        passed=result.returncode == 0,
        changed_module=_module_differs_from_base(clone, task),
        detail=tail[-1] if tail else f"exit {result.returncode}",
    )


def _module_differs_from_base(clone: Path, task: Task) -> bool:
    """True when the module's working-tree content differs from its base
    commit — i.e. the agent actually edited it."""
    result = subprocess.run(
        ["git", "diff", "--quiet", task.base_sha, "--", task.module],
        cwd=clone,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # 0 = identical, 1 = differs; anything else (bad sha, not a repo) is not a
    # claim we can make, so report no change rather than invent one.
    return result.returncode == 1
