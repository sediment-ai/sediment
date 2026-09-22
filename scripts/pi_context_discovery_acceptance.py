# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise native pi discovery and factual reads against real API/PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

from release_rehearsal import scratch_database

from sediment_core import (
    FactStore,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallResponsePart,
)
from sediment_core.postgres_engine import create_postgres_engine
from sediment_core.postgres_migrations import upgrade_database


ROOT = Path(__file__).resolve().parents[1]
ORG = "discovery-acceptance"


def _seed(
    store: FactStore, *, factual: bool = False
) -> tuple[list[str], list[InferenceCall]]:
    """The agent learns the relevant ID only from a discovery response."""
    sources = [f"source-{uuid4().hex}" for _ in range(3)]
    calls = []
    descriptions = (
        "Shipment constraint: Exclude replay shipments.",
        "Shipment loading constraint: verify the manifest before dispatch.",
        "Keep invoice amounts in integer minor units.",
        "Shipment replay constraint outside the permitted set.",
    )
    for index, (session, description) in enumerate(
        zip([*sources, "outside-grant"], descriptions, strict=True)
    ):
        parts = [TextPart(content=description)]
        inputs = []
        if index == 0:
            parts.append(
                ToolCallResponsePart(
                    id="shipment-check",
                    result={
                        "constraint": "Exclude replay shipments.",
                        "integer": 9_007_199_254_740_993,
                    },
                )
            )
        if factual and index == 2:
            requirement = TextPart(
                content="Requirements: Invoice identifiers must remain exact integers."
            )
            inputs = [InferenceMessage(role="user", parts=[requirement])]
            parts = [
                TextPart(
                    content="Failed attempt: converting identifiers to floats "
                    "changed the receipt."
                ),
                ReasoningPart(content="The prior attempt rounded an identifier."),
                ToolCallResponsePart(
                    id="failed-check",
                    result={
                        "integer": 9_007_199_254_740_993,
                        "large_integer": int("1" + "0" * 399 + "7"),
                        "text": "surrogate\ud800 and NUL\0",
                    },
                ),
                requirement,
            ]
        call = InferenceCall(
            org_id=ORG,
            session_id=session,
            gateway_provider=GatewayProvider.LITELLM,
            user_id="user-identity-must-stay-private",
            raw={"private": "provider-raw-must-stay-private"},
            input_messages=inputs,
            output_messages=[InferenceMessage(role="assistant", parts=parts)],
        )
        store.store_inference_call(call)
        calls.append(call)
    return sources, calls


def run(database_url: str, *, factual: bool = False) -> None:
    """Only the parent/API receive database settings; pi gets one read token."""
    node = shutil.which("node")
    if node is None or not subprocess.check_output(
        [node, "--version"], text=True, timeout=10
    ).startswith("v24."):
        raise RuntimeError("Node 24 on PATH is required")
    native_test = ROOT / (
        "shims/pi/test/evidence-http.test.ts"
        if factual
        else "shims/pi/test/discovery-http.test.ts"
    )
    if not native_test.is_file() or not (ROOT / "shims/pi/node_modules").is_dir():
        raise RuntimeError("Install the locked pi dependencies before this check")
    with (
        scratch_database(database_url) as url,
        tempfile.TemporaryDirectory(
            prefix="sediment-discovery-acceptance-"
        ) as directory,
    ):
        upgrade_database(url)
        engine = create_postgres_engine(url)
        try:
            store = FactStore(engine)
            sources, calls = _seed(store, factual=factual)
            before = {table: store.count_facts(ORG, table) for table in FactTable}
            if any(
                count
                for table, count in before.items()
                if table != FactTable.INFERENCE_CALLS
            ):
                raise RuntimeError(
                    "Acceptance evidence must have no commit observations"
                )
            token = secrets.token_hex(32)
            # No ambient Sediment/model/forge credentials enter either child.
            environment = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
            }
            api_environment = {
                **environment,
                "SEDIMENT_ORG_ID": ORG,
                "SEDIMENT_DATABASE_URL": url,
                "SEDIMENT_DEV_MODE": "true",
                "SEDIMENT_OPERATOR_TOKEN": secrets.token_hex(32),
                "SEDIMENT_RETRIEVAL_TOKEN": token,
                "SEDIMENT_RETRIEVAL_SESSION_IDS": json.dumps(sources),
            }
            with (
                socket.socket() as listener,
                open(Path(directory) / "api.log", "wb") as log,
            ):
                listener.bind(("127.0.0.1", 0))
                listener.listen(128)
                endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
                api = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "sediment_api.main:app",
                        "--fd",
                        str(listener.fileno()),
                        "--no-access-log",
                    ],
                    cwd=directory,
                    env=api_environment,
                    pass_fds=(listener.fileno(),),
                    start_new_session=True,
                    stdout=log,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        if api.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Disposable API startup failed")
                        try:
                            with urlopen(endpoint + "/health", timeout=0.5) as response:
                                if response.status == 200:
                                    break
                        except (URLError, TimeoutError):
                            time.sleep(0.1)
                    prefix = (
                        "SEDIMENT_PI_EVIDENCE" if factual else "SEDIMENT_PI_DISCOVERY"
                    )

                    def native(probe: dict[str, object] | None = None) -> None:
                        child = {
                            **environment,
                            f"{prefix}_API_URL": endpoint,
                            f"{prefix}_TOKEN": token,
                        }
                        if probe is not None:
                            child[f"{prefix}_KNOWN"] = json.dumps(probe)
                        subprocess.run(
                            [node, "--test", str(native_test)],
                            cwd=ROOT / "shims/pi",
                            env=child,
                            check=True,
                            timeout=120,
                        )

                    native()
                    if factual:
                        source = calls[2]
                        store.quarantine_fact(
                            ORG,
                            FactTable.INFERENCE_CALLS,
                            source.inference_call_id,
                            reason="native acceptance",
                        )
                        probe = {
                            "session_id": source.session_id,
                            "inference_call_id": source.inference_call_id,
                            "unavailable": True,
                            "revision": store.quarantine_revision(ORG),
                        }
                        native(probe)
                        store.release_fact(
                            ORG,
                            FactTable.INFERENCE_CALLS,
                            source.inference_call_id,
                            reason="native acceptance release",
                        )
                        native(
                            {
                                **probe,
                                "unavailable": False,
                                "revision": store.quarantine_revision(ORG),
                            }
                        )
                    after = {
                        table: store.count_facts(ORG, table) for table in FactTable
                    }
                    if before != after:
                        raise RuntimeError("Read operations changed Fact counts")
                    retained = {
                        call.inference_call_id: call
                        for call in store.read_inference_calls(ORG)
                    }
                    if retained != {call.inference_call_id: call for call in calls}:
                        raise RuntimeError("Read operations changed captured evidence")
                finally:
                    if api.poll() is None:
                        os.killpg(api.pid, signal.SIGTERM)
                        try:
                            api.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(api.pid, signal.SIGKILL)
                            api.wait(timeout=5)
        finally:
            engine.dispose()
    mode = "factual evidence" if factual else "discovery"
    print(f"Native {mode} acceptance passed; disposable API and database removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--factual",
        action="store_true",
        help="Read an unranked authorized Session, then verify Quarantine and release.",
    )
    arguments = parser.parse_args()
    try:
        run(os.environ["SEDIMENT_TEST_DATABASE_URL"], factual=arguments.factual)
    except KeyError:
        raise SystemExit("Set SEDIMENT_TEST_DATABASE_URL to a disposable test cluster.")
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        # Exception details from drivers/processes can contain connection values.
        raise SystemExit(f"Native discovery acceptance failed: {type(error).__name__}")
