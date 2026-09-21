# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository closeout contract for the sole PostgreSQL fact store."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
ACTIVE_ROOTS = (
    "apps",
    "cli",
    "docs",
    "packages",
    "scripts",
    "sim",
    ".github",
)
ACTIVE_ROOT_FILES = (
    "AGENTS.md",
    "CONTEXT.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "README.md",
    "docker-compose.yml",
    "pyproject.toml",
)
TEXT_SUFFIXES = {".md", ".py", ".svg", ".toml", ".yaml", ".yml"}
MIGRATION_REFERENCES = {
    "docs/adr/0012-postgresql-fact-store.md",
}
FORBIDDEN = (
    "sql" + "ite",
    "db" + "_path",
    "sediment" + "_db" + "_path",
    "--db" + "-path",
)


def _active_files() -> list[Path]:
    files = [REPOSITORY_ROOT / name for name in ACTIVE_ROOT_FILES]
    for root_name in ACTIVE_ROOTS:
        root = REPOSITORY_ROOT / root_name
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in TEXT_SUFFIXES
            and "__pycache__" not in path.parts
            and path.relative_to(REPOSITORY_ROOT).as_posix() not in MIGRATION_REFERENCES
        )
    return sorted(files)


def test_active_repository_has_no_retired_fact_store_assumptions() -> None:
    violations: list[str] = []
    for path in _active_files():
        relative = path.relative_to(REPOSITORY_ROOT)
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            lowered = line.lower()
            if any(token in lowered for token in FORBIDDEN):
                violations.append(f"{relative}:{line_number}: {line.strip()}")

    assert violations == []
