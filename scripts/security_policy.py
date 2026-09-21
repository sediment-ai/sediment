# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evaluate retained scanner evidence and the reviewed maintenance catalog.

This module evaluates evidence; it doesn't generate SBOMs or infer upstream
support from an absence of known vulnerabilities.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote


def _review_valid(record: dict, today: date) -> bool:
    try:
        reviewed = date.fromisoformat(record["reviewed_on"])
        expires = date.fromisoformat(record["expires_on"])
        return (
            reviewed <= today <= expires
            and 0 <= (expires - reviewed).days <= 30
            and bool(record["owner"].strip())
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _source(value: object) -> bool:
    return isinstance(value, str) and value.startswith("https://") and len(value) > 12


def check_source_reviews(
    dispositions: list[dict], digest: str, today: date
) -> list[str]:
    """Reject stale retained reviews before automatic artifact builds."""
    if not isinstance(dispositions, list):
        return ["malformed source review catalog"]
    errors = []
    for record in dispositions:
        if not isinstance(record, dict):
            errors.append("malformed source review")
        elif not _review_valid(record, today) or record.get("source_digest") != digest:
            errors.append(
                f"{record.get('id', 'unknown')}: stale or invalid source review"
            )
    return errors


def check_vulnerabilities(
    report: dict,
    artifact: str,
    architecture: str,
    dispositions: list[dict],
    today: date,
    *,
    assurance: dict | None = None,
) -> list[str]:
    """Require a fresh complete scan, fixes, and exact reviewed dispositions."""
    errors = []
    assurance = assurance or {}
    try:
        scanned = datetime.fromisoformat(report["CreatedAt"].replace("Z", "+00:00"))
        if scanned.tzinfo is None or not 0 <= (today - scanned.date()).days <= 1:
            errors.append("vulnerability scan is stale or future-dated")
        if report["SchemaVersion"] != 2 or not report["Results"]:
            raise ValueError
        if report.get("Metadata", {}).get("OS", {}).get("EOSL"):
            errors.append("operating system is unsupported")
        for result in report["Results"]:
            if not isinstance(result["Packages"], list) or not result["Packages"]:
                raise ValueError
            for finding in result.get("Vulnerabilities", []):
                identity = {
                    "artifact": artifact,
                    "architecture": architecture,
                    "package": finding["PkgName"],
                    "version": finding["InstalledVersion"],
                    "id": finding["VulnerabilityID"],
                }
                label = f"{artifact}/{architecture}: {identity['package']} {identity['version']} {identity['id']}"
                matches = [
                    d
                    for d in dispositions
                    if d.get("id") == identity["id"]
                    and f"{artifact}/{architecture}/{identity['package']}@{identity['version']}"
                    in d.get("targets", [])
                ]
                disposition = matches[0] if len(matches) == 1 else {}
                approved = (
                    _review_valid(disposition, today)
                    and disposition.get("status") in {"not_affected", "mitigated"}
                    and bool(disposition.get("reason", "").strip())
                    and bool(disposition.get("evidence"))
                    and all(_source(e) for e in disposition["evidence"])
                    and re.fullmatch(
                        r"[0-9a-f]{64}", disposition.get("source_digest", "")
                    )
                    is not None
                    and disposition["source_digest"] == assurance.get("source_digest")
                    and bool(disposition.get("required_predicates"))
                    and all(
                        assurance.get("predicates", {}).get(name) is True
                        for name in disposition["required_predicates"]
                    )
                )
                # An available fix can only be superseded by evidence that the
                # installed component itself is not affected, never by a delay.
                if finding.get("FixedVersion"):
                    if not (approved and disposition["status"] == "not_affected"):
                        errors.append(f"{label}: fixable vulnerability")
                elif finding["Severity"] in {"HIGH", "CRITICAL"} and not approved:
                    errors.append(f"{label}: missing valid disposition")
    except (KeyError, TypeError, ValueError, AttributeError):
        errors.append("missing or malformed vulnerability scan evidence")
    return errors


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise ValueError("unrecognized runtime version")
    return tuple(int(p) for p in value.split("."))


def check_maintenance(
    components: list[dict], runtimes: dict[str, str], catalog: dict, today: date
) -> list[str]:
    """Match every observed component to an explicit, time-bounded review."""
    errors = []
    try:
        if catalog["schema_version"] != 1 or not _review_valid(catalog, today):
            errors.append("maintenance catalog has no valid review")
        if not components or not runtimes:
            errors.append("missing component or runtime inventory")
        for component in components:
            identity = f"{component['ecosystem']}/{component['name']}"
            entry = catalog["packages"].get(identity, {})
            if (
                entry.get("status") != "maintained"
                or component["version"] not in entry.get("versions", [])
                or not _source(entry.get("source"))
                or not _source(entry.get("evidence"))
            ):
                errors.append(
                    f"{identity} {component['version']}: unsupported or unreviewed"
                )
        for name, value in runtimes.items():
            version = _version(value)
            line = (
                str(version[0])
                if name in {"node", "postgresql", "libpq", "libpq-debian"}
                else ".".join(map(str, version[:2]))
            )
            entry = catalog["runtimes"].get(name, {}).get(line, {})
            if (
                not entry
                or version < _version(entry["minimum"])
                or today
                > date.fromisoformat(
                    entry["review_horizon"]
                    if entry.get("support_kind") == "rolling_window"
                    else entry["end_of_support"]
                )
            ):
                errors.append(f"{name} {value}: unsupported runtime")
    except (KeyError, TypeError, ValueError, AttributeError):
        errors.append("missing or malformed maintenance evidence")
    return errors


def bundled_library_ref(library: dict) -> str:
    """Identify the measured file, without treating an ABI as a release."""
    return f"sediment:bundled:{library['sha256']}:{quote(library['path'], safe='')}"


def check_bundled_libraries(
    inventory: dict, directory: Path, catalog: dict
) -> list[str]:
    """Require exact reviewed NumPy files and their retained SBOM relationships."""
    try:
        parents = [
            c
            for c in inventory["components"]
            if c.get("ecosystem") == "pypi" and c.get("name") == "numpy"
        ]
        sbom_name = inventory["sbom"]
        if (
            not isinstance(sbom_name, str)
            or Path(sbom_name).name != sbom_name
            or sbom_name not in inventory["files"]
        ):
            raise ValueError("unbound SBOM")
        bom = json.loads((directory / sbom_name).read_text())
        parent_purls = sorted(
            c["purl"].split("?")[0]
            for c in bom["components"]
            if c.get("purl", "").startswith("pkg:pypi/numpy@")
        )
        if parent_purls != sorted(f"pkg:pypi/numpy@{p['version']}" for p in parents):
            raise ValueError("SBOM NumPy parent coverage differs")
        probe_name = inventory.get("native_probe")
        if not parents and probe_name is None:
            return []
        if (
            not isinstance(probe_name, str)
            or Path(probe_name).name != probe_name
            or probe_name not in inventory["files"]
        ):
            raise ValueError("missing bound probe")
        probe = json.loads((directory / probe_name).read_text())
        probe_parents = [
            c
            for c in probe["components"]
            if c.get("ecosystem") == "pypi" and c.get("name") == "numpy"
        ]
        if sorted(json.dumps(p, sort_keys=True) for p in probe_parents) != sorted(
            json.dumps(p, sort_keys=True) for p in parents
        ):
            raise ValueError("observed NumPy parent coverage differs")
        observed = probe["bundled_libraries"]
        if not isinstance(observed, list):
            raise ValueError("malformed library list")
        expected = []
        for parent in parents:
            entry = catalog["packages"]["pypi/numpy"]
            reviewed = {item["name"]: item for item in entry["bundled_components"]}
            files = entry["bundled_files"][parent["version"]][inventory["architecture"]]
            if not files:
                raise ValueError("missing architecture review")
            for library in files:
                review = reviewed[library["name"]]
                if (
                    not _source(review.get("source"))
                    or any(
                        library.get(key) != review.get(key)
                        for key in ("version", "abi")
                    )
                    or not re.fullmatch(r"[0-9a-f]{64}", library["sha256"])
                    or not Path(library["path"]).is_absolute()
                ):
                    raise ValueError("malformed library review")
                expected.append({**library, "parent": parent})
        if sorted(json.dumps(item, sort_keys=True) for item in observed) != sorted(
            json.dumps(item, sort_keys=True) for item in expected
        ):
            raise ValueError("measured files differ from review")
        child_components = [
            c
            for c in bom["components"]
            if c.get("bom-ref", "").startswith("sediment:bundled:")
        ]
        children = {c["bom-ref"]: c for c in child_components}
        if len(children) != len(child_components):
            raise ValueError("SBOM library reference is ambiguous")
        if set(children) != {bundled_library_ref(item) for item in observed}:
            raise ValueError("SBOM library coverage differs")
        for library in observed:
            parent = library["parent"]
            parent_purl = f"pkg:pypi/{parent['name']}@{parent['version']}"
            matches = [
                c
                for c in bom["components"]
                if c.get("purl", "").split("?")[0] == parent_purl
            ]
            if len(matches) != 1:
                raise ValueError("SBOM parent absent or ambiguous")
            parent_ref = matches[0]["bom-ref"]
            ref = bundled_library_ref(library)
            child = children[ref]
            properties = {p["name"]: p["value"] for p in child.get("properties", [])}
            if (
                child["type"] != "library"
                or child["name"] != library["name"]
                or child.get("version") != library.get("version")
                or child.get("hashes")
                != [{"alg": "SHA-256", "content": library["sha256"]}]
                or properties.get("sediment:bundled:path") != library["path"]
                or properties.get("sediment:bundled:parent") != parent_ref
                or properties.get("sediment:bundled:abi") != library.get("abi")
                or not any(
                    d["ref"] == parent_ref and ref in d.get("dependsOn", [])
                    for d in bom.get("dependencies", [])
                )
            ):
                raise ValueError("SBOM library or parent relationship differs")
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        return ["NumPy bundled-library evidence is missing, changed, or unreviewed"]
    return []


def check_upstream(entry: dict, package: dict, repository: dict | None) -> list[str]:
    """Check registry withdrawal and the explicitly reviewed source location."""
    errors = []
    if package.get("yanked") is not False or package.get("deprecated"):
        errors.append("registry metadata missing, withdrawn, or deprecated")
    if "github.com/" in entry.get("source", ""):
        if (
            not repository
            or repository.get("archived") is not False
            or repository.get("disabled") is not False
        ):
            errors.append("upstream repository missing, archived, or disabled")
    return errors


def check_inventory_hashes(directory: Path, manifest: dict) -> list[str]:
    """Refuse missing, changed, linked, or escaping retained evidence."""
    errors = []
    try:
        if manifest["schema_version"] != 1 or not manifest["files"]:
            raise ValueError
        for name, expected in manifest["files"].items():
            path = directory / name
            if (
                Path(name).name != name
                or path.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                errors.append(f"inventory integrity failed: {name}")
    except (KeyError, TypeError, ValueError, OSError):
        errors.append("missing or malformed retained inventory")
    return errors


def check_release_window(installed: str, releases: dict, count: int) -> list[str]:
    """Use published, nonwithdrawn stable releases for vendor minor-line support."""
    try:
        version = _version(installed)
        active = sorted(
            {
                _version(name)[:2]
                for name, files in releases.items()
                if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", name)
                and files
                and any(not item.get("yanked", False) for item in files)
            }
        )
        if count < 1 or not active or version[:2] not in active[-count:]:
            return ["release line is outside the upstream support window"]
    except (ValueError, TypeError, AttributeError):
        return ["missing or malformed upstream release evidence"]
    return []


def check_latest_release(installed: str, releases: dict, entry: dict) -> list[str]:
    """Apply a reviewed vendor policy without equating age with discontinuation."""
    try:
        version = _version(installed)
        stable = [
            _version(name)
            for name, files in releases.items()
            if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", name)
            and files
            and any(not item.get("yanked", False) for item in files)
        ]
        window = entry["support_window"]
        if window == "latest_major_line":
            if str(version[0]) not in entry["supported_major_lines"]:
                return ["unsupported upstream major release line"]
            stable = [v for v in stable if v[0] == version[0]]
        elif window != "latest_stable":
            return ["unrecognized upstream support policy"]
        if not stable or version != max(stable):
            return ["installed release is outside upstream latest-version support"]
    except (ValueError, TypeError, KeyError, AttributeError):
        return ["missing or malformed upstream release evidence"]
    return []
