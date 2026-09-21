# SPDX-License-Identifier: AGPL-3.0-or-later
"""Start the supplied LiteLLM routing/capture configuration without a database."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

DATABASE_ENVIRONMENT = {
    "DIRECT_URL",
    "IAM_TOKEN_DB_AUTH",
    "AZURE_POSTGRESQL_AUTH",
    "STORE_MODEL_IN_DB",
}
CONFIGURATION_OVERRIDES = {
    "CONFIG_FILE_PATH",
    "WORKER_CONFIG",
    "LITELLM_CONFIG_BUCKET_NAME",
    "USE_AWS_KMS",
}


def check_config_overrides(environment: dict[str, str]) -> None:
    if CONFIGURATION_OVERRIDES.intersection(str(key).upper() for key in environment):
        raise ValueError("This gateway requires a self-contained local config")


def has_database_environment(environment: dict[str, str]) -> bool:
    return any(
        value is not None
        and (
            str(name).upper().startswith(("DATABASE_", "LITELLM_PGBOUNCER_"))
            or str(name).upper() in DATABASE_ENVIRONMENT
        )
        for name, value in environment.items()
    )


def validate_configuration(arguments: list[str], environment: dict[str, str]) -> None:
    if has_database_environment(environment):
        raise ValueError("This gateway image does not support database configuration")
    check_config_overrides(environment)
    paths = []
    for index, argument in enumerate(arguments):
        option = argument.lstrip("-").split("=", 1)[0]
        if argument.startswith("-") and (
            "prisma" in option
            or option.endswith("_db_auth")
            or option.startswith("database_")
        ):
            raise ValueError(
                "This gateway image does not support database configuration"
            )
        if argument in {"--config", "-c"}:
            if index + 1 >= len(arguments):
                raise ValueError("A local gateway config file is required")
            paths.append(arguments[index + 1])
        elif argument.startswith("--config="):
            paths.append(argument.removeprefix("--config="))
    if len(paths) != 1 or not Path(paths[0]).is_file():
        raise ValueError("Exactly one local gateway config file is required")
    try:
        config = yaml.safe_load(Path(paths[0]).read_text())
    except (OSError, ValueError, yaml.YAMLError):
        raise ValueError("Cannot read the local gateway config") from None
    if not isinstance(config, dict):
        raise ValueError("The gateway config must be a mapping")
    if "include" in config:
        raise ValueError("This gateway requires a self-contained local config")
    general = config.get("general_settings") or {}
    configured_environment = config.get("environment_variables") or {}
    if not isinstance(general, dict) or not isinstance(configured_environment, dict):
        raise ValueError("Invalid gateway config settings")
    check_config_overrides(configured_environment)
    if {"key_management_system", "key_management_settings"}.intersection(general):
        raise ValueError("This gateway requires a self-contained local config")
    if any(
        value and (str(name).startswith("database_") or name == "store_model_in_db")
        for name, value in general.items()
    ) or has_database_environment(configured_environment):
        raise ValueError("This gateway image does not support database configuration")
    models = config.get("model_list")
    if not isinstance(models, list) or not models:
        raise ValueError("This gateway requires explicit Anthropic model routes")
    for model in models:
        parameters = model.get("litellm_params") if isinstance(model, dict) else None
        upstream = parameters.get("model") if isinstance(parameters, dict) else None
        if not isinstance(upstream, str) or not upstream.startswith("anthropic/"):
            raise ValueError("This gateway supports only Anthropic model routes")
        if parameters.get("custom_llm_provider") not in (None, "anthropic"):
            raise ValueError("This gateway supports only Anthropic model routes")


if __name__ == "__main__":
    try:
        validate_configuration(sys.argv[1:], dict(os.environ))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    os.execv("/app/docker/prod_entrypoint.sh", ["prod_entrypoint.sh", *sys.argv[1:]])
