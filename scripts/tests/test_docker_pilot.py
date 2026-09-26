# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in rehearsal of a fresh Compose deployment and developer enrollment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("SEDIMENT_TEST_DOCKER_PILOT") != "1",
    reason="set SEDIMENT_TEST_DOCKER_PILOT=1 to build and rehearse disposable containers",
)


def test_fresh_docker_pilot_enrollment_and_restart(tmp_path):
    project = f"sediment-pilot-test-{uuid4().hex[:12]}"
    checkout = tmp_path / "deployment"
    checkout.mkdir()
    # Copy only the build inputs; never copy a deployment's .env or Git config.
    for name in ("packages", "apps", "cli", "docker"):
        shutil.copytree(
            ROOT / name,
            checkout / name,
            ignore=shutil.ignore_patterns("__pycache__", ".venv", "*.egg-info"),
        )
    for name in (
        "Dockerfile",
        ".dockerignore",
        "docker-compose.yml",
        "pyproject.toml",
        "uv.lock",
    ):
        shutil.copyfile(ROOT / name, checkout / name)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SEDIMENT_", "COMPOSE_", "OTEL_", "CODEX_", "GIT_"))
    }
    environment.update(COMPOSE_PROJECT_NAME=project, SEDIMENT_API_PORT="0")
    secrets = []

    def run(*args, cwd=checkout, env=environment, input=None, timeout=60):
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=input,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        for secret in secrets:
            output = output.replace(secret, "[redacted]")
        assert result.returncode == 0, output[-6000:]
        return result.stdout

    run(
        sys.executable,
        "-S",
        str(ROOT / "scripts/create_deploy_env.py"),
        "--output",
        str(checkout / ".env"),
        "--ingest-client",
        "pilot-developer",
    )
    values = dict(
        line.split("=", 1)
        for line in (checkout / ".env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    ingest = json.loads(values["SEDIMENT_INGEST_TOKENS"])["pilot-developer"]
    operator = values["SEDIMENT_OPERATOR_TOKEN"]
    secrets.extend(
        [
            ingest,
            operator,
            *(
                value
                for key, value in values.items()
                if value
                and any(word in key for word in ("TOKEN", "PASSWORD", "SECRET", "KEY"))
            ),
        ]
    )
    compose = ("docker", "compose", "--project-directory", str(checkout))
    try:
        run(*compose, "up", "--build", "--wait", "--wait-timeout", "120", timeout=900)

        def endpoint():
            return "http://" + run(*compose, "port", "api", "8000").strip()

        url = endpoint()
        assert httpx.get(url + "/health", timeout=5).json()["status"] == "ok"
        migrator = run(*compose, "ps", "-aq", "migrate").strip()
        started = run("docker", "inspect", "--format", "{{.State.StartedAt}}", migrator)
        assert "at_head" in run(
            *compose,
            "--profile",
            "operator",
            "run",
            "--rm",
            "operator",
            "sediment",
            "db",
            "status",
        )
        assert "inference_calls" in run(
            *compose,
            "--profile",
            "operator",
            "run",
            "--rm",
            "operator",
            "sediment",
            "facts",
        )
        assert (
            run("docker", "inspect", "--format", "{{.State.StartedAt}}", migrator)
            == started
        )

        user_dir = tmp_path / "developer"
        for name in (".claude", ".codex", ".cursor"):
            (user_dir / name).mkdir(parents=True)
        repo = tmp_path / "repository"
        repo.mkdir()
        client_env = {
            **environment,
            "HOME": str(user_dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(user_dir / ".gitconfig"),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{environment['PATH']}",
        }

        def client(*args, input=None, source_env=False):
            command = (sys.executable, "-m", "sediment_cli.cli", *args)
            if source_env:
                command = (
                    "/bin/sh",
                    "-ec",
                    '. "$HOME/.sediment/env.sh"; exec "$@"',
                    "pilot-check",
                    *command,
                )
            return run(*command, cwd=repo, env=client_env, input=input)

        run("git", "init", "-q", cwd=repo, env=client_env)
        original_hook = "#!/bin/sh\n# Existing developer hook.\n"
        hook = repo / ".git/hooks/post-commit"
        hook.write_text(original_hook)
        client("login", url, "--capture", "--with-token", input=ingest + "\n")
        client(
            "install",
            "--user-id",
            "pilot-developer",
            "--codex-profile",
            "sediment-pilot",
            str(repo),
        )
        doctor = client("doctor", str(repo), source_env=True)
        assert "ingest token valid" in doctor
        assert "capture endpoint: SEDIMENT_OTLP_ENDPOINT is accepted" in doctor
        for name in (".sediment/env.sh", ".codex/sediment-pilot.config.toml"):
            path = user_dir / name
            assert ingest in path.read_text() and operator not in path.read_text()
            assert path.stat().st_mode & 0o777 == 0o600
        assert (
            httpx.get(
                url + "/v1/facts", headers={"Authorization": f"Bearer {ingest}"}
            ).status_code
            == 403
        )
        client("login", url, "--with-token", input=operator + "\n")
        client("demo")

        def facts():
            response = httpx.get(
                url + "/v1/facts", headers={"Authorization": f"Bearer {operator}"}
            )
            response.raise_for_status()
            return response.json()

        before = facts()
        assert before["tables"]["inference_calls"]["total"] == 1
        assert before["tables"]["developer_decisions"]["total"] == 1
        client("demo")
        assert facts() == before
        client(
            "mark",
            "--tool",
            "claude-code",
            input=json.dumps({"session_id": "pilot-hook-check", "cwd": str(repo)}),
        )
        run(
            "git",
            "-c",
            "user.name=Pilot",
            "-c",
            "user.email=pilot@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "test: verify pilot hooks",
            cwd=repo,
            env=client_env,
        )
        assert "pilot-hook-check" in run(
            "git",
            "notes",
            "--ref=refs/notes/sediment",
            "show",
            "HEAD",
            cwd=repo,
            env=client_env,
        )

        run(*compose, "down")
        run(*compose, "up", "--wait", "--wait-timeout", "120", timeout=180)
        url = endpoint()  # Docker may allocate another ephemeral host port.
        assert facts() == before
        run(
            sys.executable,
            "-S",
            str(ROOT / "scripts/smoke.py"),
            url,
            env={
                **environment,
                "SEDIMENT_OPERATOR_TOKEN": operator,
                "SEDIMENT_GITHUB_WEBHOOK_SECRET": values[
                    "SEDIMENT_GITHUB_WEBHOOK_SECRET"
                ],
            },
        )
        assert facts()["tables"]["pushes"]["total"] == 1
        assert facts()["tables"]["ci_outcomes"]["total"] == 1
        client("uninstall", "--agents", str(repo))
        assert hook.read_text() == original_hook
        assert not (user_dir / ".sediment/env.sh").exists()
    finally:
        run(
            *compose,
            "--profile",
            "operator",
            "down",
            "--volumes",
            "--rmi",
            "all",
            timeout=120,
        )
        assert not run(
            "docker",
            "volume",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Name}}",
        ).strip()
