# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Per-repo verification-command configuration for RLVR targets.

``verification_command`` is **operator configuration, never inference.** Mechanically
deriving a runnable eval command from arbitrary CI YAML is not solvable in
general — a workflow shells out, sets up services, matrixes over versions —
so SWE-bench hand-curates a per-repo eval command and Sediment requires
operator configuration. When a repo has no configured command, target rows
omit verification configuration and retain only recorded verifier results.

The TOML file is named by ``SEDIMENT_VERIFIER_COMMANDS_FILE`` and
loaded by :class:`VerifierCommandsSettings`. Shape — one table per
``owner/repo``::

    [repos."owner/repo"]
    verification_command = "pytest -q"

    [repos."owner/other"]
    verification_command = "make test"

This module reads configuration only. It never runs a command. The consumer
owns verifier execution and its isolation requirements.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sediment_core import RepoSlug, normalize_repo_slug

logger = logging.getLogger("sediment.export.verifier_commands")


@dataclass(frozen=True)
class VerifierCommands:
    """An immutable ``owner/repo -> verification_command`` lookup.

    Built by :meth:`load` from the TOML file, or :meth:`empty` when no file is
    configured. A stateless value object — the projection holds one for the
    duration of a run and asks it per task row."""

    _by_repo: dict[RepoSlug, str]

    @classmethod
    def empty(cls) -> VerifierCommands:
        """Return an unconfigured per-repo lookup."""
        return cls(_by_repo={})

    @classmethod
    def load(cls, path: str | Path) -> VerifierCommands:
        """Parse the verifier-commands TOML at ``path``.

        Fail-soft, like the notes reader (``sediment_derive.notes``): every
        degenerate case yields an **empty** mapping rather than raising, so a
        config typo degrades every row to recorded-result-as-label (visible in
        the output) and never aborts an export. The log level distinguishes the
        expected from the suspicious:

        * **File absent** — the expected unconfigured state; logged at *info*.
        * **File present with a** ``[repos]`` **table** — logged at *info* with
          the repo count (a legitimately empty table is a valid 0-repo config).
        * **File present without a** ``[repos]`` **table** (a typo'd top-level
          key), unreadable bytes, or malformed TOML — an operator mistake that
          silently disables every command; logged at *warning*, naming the
          problem.

        An entry whose ``verification_command`` is missing, non-string, or blank is
        skipped with a warning; well-formed sibling entries still load.
        Two keys that normalize to the same repo slug (case variants; exact
        duplicates are a TOML parse error) collide: the later entry wins,
        logged at *warning* (``verifier_command_key_collision``).
        """
        p = Path(path)
        try:
            raw = p.read_bytes()
        except FileNotFoundError:
            # The expected unconfigured state, not an operator error: a runner
            # pointed at a path that isn't there. Degrade quietly at info.
            logger.info("verifier_commands_file_absent", extra={"path": str(p)})
            return cls.empty()
        except OSError as exc:
            logger.warning(
                "verifier_commands_file_unreadable",
                extra={"path": str(p), "error": str(exc)},
            )
            return cls.empty()
        try:
            data = tomllib.loads(raw.decode("utf-8", errors="replace"))
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            logger.warning(
                "verifier_commands_file_malformed",
                extra={"path": str(p), "error": str(exc)},
            )
            return cls.empty()

        # A missing [repos] key is not the same as an empty [repos] table: the
        # former is a typo'd top-level key that would silently disable every
        # command (warning), the latter is a valid config that happens to
        # configure zero repos (info, below).
        if "repos" not in data:
            logger.warning(
                "verifier_commands_bad_shape",
                extra={"path": str(p), "reason": "top-level [repos] table missing"},
            )
            return cls.empty()
        repos = data["repos"]
        if not isinstance(repos, dict):
            logger.warning(
                "verifier_commands_bad_shape",
                extra={"path": str(p), "reason": "[repos] is not a table"},
            )
            return cls.empty()

        by_repo: dict[RepoSlug, str] = {}
        for repo, entry in repos.items():
            command = (
                entry.get("verification_command") if isinstance(entry, dict) else None
            )
            if not isinstance(command, str) or not command.strip():
                logger.warning(
                    "verifier_command_entry_invalid",
                    extra={"path": str(p), "repo": repo},
                )
                continue
            # for_repo is an exact match against CIOutcome.repo, which the
            # schema lowercases — an operator key typed in GitHub's display
            # case must not silently stop matching.
            try:
                key: RepoSlug = normalize_repo_slug(str(repo))
            except ValueError:
                key = ""
            if not key:
                logger.warning(
                    "verifier_command_entry_invalid",
                    extra={"path": str(p), "repo": repo},
                )
                continue
            if key in by_repo:
                logger.warning(
                    "verifier_command_key_collision",
                    extra={"path": str(p), "repo": key},
                )
            by_repo[key] = command.strip()
        logger.info(
            "verifier_commands_loaded",
            extra={"path": str(p), "repo_count": len(by_repo)},
        )
        return cls(_by_repo=by_repo)

    def for_repo(self, repo: RepoSlug) -> str | None:
        """Return the configured verification command for ``repo``."""
        return self._by_repo.get(repo)


class VerifierCommandsSettings(BaseSettings):
    """Load the verifier-commands file path from the environment.

    ``SEDIMENT_VERIFIER_COMMANDS_FILE`` names the per-repo TOML.
    Default ``None`` means **unconfigured**: :meth:`resolve` returns an empty
    :class:`VerifierCommands`, so target rows omit verification configuration.
    """

    # extra="ignore": the process env / .env is shared with other components
    # (apps/api's Settings), so tolerate keys this model does
    # not own instead of failing construction.
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SEDIMENT_", extra="ignore"
    )

    verifier_commands_file: str | None = None

    def resolve(self) -> VerifierCommands:
        """The loaded commands, or an empty mapping when unconfigured."""
        if not self.verifier_commands_file:
            return VerifierCommands.empty()
        return VerifierCommands.load(self.verifier_commands_file)
