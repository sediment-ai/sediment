# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retained releases are untrusted data; incomplete evidence cannot pass."""

import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "rescan_releases", ROOT / "scripts/rescan_releases.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def evidence(directory):
    wheel_hashes = {}
    for package in ("api", "capture", "cli", "core", "derive", "export"):
        wheel = directory / f"sediment_{package}-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"synthetic immutable wheel bytes")
        wheel_hashes[wheel.name] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (directory / "release-wheel-hashes.json").write_text(json.dumps(wheel_hashes))
    static = directory / "source.semgrep.json"
    static.write_text(
        json.dumps({"results": [], "errors": [], "paths": {"scanned": ["api.py"]}})
    )
    (directory / "source.static-metadata.json").write_text(
        json.dumps(
            {
                "scanner": "semgrep",
                "version": "1.177.0",
                "report_sha256": hashlib.sha256(static.read_bytes()).hexdigest(),
            }
        )
    )
    for artifact, architecture in [
        *(
            (a, arch)
            for a in ("api", "postgres", "gateway")
            for arch in ("amd64", "arm64")
        ),
        ("client", "x86_64"),
        ("pi", "x86_64"),
    ]:
        name = f"{artifact}-{architecture}"
        sbom = directory / f"{name}.cdx.json"
        sbom.write_text('{"bomFormat":"CycloneDX"}')
        (directory / f"{name}.inventory.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "artifact": artifact,
                    "architecture": architecture,
                    "source_commit": "a" * 40,
                    "support": {"starts_on": "2026-09-12", "ends_on": "2026-12-11"},
                    "sbom": sbom.name,
                    "files": {sbom.name: hashlib.sha256(sbom.read_bytes()).hexdigest()},
                }
            )
        )
        (directory / f"{name}.gate.json").write_text('{"errors":[]}')


def test_bundle_requires_all_eight_artifact_inventories(tmp_path):
    script = module()
    evidence(tmp_path)
    script.bundle(tmp_path, "v0.1.0", "a" * 40, today=date(2026, 9, 12))
    metadata = json.loads((tmp_path / "security-support.json").read_text())
    assert len(metadata["inventories"]) == 8
    assert metadata["support"]["ends_on"] == "2026-12-11"
    assert "security_update_target_days" not in metadata["support"]
    assert "security-support.json" in (tmp_path / "SHA256SUMS").read_text()
    script.verify_release(tmp_path, "v0.1.0", today=date(2026, 9, 12))
    (tmp_path / "gateway-arm64.inventory.json").unlink()
    with pytest.raises(script.ReleaseFailure):
        script.bundle(tmp_path, "v0.1.0", "a" * 40, today=date(2026, 9, 12))


@pytest.mark.parametrize(
    "change", ["tamper", "missing", "path", "duplicate", "unhashed"]
)
def test_retained_checksum_failure_precedes_scanning(tmp_path, change):
    script = module()
    evidence(tmp_path)
    script.bundle(tmp_path, "v0.1.0", "a" * 40, today=date(2026, 9, 12))
    sums = tmp_path / "SHA256SUMS"
    target = tmp_path / "api-amd64.cdx.json"
    if change == "tamper":
        target.write_text("changed")
    elif change == "missing":
        target.unlink()
    elif change == "path":
        sums.write_text(sums.read_text() + "0" * 64 + "  ../outside\n")
    elif change == "duplicate":
        sums.write_text(sums.read_text() + sums.read_text().splitlines()[0] + "\n")
    else:
        sums.write_text(
            "\n".join(
                line
                for line in sums.read_text().splitlines()
                if not line.endswith("security-support.json")
            )
        )
    with pytest.raises(script.ReleaseFailure):
        script.verify_release(tmp_path, "v0.1.0", today=date(2026, 9, 12))


def test_failed_or_wrong_commit_gate_cannot_be_bundled(tmp_path):
    script = module()
    evidence(tmp_path)
    (tmp_path / "pi-x86_64.gate.json").write_text('{"errors":["unmaintained"]}')
    with pytest.raises(script.ReleaseFailure):
        script.bundle(tmp_path, "v0.1.0", "a" * 40, today=date(2026, 9, 12))
    (tmp_path / "pi-x86_64.gate.json").write_text('{"errors":[]}')
    with pytest.raises(script.ReleaseFailure):
        script.bundle(tmp_path, "v0.1.0", "b" * 40, today=date(2026, 9, 12))


@pytest.mark.parametrize(
    "change", ["different-wheel", "missing-wheel", "static-findings", "missing-static"]
)
def test_bundle_binds_published_wheels_and_requires_static_success(tmp_path, change):
    script = module()
    evidence(tmp_path)
    if change == "different-wheel":
        next(tmp_path.glob("*.whl")).write_bytes(b"rebuilt wheel")
    elif change == "missing-wheel":
        next(tmp_path.glob("*.whl")).unlink()
    elif change == "static-findings":
        (tmp_path / "source.semgrep.json").write_text('{"results":["unsafe"]}')
    else:
        (tmp_path / "source.semgrep.json").unlink()
    with pytest.raises(script.ReleaseFailure):
        script.bundle(tmp_path, "v0.1.0", "a" * 40, today=date(2026, 9, 12))


def test_daily_rescan_lists_expired_and_fails_missing_supported_release(
    tmp_path, monkeypatch
):
    script = module()
    releases = [
        {
            "id": 1,
            "tag_name": "v0.0.1",
            "draft": False,
            "published_at": "2026-01-01T00:00:00Z",
            "assets": [],
        },
        {
            "id": 2,
            "tag_name": "v0.1.0",
            "draft": False,
            "published_at": "2026-09-10T00:00:00Z",
            "assets": [],
        },
    ]
    monkeypatch.setattr(script, "list_releases", lambda repository: releases)
    assert (
        script.scan("sediment-ai/sediment", tmp_path, "trivy", today=date(2026, 9, 12))
        == 1
    )
    report = json.loads((tmp_path / "retained-releases.json").read_text())
    assert report["outside_support"][0]["tag"] == "v0.0.1"
    assert report["failures"][0]["tag"] == "v0.1.0"


def test_daily_rescan_uses_only_verified_inventories_and_reports_scanner_failure(
    tmp_path, monkeypatch
):
    script = module()
    releases = [
        {
            "id": 2,
            "tag_name": "v0.1.0",
            "draft": False,
            "published_at": "2026-09-10T00:00:00Z",
            "assets": [],
        }
    ]
    monkeypatch.setattr(script, "list_releases", lambda repository: releases)

    def download(repository, release, directory):
        evidence(directory)
        script.bundle(directory, "v0.1.0", "a" * 40, today=date(2026, 9, 12))

    monkeypatch.setattr(script, "download_release", download)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise script.ReleaseFailure("scanner failed")

    monkeypatch.setattr(script, "run", run)
    assert (
        script.scan("sediment-ai/sediment", tmp_path, "trivy", today=date(2026, 9, 12))
        == 1
    )
    assert calls and all("rescan" in command for command in calls)
    assert all("--inventory" in command for command in calls)
    assert not any("checkout" in command or "docker" in command for command in calls)


@pytest.mark.parametrize("mutation", ["path", "duplicate", "oversize", "invalid-id"])
def test_download_rejects_untrusted_asset_metadata_before_execution(
    tmp_path, monkeypatch, mutation
):
    script = module()
    assets = [
        {"name": name, "id": index, "size": 1}
        for index, name in enumerate(("SHA256SUMS", "security-support.json"), 1)
    ]
    if mutation == "path":
        assets.append({"name": "../unsafe", "id": 3, "size": 1})
    elif mutation == "duplicate":
        assets.append(assets[0])
    elif mutation == "oversize":
        assets[0]["size"] = script.MAX_ASSET_BYTES + 1
    else:
        assets[0]["id"] = "1; touch unsafe"
    commands = []
    monkeypatch.setattr(
        script, "run", lambda command, **kwargs: commands.append(command)
    )
    with pytest.raises(script.ReleaseFailure):
        script.download_release("sediment-ai/sediment", {"assets": assets}, tmp_path)
    assert commands == []


def test_downloads_only_asset_bytes_and_verifies_declared_size(tmp_path, monkeypatch):
    script = module()
    assets = [
        {"name": name, "id": index, "size": 4}
        for index, name in enumerate(("SHA256SUMS", "security-support.json"), 1)
    ]
    commands = []

    def run(command, *, stdout):
        commands.append(command)
        stdout.write(b"data")

    monkeypatch.setattr(script, "run", run)
    script.download_release("sediment-ai/sediment", {"assets": assets}, tmp_path)
    assert len(commands) == 2
    assert all(command[:2] == ["gh", "api"] for command in commands)
    assert all("Accept: application/octet-stream" in command for command in commands)
    assert {path.read_bytes() for path in tmp_path.iterdir()} == {b"data"}
