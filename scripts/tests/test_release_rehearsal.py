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

# Installed rehearsals enforce wall-clock deadlines that parallel test load
# breaks, so CI runs `serial` tests alone after the parallel pass.
pytestmark = pytest.mark.serial

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
    assert callable(getattr(rehearsal, "validate_installed_evidence", None))
    assert callable(getattr(rehearsal, "build_and_install", None))
    assert callable(getattr(rehearsal, "exercise_installed_release", None))
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


@pytest.fixture(scope="module")
def installed_release(tmp_path_factory):
    """One validated build and installation for the installed failure paths.

    Failure tests only read the installed environment; each one gets its own
    workspace, home directory, and scratch database. CI's explicit
    `release_rehearsal.py` step remains the required complete build.
    """
    temp = tmp_path_factory.mktemp("installed-release")
    version = _project(REPO_ROOT / "pyproject.toml")["version"]
    venv, wheels, errors = _load_rehearsal().build_and_install(
        REPO_ROOT, temp / "dist", temp, version
    )
    assert errors == []
    return venv, wheels, version


def test_rehearsal_does_not_report_success_when_runtime_check_fails(
    installed_release, tmp_path
) -> None:
    venv, wheels, version = installed_release
    errors = _load_rehearsal().exercise_installed_release(
        venv,
        wheels,
        version,
        "postgresql+psycopg://postgres:postgres@127.0.0.1:9/postgres",
        tmp_path,
    )
    assert errors == ["runtime rehearsal: scratch database creation failed"]


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


def _installed_report(rehearsal, venv: Path) -> dict:
    """A report carrying every identity the installed evidence check requires."""
    from copy import deepcopy

    stages = deepcopy(rehearsal.PIPELINE_EXPECTATIONS)
    digest = "a" * 64
    stages["repository_identity"]["observed_repo_slugs"] = [
        "synthetic/rehearsal",
        "synthetic/renamed-rehearsal",
    ]
    for name in ("model_report", "lifecycle_report"):
        stages[name].update(
            scope={"org_id": "release-rehearsal"},
            sha256_before=digest,
            sha256_after=digest,
        )
    stages["delivery"].update(
        helper_version="0.1.0",
        callback_runtime="synthetic CustomLogger import stand-in",
        configuration={
            "max_active_bytes": 256 * 1024 * 1024,
            "replay_window_seconds": 24 * 60 * 60,
        },
        receipts=[{}] * 6,
    )
    stages["transcript_batch"].update(
        call_ids=[f"rehearsal-batch-{index:02d}" for index in range(32)],
        fact_ids=[f"fact-{index}" for index in range(32)],
        request_sha256=[digest, "b" * 64],
    )
    stages["capture"]["gateway_ids"] = ["call-1"]
    stages["training"]["row_ids"] = ["row-1"]
    return {
        "fixture_version": rehearsal.PIPELINE_FIXTURE_VERSION,
        "runtime_versions": dict.fromkeys(sorted(FIRST_PARTY), "0.1.0"),
        "installed_modules": {
            f"module_{index}": str(venv / "lib" / f"module_{index}.py")
            for index in range(len(FIRST_PARTY) + 1)
        },
        "venv": str(venv),
        "python_version": "3.12.14",
        "postgresql_version": "17.11",
        "entry_points": list(rehearsal.INSTALLED_ENTRY_POINTS),
        "source_sha256": {"callback": digest, "delivery_helper": digest},
        "stages": stages,
    }


def test_installed_evidence_accepts_a_complete_installed_report(tmp_path):
    rehearsal = _load_rehearsal()
    report = _installed_report(rehearsal, tmp_path.resolve())
    assert rehearsal.validate_pipeline_report(report) == []
    assert (
        rehearsal.validate_installed_evidence(
            report, venv=tmp_path, release_version="0.1.0", server_version="17.11"
        )
        == []
    )


@pytest.mark.parametrize(
    ("label", "path", "value"),
    [
        ("runtime versions", ("runtime_versions", "sediment-core"), "9.9.9"),
        ("installed modules", ("installed_modules", "module_0"), "/checkout/x.py"),
        ("installed modules", ("venv",), "/elsewhere"),
        ("Python version", ("python_version",), "3.13.0"),
        ("PostgreSQL version", ("postgresql_version",), "16.4"),
        ("entry points", ("entry_points",), ["sediment server"]),
        ("source hashes", ("source_sha256", "callback"), "not-a-digest"),
        ("authority", ("stages", "authority", "extra"), True),
        ("lost acknowledgment", ("stages", "lost_acknowledgment", "extra"), 1),
        (
            "repository slugs",
            ("stages", "repository_identity", "observed_repo_slugs"),
            ["synthetic/rehearsal"],
        ),
        ("operational report bytes", ("stages", "model_report", "sha256_after"), ""),
        ("operational report bytes", ("stages", "lifecycle_report", "scope"), {}),
        ("delivery receipts", ("stages", "delivery", "helper_version"), "9.9.9"),
        ("delivery receipts", ("stages", "delivery", "receipts"), []),
        (
            "transcript batch identities",
            ("stages", "transcript_batch", "fact_ids"),
            ["duplicate"] * 32,
        ),
        ("row identities", ("stages", "training", "row_ids"), []),
        ("row identities", ("stages", "capture"), None),
    ],
)
def test_installed_evidence_names_each_absent_or_contradictory_identity(
    tmp_path, label, path, value
):
    rehearsal = _load_rehearsal()
    report = _installed_report(rehearsal, tmp_path.resolve())
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert rehearsal.validate_installed_evidence(
        report, venv=tmp_path, release_version="0.1.0", server_version="17.11"
    ) == [f"runtime rehearsal: {label} evidence absent or contradictory"]


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Linux CI runs this complete rehearsal once as its own required step",
)
def test_complete_rehearsal_finds_homebrew_libpq_without_pg_config(
    capsys, postgres_admin_url, monkeypatch
):
    # Homebrew's keg-only libpq stays off PATH. This complete run proves the
    # installed runtime still finds it; `validate_installed_evidence` owns
    # the report assertions that every platform's rehearsal enforces.
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            entry
            for entry in os.environ["PATH"].split(os.pathsep)
            if not (Path(entry) / "pg_config").exists()
        ),
    )
    assert _load_rehearsal().rehearse(REPO_ROOT, postgres_admin_url) == []
    reports = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("pipeline acceptance: ")
    ]
    assert len(reports) == 1, "installed pipeline acceptance never ran"


# A controlled worker owns the same process shapes as the installed pipeline:
# a server child outside the worker's output pipes and a descendant that
# ignores SIGTERM. It reports both before the parent interrupts it.
_WORKER_TREE = """
import json, subprocess, sys, threading
server = subprocess.Popen(
    [sys.executable, "-c", "import threading; threading.Event().wait()"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
stubborn = subprocess.Popen(
    [sys.executable, "-c",
     "import signal, threading; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
     "print('ready', flush=True); threading.Event().wait()"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
)
assert stubborn.stdout.readline() == "ready\\n"
print(json.dumps({"server_pid": server.pid, "stubborn_pid": stubborn.pid}), flush=True)
threading.Event().wait()
"""

# Injected into the real installed worker: record the installed server and add
# the same SIGTERM-ignoring descendant at the first gateway capture.
_INSTALLED_PRELUDE = """
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

_INTERRUPTION_ERRORS = {
    "timeout": "runtime rehearsal: installed pipeline timed out",
    "interrupt": "runtime rehearsal: installed pipeline interrupted",
    "worker_exit": "runtime rehearsal: installed pipeline produced no report",
}


def _running(pid: int) -> bool:
    status = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(status) and not status.startswith("Z")


def _interrupt_worker(monkeypatch, interruption: str, owned: dict, prelude=None):
    """Interrupt the bounded worker once it reports the processes it owns."""
    import selectors

    real_popen = subprocess.Popen

    def instrument_worker(args, *a, **kw):
        # Match the worker by its command, then require the process group
        # cleanup depends on; a missing group fails here without spawning.
        worker = "-c" in args
        if worker:
            assert kw.get("start_new_session") is True, "worker needs its own group"
        if worker and prelude is not None:
            args = list(args)
            index = args.index("-c") + 1
            assert "exercise_installed_pipeline" in args[index]
            args[index] = args[index].replace("try:\n", prelude + "\ntry:\n", 1)
        process = real_popen(args, *a, **kw)
        if not worker:
            return process
        communicate = process.communicate
        triggered = False

        def interrupt(input=None, timeout=None):
            nonlocal triggered
            if not triggered:
                triggered = True
                with selectors.DefaultSelector() as ready:
                    ready.register(process.stdout, selectors.EVENT_READ)
                    assert ready.select(timeout=20), "worker never reported"
                receipt = json.loads(process.stdout.readline())
                assert set(receipt) == {"server_pid", "stubborn_pid"}, receipt
                owned.update(receipt, worker_pid=process.pid)
                assert _running(owned["server_pid"]), "server was not running"
                if interruption == "interrupt":
                    raise KeyboardInterrupt
                if interruption == "worker_exit":
                    process.kill()
                else:
                    # Expire the real deadline only after the handshake.
                    return communicate(input=input, timeout=0)
            return communicate(input=input, timeout=timeout)

        process.communicate = interrupt
        return process

    monkeypatch.setattr(subprocess, "Popen", instrument_worker)


def _assert_owned_processes_stopped(owned: dict, peer) -> None:
    import time

    assert owned, "worker never reported its processes"
    deadline = time.monotonic() + 3
    while any(_running(pid) for pid in owned.values()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(_running(pid) for pid in owned.values()), (
        "owned process survived worker cleanup"
    )
    assert peer.poll() is None, "cleanup stopped an unrelated process"


def _stop(owned: dict, peer) -> None:
    import signal

    # Preserve ownership even while a regression is red.
    for pid in owned.values():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    peer.terminate()
    peer.wait(timeout=5)


@pytest.mark.parametrize("interruption", ["timeout", "interrupt", "worker_exit"])
def test_worker_cleanup_stops_its_process_group_and_reports_the_cause(
    monkeypatch, tmp_path, interruption
):
    owned = {}
    peer = subprocess.Popen(
        [sys.executable, "-c", "import threading; threading.Event().wait()"],
        start_new_session=True,
    )
    _interrupt_worker(monkeypatch, interruption, owned)
    try:
        try:
            _, errors = _load_rehearsal()._run_installed_pipeline(
                [sys.executable, "-c", _WORKER_TREE],
                cwd=tmp_path,
                env=dict(os.environ),
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            errors = [type(exc).__name__]
        _assert_owned_processes_stopped(owned, peer)
        assert errors == [_INTERRUPTION_ERRORS[interruption]]
    finally:
        _stop(owned, peer)


def test_installed_rehearsal_stops_owned_server_when_worker_times_out(
    monkeypatch, installed_release, postgres_admin_url, tmp_path
):
    # The installed pipeline must start its server inside the worker's
    # process group; a detached server would survive the parent's cleanup.
    venv, wheels, version = installed_release
    owned = {}
    peer = subprocess.Popen(
        [sys.executable, "-c", "import threading; threading.Event().wait()"],
        start_new_session=True,
    )
    _interrupt_worker(monkeypatch, "timeout", owned, prelude=_INSTALLED_PRELUDE)
    try:
        try:
            errors = _load_rehearsal().exercise_installed_release(
                venv, wheels, version, postgres_admin_url, tmp_path
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            errors = [type(exc).__name__]
        _assert_owned_processes_stopped(owned, peer)
        assert errors == [_INTERRUPTION_ERRORS["timeout"]]
    finally:
        _stop(owned, peer)
