# SPDX-License-Identifier: AGPL-3.0-or-later
"""Docker Compose process-environment boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _compose_config(
    project: Path, environment: dict[str, str], *, gateway: bool = True
) -> subprocess.CompletedProcess[str]:
    profile = ["--profile", "operator", *(["--profile", "gateway"] if gateway else [])]
    return subprocess.run(
        [
            "docker",
            "compose",
            *profile,
            "config",
            "--format",
            "json",
        ],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _compose_project(tmp_path: Path, environment: dict[str, str]) -> Path:
    shutil.copyfile(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in environment.items())
    )
    return tmp_path


def test_compose_passes_each_secret_only_to_the_process_that_uses_it(
    tmp_path,
) -> None:
    values = {
        **_core_environment(),
        "POSTGRES_PASSWORD": "sentinel-postgres-592",
        "SEDIMENT_ORG_ID": "sentinel-org-592",
        "SEDIMENT_API_BEARER_TOKEN": "sentinel-api-592",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET": "sentinel-webhook-592",
        "SEDIMENT_ALLOWED_CLONE_HOSTS": '["github.com"]',
        "SEDIMENT_ENABLE_DOCS": "false",
        "SEDIMENT_DEV_MODE": "false",
        "ANTHROPIC_API_KEY": "sentinel-provider-592",
        "LITELLM_MASTER_KEY": "sentinel-gateway-592",
    }
    environment = os.environ.copy()
    environment.update(values)

    project = _compose_project(tmp_path, values)
    result = _compose_config(project, environment)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    keys = {
        service: {
            key
            for key, value in config.get("environment", {}).items()
            if value is not None
        }
        for service, config in services.items()
    }

    assert keys["postgres"] == {
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_INITDB_ARGS",
    }
    assert keys["migrate"] == {
        "SEDIMENT_BOOTSTRAP_DATABASE_URL",
        "SEDIMENT_MIGRATOR_PASSWORD",
        "SEDIMENT_RUNTIME_PASSWORD",
        "SEDIMENT_OPERATOR_PASSWORD",
    }
    assert keys["operator"] == {
        "SEDIMENT_DATABASE_URL",
        "SEDIMENT_ORG_ID",
        "SEDIMENT_MIRROR_PATH",
        "TMPDIR",
    }
    assert keys["api"] == {
        "SEDIMENT_OPERATOR_TOKEN",
        "SEDIMENT_INGEST_TOKENS",
        "SEDIMENT_ALLOWED_CLONE_HOSTS",
        "SEDIMENT_API_BEARER_TOKEN",
        "SEDIMENT_DATABASE_URL",
        "SEDIMENT_DEV_MODE",
        "SEDIMENT_ENABLE_DOCS",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET",
        "SEDIMENT_MIRROR_PATH",
        "SEDIMENT_ORG_ID",
    }
    assert keys["gateway"] == {
        "SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN",
        "ANTHROPIC_API_KEY",
        "LITELLM_MASTER_KEY",
        "PYTHONPATH",
        "SEDIMENT_API_BEARER_TOKEN",
        "SEDIMENT_INGEST_URL",
        "SEDIMENT_DELIVERY_DIR",
    }

    for service in ("postgres", "migrate", "api", "operator"):
        values = services[service].get("environment", {}).values()
        assert "sentinel-provider-592" not in values
        assert "sentinel-gateway-592" not in values


def test_compose_names_a_missing_required_secret(tmp_path) -> None:
    for key in (
        "POSTGRES_PASSWORD",
        "SEDIMENT_ORG_ID",
        "SEDIMENT_OPERATOR_TOKEN",
        "SEDIMENT_INGEST_TOKENS",
        "SEDIMENT_MIGRATOR_PASSWORD",
        "SEDIMENT_RUNTIME_PASSWORD",
        "SEDIMENT_OPERATOR_PASSWORD",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET",
        "SEDIMENT_ALLOWED_CLONE_HOSTS",
        "SEDIMENT_ENABLE_DOCS",
        "SEDIMENT_DEV_MODE",
    ):
        values = _core_environment()
        values.pop(key)
        environment = os.environ.copy()
        environment.update(values)
        environment.pop(key, None)
        project_dir = tmp_path / key
        project_dir.mkdir()
        project = _compose_project(project_dir, values)

        result = _compose_config(project, environment)

        assert result.returncode != 0
        assert key in result.stderr


def _core_environment() -> dict[str, str]:
    return {
        "POSTGRES_PASSWORD": "postgres",
        "SEDIMENT_MIGRATOR_PASSWORD": "migration-password",
        "SEDIMENT_RUNTIME_PASSWORD": "runtime-password",
        "SEDIMENT_OPERATOR_PASSWORD": "operator-password",
        "SEDIMENT_OPERATOR_TOKEN": "operator-token",
        "SEDIMENT_INGEST_TOKENS": '{"gateway":"gateway-ingest-token"}',
        "SEDIMENT_GATEWAY_INGEST_TOKEN": "gateway-ingest-token",
        "SEDIMENT_ORG_ID": "sediment",
        "SEDIMENT_API_BEARER_TOKEN": "api-token",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET": "webhook-secret",
        "SEDIMENT_ALLOWED_CLONE_HOSTS": '["github.com"]',
        "SEDIMENT_ENABLE_DOCS": "false",
        "SEDIMENT_DEV_MODE": "false",
    }


def test_parallel_deployments_keep_ports_volumes_and_image_tags_separate(tmp_path):
    values = {
        **_core_environment(),
        "COMPOSE_PROJECT_NAME": "pilot-check",
        "SEDIMENT_API_PORT": "18080",
        "SEDIMENT_GATEWAY_PORT": "14000",
    }
    project = _compose_project(tmp_path, values)
    environment = {key: value for key, value in os.environ.items() if key not in values}
    result = _compose_config(project, environment)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["name"] == "pilot-check"
    services = config["services"]
    for name, port, target in (("api", "18080", 8000), ("gateway", "14000", 4000)):
        assert services[name]["ports"] == [
            {
                "mode": "ingress",
                "host_ip": "127.0.0.1",
                "target": target,
                "published": port,
                "protocol": "tcp",
            }
        ]
        assert "SEDIMENT_API_PORT" not in services[name]["environment"]
        assert "SEDIMENT_GATEWAY_PORT" not in services[name]["environment"]
    for name in ("api", "migrate", "operator"):
        assert services[name]["image"] == "pilot-check-api:local"
    assert services["postgres"]["image"] == "pilot-check-postgres:local"
    assert services["gateway"]["image"] == "pilot-check-gateway:local"
    assert all(v["name"].startswith("pilot-check_") for v in config["volumes"].values())


def test_default_compose_keeps_existing_ports_and_image_names(tmp_path):
    values = _core_environment()
    project = _compose_project(tmp_path, values)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COMPOSE_", "SEDIMENT_"))
    }
    result = _compose_config(project, environment)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["name"] == "sediment"
    for name, port in (("api", "8000"), ("gateway", "4000")):
        service = config["services"][name]
        assert service["image"] == f"sediment-{name}:local"
        assert service["ports"][0]["host_ip"] == "127.0.0.1"
        assert service["ports"][0]["published"] == port


def test_api_only_compose_does_not_require_gateway_keys(tmp_path) -> None:
    values = _core_environment()
    environment = os.environ.copy()
    environment.update(values)
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("LITELLM_MASTER_KEY", None)
    project = _compose_project(tmp_path, values)

    result = _compose_config(project, environment, gateway=False)

    assert result.returncode == 0, result.stderr
    assert "ANTHROPIC_API_KEY" not in result.stderr
    assert "LITELLM_MASTER_KEY" not in result.stderr


def _gateway_process(
    tmp_path: Path, *, anthropic_key: str = "", master_key: str = ""
) -> subprocess.CompletedProcess[str]:
    values = {
        **_core_environment(),
        "ANTHROPIC_API_KEY": anthropic_key,
        "LITELLM_MASTER_KEY": master_key,
    }
    environment = os.environ.copy()
    environment.update(values)
    image_entrypoint = tmp_path / "sediment-gateway.py"
    image_entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")
    image_entrypoint.chmod(0o755)
    project = _compose_project(tmp_path, values)
    result = _compose_config(project, environment)
    assert result.returncode == 0, result.stderr
    gateway = json.loads(result.stdout)["services"]["gateway"]
    entrypoint = gateway.get("entrypoint")
    assert isinstance(entrypoint, list), "gateway has no runtime key validation"
    runtime_entrypoint = [
        part.replace("$$", "$").replace(
            "/usr/local/bin/sediment-gateway.py", str(image_entrypoint)
        )
        if isinstance(part, str)
        else part
        for part in entrypoint
    ]
    return subprocess.run(
        [*runtime_entrypoint, *gateway["command"]],
        cwd=tmp_path,
        env={**environment, **gateway["environment"]},
        text=True,
        capture_output=True,
        check=False,
    )


def test_gateway_runtime_names_a_missing_anthropic_key(tmp_path) -> None:
    result = _gateway_process(tmp_path, master_key="sk-master")

    assert result.returncode != 0
    assert "ANTHROPIC_API_KEY" in result.stderr


def test_gateway_runtime_names_a_missing_master_key(tmp_path) -> None:
    result = _gateway_process(tmp_path, anthropic_key="sk-anthropic")

    assert result.returncode != 0
    assert "LITELLM_MASTER_KEY" in result.stderr


def test_gateway_runtime_starts_with_both_keys(tmp_path) -> None:
    result = _gateway_process(
        tmp_path,
        anthropic_key="sk-anthropic",
        master_key="sk-master",
    )

    assert result.returncode == 0, result.stderr


def test_gateway_replay_mounts_shared_owner_and_explicit_private_volume(tmp_path):
    values = _core_environment()
    environment = {**os.environ, **values, "SEDIMENT_DELIVERY_DIR": ""}
    project = _compose_project(tmp_path, values)
    result = _compose_config(project, environment)
    assert result.returncode == 0, result.stderr
    gateway = json.loads(result.stdout)["services"]["gateway"]
    assert gateway["environment"].get("SEDIMENT_DELIVERY_DIR") == ""
    mounts = {v["target"]: v for v in gateway["volumes"]}
    assert mounts["/app/sediment_delivery.py"]["source"].endswith(
        "cli/sediment_cli/delivery.py"
    )
    assert mounts["/app/sediment_delivery.py"]["read_only"] is True
    assert mounts["/data/delivery"]["type"] == "volume"
    environment["SEDIMENT_DELIVERY_DIR"] = "/data/delivery/pending"
    result = _compose_config(project, environment)
    services = json.loads(result.stdout)["services"]
    assert (
        services["gateway"]["environment"]["SEDIMENT_DELIVERY_DIR"]
        == "/data/delivery/pending"
    )
    for service in ("api", "migrate", "postgres"):
        assert "SEDIMENT_DELIVERY_DIR" not in services[service]["environment"]


def test_compose_separates_database_network_and_scoped_credentials(tmp_path):
    values = {
        **_core_environment(),
        "SEDIMENT_MIGRATOR_PASSWORD": "migration-only",
        "SEDIMENT_RUNTIME_PASSWORD": "runtime-only",
        "SEDIMENT_OPERATOR_PASSWORD": "operator-db-only",
        "SEDIMENT_OPERATOR_TOKEN": "operator-http-only",
        "SEDIMENT_INGEST_TOKENS": '{"gateway":"gateway-ingest-only"}',
        "SEDIMENT_GATEWAY_INGEST_TOKEN": "gateway-ingest-only",
    }
    project = _compose_project(tmp_path, values)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "gateway",
            "--profile",
            "operator",
            "config",
            "--format",
            "json",
        ],
        cwd=project,
        env={**os.environ, **values},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    services = config["services"]
    assert set(services["postgres"]["networks"]) == {"database"}
    assert config["networks"]["database"]["internal"] is True
    assert "database" not in services["gateway"]["networks"]
    assert not services["postgres"].get("ports")
    assert (
        "sediment_runtime:runtime-only@"
        in services["api"]["environment"]["SEDIMENT_DATABASE_URL"]
    )
    assert (
        "sediment_operator:operator-db-only@"
        in services["operator"]["environment"]["SEDIMENT_DATABASE_URL"]
    )
    assert services["migrate"]["command"] == ["sediment", "db", "provision"]
    # A read-only operator invocation must not start credential provisioning.
    assert set(services["operator"]["depends_on"]) == {"postgres"}
    assert (
        services["gateway"]["environment"]["SEDIMENT_API_BEARER_TOKEN"]
        == "gateway-ingest-only"
    )
    assert (
        services["gateway"]["environment"]["SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN"]
        == "http://api:8000"
    )
    for service in services.values():
        assert all(mount.startswith("/") for mount in service.get("tmpfs", []))
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert int(service["mem_limit"]) > 0 and int(service["pids_limit"]) > 0
        assert float(service["cpus"]) > 0
        assert service["logging"]["options"] == {"max-file": "3", "max-size": "10m"}
        assert not any("docker.sock" in str(v) for v in service.get("volumes", []))


@pytest.mark.parametrize(
    "source_setting,source_value",
    [
        ("SEDIMENT_RETRIEVAL_SESSION_ID", "one-source-session"),
        ("SEDIMENT_RETRIEVAL_SESSION_IDS", '["source-one","source-two"]'),
    ],
)
def test_compose_retrieval_pair_is_absent_by_default_and_scoped_to_api(
    tmp_path, source_setting, source_value
):
    values = _core_environment()
    project = _compose_project(tmp_path, values)
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith("SEDIMENT_RETRIEVAL_")
    }
    absent = _compose_config(project, {**environment, **values})
    assert absent.returncode == 0, absent.stderr
    services = json.loads(absent.stdout)["services"]
    for service in services.values():
        assert not any(
            key.startswith("SEDIMENT_RETRIEVAL_") and value is not None
            for key, value in service.get("environment", {}).items()
        )

    retrieval = {
        "SEDIMENT_RETRIEVAL_TOKEN": "retrieval-test-token-long-enough",
        source_setting: source_value,
    }
    # Both shell variables and Compose's private .env must work.
    for from_file in (False, True):
        if from_file:
            _compose_project(project, {**values, **retrieval})
        result = _compose_config(
            project, {**environment, **values, **({} if from_file else retrieval)}
        )
        assert result.returncode == 0, result.stderr
        services = json.loads(result.stdout)["services"]
        for name, service in services.items():
            supplied = service.get("environment", {})
            for key, value in retrieval.items():
                if name == "api":
                    assert supplied.get(key) == value
                else:
                    assert key not in supplied


def test_pilot_profile_starts_gateway_and_private_proxy(tmp_path):
    values = {
        **_core_environment(),
        "COMPOSE_PROFILES": "https",
        "SEDIMENT_DOMAIN": "sediment.example.com",
        "SEDIMENT_ACME_EMAIL": "ops@example.com",
    }
    project = _compose_project(tmp_path, values)
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=project,
        env={**os.environ, **values},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "proxy" in services
    assert "gateway" in services
    proxy = services["proxy"]
    assert set(proxy["networks"]) == {"edge"}
    assert proxy["depends_on"]["gateway"]["condition"] == "service_healthy"
    assert proxy["depends_on"]["api"]["condition"] == "service_healthy"
    assert {p["published"] for p in proxy["ports"]} == {"80", "443"}
    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]
    assert all("docker.sock" not in str(m) for m in proxy["volumes"])
    assert not any(
        "TOKEN" in key or "PASSWORD" in key or "API_KEY" in key
        for key in proxy["environment"]
    )
    assert proxy["healthcheck"]["test"]
    assert any(m["type"] == "volume" for m in proxy["volumes"])


def test_gateway_readiness_checks_the_running_application(tmp_path):
    values = _core_environment()
    project = _compose_project(tmp_path, values)
    result = _compose_config(project, {**os.environ, **values})
    assert result.returncode == 0, result.stderr
    gateway = json.loads(result.stdout)["services"]["gateway"]
    assert "/health/liveliness" in " ".join(gateway["healthcheck"]["test"])
