# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release-contract tests for the six no-publish distributions."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LICENSE_TEXT = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
MEMBER_PROJECTS = (
    REPO_ROOT / "packages" / "core" / "pyproject.toml",
    REPO_ROOT / "packages" / "capture" / "pyproject.toml",
    REPO_ROOT / "packages" / "derive" / "pyproject.toml",
    REPO_ROOT / "packages" / "export" / "pyproject.toml",
    REPO_ROOT / "apps" / "api" / "pyproject.toml",
    REPO_ROOT / "cli" / "pyproject.toml",
)
FIRST_PARTY = {
    "sediment-core",
    "sediment-capture",
    "sediment-derive",
    "sediment-export",
    "sediment-api",
    "sediment-cli",
}


PROJECT_URLS = {"Source", "Documentation", "Issues", "Changelog"}
REQUIRED_CONTENT = {
    "sediment-core": (
        "sediment_core/__init__.py",
        "sediment_core/alembic/env.py",
        "sediment_core/alembic/versions/0001_postgresql_baseline.py",
    ),
    "sediment-capture": ("sediment_capture/__init__.py",),
    "sediment-derive": ("sediment_derive/__init__.py",),
    "sediment-export": ("sediment_export/__init__.py",),
    "sediment-api": ("sediment_api/__init__.py",),
    "sediment-cli": (
        "sediment_cli/__init__.py",
        "sediment_cli/transcript.py",
        "sediment_cli/delivery.py",
    ),
}
INTERNAL_REQUIREMENTS = {
    "sediment-core": (),
    "sediment-capture": ("sediment-core",),
    "sediment-derive": ("sediment-core",),
    "sediment-export": ("sediment-core", "sediment-derive"),
    "sediment-api": (
        "sediment-core",
        "sediment-capture",
        "sediment-derive",
        "sediment-export",
    ),
    "sediment-cli": (
        "sediment-core",
        "sediment-derive",
        "sediment-export",
        "sediment-api",
    ),
}


def _load_rehearsal():
    path = REPO_ROOT / "scripts" / "release_rehearsal.py"
    spec = importlib.util.spec_from_file_location("release_rehearsal", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0]


def _copy_release_source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    shutil.copy2(REPO_ROOT / "LICENSE", root / "LICENSE")
    for source in (REPO_ROOT / "pyproject.toml", *MEMBER_PROJECTS):
        relative = source.relative_to(REPO_ROOT)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if source in MEMBER_PROJECTS:
            shutil.copy2(source.with_name("LICENSE"), target.with_name("LICENSE"))
    runtime = REPO_ROOT / "apps" / "api" / "sediment_api" / "__init__.py"
    target = root / runtime.relative_to(REPO_ROOT)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(runtime, target)
    return root


def _write_wheel(
    directory: Path,
    name: str,
    *,
    version: str = "0.1.0",
    requirement_override: str | None = None,
    include_content: bool = True,
    include_urls: bool = True,
    include_enterprise: bool = False,
    include_license: bool = True,
    license_text: str = LICENSE_TEXT,
) -> Path:
    normalized = name.replace("-", "_")
    path = directory / f"{normalized}-0.1.0-py3-none-any.whl"
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {name} summary",
        "Requires-Python: >=3.12",
        "License-Expression: AGPL-3.0-or-later",
    ]
    if include_license:
        metadata.append("License-File: LICENSE")
    if include_urls:
        metadata.extend(
            f"Project-URL: {label}, https://example.test/{label.lower()}"
            for label in sorted(PROJECT_URLS)
        )
    requirements = [
        f"{dependency}==0.1.0" for dependency in INTERNAL_REQUIREMENTS[name]
    ]
    if requirement_override is not None:
        requirements[0] = requirement_override
    metadata.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
    metadata.append("")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{normalized}-0.1.0.dist-info/METADATA", "\n".join(metadata))
        if include_content:
            for member in REQUIRED_CONTENT[name]:
                archive.writestr(member, "")
        if include_enterprise:
            archive.writestr("enterprise/private.py", "")
        if include_license:
            archive.writestr(
                f"{normalized}-0.1.0.dist-info/licenses/LICENSE", license_text
            )
    return path


def _wheel_set(directory: Path) -> list[Path]:
    directory.mkdir()
    return [_write_wheel(directory, name) for name in sorted(FIRST_PARTY)]


def _write_sdist(
    directory: Path,
    name: str,
    *,
    version: str = "0.1.0",
    include_content: bool = True,
    include_urls: bool = True,
    include_enterprise: bool = False,
    include_license: bool = True,
    license_text: str = LICENSE_TEXT,
    buildable: bool = True,
) -> Path:
    directory.mkdir(exist_ok=True)
    normalized = name.replace("-", "_")
    path = directory / f"{normalized}-0.1.0.tar.gz"
    root = f"{normalized}-0.1.0"
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {name} summary",
        "Requires-Python: >=3.12",
        "License-Expression: AGPL-3.0-or-later",
    ]
    if include_license:
        metadata.append("License-File: LICENSE")
    if include_urls:
        metadata.extend(
            f"Project-URL: {label}, https://example.test/{label.lower()}"
            for label in sorted(PROJECT_URLS)
        )
    metadata.append("")
    backend = "hatchling.build" if buildable else "missing_backend.build"
    files = {
        f"{root}/PKG-INFO": "\n".join(metadata),
        f"{root}/pyproject.toml": (
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            f'build-backend = "{backend}"\n\n'
            "[project]\n"
            f'name = "{name}"\n'
            'version = "0.1.0"\n'
        ),
    }
    if include_content:
        files.update({f"{root}/{member}": "" for member in REQUIRED_CONTENT[name]})
    if include_enterprise:
        files[f"{root}/enterprise/private.py"] = ""
    if include_license:
        files[f"{root}/LICENSE"] = license_text
    with tarfile.open(path, "w:gz") as archive:
        for member, contents in files.items():
            encoded = contents.encode()
            info = tarfile.TarInfo(member)
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
    return path


def _sdist_set(directory: Path) -> list[Path]:
    return [_write_sdist(directory, name) for name in sorted(FIRST_PARTY)]


def test_source_release_metadata_is_synchronized_and_complete() -> None:
    root = _project(REPO_ROOT / "pyproject.toml")
    release_version = root["version"]

    for path in (REPO_ROOT / "pyproject.toml", *MEMBER_PROJECTS):
        project = _project(path)
        assert project["version"] == release_version, path
        for requirement in project.get("dependencies", []):
            if _requirement_name(requirement) in FIRST_PARTY:
                assert requirement == (
                    f"{_requirement_name(requirement)}=={release_version}"
                ), path

    for path in MEMBER_PROJECTS:
        project = _project(path)
        assert project["description"].strip(), path
        assert project["requires-python"] == ">=3.12", path
        assert project["license"] == "AGPL-3.0-or-later", path
        assert project["license-files"] == ["LICENSE"], path
        assert path.with_name("LICENSE").read_text(encoding="utf-8") == LICENSE_TEXT
        assert set(project.get("urls", {})) == PROJECT_URLS, path


def test_workflow_actions_are_immutable_and_tracked() -> None:
    action_line = re.compile(r"^\s*(?:-\s+)?uses:\s+([^\s#]+)(?:\s+#\s+(.+))?$")
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")):
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            match = action_line.match(line)
            if match is None or match.group(1).startswith("./"):
                continue
            reference, comment = match.groups()
            assert re.search(r"@[0-9a-f]{40}$", reference), (
                f"{path}:{line_number}: mutable action reference {reference}"
            )
            assert comment and re.fullmatch(r"v\d+(?:\.\d+){0,2}", comment), (
                f"{path}:{line_number}: missing readable action version"
            )

    dependabot = (REPO_ROOT / ".github" / "dependabot.yml").read_text()
    assert "package-ecosystem: github-actions" in dependabot


def test_no_publish_release_rehearsal_entrypoint_exists() -> None:
    assert (REPO_ROOT / "scripts" / "release_rehearsal.py").is_file()


def test_rehearsal_exposes_shared_release_validators() -> None:
    rehearsal = _load_rehearsal()
    assert callable(getattr(rehearsal, "validate_source", None))
    assert callable(getattr(rehearsal, "validate_actions", None))
    assert callable(getattr(rehearsal, "validate_wheels", None))
    assert callable(getattr(rehearsal, "validate_sdists", None))
    assert callable(getattr(rehearsal, "rebuild_wheels_from_sdists", None))
    assert callable(getattr(rehearsal, "validate_installed_hooks", None))
    assert callable(getattr(rehearsal, "rehearse", None))


@pytest.mark.parametrize(
    ("defect", "expected"),
    (
        ("version", "version"),
        ("requirement", "exact"),
        ("runtime", "runtime"),
        ("tag", "tag"),
        ("release-shape", "release version must"),
    ),
)
def test_source_validator_rejects_release_skew(
    tmp_path: Path, defect: str, expected: str
) -> None:
    root = _copy_release_source(tmp_path)
    current = re.search(
        r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M
    ).group(1)
    tag = f"v{current}"
    if defect == "version":
        path = root / "packages" / "capture" / "pyproject.toml"
        path.write_text(
            path.read_text().replace(f'version = "{current}"', 'version = "9.9.9"')
        )
    elif defect == "requirement":
        path = root / "packages" / "capture" / "pyproject.toml"
        path.write_text(
            path.read_text().replace(f"sediment-core=={current}", "sediment-core")
        )
    elif defect == "runtime":
        path = root / "apps" / "api" / "sediment_api" / "__init__.py"
        path.write_text(path.read_text().replace(f'"{current}"', '"9.9.9"'))
    elif defect == "release-shape":
        path = root / "pyproject.toml"
        path.write_text(
            path.read_text().replace(
                f'version = "{current}"', f'version = "{current}rc1.post1"'
            )
        )
    else:
        tag = "v9.9.9"

    errors = _load_rehearsal().validate_source(root, tag)
    assert any(expected in error for error in errors), errors


def test_action_validator_rejects_mutable_reference(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    errors = _load_rehearsal().validate_actions(tmp_path)
    assert any("immutable" in error for error in errors), errors


def test_action_validator_rejects_mutable_two_line_reference(tmp_path: Path) -> None:
    # The `- name:` / `uses:` step style and job-level reusable-workflow
    # `uses:` lines carry no list dash; the gate must still see them.
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - name: Check out\n"
        "        uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    errors = _load_rehearsal().validate_actions(tmp_path)
    assert any("immutable" in error for error in errors), errors


@pytest.mark.parametrize(
    ("defect", "expected"),
    (
        ("missing", "wheel set"),
        ("extra", "wheel set"),
        ("version", "version"),
        ("requirement", "exact"),
        ("content", "missing package content"),
        ("metadata", "project URLs"),
        ("enterprise", "forbidden package content"),
        ("license", "license file"),
        ("license-content", "license file content"),
    ),
)
def test_wheel_validator_rejects_release_defects(
    tmp_path: Path, defect: str, expected: str
) -> None:
    wheels = _wheel_set(tmp_path / "wheels")
    if defect == "missing":
        wheels.pop()
    elif defect == "extra":
        extra = tmp_path / "wheels" / "not_sediment-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(extra, "w"):
            pass
        wheels.append(extra)
    elif defect == "version":
        wheels.remove(next(path for path in wheels if "sediment_core" in path.name))
        wheels.append(
            _write_wheel(tmp_path / "wheels", "sediment-core", version="9.9.9")
        )
    elif defect == "requirement":
        wheels.remove(next(path for path in wheels if "sediment_capture" in path.name))
        wheels.append(
            _write_wheel(
                tmp_path / "wheels",
                "sediment-capture",
                requirement_override="sediment-core>=0.1.0",
            )
        )
    elif defect == "content":
        wheels.remove(next(path for path in wheels if "sediment_cli" in path.name))
        wheels.append(
            _write_wheel(tmp_path / "wheels", "sediment-cli", include_content=False)
        )
    elif defect == "metadata":
        wheels.remove(next(path for path in wheels if "sediment_export" in path.name))
        wheels.append(
            _write_wheel(tmp_path / "wheels", "sediment-export", include_urls=False)
        )
    elif defect == "enterprise":
        wheels.remove(next(path for path in wheels if "sediment_core" in path.name))
        wheels.append(
            _write_wheel(tmp_path / "wheels", "sediment-core", include_enterprise=True)
        )
    elif defect == "license":
        wheels.remove(next(path for path in wheels if "sediment_derive" in path.name))
        wheels.append(
            _write_wheel(tmp_path / "wheels", "sediment-derive", include_license=False)
        )
    else:
        wheels.remove(next(path for path in wheels if "sediment_api" in path.name))
        wheels.append(
            _write_wheel(tmp_path / "wheels", "sediment-api", license_text="wrong")
        )

    errors = _load_rehearsal().validate_wheels(wheels, "0.1.0")
    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize(
    ("defect", "expected"),
    (
        ("missing", "source distribution set"),
        ("extra", "source distribution set"),
        ("version", "version"),
        ("content", "missing package content"),
        ("metadata", "project URLs"),
        ("enterprise", "forbidden package content"),
        ("license", "license file"),
        ("license-content", "license file content"),
    ),
)
def test_sdist_validator_rejects_release_defects(
    tmp_path: Path, defect: str, expected: str
) -> None:
    sdists = _sdist_set(tmp_path / "sdists")
    if defect == "missing":
        sdists.pop()
    elif defect == "extra":
        extra = tmp_path / "sdists" / "not_sediment-0.1.0.tar.gz"
        with tarfile.open(extra, "w:gz"):
            pass
        sdists.append(extra)
    else:
        name = {
            "version": "sediment-core",
            "content": "sediment-cli",
            "metadata": "sediment-export",
            "enterprise": "sediment-capture",
            "license": "sediment-derive",
            "license-content": "sediment-api",
        }[defect]
        sdists.remove(
            next(path for path in sdists if name.replace("-", "_") in path.name)
        )
        sdists.append(
            _write_sdist(
                tmp_path / "sdists",
                name,
                version="9.9.9" if defect == "version" else "0.1.0",
                include_content=defect != "content",
                include_urls=defect != "metadata",
                include_enterprise=defect == "enterprise",
                include_license=defect != "license",
                license_text="wrong" if defect == "license-content" else LICENSE_TEXT,
            )
        )

    errors = _load_rehearsal().validate_sdists(sdists, "0.1.0")
    assert any(expected in error for error in errors), errors


def test_sdist_rebuild_reports_an_isolation_failure(tmp_path: Path) -> None:
    sdist = _write_sdist(tmp_path / "sdists", "sediment-core", buildable=False)
    _, errors = _load_rehearsal().rebuild_wheels_from_sdists(
        [sdist], tmp_path / "rebuilt", tmp_path
    )
    assert any("wheel build failed" in error for error in errors), errors


def test_rehearsal_requires_a_postgresql_target() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SEDIMENT_DATABASE_URL", "SEDIMENT_TEST_DATABASE_URL"}
    }
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "release_rehearsal.py")],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--database-url" in result.stderr


def test_pull_request_and_tag_workflows_run_the_same_rehearsal() -> None:
    command = "uv run python scripts/release_rehearsal.py"
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    release = (REPO_ROOT / ".github" / "workflows" / "release.yaml").read_text()
    assert command in ci
    assert command in release
    assert f'{command} --tag "$GITHUB_REF_NAME" --out-dir dist' in release
    assert release.index(command) < release.index("uv publish")


def test_tag_release_uses_one_validated_artifact_handoff() -> None:
    release = (REPO_ROOT / ".github" / "workflows" / "release.yaml").read_text()

    assert "concurrency:" in release
    assert "release-${{ github.ref }}" in release
    assert "releases remain disabled while the repository is private" in release
    assert 'git cat-file -t "$GITHUB_REF_NAME"' in release
    assert "git merge-base --is-ancestor HEAD origin/main" in release
    assert release.count('git rev-parse "$GITHUB_REF_NAME^{commit}"') == 3
    assert "release-commit:" in release
    assert "EXPECTED_RELEASE_COMMIT" in release
    assert "persist-credentials: false" in release
    assert "actions/upload-artifact@" in release
    assert release.count("actions/download-artifact@") == 4
    assert release.count("uv run python scripts/release_rehearsal.py") == 1

    assert "publish-pypi:" in release
    assert "needs: build" in release
    assert "name: pypi" in release
    publish_pypi = release.split("  publish-pypi:", 1)[1].split("  publish-github:", 1)[
        0
    ]
    assert "actions/checkout@" in publish_pypi
    assert "EXPECTED_RELEASE_COMMIT" in publish_pypi
    assert "git merge-base --is-ancestor HEAD origin/main" in publish_pypi
    assert publish_pypi.index('git rev-parse "$GITHUB_REF_NAME^{commit}"') < (
        publish_pypi.index("uv publish")
    )
    assert release.count("id-token: write") == 1
    assert "uv publish" in release
    assert "--trusted-publishing always" in release
    assert "--publish-url https://upload.pypi.org/legacy/" in release
    assert "--check-url https://pypi.org/simple/" in release
    publish_order = (
        "dist/sediment_api-*",
        "dist/sediment_capture-*",
        "dist/sediment_cli-*",
        "dist/sediment_core-*",
        "dist/sediment_derive-*",
        "dist/sediment_export-*",
    )
    assert [release.index(pattern) for pattern in publish_order] == sorted(
        release.index(pattern) for pattern in publish_order
    )

    assert "publish-github:" in release
    assert "needs: [build, security, prepare-release, publish-pypi]" in release
    assert "contents: write" in release
    assert "SHA256SUMS" in release
    assert "install.sh" in release
    assert "gh release create" in release
    assert "--draft" in release
    assert "gh release delete-asset" in release
    assert "expected_assets" in release
    assert "actual_assets" in release
    assert "gh release edit" in release
    assert "--prerelease" in release


def test_installed_hook_validator_rejects_checkout_commands(tmp_path: Path) -> None:
    sediment = tmp_path / "venv" / "bin" / "sediment"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        'python3 "/checkout/scripts/'
                                        'sediment_transcript.py" --agent claude-code'
                                    ),
                                }
                            ]
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "Edit|Write",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        'python3 "/checkout/scripts/'
                                        'sediment_transcript.py" snapshot '
                                        "--agent claude-code"
                                    ),
                                }
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    errors = _load_rehearsal().validate_installed_hooks(settings, sediment)
    assert any("installed sediment" in error for error in errors), errors


def test_rehearsal_fails_before_building_when_source_contract_is_invalid(
    tmp_path: Path,
) -> None:
    root = _copy_release_source(tmp_path)
    errors = _load_rehearsal().rehearse(
        root,
        "postgresql+psycopg://postgres:postgres@localhost/postgres",
        "v9.9.9",
    )
    assert any("tag" in error for error in errors), errors


def test_rehearsal_does_not_report_success_when_runtime_check_fails() -> None:
    errors = _load_rehearsal().rehearse(
        REPO_ROOT,
        "postgresql+psycopg://postgres:postgres@127.0.0.1:9/postgres",
    )
    assert any("runtime rehearsal" in error for error in errors), errors


@pytest.mark.skipif(sys.platform != "darwin", reason="Homebrew library discovery")
def test_scratch_database_finds_homebrew_libpq_in_a_fresh_process(postgres_admin_url):
    if not shutil.which("brew"):
        pytest.skip("Homebrew is required by the macOS installation guide")
    environment = os.environ.copy()
    # A typical Homebrew install leaves keg-only libpq off PATH. Isolate the
    # process so another test's driver import cannot make this check pass.
    environment["PATH"] = os.pathsep.join(
        entry
        for entry in environment["PATH"].split(os.pathsep)
        if not (Path(entry) / "pg_config").exists()
    )
    environment["SEDIMENT_TEST_DATABASE_URL"] = postgres_admin_url
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import os, runpy, sys",
                    "rehearsal = runpy.run_path(sys.argv[1])",
                    "with rehearsal['scratch_database'](os.environ['SEDIMENT_TEST_DATABASE_URL']) as url:",
                    "    assert 'sediment_rehearsal_' in url",
                ]
            ),
            str(REPO_ROOT / "scripts/release_rehearsal.py"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("query_override", [False, True])
def test_runtime_corpus_owns_and_cleans_an_exact_scratch_database(
    query_override, postgres_admin_url
):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    rehearsal = _load_rehearsal()
    assert callable(getattr(rehearsal, "scratch_database", None))
    admin_url = postgres_admin_url
    if query_override:
        parsed = make_url(admin_url)
        admin_url = parsed.update_query_dict(
            {"dbname": parsed.database}
        ).render_as_string(hide_password=False)
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with rehearsal.scratch_database(admin_url) as scratch_url:
            name = make_url(scratch_url).database
            assert name != make_url(admin_url).database
            assert re.fullmatch(r"sediment_rehearsal_[0-9a-f]{32}", name)
            runtime_engine = create_engine(scratch_url)
            try:
                with runtime_engine.connect() as connection:
                    assert (
                        connection.exec_driver_sql(
                            "SELECT current_database()"
                        ).scalar_one()
                        == name
                    )
            finally:
                runtime_engine.dispose()
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT count(*) FROM pg_database WHERE datname=:name"),
                        {"name": name},
                    ).scalar_one()
                    == 1
                )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM pg_database WHERE datname=:name"),
                    {"name": name},
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_runtime_corpus_refuses_unavailable_admin_without_disclosing_url():
    rehearsal = _load_rehearsal()
    assert callable(getattr(rehearsal, "scratch_database", None))
    with pytest.raises(
        RuntimeError, match="scratch database creation failed"
    ) as caught:
        with rehearsal.scratch_database(
            "postgresql+psycopg://secret-user:secret-password@127.0.0.1:9/operator"
        ):
            pytest.fail("unavailable administration admitted the corpus")
    assert "secret" not in str(caught.value)


def test_runtime_corpus_rejects_non_postgresql_before_opening_it(monkeypatch):
    import sqlalchemy

    opened = []

    def unexpected_engine(*args, **kwargs):
        opened.append(args)
        raise AssertionError("non-PostgreSQL engine creation attempted")

    monkeypatch.setattr(sqlalchemy, "create_engine", unexpected_engine)
    with pytest.raises(RuntimeError, match="scratch database creation failed"):
        with _load_rehearsal().scratch_database("mysql://operator@localhost/operator"):
            pytest.fail("non-PostgreSQL target admitted")
    assert opened == []


def test_runtime_corpus_cleans_its_database_when_body_fails(postgres_admin_url):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    admin_url = postgres_admin_url
    with pytest.raises(ValueError, match="synthetic stop"):
        with _load_rehearsal().scratch_database(admin_url) as scratch_url:
            name = make_url(scratch_url).database
            raise ValueError("synthetic stop")
    engine = create_engine(admin_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM pg_database WHERE datname=:name"),
                    {"name": name},
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_pipeline_report_rejects_boolean_in_place_of_a_count():
    from copy import deepcopy

    rehearsal = _load_rehearsal()
    report = {
        "fixture_version": rehearsal.PIPELINE_FIXTURE_VERSION,
        "stages": deepcopy(rehearsal.PIPELINE_EXPECTATIONS),
    }
    report["stages"]["training"]["sft_rows"] = True
    assert rehearsal.validate_pipeline_report(report)


def test_pipeline_receipts_require_exact_counts_independent_of_arrival_order():
    from copy import deepcopy
    from itertools import permutations

    receipts = []
    for stored in (True, False):
        for records, candidates in (
            (
                {"received": 5, "translated": 2, "untranslated": 2, "malformed": 1},
                (3, 0, 0, 0),
            ),
            (
                {"received": 3, "translated": 3, "untranslated": 0, "malformed": 0},
                (0, 1, 1, 1),
            ),
        ):
            receipts.append(
                {
                    "records": records,
                    "facts": {
                        name: {
                            "candidates": count,
                            "stored": count if stored else 0,
                            "duplicates": 0 if stored else count,
                        }
                        for name, count in zip(
                            (
                                "developer_decisions",
                                "edit_observations",
                                "rejected_edits",
                                "retry_linkages",
                            ),
                            candidates,
                            strict=True,
                        )
                    },
                }
            )
    validate = _load_rehearsal().validate_pipeline_receipts
    for order in permutations(receipts):
        assert validate(list(order))
    for changed in (receipts[:-1], [*receipts, receipts[0]]):
        assert not validate(changed)
    for field in ("candidates", "stored", "duplicates"):
        changed = deepcopy(receipts)
        changed[0]["facts"]["developer_decisions"][field] += 1
        assert not validate(changed)
    changed = deepcopy(receipts)
    changed[1]["facts"]["edit_observations"]["stored"] = True
    assert not validate(changed)


@pytest.mark.parametrize(
    "stage",
    [
        "capture",
        "authority",
        "transcript",
        "transcript_batch",
        "bundle",
        "repository_identity",
        "training",
        "quarantine",
        "cohort",
        "model_report",
        "lifecycle_report",
        "delivery",
        "lost_acknowledgment",
        "reproduction",
        "tamper",
    ],
)
def test_rehearsal_cannot_pass_with_missing_pipeline_stage(stage):
    from copy import deepcopy

    rehearsal = _load_rehearsal()
    report = {
        "fixture_version": rehearsal.PIPELINE_FIXTURE_VERSION,
        "stages": deepcopy(rehearsal.PIPELINE_EXPECTATIONS),
    }
    assert rehearsal.validate_pipeline_report(report) == []
    report["stages"].pop(stage, None)
    assert rehearsal.validate_pipeline_report(report) == [
        f"runtime rehearsal: {stage} evidence absent or contradictory"
    ]


def test_installed_rehearsal_executes_and_reports_every_pipeline_stage(
    capsys, postgres_admin_url, monkeypatch
):
    from sqlalchemy import create_engine

    if sys.platform == "darwin":
        monkeypatch.setenv(
            "PATH",
            os.pathsep.join(
                entry
                for entry in os.environ["PATH"].split(os.pathsep)
                if not (Path(entry) / "pg_config").exists()
            ),
        )
    rehearsal = _load_rehearsal()
    errors = rehearsal.rehearse(REPO_ROOT, postgres_admin_url)
    assert errors == []
    reports = [
        line.removeprefix("pipeline acceptance: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("pipeline acceptance: ")
    ]
    assert len(reports) == 1, "installed pipeline acceptance never ran"
    report = json.loads(reports[0])
    assert rehearsal.validate_pipeline_report(report) == []
    assert set(report["runtime_versions"]) == FIRST_PARTY
    assert len(report["wheel_sha256"]) == len(FIRST_PARTY)
    # The report names the server the rehearsal ran against, not a pinned major.
    engine = create_engine(postgres_admin_url)
    try:
        with engine.connect() as connection:
            server_version = connection.exec_driver_sql(
                "SHOW server_version"
            ).scalar_one()
    finally:
        engine.dispose()
    assert report["postgresql_version"] == server_version
    assert report["stages"]["authority"] == {
        "operator_identity": "operator",
        "capture_identity": "legacy",
        "ingest_reads_denied": True,
        "operator_reads_allowed": True,
        "capture_files_ingest_only": True,
        "capture_files_private": True,
        "api_database_role": "sediment_runtime",
        "production_validation": True,
    }
    assert report["python_version"].startswith("3.12.")
    assert "sediment transcript --agent claude-code" in report["entry_points"]
    for route in ("model-outcomes", "accepted-work-lifecycle"):
        assert f"GET /v1/reports/{route}" in report["entry_points"]
    identity = report["stages"]["repository_identity"]
    assert all(
        identity[key] == value
        for key, value in rehearsal.PIPELINE_EXPECTATIONS["repository_identity"].items()
    )
    assert identity["observed_repo_slugs"] == [
        "synthetic/rehearsal",
        "synthetic/renamed-rehearsal",
    ]
    assert "POST /ingest/github/repository" in report["entry_points"]
    assert "GET /query/commit/{sha}" in report["entry_points"]
    model = report["stages"]["model_report"]
    assert model["completions"] == 4
    assert model["explicit_accepts"] == 3
    assert model["attributed_inference_calls"] == 2
    assert model["ci_linked"] == model["ci_passed"] == 1
    lifecycle = report["stages"]["lifecycle_report"]
    assert lifecycle["accepted_calls"] == lifecycle["observed_accepts"] == 2
    assert lifecycle["attributed"] == lifecycle["edit_observations"] == 1
    assert lifecycle["pull_request_membership"] == lifecycle["ci_linked"] == 0
    assert lifecycle["missing_pull_request_membership"] == 1
    for stage in (model, lifecycle):
        assert stage["authenticated"] is stage["bytes_equal"] is True
        assert stage["sha256_before"] == stage["sha256_after"]
        assert re.fullmatch("[0-9a-f]{64}", stage["sha256_before"])
    assert model["scope"] == lifecycle["scope"]
    delivery = report["stages"]["delivery"]
    assert delivery["gateway_queued"] == 4
    assert delivery["otlp_queued"] == 2
    assert delivery["terminal_receipts"] == 6
    assert delivery["pending"] == 0
    for proof in (
        "sender_restarted",
        "callback_worker_stopped",
        "source_bytes_preserved",
        "capture_instants_preserved",
        "transcript_file_changed",
    ):
        assert delivery[proof] is True
    assert delivery["helper_version"] == report["runtime_versions"]["sediment-cli"]
    batch = report["stages"]["transcript_batch"]
    assert batch["requests"] == 2
    assert batch["edit_observations"] == 32
    assert batch["pending"] == 0
    assert batch["source_changed"] is batch["transcript_removed"] is True
    assert batch["prepared_bytes_replayed"] is batch["fact_ids_retained"] is True
    assert batch["call_ids"] == [f"rehearsal-batch-{index:02d}" for index in range(32)]
    assert len(set(batch["fact_ids"])) == 32
    assert len(batch["request_sha256"]) == 2
    assert all(re.fullmatch("[0-9a-f]{64}", value) for value in batch["request_sha256"])
    assert delivery["callback_runtime"] == "synthetic CustomLogger import stand-in"
    assert delivery["configuration"]["max_active_bytes"] == 256 * 1024 * 1024
    assert delivery["configuration"]["replay_window_seconds"] == 24 * 60 * 60
    assert len(delivery["receipts"]) == 6
    assert report["stages"]["lost_acknowledgment"] == {
        "committed_before_retry": True,
        "retained_fact_id": True,
        "gateway_duplicates": 1,
        "inference_calls": 4,
    }
    for source in ("callback", "delivery_helper"):
        assert re.fullmatch("[0-9a-f]{64}", report["source_sha256"][source])
    assert report["stages"]["capture"]["gateway_ids"]
    assert report["stages"]["training"]["row_ids"]
    assert "installed_modules" in report, "full imported-module proof absent"
    assert len(report["installed_modules"]) > len(FIRST_PARTY)
    assert all(
        Path(path).is_relative_to(report["venv"])
        for path in report["installed_modules"].values()
    )


@pytest.mark.parametrize("interruption", ["timeout", "interrupt", "worker_exit"])
def test_installed_rehearsal_stops_owned_server_when_worker_stops(
    monkeypatch, interruption, postgres_admin_url
):
    import selectors
    import signal
    import time

    owned = {}
    real_popen = subprocess.Popen
    prelude = """
import httpx, subprocess, sys, threading
owned = {}
original_popen = subprocess.Popen
original_post = httpx.post
def track_server(args, *a, **kw):
    process = original_popen(args, *a, **kw)
    if len(args) > 1 and args[1] == "server":
        owned["server_pid"] = process.pid
    return process
def pause_capture(url, *a, **kw):
    response = original_post(url, *a, **kw)
    if url.endswith("/ingest/gateway"):
        stubborn = original_popen(
            [sys.executable, "-c",
             "import signal, threading; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "print('ready', flush=True); threading.Event().wait()"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        assert stubborn.stdout.readline() == "ready\\n"
        owned["stubborn_pid"] = stubborn.pid
        print(json.dumps(owned), flush=True)
        threading.Event().wait()
    return response
subprocess.Popen = track_server
httpx.post = pause_capture
"""

    def instrument_worker(args, *a, **kw):
        pipeline = "-I" in args and "-c" in args
        if pipeline:
            args = list(args)
            index = args.index("-c") + 1
            assert "exercise_installed_pipeline" in args[index]
            args[index] = args[index].replace("try:\n", prelude + "\ntry:\n", 1)
        process = real_popen(args, *a, **kw)
        if pipeline:
            communicate = process.communicate
            triggered = False

            def interrupt(input=None, timeout=None):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    with selectors.DefaultSelector() as ready:
                        ready.register(process.stdout, selectors.EVENT_READ)
                        assert ready.select(timeout=20), "installed server not ready"
                    receipt = json.loads(process.stdout.readline())
                    owned.update(receipt, worker_pid=process.pid)
                    assert running(owned["server_pid"]), (
                        "cleanup test did not reach a running installed server"
                    )
                    if interruption == "interrupt":
                        raise KeyboardInterrupt
                    if interruption == "worker_exit":
                        process.kill()
                    else:
                        # Expire the real deadline only after the server handshake.
                        return communicate(input=input, timeout=0)
                return communicate(input=input, timeout=timeout)

            process.communicate = interrupt
        return process

    def running(pid):
        status = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return bool(status) and not status.startswith("Z")

    peer = real_popen(
        [sys.executable, "-c", "import threading; threading.Event().wait()"],
        start_new_session=True,
    )
    monkeypatch.setattr(subprocess, "Popen", instrument_worker)
    try:
        try:
            errors = _load_rehearsal().rehearse(REPO_ROOT, postgres_admin_url)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            errors = [type(exc).__name__]
        assert owned, "worker never reached the installed server"
        deadline = time.monotonic() + 3
        while (
            any(running(pid) for pid in owned.values()) and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not any(running(pid) for pid in owned.values()), (
            "owned process survived worker cleanup"
        )
        assert peer.poll() is None, "cleanup stopped an unrelated process"
        reason = {
            "timeout": "installed pipeline timed out",
            "interrupt": "installed pipeline interrupted",
            "worker_exit": "installed pipeline produced no report",
        }[interruption]
        assert errors == [f"runtime rehearsal: {reason}"]
    finally:
        # Preserve ownership even while the regression is red.
        for pid in owned.values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        peer.terminate()
        peer.wait(timeout=5)
