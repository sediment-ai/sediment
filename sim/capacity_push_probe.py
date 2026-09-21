# SPDX-License-Identifier: AGPL-3.0-or-later
"""One signed Push and its observed Git-note edge during a capacity run.

The caller owns the seeded sim repository, HTTP API, FactStore, and job timing.
File clone URLs require the runner's explicit isolated development policy.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import TypeAdapter
from sediment_capture import sign_payload
from sediment_cli.client import validate_server_url
from sediment_core import FactStore, ForgeProvider, NonEmptyId
from sediment_derive.notes import AttributionNote, AttributionSession
from sqlalchemy.exc import SQLAlchemyError

from sim import scenarios


@dataclass(frozen=True)
class PushProbe:
    session_id: str
    commit_sha: str
    body: bytes = field(repr=False)


def prepare_push(seed_workdir: Path, session_id: str) -> PushProbe:
    """Append one commit and note to the original, clean sim repository."""
    session_id = TypeAdapter(NonEmptyId).validate_python(session_id)
    repo = scenarios.gen_repo._Repo(
        (seed_workdir / scenarios.gen_repo.REPO_NAME).resolve(), random.Random(0)
    )
    if repo.git("status", "--porcelain"):
        raise ValueError("push_probe_dirty_seed")
    if repo.git("branch", "--show-current") != "main":
        raise ValueError("push_probe_seed_branch")
    before = repo.git("rev-parse", "HEAD")
    repo.clock = datetime.fromisoformat(repo.git("show", "-s", "--format=%cI", "HEAD"))
    repo.write("capacity/push-probe.txt", f"{session_id}\n{before}\n")
    after = repo.commit("Add capacity Push observation probe")
    note = AttributionNote(
        v=scenarios.SCHEMA_VERSION,
        sessions=[
            AttributionSession(
                tool=scenarios.TOOL,
                session_id=session_id,
                stamped_at=repo.clock.isoformat(),
            )
        ],
    )
    repo.git(
        "notes",
        f"--ref={scenarios.NOTES_REF}",
        "add",
        "-F",
        "-",
        "HEAD",
        stdin=note.model_dump_json(),
    )
    payload = scenarios._fixture("github_push.json")
    payload.update(ref="refs/heads/main", before=before, after=after, forced=False)
    payload["repository"].update(
        id=scenarios.REPOSITORY_ID,
        name="simcorp-billing",
        full_name=scenarios.REPO_FULL,
        clone_url=repo.path.as_uri(),
    )
    payload["repository"]["owner"]["login"] = "simcorp"
    payload["commits"] = [{"id": after, "message": "capacity Push probe"}]
    payload["head_commit"] = payload["commits"][0]
    return PushProbe(session_id, after, json.dumps(payload).encode())


class PushProbeFailure(RuntimeError):
    """A content-free failure with timing and completed receipt evidence."""

    def __init__(self, reason: str, result: dict):
        super().__init__(reason)
        self.result = {**result, "failure": reason, "failed_at": time.monotonic()}


def post_and_wait(
    url: str, *, secret: str, store: FactStore, probe: PushProbe, timeout: float
) -> dict:
    """Require a fresh observation, then verify the duplicate Push receipt.

    Monotonic timestamps share the caller's clock. An observation timeout proves
    missing completion; worker logs distinguish lock timeout from other causes.
    The caller owns the FactStore's connection/statement deadlines and cleanup.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("push_probe_invalid_timeout")
    url = validate_server_url(url)
    result = {"session_id": probe.session_id, "commit_sha": probe.commit_sha}
    key = (ForgeProvider.GITHUB, "git.simcorp.example", str(scenarios.REPOSITORY_ID))

    def observations():
        try:
            return store.read_session_commit_observations(
                scenarios.ORG,
                repository_commits={(key, probe.commit_sha)},
                session_ids={probe.session_id},
                limit=2,
            )
        except SQLAlchemyError:
            raise PushProbeFailure("push_probe_observation_read", result) from None

    if observations():
        raise PushProbeFailure("push_probe_preexisting_observation", result)
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": sign_payload(probe.body, secret),
    }
    result["started"] = time.monotonic()
    deadline = result["started"] + timeout

    with httpx.Client(trust_env=False, follow_redirects=False) as client:

        def post(duplicate: bool = False):
            prefix = "redelivery_" if duplicate else ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PushProbeFailure("push_probe_deadline", result)
            try:
                with client.stream(
                    "POST",
                    url + "/ingest/github/push",
                    content=probe.body,
                    headers=headers,
                    timeout=min(10, remaining),
                ) as response:
                    result[prefix + "http_status"] = response.status_code
                    if response.status_code != 200:
                        raise PushProbeFailure("push_probe_http_status", result)
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=1024):
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise PushProbeFailure("push_probe_receipt_size", result)
                receipt = json.loads(body)
                if not isinstance(receipt, dict) or receipt.get("stored") is not (
                    not duplicate
                ):
                    raise ValueError("receipt")
                fact_id = TypeAdapter(NonEmptyId).validate_python(
                    receipt.get("fact_id")
                )
            except httpx.TransportError:
                raise PushProbeFailure("push_probe_http_transport", result) from None
            except ValueError:
                raise PushProbeFailure("push_probe_invalid_receipt", result) from None
            result.update(
                {
                    prefix + "fact_id": fact_id,
                    prefix + "stored": receipt["stored"],
                    prefix + "acknowledged": time.monotonic(),
                }
            )

        post()
        while True:
            rows = observations()
            if rows:
                if len(rows) != 1 or rows[0].source_push_id != result["fact_id"]:
                    raise PushProbeFailure("push_probe_observation_mismatch", result)
                result.update(
                    observed=time.monotonic(), observation_id=rows[0].observation_id
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PushProbeFailure("push_probe_observation_timeout", result)
            time.sleep(min(0.1, remaining))
        post(duplicate=True)
        if result["redelivery_fact_id"] != result["fact_id"]:
            raise PushProbeFailure("push_probe_redelivery_mismatch", result)
        if [row.observation_id for row in observations()] != [result["observation_id"]]:
            raise PushProbeFailure("push_probe_observation_mismatch", result)
    result["ack_seconds"] = result["acknowledged"] - result["started"]
    result["observation_seconds"] = result["observed"] - result["started"]
    return result
