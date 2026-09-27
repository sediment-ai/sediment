# SPDX-License-Identifier: AGPL-3.0-or-later
"""The supplied gateway cannot activate removed database integrations."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def validate(arguments: list[str], environment: dict[str, str]) -> None:
    script = ROOT / "docker/gateway/entrypoint.py"
    assert script.exists(), "The gateway must validate its configuration before startup"
    boundary = runpy.run_path(str(script))
    boundary["validate_configuration"](arguments, environment)


def test_gateway_accepts_supplied_config() -> None:
    validate(["--config", str(ROOT / "litellm/config.yaml"), "--port", "4000"], {})


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "DATABASE_URL_NON_POOLING",
        "DIRECT_URL",
        "DATABASE_HOST",
        "database_user",
        "DATABASE_URL_READ_REPLICA",
        "IAM_TOKEN_DB_AUTH",
        "AZURE_POSTGRESQL_AUTH",
        "STORE_MODEL_IN_DB",
        "LITELLM_PGBOUNCER_ENABLED",
        "LITELLM_PGBOUNCER_BINARY",
    ],
)
def test_gateway_rejects_database_environment(name: str) -> None:
    with pytest.raises(ValueError, match="database"):
        validate(["--config", str(ROOT / "litellm/config.yaml")], {name: "secret"})


@pytest.mark.parametrize(
    "configuration",
    [
        "general_settings:\n  database_url: postgresql://secret\n",
        "general_settings:\n  database_url: os.environ.CUSTOM_DATABASE\n",
        "environment_variables:\n  DATABASE_URL: postgresql://secret\n",
        "environment_variables:\n  DIRECT_URL: postgresql://secret\n",
        "environment_variables:\n  DATABASE_HOST: secret\n",
        "general_settings:\n  store_model_in_db: true\n",
        "environment_variables:\n  LITELLM_PGBOUNCER_ENABLED: 'true'\n",
    ],
)
def test_gateway_rejects_database_config(tmp_path: Path, configuration: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(configuration)
    with pytest.raises(ValueError, match="database") as exc:
        validate([f"--config={config}"], {})
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "arguments", [[], ["--config"], ["--config", "https://host/config"]]
)
def test_gateway_requires_local_config(arguments: list[str]) -> None:
    with pytest.raises(ValueError, match="config"):
        validate(arguments, {})


def test_vendor_source_drift_is_rejected_without_edit(tmp_path: Path) -> None:
    script = ROOT / "docker/gateway/patch_proxy.py"
    assert script.exists(), "The vendor patch must reject an unexpected source file"
    patcher = runpy.run_path(str(script))
    proxy = tmp_path / "proxy"
    proxy.mkdir()
    (proxy / "db").mkdir()
    (proxy / "auth").mkdir()
    for name in [
        "proxy_server.py",
        "utils.py",
        "db/exception_handler.py",
        "auth/user_api_key_auth.py",
    ]:
        (proxy / name).write_text("# upstream changed\n")
    with pytest.raises(ValueError, match="source"):
        patcher["patch_proxy"](proxy)
    assert (proxy / "proxy_server.py").read_text() == "# upstream changed\n"
    assert (proxy / "utils.py").read_text() == "# upstream changed\n"


def test_tarfile_source_drift_is_rejected_without_edit(tmp_path: Path) -> None:
    patcher = runpy.run_path(str(ROOT / "docker/gateway/patch_tarfile.py"))
    source = tmp_path / "tarfile.py"
    source.write_text("os.link(tarinfo._link_target, targetpath)\n")
    original = source.read_bytes()
    with pytest.raises(ValueError, match="source"):
        patcher["patch_tarfile"](source)
    assert source.read_bytes() == original


def test_tokenizers_metadata_drift_is_rejected_without_edit(tmp_path: Path) -> None:
    patcher = runpy.run_path(str(ROOT / "docker/gateway/patch_tokenizers.py"))
    metadata = tmp_path / "METADATA"
    metadata.write_text("Requires-Dist: huggingface-hub>=0.16.4,<2.0\n")
    original = metadata.read_bytes()
    with pytest.raises(ValueError, match="metadata"):
        patcher["patch_tokenizers"](tmp_path)
    assert metadata.read_bytes() == original


@pytest.mark.parametrize("flag", ["--use_prisma_db_push", "--iam_token_db_auth"])
def test_gateway_rejects_database_cli_flags(flag: str) -> None:
    with pytest.raises(ValueError, match="database"):
        validate(["--config", str(ROOT / "litellm/config.yaml"), flag], {})


def test_gateway_accepts_config_filename_containing_prisma(tmp_path: Path) -> None:
    config = tmp_path / "without-prisma.yaml"
    config.write_text((ROOT / "litellm/config.yaml").read_text())
    validate(["--config", str(config)], {})


def test_gateway_rejects_present_empty_database_url() -> None:
    with pytest.raises(ValueError, match="database"):
        validate(["--config", str(ROOT / "litellm/config.yaml")], {"DATABASE_URL": ""})


@pytest.mark.parametrize(
    "name",
    ["CONFIG_FILE_PATH", "WORKER_CONFIG", "LITELLM_CONFIG_BUCKET_NAME", "USE_AWS_KMS"],
)
def test_gateway_rejects_config_overrides(name: str) -> None:
    with pytest.raises(ValueError, match="self-contained"):
        validate(["--config", str(ROOT / "litellm/config.yaml")], {name: "secret"})


@pytest.mark.parametrize(
    "configuration",
    [
        "include:\n  - database.yaml\n",
        "environment_variables:\n  CONFIG_FILE_PATH: database.yaml\n",
        "general_settings:\n  key_management_system: secret-manager\n",
        "general_settings:\n  key_management_settings: {}\n",
    ],
)
def test_gateway_rejects_indirect_config(tmp_path: Path, configuration: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(configuration)
    with pytest.raises(ValueError, match="self-contained"):
        validate(["--config", str(config)], {})


@pytest.mark.parametrize(
    "model",
    ["gemini/gemini-pro", "vertex_ai/gemini-pro", "os.environ.ROUTED_MODEL", "*"],
)
def test_gateway_rejects_unsupported_model_provider(tmp_path: Path, model: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"model_list:\n  - model_name: test\n    litellm_params:\n      model: {model!r}\n"
    )
    with pytest.raises(ValueError, match="Anthropic"):
        validate(["--config", str(config)], {})


def test_gateway_requires_explicit_model_routes(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model_list: []\n")
    with pytest.raises(ValueError, match="Anthropic"):
        validate(["--config", str(config)], {})


def test_gateway_rejects_provider_override(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "model_list:\n  - model_name: test\n    litellm_params:\n"
        "      model: anthropic/claude-test\n      custom_llm_provider: vertex_ai\n"
    )
    with pytest.raises(ValueError, match="Anthropic"):
        validate(["--config", str(config)], {})
