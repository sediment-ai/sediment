# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise scanner failure and retained-inventory boundaries without a network."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(
            "security_scan", ROOT / "scripts/security_scan.py"
        )
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded
    finally:
        sys.path.pop(0)


def test_scanner_failure_never_becomes_a_clean_report(tmp_path):
    scan = module()
    with pytest.raises(scan.ScanFailure, match="exit 7"):
        scan.run([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path)
    with pytest.raises(scan.ScanFailure):
        scan.run([str(tmp_path / "missing-scanner")], cwd=tmp_path)


def test_python_inventory_uses_actual_installation_and_refuses_missing_first_party():
    scan = module()
    components = [{"ecosystem": "pypi", "name": "httpx", "version": "0.28.1"}]
    with pytest.raises(scan.ScanFailure, match="first-party"):
        scan.require_components(components, "client")
    for name in scan.FIRST_PARTY:
        components.append({"ecosystem": "pypi", "name": name, "version": "0.1.0"})
    scan.require_components(components, "client")


def test_retained_rescan_does_not_build_or_execute_an_old_artifact(
    tmp_path, monkeypatch
):
    import hashlib
    import json

    scan = module()
    sbom = tmp_path / "api-arm64.cdx.json"
    sbom.write_text('{"bomFormat":"CycloneDX","specVersion":"1.6"}')
    manifest = {
        "schema_version": 1,
        "artifact": "api",
        "architecture": "arm64",
        "sbom": sbom.name,
        "files": {sbom.name: hashlib.sha256(sbom.read_bytes()).hexdigest()},
        "components": [],
        "runtimes": {},
        "support": {"ends_on": "2026-12-11"},
    }
    source = tmp_path / "api-arm64.inventory.json"
    source.write_text(json.dumps(manifest))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text('{"SchemaVersion":2}')
        return ""

    monkeypatch.setattr(scan, "run", run)
    target = tmp_path / "rescans"
    scan.rescan_inventory(source, target, "trivy")
    assert len(commands) == 1
    assert commands[0][1] == "sbom"
    assert str(sbom) in commands[0]
    assert not any("docker" in word or "build" == word for word in commands[0])
    sbom.write_text("tampered")
    with pytest.raises(scan.ScanFailure, match="integrity"):
        scan.rescan_inventory(source, target, "trivy")


def test_rescan_inventory_refuses_a_traversal_sbom_path(tmp_path, monkeypatch):
    """review finding 12: ``inventory["sbom"]`` names a plain filename, not a
    path — reuse security_policy.check_bundled_libraries's guard."""
    import hashlib
    import json

    scan = module()
    outside = tmp_path / "outside.cdx.json"
    outside.write_text('{"bomFormat":"CycloneDX","specVersion":"1.6"}')
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    real_sbom = release_dir / "api-amd64.cdx.json"
    real_sbom.write_text(outside.read_text())
    manifest = {
        "schema_version": 1,
        "artifact": "api",
        "architecture": "amd64",
        "sbom": "../outside.cdx.json",
        "files": {real_sbom.name: hashlib.sha256(real_sbom.read_bytes()).hexdigest()},
        "components": [],
        "runtimes": {},
        "support": {"ends_on": "2026-12-11"},
    }
    source = release_dir / "api-amd64.inventory.json"
    source.write_text(json.dumps(manifest))

    def run(command, **kwargs):
        raise AssertionError("must not scan before the SBOM binding is verified")

    monkeypatch.setattr(scan, "run", run)
    with pytest.raises(scan.ScanFailure, match="not bound"):
        scan.rescan_inventory(source, tmp_path / "rescans", "trivy")


def test_resolved_audit_requires_every_observed_dependency():
    scan = module()
    observed = [
        {"ecosystem": "pypi", "name": "httpx", "version": "0.28.1"},
        {"ecosystem": "pypi", "name": "anyio", "version": "4.12.1"},
    ]
    clean = {
        "dependencies": [
            {"name": p["name"], "version": p["version"], "vulns": []} for p in observed
        ]
    }
    scan.require_python_audit(clean, observed)
    for broken in [
        {"dependencies": clean["dependencies"][:1]},
        {"dependencies": [dict(d, version="0.0") for d in clean["dependencies"]]},
        {
            "dependencies": [
                dict(d, skip_reason="not found") for d in clean["dependencies"]
            ]
        },
        {
            "dependencies": [
                dict(d, vulns=[{"id": "CVE-example"}]) for d in clean["dependencies"]
            ]
        },
    ]:
        with pytest.raises(scan.ScanFailure):
            scan.require_python_audit(broken, observed)


def test_image_package_names_use_registry_canonical_spelling():
    scan = module()
    result = scan.components_from_trivy(
        {
            "Results": [
                {
                    "Type": "python-pkg",
                    "Packages": [
                        {"Name": "MarkupSafe", "Version": "3.0.3"},
                        {"Name": "google_crc32c", "Version": "1.8.0"},
                    ],
                }
            ]
        }
    )
    assert {p["name"] for p in result} == {"markupsafe", "google-crc32c"}


def test_debian_inventory_keeps_epoch_and_vendor_security_revision():
    scan = module()
    result = scan.components_from_trivy(
        {
            "Results": [
                {
                    "Type": "debian",
                    "Packages": [
                        {
                            "ID": "libssl3@3.0.20-1~deb12u2",
                            "Name": "libssl3",
                            "Version": "3.0.20",
                            "Release": "1~deb12u2",
                        },
                        {
                            "ID": "libllvm19@1:19.1.7-3~deb12u1",
                            "Name": "libllvm19",
                            "Epoch": 1,
                            "Version": "19.1.7",
                            "Release": "3~deb12u1",
                        },
                    ],
                }
            ]
        }
    )
    assert {p["version"] for p in result} == {"3.0.20-1~deb12u2", "1:19.1.7-3~deb12u1"}


def test_runtime_patch_check_uses_upstream_release_tags(monkeypatch, tmp_path):
    scan = module()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return '[{"ref":"refs/tags/v3.12.14"},{"ref":"refs/tags/v3.12.15rc1"}]'

    monkeypatch.setattr(scan, "run", run)
    assert (
        scan.live_runtime_maintenance({"python": "3.12.14"}, tmp_path / "runtime.json")
        == []
    )
    assert scan.live_runtime_maintenance(
        {"python": "3.12.13"}, tmp_path / "runtime.json"
    )
    assert commands[0][1:3] == [
        "api",
        "repos/python/cpython/git/matching-refs/tags/v3.12.",
    ]
    monkeypatch.setattr(scan, "run", lambda *a, **kw: "[]")
    assert scan.live_runtime_maintenance(
        {"python": "3.12.14"}, tmp_path / "runtime.json"
    )


def test_evaluation_rejects_missing_required_scanner_evidence(tmp_path):
    import hashlib

    scan = module()
    sbom = tmp_path / "api.cdx.json"
    sbom.write_text("{}")
    manifest = {
        "schema_version": 1,
        "artifact": "api",
        "architecture": "arm64",
        "files": {sbom.name: hashlib.sha256(sbom.read_bytes()).hexdigest()},
        "components": [{"ecosystem": "pypi", "name": "example", "version": "1.0"}],
        "runtimes": {"python": "3.12.14"},
        "support": {"ends_on": "2099-01-01"},
    }
    catalog = {
        "schema_version": 1,
        "owner": "Maintainer",
        "reviewed_on": "2026-09-12",
        "expires_on": "2026-10-12",
        "packages": {},
        "runtimes": {},
    }
    errors = scan.evaluate(manifest, tmp_path, tmp_path, catalog, [], online=False)
    assert any("required scanner" in error for error in errors)


def test_production_npm_inventory_handles_nested_versions_and_missing_data():
    scan = module()
    tree = {
        "name": "sediment-pi",
        "version": "0.1.0",
        "dependencies": {
            "@scope/example": {
                "version": "1.0.0",
                "dependencies": {"other": {"version": "2.0.0"}},
            },
            "other": {"version": "3.0.0"},
        },
    }
    assert {(p["name"], p["version"]) for p in scan.npm_components(tree)} == {
        ("@scope/example", "1.0.0"),
        ("other", "2.0.0"),
        ("other", "3.0.0"),
    }
    assert scan.npm_components({"name": "sediment-pi", "version": "0.1.0"}) == []
    with pytest.raises(scan.ScanFailure):
        scan.npm_components({"dependencies": {"missing": {"missing": True}}})


def test_image_source_binding_rejects_stale_or_unverified_images():
    scan = module()
    labels = {
        "org.opencontainers.image.revision": "a" * 40,
        "io.sediment.source-digest": "b" * 64,
    }
    scan.require_image_source(labels, "a" * 40, "b" * 64)
    for bad in (
        {},
        dict(labels, **{"io.sediment.source-digest": "unverified"}),
        dict(labels, **{"org.opencontainers.image.revision": "c" * 40}),
    ):
        with pytest.raises(scan.ScanFailure, match="source"):
            scan.require_image_source(bad, "a" * 40, "b" * 64)


def test_scan_identity_rejects_another_image():
    scan = module()
    scan.require_scan_image({"Metadata": {"ImageID": "sha256:a"}}, "sha256:a")
    for report in ({}, {"Metadata": {"ImageID": "sha256:b"}}):
        with pytest.raises(scan.ScanFailure, match="image"):
            scan.require_scan_image(report, "sha256:a")


def test_postgres_runtime_version_handles_vendor_suffix_without_inventing_a_version():
    scan = module()
    assert (
        scan.postgres_version("postgres (PostgreSQL) 17.11 (Debian 17.11-1.pgdg12+2)\n")
        == "17.11"
    )
    assert scan.postgres_version("postgres (PostgreSQL) 17.11\n") == "17.11"
    for invalid in ("", "postgres (PostgreSQL) unknown", "unrelated 17.11"):
        with pytest.raises(scan.ScanFailure):
            scan.postgres_version(invalid)


def test_image_scan_must_cover_each_executed_python_distribution():
    scan = module()
    observed = [
        {"ecosystem": "pypi", "name": "httpx", "version": "0.28.1"},
        {"ecosystem": "pypi", "name": "numpy", "version": "2.5.3"},
    ]
    scan.require_python_scan_coverage(observed, observed)
    with pytest.raises(scan.ScanFailure, match="Python"):
        scan.require_python_scan_coverage(observed, observed[:1])
    with pytest.raises(scan.ScanFailure, match="Python"):
        scan.require_python_scan_coverage(
            observed, [dict(p, version="0.0") for p in observed]
        )
