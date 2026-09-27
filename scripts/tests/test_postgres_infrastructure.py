# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_compose_runs_migrations_before_api() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    postgres = compose["services"]["postgres"]
    migration = compose["services"]["migrate"]
    api = compose["services"]["api"]

    assert postgres["image"] == "${COMPOSE_PROJECT_NAME:-sediment}-postgres:local"
    assert postgres["build"]["context"] == "."
    assert postgres["build"]["dockerfile"] == "docker/postgres/Dockerfile"
    assert postgres["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -h 127.0.0.1 -U sediment -d sediment",
    ]
    assert "sediment-postgres:/var/lib/postgresql/data" in postgres["volumes"]
    assert migration["command"] == ["sediment", "db", "provision"]
    assert migration["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert api["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )


def test_ci_uses_postgres_and_upgrades_before_tests() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    test_job = workflow["jobs"]["test"]
    commands = [step.get("run") for step in test_job["steps"] if "run" in step]
    start_index = next(
        i
        for i, command in enumerate(commands)
        if "docker build --pull -f docker/postgres/Dockerfile" in command
    )
    driver_index = next(i for i, command in enumerate(commands) if "libpq5" in command)
    migration_index = commands.index("uv run sediment db upgrade")
    roles_index = commands.index("uv run pytest -q -m cluster_roles --durations=10")
    test_index = commands.index(
        'uv run pytest -q -n 4 -m "not cluster_roles and not serial" --durations=30'
    )
    serial_index = commands.index("uv run pytest -q -m serial --durations=10")
    # The role contract needs a cluster that no real server has provisioned.
    assert start_index < migration_index < roles_index < test_index < serial_index
    assert driver_index < migration_index
    rehearsal_index = next(
        i
        for i, command in enumerate(commands)
        if "scripts/release_rehearsal.py" in command
    )
    assert serial_index < rehearsal_index
    assert test_job["env"]["SEDIMENT_TEST_DATABASE_URL"].endswith(
        "@localhost:5432/postgres"
    )
