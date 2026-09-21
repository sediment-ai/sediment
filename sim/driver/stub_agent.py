# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in agent that emits the traffic a live one would, without spend.

``run.py --dry-run`` uses this instead of ``claude -p`` / ``codex exec``. It
exists for one reason: the acceptance check for a driver deployment is "do all
three funnel layers grow?", and answering that should not cost API credits or
require gateway credentials. An operator wires up compose, runs the dry run,
and finds out whether the plumbing is right *before* spending anything on live
agents.

A real agent is observable to the pipeline in three ways, and the stub does
all three:

1. it edits the working tree (here: applies the pair's known gold patch);
2. its gateway traffic becomes an InferenceCall;
3. its OTLP export becomes a DeveloperDecision.

Both wire payloads are the **frozen fixtures**, retargeted only in the fields
that identify this run (session id, tool-use id, prompt, completion text). A
driver-invented payload would let the dry run pass against shapes the real
translators never see, which is the opposite of useful.

Honest limit: a rejected decision is only synthesized for sources that have a
frozen reject fixture. Claude Code does; Codex ships accept fixtures only
(`packages/capture/tests/fixtures/otlp/codex/`), so a stubbed Codex rejection
emits its completion and no decision. That is a gap in the *stub*, not in live
Codex, and it is visible rather than papered over with an invented payload.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Every stub fact carries these prefixes so drift_report.py can exclude
# stub traffic from observed shapes (STUB_SESSION_PREFIX there mirrors
# SESSION_PREFIX here; test_driver.py pins them together).
SESSION_PREFIX = "sess-driver-"
CALL_PREFIX = "toolu-driver-"

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "packages/capture/tests/fixtures"
ORG = "simcorp"

GATEWAY_FIXTURE = FIXTURES / "litellm_standard_logging_object.json"
# Per (source, disposition). A missing entry means "no frozen fixture for
# that shape" — see the module docstring.
DECISION_FIXTURES = {
    ("claude-code", "accept"): FIXTURES
    / "otlp/claude_code/tool_decision_accept_user.json",
    ("claude-code", "reject"): FIXTURES
    / "otlp/claude_code/tool_decision_reject_user.json",
    ("codex", "accept"): FIXTURES / "otlp/codex/accept_user_explicit.json",
}


def _set_attrs(payload: dict, overrides: dict[str, str]) -> dict:
    """Override OTLP log-record string attributes in place, by key.

    Only touches keys already present: the fixture defines the shape, and
    adding attributes here would be inventing wire the translators never saw.
    """
    for resource in payload.get("resourceLogs", []):
        for scope in resource.get("scopeLogs", []):
            for record in scope.get("logRecords", []):
                for attr in record.get("attributes", []):
                    if attr.get("key") in overrides and "stringValue" in attr.get(
                        "value", {}
                    ):
                        attr["value"]["stringValue"] = overrides[attr["key"]]
    return payload


def gateway_payload(task, session_id: str, call_id: str, completion_text: str) -> dict:
    """The frozen LiteLLM payload, retargeted at this task."""
    payload = json.loads(GATEWAY_FIXTURE.read_text())
    payload["litellm_call_id"] = call_id
    payload["messages"] = [{"role": "user", "content": task.prompt}]
    response = payload.get("response")
    if isinstance(response, dict):
        response["id"] = f"chatcmpl-{call_id}"
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].setdefault("message", {})
            message["content"] = completion_text
    metadata = payload.setdefault("metadata", {}).setdefault("requester_metadata", {})
    metadata["session_id"] = session_id
    metadata["org_id"] = ORG
    return payload


def decision_payload(task, session_id: str, tool_use_id: str) -> dict | None:
    """The frozen OTLP payload for this source and disposition, retargeted.

    ``tool_use_id`` is set to the completion's ``call_id`` so the decision can
    join it — a dry run that skipped that join would report a funnel the real
    pipeline cannot reproduce.
    """
    fixture = DECISION_FIXTURES.get((task.agent, task.disposition))
    if fixture is None:
        return None
    payload = json.loads(fixture.read_text())
    return _set_attrs(
        payload,
        {"session.id": session_id, "tool_use_id": tool_use_id, "call_id": tool_use_id},
    )


def mark_session(clone: Path, session_id: str, tool: str) -> bool:
    """Record the attribution marker a real agent's hook would.

    The third thing a live agent does to the pipeline, and the easiest to
    forget: Claude Code's ``PostToolUse`` entry runs ``mark``, the repo's
    ``post-commit`` turns the marker into a note, and ``pre-push`` ships the
    note. Without it the commit carries no note, attribution falls back to
    jaccard, and the RLVR export reports ``no_attributed_commit`` for every
    rollout — which is precisely what the first full dry run produced.

    Runs the real stamper, so the marker format cannot drift from the client.
    """
    stamper = REPO_ROOT / "scripts" / "sediment_attribution.py"
    payload = json.dumps({"session_id": session_id, "cwd": str(clone)})
    result = subprocess.run(
        [sys.executable, str(stamper), "mark", "--tool", tool],
        cwd=clone,
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0


def apply_reference_patch(clone: Path, task) -> bool:
    """Write the pair's reference content into the working tree.

    Read out of the repo's own history (``git show <gold_sha>:<module>``)
    rather than re-derived: the gold commit is the definition, and a second
    copy here would drift from ``gen_repo.py``.
    """
    result = subprocess.run(
        ["git", "-C", str(clone), "show", f"{task.gold_sha}:{task.module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return False
    (clone / task.module).write_text(result.stdout, encoding="utf-8")
    return True


def make_stub_agent(api: str, token: str):
    """Build the ``invoke`` callable ``run.run()`` expects."""
    from run import AgentRun, _post  # the runner loads this module as a sibling

    def stub(task, clone: Path, **_) -> AgentRun:
        session_id = f"{SESSION_PREFIX}{task.task_id}"
        call_id = f"{CALL_PREFIX}{task.task_id}"
        notes = []

        # A rejected edit leaves the tree untouched — that is what rejection
        # means, and it is why the retry family's first member pushes nothing.
        edited = task.disposition == "accept" and apply_reference_patch(clone, task)
        notes.append("edited" if edited else "no edit")
        if edited and mark_session(clone, session_id, task.agent):
            notes.append("marked")

        text = (clone / task.module).read_text() if edited else f"# {task.prompt}\n"
        status, _ = _post(
            f"{api.rstrip('/')}/ingest/gateway",
            json.dumps(
                {
                    "provider": "litellm",
                    "session_id": session_id,
                    "user_id": "dev@simcorp.example",
                    "payload": gateway_payload(task, session_id, call_id, text),
                }
            ).encode(),
            {"Authorization": f"Bearer {token}"},
        )
        notes.append(f"gateway={status}")

        decision = decision_payload(task, session_id, call_id)
        if decision is None:
            notes.append(f"decision=skipped (no frozen {task.agent} reject fixture)")
        else:
            status, _ = _post(
                f"{api.rstrip('/')}/v1/logs",
                json.dumps(decision).encode(),
                {"Authorization": f"Bearer {token}"},
            )
            notes.append(f"otlp={status}")
        return AgentRun(task.task_id, 0, "; ".join(notes), "")

    return stub
