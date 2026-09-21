# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verifier-commands loader tests — real TOML files on disk, never mocked
(per AGENTS.md). The loader is fail-soft: a config typo degrades every row to
recorded verifier evidence, it never aborts an export.
"""

from __future__ import annotations

import logging
from pathlib import Path

import sediment_export as export_api
from sediment_export import VerifierCommands, VerifierCommandsSettings

_LOGGER = "sediment.export.verifier_commands"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_per_repo_commands(tmp_path: Path) -> None:
    toml = _write(
        tmp_path / "verifiers.toml",
        """
        [repos."acme-corp/backend-service"]
        verification_command = "pytest -q"

        [repos."acme-corp/frontend"]
        verification_command = "npm test"
        """,
    )
    commands = VerifierCommands.load(toml)
    assert commands.for_repo("acme-corp/backend-service") == "pytest -q"
    assert commands.for_repo("acme-corp/frontend") == "npm test"
    # A repo with no entry degrades to recorded verifier evidence.
    assert commands.for_repo("acme-corp/never-configured") is None


def test_empty_config_returns_none_for_every_repo() -> None:
    commands = VerifierCommands.empty()
    assert commands.for_repo("acme-corp/anything") is None


def test_missing_file_degrades_to_empty(tmp_path: Path) -> None:
    commands = VerifierCommands.load(tmp_path / "does-not-exist.toml")
    assert commands.for_repo("acme-corp/backend-service") is None


def test_malformed_toml_degrades_to_empty(tmp_path: Path) -> None:
    toml = _write(tmp_path / "bad.toml", "this is not = valid = toml [[[")
    commands = VerifierCommands.load(toml)
    # Fail-soft: no exception, every repo unconfigured.
    assert commands.for_repo("acme-corp/backend-service") is None


def test_blank_or_missing_command_entry_is_skipped_siblings_survive(
    tmp_path: Path,
) -> None:
    toml = _write(
        tmp_path / "verifiers.toml",
        """
        [repos."acme-corp/blank"]
        verification_command = "   "

        [repos."acme-corp/missing"]
        note = "no command here"

        [repos."acme-corp/good"]
        verification_command = "  pytest  "
        """,
    )
    commands = VerifierCommands.load(toml)
    assert commands.for_repo("acme-corp/blank") is None
    assert commands.for_repo("acme-corp/missing") is None
    # A well-formed sibling still loads, and the value is stripped.
    assert commands.for_repo("acme-corp/good") == "pytest"


# The three distinct shapes load() must tell apart (a valid empty config vs. an
# operator typo must not read identically), asserted on both outcome and level.


def test_absent_file_degrades_to_empty_at_info(tmp_path: Path, caplog) -> None:
    # Shape 1: file not there at all — the expected unconfigured state, info.
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        commands = VerifierCommands.load(tmp_path / "does-not-exist.toml")
    assert commands.for_repo("acme-corp/x") is None
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in records] == [logging.INFO]
    assert records[0].message == "verifier_commands_file_absent"


def test_present_repos_table_loads_at_info_with_count(tmp_path: Path, caplog) -> None:
    # Shape 2a: a real [repos] table — info naming the repo count.
    toml = _write(
        tmp_path / "verifiers.toml",
        '[repos."acme-corp/backend-service"]\nverification_command = "pytest -q"\n',
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        commands = VerifierCommands.load(toml)
    assert commands.for_repo("acme-corp/backend-service") == "pytest -q"
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in records] == [logging.INFO]
    assert records[0].message == "verifier_commands_loaded"
    assert records[0].repo_count == 1


def test_empty_repos_table_is_a_valid_zero_repo_config_at_info(
    tmp_path: Path, caplog
) -> None:
    # Shape 2b: a legitimately empty [repos] table is a valid 0-repo config, not
    # a mistake — info at count 0, never a warning.
    toml = _write(tmp_path / "verifiers.toml", "[repos]\n")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        commands = VerifierCommands.load(toml)
    assert commands.for_repo("acme-corp/x") is None
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in records] == [logging.INFO]
    assert records[0].message == "verifier_commands_loaded"
    assert records[0].repo_count == 0


def test_missing_repos_table_degrades_to_empty_with_warning(
    tmp_path: Path, caplog
) -> None:
    # Shape 3: a present file whose top-level key is typo'd (no [repos] table)
    # would silently disable every command — warning naming the missing key.
    toml = _write(tmp_path / "verifiers.toml", 'other_key = "value"\n')
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        commands = VerifierCommands.load(toml)
    assert commands.for_repo("acme-corp/x") is None
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert records[0].message == "verifier_commands_bad_shape"
    assert "[repos]" in records[0].reason


def test_settings_unconfigured_resolves_to_empty(monkeypatch) -> None:
    monkeypatch.delenv("SEDIMENT_VERIFIER_COMMANDS_FILE", raising=False)
    commands = VerifierCommandsSettings(_env_file=None).resolve()
    assert commands.for_repo("acme-corp/x") is None


def test_settings_loads_from_env_path(tmp_path: Path, monkeypatch) -> None:
    toml = _write(
        tmp_path / "verifiers.toml",
        '[repos."acme-corp/backend-service"]\nverification_command = "pytest -q"\n',
    )
    monkeypatch.setenv("SEDIMENT_VERIFIER_COMMANDS_FILE", str(toml))
    commands = VerifierCommandsSettings(_env_file=None).resolve()
    assert commands.for_repo("acme-corp/backend-service") == "pytest -q"


def test_case_variant_keys_collide_with_warning_last_wins(
    tmp_path: Path, caplog
) -> None:
    """Two case-variant keys that normalize to the same repo log a collision
    warning and keep the later entry's command."""
    toml = _write(
        tmp_path / "verifiers.toml",
        '[repos."Acme/App"]\nverification_command = "first"\n'
        '[repos."acme/app"]\nverification_command = "second"\n',
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        commands = VerifierCommands.load(toml)
    # Last write wins.
    assert commands.for_repo("acme/app") == "second"
    records = [r for r in caplog.records if r.name == _LOGGER]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].message == "verifier_command_key_collision"
    assert warnings[0].repo == "acme/app"


def test_distinct_repos_produce_no_collision_warning(tmp_path: Path, caplog) -> None:
    """Two genuinely different repos must not trigger the collision warning."""
    toml = _write(
        tmp_path / "verifiers.toml",
        '[repos."acme/alpha"]\nverification_command = "a"\n'
        '[repos."acme/beta"]\nverification_command = "b"\n',
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        VerifierCommands.load(toml)
    records = [r for r in caplog.records if r.name == _LOGGER]
    messages = [r.message for r in records]
    assert "verifier_command_key_collision" not in messages


def test_display_case_key_matches_lowercased_repo(tmp_path: Path) -> None:
    """CIOutcome.repo is lowercased at the schema and for_repo is an exact
    match — an operator key typed in GitHub's display case must keep
    matching instead of silently degrading every task row."""
    toml = _write(
        tmp_path / "verifiers.toml",
        '[repos."Acme-Corp/Backend-Service"]\nverification_command = "pytest -q"\n',
    )
    commands = VerifierCommands.load(toml)
    assert commands.for_repo("acme-corp/backend-service") == "pytest -q"


def test_verifier_commands_rejects_removed_toml_spelling(
    tmp_path: Path, caplog
) -> None:
    toml = _write(
        tmp_path / "verifiers.toml",
        '[repos."acme-corp/backend-service"]\nrerun_command = "pytest -q"\n',
    )
    with caplog.at_level(logging.WARNING, logger="sediment.export.verifier_commands"):
        commands = export_api.VerifierCommands.load(toml)
    assert commands.for_repo("acme-corp/backend-service") is None
    assert [record.message for record in caplog.records] == [
        "verifier_command_entry_invalid"
    ]


def test_verifier_command_settings_ignore_removed_environment_name(
    tmp_path: Path, monkeypatch
) -> None:
    legacy = _write(
        tmp_path / "legacy.toml",
        '[repos."acme/app"]\nverification_command = "legacy"\n',
    )
    monkeypatch.delenv("SEDIMENT_VERIFIER_COMMANDS_FILE", raising=False)
    monkeypatch.setenv("SEDIMENT_REWARD_COMMANDS_FILE", str(legacy))
    settings = export_api.VerifierCommandsSettings(_env_file=None)
    assert settings.resolve().for_repo("acme/app") is None


def test_reward_commands_public_names_are_removed() -> None:
    assert hasattr(export_api, "VerifierCommands")
    assert hasattr(export_api, "VerifierCommandsSettings")
    assert not hasattr(export_api, "RewardCommands")
    assert not hasattr(export_api, "RewardCommandsSettings")
