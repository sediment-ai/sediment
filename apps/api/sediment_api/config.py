# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration via environment variables (``SEDIMENT_`` prefix, .env)."""

import json
import re
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sediment_core import ForgeHost, normalize_org_id

# Token values that mean "operator never configured a real secret".
_INSECURE_DEFAULTS = {
    "",
    "changeme",
    "change-me",
    "replace-me",
    "your-token",
    "your-secret",
    "password",
    "secret",
}
_CLIENT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
# ADR 0018: production rejects a hand-edited operator, ingest, or webhook
# secret shorter than this floor. scripts/create_deploy_env.py generates
# 64-character secrets; 24 stays well under that while still costing an
# unthrottled online guesser (see docs/adr/0018) meaningfully more than a
# short word does.
_MIN_SECRET_LENGTH = 24


class Settings(BaseSettings):
    # extra="ignore": the .env is shared with the LiteLLM container (e.g.
    # ANTHROPIC_API_KEY), so tolerate keys this model doesn't own instead of
    # crashing at startup.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SEDIMENT_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # Tenancy: the org every fact is stamped with. Required — no
    # default — and normalized at construction, so a missing or invalid
    # SEDIMENT_ORG_ID fails the boot and two case-variant configs converge
    # on the same tenant.
    org_id: str

    # PostgreSQL is the sole active fact store. SecretStr prevents settings
    # representations and validation diagnostics from echoing credentials.
    database_url: SecretStr

    # Raw git substrate: base dir for the per-(org, repo) bare mirrors.
    # None (unset) disables mirror refresh entirely — facts still flow;
    # the mirror is best-effort substrate (ADR 0001).
    mirror_path: str | None = None
    # Clone-host allowlist for the mirror's clone_url confinement, JSON in
    # the env (SEDIMENT_ALLOWED_CLONE_HOSTS='["github.com"]'). Empty = any
    # public host. An explicit entry trusts that host, including private
    # addresses; production still refuses file: URLs.
    allowed_clone_hosts: list[str] = []

    # Auth
    api_bearer_token: str = Field(default="", repr=False)
    operator_token: SecretStr = SecretStr("")
    ingest_tokens: Annotated[dict[str, SecretStr], NoDecode] = Field(
        default_factory=dict
    )
    github_webhook_secret: str = Field(default="changeme", repr=False)

    # Trusted provider namespace, never derived from webhook headers or URLs.
    github_host: ForgeHost = "github.com"

    # Fail-closed escape hatch. False (the production default) means
    # validate_production_security() enforces real secrets; set
    # SEDIMENT_DEV_MODE=true ONLY for local dev, where the "changeme"
    # defaults are intentionally tolerated.
    dev_mode: bool = False

    enable_docs: bool = True

    @field_validator("org_id")
    @classmethod
    def _canonical_org(cls, v: str) -> str:
        return normalize_org_id(v)

    @field_validator("database_url")
    @classmethod
    def _strip_database_url(cls, v: SecretStr) -> SecretStr:
        # A mounted/piped secret often arrives with a trailing newline (see
        # _strip_secret); rstrip only \r/\n so a percent-encoded space in the
        # password survives. Reject empty-after-trim so the boot fails fast
        # at construction with a named-setting message instead of after a
        # ~30 s retry in lifespan against a newline-corrupted target.
        cleaned = v.get_secret_value().rstrip("\r\n")
        if not cleaned:
            raise ValueError(
                "database_url is unset or empty; set SEDIMENT_DATABASE_URL "
                "to a migrated PostgreSQL database."
            )
        return SecretStr(cleaned)

    @field_validator("api_bearer_token", "github_webhook_secret")
    @classmethod
    def _strip_secret(cls, v: str) -> str:
        # A mounted/piped secret often arrives with a trailing newline;
        # stored verbatim it can never match what a client sends, and a
        # whitespace-only value would sail past the insecure-default check.
        return v.strip()

    @field_validator("operator_token")
    @classmethod
    def _strip_operator_token(cls, value: SecretStr) -> SecretStr:
        return SecretStr(value.get_secret_value().strip())

    @field_validator("ingest_tokens", mode="before")
    @classmethod
    def _parse_ingest_tokens(cls, value):
        def unique_pairs(pairs):
            result = {}
            for key, secret in pairs:
                if key in result:
                    raise ValueError(
                        "ingest_tokens contains duplicate client identifiers"
                    )
                result[key] = secret
            return result

        if isinstance(value, str):
            try:
                value = json.loads(value, object_pairs_hook=unique_pairs)
            except (ValueError, RecursionError):
                raise ValueError(
                    "ingest_tokens must be a JSON object with unique client identifiers"
                ) from None
        if not isinstance(value, dict):
            raise ValueError("ingest_tokens must map client identifiers to secrets")
        normalized = {}
        for client_id, secret in value.items():
            if (
                not isinstance(client_id, str)
                or not _CLIENT_ID.fullmatch(client_id)
                or client_id in {"operator", "legacy"}
            ):
                raise ValueError(
                    "ingest_tokens contains an invalid or reserved client identifier"
                )
            if isinstance(secret, SecretStr):
                secret = secret.get_secret_value()
            if not isinstance(secret, str):
                raise ValueError("ingest_tokens must map client identifiers to secrets")
            normalized[client_id] = SecretStr(secret.strip())
        return normalized

    def validate_production_security(self) -> list[str]:
        """Return human-readable security problems for a production boot.

        Pure: no I/O, no logging, never echoes a configured secret value. The
        caller (main.py) is responsible for raising. Returns ``[]`` when
        ``dev_mode`` is True (local dev opts out of the check).
        """
        if self.dev_mode:
            return []

        problems: list[str] = []
        operator = self.operator_token.get_secret_value()
        ingest = [secret.get_secret_value() for secret in self.ingest_tokens.values()]
        if self.api_bearer_token:
            ingest.append(self.api_bearer_token)
        if (
            operator.lower() in _INSECURE_DEFAULTS
            or len(operator) < _MIN_SECRET_LENGTH
            or any(not 33 <= ord(c) <= 126 for c in operator)
        ):
            problems.append(
                "operator_token is unset, invalid, or shorter than "
                f"{_MIN_SECRET_LENGTH} characters; set SEDIMENT_OPERATOR_TOKEN to a "
                "strong secret."
            )
        if not ingest:
            problems.append(
                "ingest credentials are unset; set SEDIMENT_INGEST_TOKENS or the ingest-only api_bearer_token (SEDIMENT_API_BEARER_TOKEN)."
            )
        if any(
            secret.lower() in _INSECURE_DEFAULTS
            or len(secret) < _MIN_SECRET_LENGTH
            or any(not 33 <= ord(c) <= 126 for c in secret)
            for secret in ingest
        ):
            problems.append(
                "ingest_tokens or api_bearer_token contains an empty, invalid, or "
                f"shorter-than-{_MIN_SECRET_LENGTH}-character secret; configure real "
                "ingest credentials."
            )
        if len(ingest) != len(set(ingest)) or operator in ingest:
            problems.append(
                "operator and ingest credentials must be distinct; duplicate secrets are not allowed."
            )
        if (
            self.github_webhook_secret.lower() in _INSECURE_DEFAULTS
            or len(self.github_webhook_secret) < _MIN_SECRET_LENGTH
        ):
            problems.append(
                "github_webhook_secret is unset, invalid, or shorter than "
                f"{_MIN_SECRET_LENGTH} characters; set SEDIMENT_GITHUB_WEBHOOK_SECRET "
                "to a strong secret."
            )
        return problems

    def config_warnings(self) -> list[str]:
        """Advisory (non-fatal) posture warnings for a production boot. Pure:
        no I/O, no logging. main.py logs each at WARNING. Unlike
        validate_production_security(), these never block the boot."""
        warnings: list[str] = []
        # Mirror mode enforces clone-URL confinement outside dev mode, but an
        # empty allowlist still admits any *public* host a valid-signature
        # webhook names (internal/private hosts and file: URLs stay refused).
        # Surface the open posture at boot so an operator chose it knowingly.
        if not self.dev_mode and self.mirror_path and not self.allowed_clone_hosts:
            warnings.append(
                "SEDIMENT_MIRROR_PATH is set with an empty "
                "SEDIMENT_ALLOWED_CLONE_HOSTS: any public host a valid-signature "
                "webhook names will be fetched (internal/private hosts and file: "
                "URLs are still refused). Set SEDIMENT_ALLOWED_CLONE_HOSTS to your "
                'forge host(s) to close this, e.g. ["github.com"].'
            )
        return warnings


settings = Settings()
