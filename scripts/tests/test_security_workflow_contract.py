# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security checks must cover release artifacts before either publisher runs."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_security_covers_every_architecture_and_release_wheel_handoff():
    security = (ROOT / ".github/workflows/security.yml").read_text()
    assert "workflow_call:" in security
    assert "workflow_dispatch:" in security
    assert "pull_request:" in security
    assert "branches: [main]" in security
    assert "artifact: [api, postgres, gateway]" in security
    assert "architecture: [amd64, arm64]" in security
    assert "ubuntu-24.04-arm" in security
    assert "inputs.wheels-artifact" in security
    assert "--wheels dist" in security
    assert "if: inputs.wheels-artifact == ''" in security
    assert "--metrics off" in (ROOT / "scripts/security_static.py").read_text()
    assert "continue-on-error" not in security
    assert "docker push" not in security


def test_both_publishers_depend_on_security_and_attach_all_evidence():
    release = (ROOT / ".github/workflows/release.yaml").read_text()
    assert "uses: ./.github/workflows/security.yml" in release
    assert "wheels-artifact: python-distributions" in release
    assert "needs: [build, security, prepare-release]" in release
    assert "needs: [build, security, prepare-release, publish-pypi]" in release
    assert "pattern: security-evidence-*" in release
    assert "scripts/rescan_releases.py bundle" in release
    assert "assets=(dist/*)" in release
    assert "releases remain disabled while the repository is private" in release
    assert "name: pypi" in release


def test_database_jobs_run_derived_postgres_and_cleanup():
    for filename in ("ci.yml", "release.yaml"):
        workflow = (ROOT / ".github/workflows" / filename).read_text()
        assert "docker build --pull -f docker/postgres/Dockerfile" in workflow
        assert "image: postgres:" not in workflow
        assert "127.0.0.1:5432:5432" in workflow
        # The entrypoint's temporary Unix-socket-only init server is not ready
        # for the host-side migration/rehearsal connection.
        assert "pg_isready -h 127.0.0.1 -U postgres -d postgres" in workflow
        assert "docker rm -f sediment-ci-postgres" in workflow


def test_daily_rescan_and_all_dependency_ecosystems_are_enabled():
    rescan = (ROOT / ".github/workflows/security-rescan.yml").read_text()
    assert "schedule:" in rescan
    assert "workflow_dispatch:" in rescan
    assert "scripts/rescan_releases.py scan" in rescan
    assert "contents: read" in rescan
    dependabot = (ROOT / ".github/dependabot.yml").read_text()
    for ecosystem in ("uv", "npm", "docker", "github-actions"):
        assert f"package-ecosystem: {ecosystem}" in dependabot
    for directory in ("/docker/postgres", "/docker/gateway", "/shims/pi"):
        assert directory in dependabot
    assert "interval: weekly" not in dependabot


def test_every_scanned_dockerfile_and_dependabot_docker_directory_exists():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text())
    for entry in workflow["jobs"]["images"]["strategy"]["matrix"]["include"]:
        if "dockerfile" in entry:
            assert (ROOT / entry["dockerfile"]).is_file(), entry
    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    for update in dependabot["updates"]:
        if update["package-ecosystem"] == "docker":
            directories = update.get("directories", [update.get("directory")])
            for directory in directories:
                assert (ROOT / directory.lstrip("/") / "Dockerfile").is_file(), update


def test_scanned_gateway_images_run_the_runtime_regressions():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text())
    steps = workflow["jobs"]["images"]["steps"]
    runtime_checks = [
        step for step in steps if "test_container_images.py" in step.get("run", "")
    ]
    assert len(runtime_checks) == 1
    check = runtime_checks[0]
    assert check["if"] == "matrix.artifact == 'gateway'"
    assert check["env"]["SEDIMENT_TEST_GATEWAY_IMAGE"] == (
        "sediment-security:gateway-${{ matrix.architecture }}"
    )
    assert "-k gateway" in check["run"]
    assert not check.get("continue-on-error", False)
    assert steps.index(check) > next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Build and scan the exact final image"
    )
