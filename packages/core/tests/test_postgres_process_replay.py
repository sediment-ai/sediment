# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real commit-boundary process interruption and FactStore replay."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sediment_core import AgentHarness, DeveloperDecision, FactStore, InteractionMode
from sediment_core.postgres_engine import create_postgres_engine

ORG = "process-replay"


def _decisions():
    boundary = datetime(2026, 9, 7, tzinfo=UTC)
    return [
        DeveloperDecision(
            org_id=ORG,
            session_id=f"session-{index // 2}",
            user_id="test-user",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path=f"file-{index}.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id=f"call-{index}",
            occurred_at=boundary + timedelta(seconds=index),
            captured_at=boundary,
            raw={"text": "exact\x00\ud800", "index": index},
        )
        for index in range(3)
    ]


def _worker(phase):
    engine = create_postgres_engine(os.environ["SEDIMENT_DATABASE_URL"])
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        facts = _decisions()
        original_commit = engine.dialect.do_commit

        def at_commit(connection):
            # This is the real DBAPI connection after every batch/Session write.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT (SELECT count(*) FROM developer_decisions WHERE org_id=%s), "
                    "(SELECT count(*) FROM sessions WHERE org_id=%s)",
                    (ORG, ORG),
                )
                assert cursor.fetchone() == (3, 2)

            def pause():
                print(
                    json.dumps(
                        {
                            "phase": phase,
                            "facts": [fact.model_dump(mode="json") for fact in facts],
                        }
                    ),
                    flush=True,
                )
                sys.stdin.read(1)

            if phase == "before":
                pause()
            original_commit(connection)
            if phase == "after":
                pause()

        if phase != "replay":
            engine.dialect.do_commit = at_commit
        store = FactStore(engine)
        inserted = store.store_decisions(facts)
        print(
            json.dumps(
                {
                    "inserted": inserted,
                    "candidates": [fact.decision_id for fact in facts],
                    "facts": [
                        fact.model_dump(mode="json")
                        for fact in store.read_decisions(ORG)
                    ],
                }
            ),
            flush=True,
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_process_interruption_at_real_commit_replays_atomically(
    postgres_database_factory, phase
):
    database_url = postgres_database_factory()
    env = {
        **os.environ,
        "SEDIMENT_DATABASE_URL": database_url,
        "SEDIMENT_TEST_DATABASE_URL": database_url,
    }
    command = [sys.executable, str(Path(__file__).resolve())]
    engine = create_postgres_engine(database_url)
    store = FactStore(engine)
    try:
        with subprocess.Popen(
            [*command, phase],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdout, selectors.EVENT_READ)
                    assert ready.select(timeout=15), (
                        "worker never reached the commit boundary"
                    )
                message = child.stdout.readline()
                assert message, "worker exited before reporting the commit boundary"
                signal = json.loads(message)
                assert signal["phase"] == phase
                original = [
                    DeveloperDecision.model_validate(row) for row in signal["facts"]
                ]
                assert store.read_decisions(ORG) == (
                    [] if phase == "before" else original
                )
                sessions = store.read_sessions(ORG)
                assert len(sessions) == (0 if phase == "before" else 2)
                child.kill()
                child.wait(timeout=10)
                assert child.returncode < 0
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
        assert store.read_decisions(ORG) == ([] if phase == "before" else original)
        assert store.read_sessions(ORG) == sessions
        replay = subprocess.run(
            [*command, "replay"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        receipt = json.loads(replay.stdout)
        assert receipt["inserted"] == [phase == "before"] * 3
        persisted = [DeveloperDecision.model_validate(row) for row in receipt["facts"]]
        assert len(persisted) == 3
        assert {fact.decision_id for fact in original}.isdisjoint(receipt["candidates"])
        if phase == "after":
            assert persisted == original
            assert store.read_sessions(ORG) == sessions
        else:
            assert {fact.decision_id for fact in persisted} == set(
                receipt["candidates"]
            )
        assert [fact.model_dump(exclude={"decision_id"}) for fact in persisted] == [
            fact.model_dump(exclude={"decision_id"}) for fact in original
        ]
        assert [session.session_id for session in store.read_sessions(ORG)] == [
            "session-0",
            "session-1",
        ]
        # A second completed replay also preserves full canonical rows and Sessions.
        stable_sessions = store.read_sessions(ORG)
        again = subprocess.run(
            [*command, "replay"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        assert json.loads(again.stdout)["inserted"] == [False] * 3
        assert store.read_decisions(ORG) == persisted
        assert store.read_sessions(ORG) == stable_sessions
    finally:
        engine.dispose()


if __name__ == "__main__":
    _worker(sys.argv[1])
