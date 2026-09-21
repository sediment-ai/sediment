# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate runtime augmentation using the pinned CycloneDX implementation."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("SEDIMENT_SECURITY_TOOL_TESTS") != "1",
    reason="requires the pinned standalone CycloneDX scanner environment",
)


def test_generator_preserves_packages_and_adds_observed_runtime(tmp_path):
    sbom = tmp_path / "bom.json"
    original = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "sediment", "version": "0.1.0"}
        },
        "components": [
            {
                "type": "library",
                "name": "libexample",
                "version": "1.2",
                "bom-ref": "pkg:deb/debian/libexample@1.2",
                "purl": "pkg:deb/debian/libexample@1.2",
                "hashes": [{"alg": "SHA-256", "content": "a" * 64}],
                "properties": [
                    {"name": "aquasecurity:trivy:PkgType", "value": "debian"}
                ],
            }
        ],
    }
    sbom.write_text(json.dumps(original))
    observed = tmp_path / "runtime.json"
    observed.write_text(json.dumps({"python": "3.12.14", "openssl-debian": "3.0.20"}))
    command = [
        "uv",
        "tool",
        "run",
        "--from",
        "cyclonedx-bom==7.3.1",
        "python",
        str(ROOT / "scripts/security_sbom.py"),
        str(sbom),
        str(observed),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    bom = json.loads(sbom.read_text())
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.6"
    packages = {c["name"]: c for c in bom["components"]}
    for key in ["version", "purl", "hashes", "properties"]:
        assert packages["libexample"][key] == original["components"][0][key]
    assert packages["python"]["version"] == "3.12.14"
    assert packages["openssl-debian"]["version"] == "3.0.20"
    assert packages["python"]["type"] == "platform"
    # Repeating the observation does not create duplicate components.
    assert subprocess.run(command, capture_output=True, text=True).returncode == 0
    assert len(json.loads(sbom.read_text())["components"]) == 3
    observed.write_text('{"python":"not observed"}')
    assert subprocess.run(command, capture_output=True, text=True).returncode != 0


def test_generator_records_native_hashes_and_parent_without_inventing_versions(
    tmp_path,
):
    sbom = tmp_path / "bom.json"
    parent_ref = "pkg:pypi/numpy@2.5.3"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "components": [
                    {
                        "type": "library",
                        "name": "numpy",
                        "version": "2.5.3",
                        "purl": parent_ref,
                        "bom-ref": parent_ref,
                    }
                ],
            }
        )
    )
    runtimes = tmp_path / "runtimes.json"
    runtimes.write_text('{"python":"3.13.14"}')
    native = tmp_path / "native.json"
    libraries = [
        {"name": "OpenBLAS", "version": "0.3.34.106.0"},
        {"name": "libgfortran", "abi": "5"},
        {"name": "libquadmath", "abi": "0"},
    ]
    for index, library in enumerate(libraries):
        library.update(
            parent={"ecosystem": "pypi", "name": "numpy", "version": "2.5.3"},
            path=f"/numpy.libs/{library['name']}.so",
            sha256=str(index + 1) * 64,
        )
    native.write_text(json.dumps({"bundled_libraries": libraries}))
    command = [
        "uv",
        "tool",
        "run",
        "--from",
        "cyclonedx-bom==7.3.1",
        "python",
        str(ROOT / "scripts/security_sbom.py"),
        str(sbom),
        str(runtimes),
        str(native),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    bom = json.loads(sbom.read_text())
    children = {
        c["name"]: c for c in bom["components"] if c["name"] not in {"numpy", "python"}
    }
    assert set(children) == {"OpenBLAS", "libgfortran", "libquadmath"}
    assert children["OpenBLAS"]["version"] == "0.3.34.106.0"
    for library in libraries:
        child = children[library["name"]]
        assert child["type"] == "library"
        assert child["hashes"] == [{"alg": "SHA-256", "content": library["sha256"]}]
        props = {p["name"]: p["value"] for p in child["properties"]}
        assert props["sediment:bundled:parent"] == parent_ref
        assert props["sediment:bundled:path"] == library["path"]
        if "abi" in library:
            assert "version" not in child
            assert props["sediment:bundled:abi"] == library["abi"]
        assert any(
            d["ref"] == parent_ref and child["bom-ref"] in d["dependsOn"]
            for d in bom["dependencies"]
        )
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert len(json.loads(sbom.read_text())["components"]) == 5
    libraries[0]["parent"] = {**libraries[0]["parent"], "version": "2.5.4"}
    native.write_text(json.dumps({"bundled_libraries": libraries}))
    assert subprocess.run(command, capture_output=True).returncode != 0
