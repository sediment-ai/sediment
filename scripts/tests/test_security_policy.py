# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security gates reject incomplete evidence as well as known bad components."""

import copy
import importlib.util
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def policy_module():
    spec = importlib.util.spec_from_file_location(
        "security_policy", ROOT / "scripts/security_policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scan():
    return {
        "SchemaVersion": 2,
        "CreatedAt": "2026-09-12T08:00:00Z",
        "Metadata": {"OS": {"Family": "debian", "Name": "12", "EOSL": False}},
        "Results": [
            {
                "Target": "image",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [{"Name": "example", "Version": "1.0"}],
                "Vulnerabilities": [],
            }
        ],
    }


@pytest.fixture
def finding():
    return {
        "VulnerabilityID": "CVE-2026-1234",
        "PkgName": "example",
        "InstalledVersion": "1.0",
        "Severity": "HIGH",
        "FixedVersion": "",
    }


def test_clean_complete_scan_passes(scan):
    assert (
        policy_module().check_vulnerabilities(
            scan, "api", "arm64", [], date(2026, 9, 12)
        )
        == []
    )


@pytest.mark.parametrize("severity", ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
def test_every_fixable_finding_blocks(scan, finding, severity):
    finding.update(Severity=severity, FixedVersion="1.1")
    scan["Results"][0]["Vulnerabilities"] = [finding]
    assert "fixable" in " ".join(
        policy_module().check_vulnerabilities(
            scan, "api", "arm64", [], date(2026, 9, 12)
        )
    )


def test_unfixed_high_requires_exact_unexpired_disposition(scan, finding):
    scan["Results"][0]["Vulnerabilities"] = [finding]
    disposition = {
        "targets": ["api/arm64/example@1.0"],
        "source_digest": "a" * 64,
        "required_predicates": ["minizip_absent"],
        "id": "CVE-2026-1234",
        "status": "not_affected",
        "owner": "Sediment maintainers",
        "reviewed_on": "2026-09-12",
        "expires_on": "2026-10-12",
        "reason": "Affected binary is absent",
        "evidence": ["https://vendor.example/advisory"],
    }
    check = policy_module().check_vulnerabilities
    assurance = {"source_digest": "a" * 64, "predicates": {"minizip_absent": True}}
    assert (
        check(
            scan, "api", "arm64", [disposition], date(2026, 9, 12), assurance=assurance
        )
        == []
    )
    assert check(scan, "api", "arm64", [disposition], date(2026, 9, 12))
    assert check(
        scan,
        "api",
        "arm64",
        [disposition],
        date(2026, 9, 12),
        assurance={**assurance, "predicates": {"minizip_absent": False}},
    )
    for key, value in [
        ("targets", ["api/amd64/example@1.0"]),
        ("targets", ["api/arm64/example@2.0"]),
        ("source_digest", "b" * 64),
        ("required_predicates", []),
        ("required_predicates", ["unchecked"]),
        ("expires_on", "2026-09-11"),
        ("expires_on", "2026-10-13"),
        ("owner", ""),
        ("evidence", []),
        ("status", "ignored"),
    ]:
        invalid = {**disposition, key: value}
        assert check(
            scan, "api", "arm64", [invalid], date(2026, 9, 12), assurance=assurance
        ), key


@pytest.mark.parametrize(
    "mutation", ["missing", "empty", "stale", "future", "eol", "packages"]
)
def test_incomplete_stale_or_unsupported_scan_fails(scan, mutation):
    if mutation == "missing":
        del scan["Results"]
    elif mutation == "empty":
        scan["Results"] = []
    elif mutation == "stale":
        scan["CreatedAt"] = "2026-09-09T08:00:00Z"
    elif mutation == "future":
        scan["CreatedAt"] = "2026-09-13T08:00:00Z"
    elif mutation == "eol":
        scan["Metadata"]["OS"]["EOSL"] = True
    else:
        del scan["Results"][0]["Packages"]
    assert policy_module().check_vulnerabilities(
        scan, "api", "arm64", [], date(2026, 9, 12)
    )


def test_source_review_preflight_requires_matching_unexpired_reviews():
    check = getattr(policy_module(), "check_source_reviews", None)
    assert callable(check), "Fail stale source reviews before building images"
    review = {
        "id": "CVE-2026-1234",
        "source_digest": "a" * 64,
        "owner": "Sediment maintainers",
        "reviewed_on": "2026-09-12",
        "expires_on": "2026-10-12",
    }
    assert check([review], "a" * 64, date(2026, 9, 12)) == []
    for changed in (
        {"source_digest": "b" * 64},
        {"expires_on": "2026-09-11"},
        {"reviewed_on": "2026-09-13"},
        {"expires_on": "2026-10-13"},
        {"owner": ""},
    ):
        errors = check([{**review, **changed}], "a" * 64, date(2026, 9, 12))
        assert len(errors) == 1
        assert "CVE-2026-1234" in errors[0]
    assert check([{}], "a" * 64, date(2026, 9, 12))


def catalog():
    return {
        "schema_version": 1,
        "owner": "Sediment maintainers",
        "reviewed_on": "2026-09-12",
        "expires_on": "2026-10-12",
        "packages": {
            "pypi/example": {
                "versions": ["1.0"],
                "status": "maintained",
                "source": "https://github.com/example/example",
                "evidence": "https://github.com/example/example/releases",
            }
        },
        "runtimes": {
            "python": {"3.12": {"minimum": "3.12.14", "end_of_support": "2028-10-31"}}
        },
    }


def test_maintenance_tracks_exact_components_and_runtime_independently():
    check = policy_module().check_maintenance
    components = [{"ecosystem": "pypi", "name": "example", "version": "1.0"}]
    runtime = {"python": "3.12.14"}
    assert check(components, runtime, catalog(), date(2026, 9, 12)) == []
    for field, value in [("name", "unreviewed"), ("version", "1.1")]:
        assert check(
            [{**components[0], field: value}], runtime, catalog(), date(2026, 9, 12)
        )
    assert check(components, {"python": "3.9.6"}, catalog(), date(2026, 9, 12))
    assert check(components, {"python": "3.12.13"}, catalog(), date(2026, 9, 12))
    assert check(components, {}, catalog(), date(2026, 9, 12))
    assert check(components, runtime, catalog(), date(2026, 10, 13))
    for change in [{"status": "discontinued"}, {"source": ""}, {"evidence": ""}]:
        altered = copy.deepcopy(catalog())
        altered["packages"]["pypi/example"].update(change)
        assert check(components, runtime, altered, date(2026, 9, 12))


def test_registry_and_repository_errors_are_not_maintenance_approval():
    check = policy_module().check_upstream
    approved = {"source": "https://github.com/example/example"}
    package = {"yanked": False}
    repository = {"archived": False, "disabled": False}
    assert check(approved, package, repository) == []
    for invalid in [{}, {"error": "offline"}, {"yanked": True}]:
        assert check(approved, invalid, repository)
    for invalid in [{}, {"archived": True, "disabled": False}, {"error": "offline"}]:
        assert check(approved, package, invalid)


def test_retained_inventory_detects_changed_or_missing_evidence(tmp_path):
    import hashlib

    artifact = tmp_path / "api.cdx.json"
    artifact.write_text('{"bomFormat":"CycloneDX"}')
    manifest = {
        "schema_version": 1,
        "files": {artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()},
    }
    check = policy_module().check_inventory_hashes
    assert check(tmp_path, manifest) == []
    artifact.write_text("changed")
    assert check(tmp_path, manifest)
    artifact.unlink()
    assert check(tmp_path, manifest)
    manifest["files"] = {"../outside.json": "0" * 64}
    assert check(tmp_path, manifest)


def test_release_windows_follow_active_stable_minor_lines():
    check = policy_module().check_release_window
    releases = {
        "1.80.0": [{}],
        "1.81.2": [{}],
        "1.82.0": [{}],
        "1.83.1": [{}],
        "1.84.0rc1": [{}],
        "1.85.0": [{"yanked": True}],
    }
    assert check("1.82.0", releases, 2) == []
    assert check("1.83.1", releases, 2) == []
    assert check("1.81.2", releases, 2)
    assert check("1.84.0rc1", releases, 2)
    assert check("1.83.1", {}, 2)


def test_postgresql_major_and_debian_openssl_are_separate_support_providers():
    reviewed = catalog()
    reviewed["runtimes"] = {
        "postgresql": {"17": {"minimum": "17.11", "end_of_support": "2029-11-08"}},
        "openssl-debian": {
            "3.0": {"minimum": "3.0.20", "end_of_support": "2028-06-30"}
        },
    }
    components = [{"ecosystem": "pypi", "name": "example", "version": "1.0"}]
    check = policy_module().check_maintenance
    assert (
        check(
            components,
            {"postgresql": "17.11", "openssl-debian": "3.0.20"},
            reviewed,
            date(2026, 9, 12),
        )
        == []
    )
    assert check(components, {"openssl": "3.0.20"}, reviewed, date(2026, 9, 12))


def test_latest_only_policies_and_latest_within_supported_major():
    check = policy_module().check_latest_release
    releases = {"1.28.1": [{}], "1.30.0": [{}], "2.0.0": [{}], "2.1.0rc1": [{}]}
    assert check("2.0.0", releases, {"support_window": "latest_stable"}) == []
    assert check("1.30.0", releases, {"support_window": "latest_stable"})
    policy = {
        "support_window": "latest_major_line",
        "supported_major_lines": ["1", "2"],
    }
    assert check("1.30.0", releases, policy) == []
    assert check("1.28.1", releases, policy)
    assert check("3.0.0", {"3.0.0": [{}]}, policy)
    assert check("1.0.0", {}, {"support_window": "latest_stable"})


def test_rolling_support_uses_review_horizon_without_invented_vendor_eol():
    reviewed = catalog()
    reviewed["runtimes"] = {
        "litellm": {
            "1.100": {
                "minimum": "1.100.1",
                "support_kind": "rolling_window",
                "review_horizon": "2026-10-12",
            }
        }
    }
    components = [{"ecosystem": "pypi", "name": "example", "version": "1.0"}]
    check = policy_module().check_maintenance
    assert check(components, {"litellm": "1.100.1"}, reviewed, date(2026, 9, 12)) == []
    assert check(components, {"litellm": "1.100.1"}, reviewed, date(2026, 10, 13))


def test_latest_policy_accepts_two_part_upstream_release_numbers():
    check = policy_module().check_latest_release
    assert (
        check("17.1", {"17.0": [{}], "17.1": [{}]}, {"support_window": "latest_stable"})
        == []
    )
    assert (
        check("3.0", {"2.23": [{}], "3.0": [{}]}, {"support_window": "latest_stable"})
        == []
    )
