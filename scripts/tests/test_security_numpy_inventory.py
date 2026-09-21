# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bundled native files require exact maintenance and SBOM coverage."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PARENT = {"ecosystem": "pypi", "name": "numpy", "version": "2.5.3"}


def module(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded
    finally:
        sys.path.pop(0)


@pytest.fixture
def native_evidence(tmp_path):
    policy = module("security_policy")
    library = {
        "name": "OpenBLAS",
        "version": "0.3.34.106.0",
        "path": "/numpy.libs/libopenblas.so",
        "sha256": "a" * 64,
    }
    probe = {
        "components": [PARENT],
        "bundled_libraries": [{**library, "parent": PARENT}],
    }
    parent_ref = "pkg:pypi/numpy@2.5.3"
    child_ref = policy.bundled_library_ref(probe["bundled_libraries"][0])
    sbom = {
        "components": [
            {
                "type": "library",
                "name": "numpy",
                "version": "2.5.3",
                "purl": parent_ref,
                "bom-ref": parent_ref,
            },
            {
                "type": "library",
                "name": "OpenBLAS",
                "version": library["version"],
                "bom-ref": child_ref,
                "hashes": [{"alg": "SHA-256", "content": library["sha256"]}],
                "properties": [
                    {"name": "sediment:bundled:path", "value": library["path"]},
                    {"name": "sediment:bundled:parent", "value": parent_ref},
                ],
            },
        ],
        "dependencies": [{"ref": parent_ref, "dependsOn": [child_ref]}],
    }
    inventory = {
        "components": [PARENT],
        "architecture": "arm64",
        "native_probe": "native.json",
        "sbom": "bom.json",
        "files": {},
    }
    catalog = {
        "packages": {
            "pypi/numpy": {
                "bundled_components": [
                    {
                        "name": "OpenBLAS",
                        "version": library["version"],
                        "source": "https://github.com/OpenMathLib/OpenBLAS",
                    }
                ],
                "bundled_files": {"2.5.3": {"arm64": [library]}},
            }
        }
    }

    def save():
        for name, data in (("native.json", probe), ("bom.json", sbom)):
            path = tmp_path / name
            path.write_text(json.dumps(data))
            inventory["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()

    save()
    return policy, inventory, probe, sbom, catalog, save


def test_reviewed_native_files_and_parent_relationship_pass(native_evidence, tmp_path):
    policy, inventory, _, _, catalog, _ = native_evidence
    assert policy.check_bundled_libraries(inventory, tmp_path, catalog) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "probe_missing",
        "probe_unhashed",
        "missing",
        "extra",
        "hash",
        "path",
        "version",
        "abi",
        "parent",
        "inventory_parent",
        "probe_parent",
        "only_sbom_parent",
        "architecture",
        "unreviewed",
        "sbom_missing",
        "sbom_hash",
        "sbom_duplicate",
        "parent_edge",
    ],
)
def test_missing_or_changed_native_coverage_blocks(native_evidence, tmp_path, mutation):
    policy, inventory, probe, sbom, catalog, save = native_evidence
    if mutation == "probe_missing":
        inventory.pop("native_probe")
    elif mutation == "probe_unhashed":
        inventory["native_probe"] = "unbound.json"
        (tmp_path / "unbound.json").write_text(json.dumps(probe))
    elif mutation == "missing":
        probe["bundled_libraries"].clear()
    elif mutation == "extra":
        probe["bundled_libraries"].append(
            {**probe["bundled_libraries"][0], "path": "/numpy.libs/unreviewed.so"}
        )
    elif mutation in {"hash", "path", "version", "abi"}:
        key = "sha256" if mutation == "hash" else mutation
        probe["bundled_libraries"][0][key] = "b" * 64 if key == "sha256" else "changed"
    elif mutation == "parent":
        probe["bundled_libraries"][0]["parent"] = {**PARENT, "version": "2.5.4"}
    elif mutation == "inventory_parent":
        inventory["components"] = []
        probe["bundled_libraries"] = []
        sbom["components"].pop()
    elif mutation == "probe_parent":
        probe["components"] = []
    elif mutation == "only_sbom_parent":
        inventory["components"] = []
        inventory.pop("native_probe")
        sbom["components"].pop()
    elif mutation == "architecture":
        inventory["architecture"] = "amd64"
    elif mutation == "unreviewed":
        catalog["packages"]["pypi/numpy"]["bundled_components"].clear()
    elif mutation == "sbom_missing":
        sbom["components"].pop()
    elif mutation == "sbom_hash":
        sbom["components"][1]["hashes"][0]["content"] = "b" * 64
    elif mutation == "sbom_duplicate":
        sbom["components"].insert(1, {**sbom["components"][1], "version": "unreviewed"})
    elif mutation == "parent_edge":
        sbom["dependencies"].clear()
    save()  # Rehashed tampering still fails the independent reviewed mapping.
    assert policy.check_bundled_libraries(inventory, tmp_path, catalog)


def test_artifact_without_numpy_does_not_need_native_numpy_evidence(
    native_evidence, tmp_path
):
    policy, inventory, _, sbom, catalog, save = native_evidence
    inventory["components"] = []
    inventory.pop("native_probe")
    sbom["components"] = []
    save()
    assert policy.check_bundled_libraries(inventory, tmp_path, catalog) == []


def test_evaluate_cannot_skip_native_gate(native_evidence, tmp_path, monkeypatch):
    scan = module("security_scan")
    _, inventory, probe, _, catalog, save = native_evidence
    probe["bundled_libraries"].clear()
    save()
    inventory.update(artifact="gateway", runtimes={}, support={"ends_on": "2099-01-01"})
    monkeypatch.setattr(scan, "check_maintenance", lambda *a: [])
    errors = scan.evaluate(inventory, tmp_path, tmp_path, catalog, [], online=False)
    assert any("bundled" in error.lower() for error in errors)


def test_final_inventory_binds_native_probe_bytes(tmp_path, monkeypatch):
    scan = module("security_scan")
    native = tmp_path / "gateway-arm64.native.json"
    native.write_text(json.dumps({"bundled_libraries": []}))
    sbom = tmp_path / "gateway-arm64.cdx.json"
    sbom.write_text("{}")
    commands = []
    monkeypatch.setattr(
        scan, "run", lambda command: commands.append(command) or "a" * 40
    )
    result = scan.finalize(
        tmp_path,
        "gateway",
        "arm64",
        [{"name": "litellm"}],
        {"python": "3.13.14"},
        [sbom],
        sbom=sbom,
        native_probe=native,
    )
    inventory = json.loads(result.read_text())
    assert inventory["native_probe"] == native.name
    assert (
        inventory["files"][native.name]
        == hashlib.sha256(native.read_bytes()).hexdigest()
    )
    assert str(native) in commands[0]


@pytest.mark.parametrize(
    "version,stale,end_of_support",
    [("16.15", "16.14", "2028-11-09"), ("18.6", "18.5", "2030-11-14")],
)
def test_loaded_host_libpq_is_reviewed_and_latest_patch_is_still_required(
    tmp_path, monkeypatch, version, stale, end_of_support
):
    from datetime import date

    policy = module("security_policy")
    scan = module("security_scan")
    catalog = json.loads((ROOT / "security/maintenance.json").read_text())
    parent = {"ecosystem": "pypi", "name": "psycopg", "version": "3.3.5"}
    assert (
        policy.check_maintenance(
            [parent], {"libpq": version}, catalog, date(2026, 9, 12)
        )
        == []
    )
    major = version.split(".")[0]
    entry = catalog["runtimes"]["libpq"][major]
    assert entry["end_of_support"] == end_of_support
    commands = []
    monkeypatch.setattr(
        scan,
        "run",
        lambda args: (
            commands.append(args)
            or json.dumps([{"ref": f"refs/tags/REL_{version.replace('.', '_')}"}])
        ),
    )
    assert (
        scan.live_runtime_maintenance({"libpq": version}, tmp_path / "runtime.json")
        == []
    )
    assert (
        commands[0][-1]
        == f"repos/postgres/postgres/git/matching-refs/tags/REL_{major}_"
    )
    assert scan.live_runtime_maintenance({"libpq": stale}, tmp_path / "runtime.json")


def test_native_probe_hashes_physical_files_and_omits_unknown_release_versions(
    tmp_path,
):
    scan = module("security_scan")
    numpy = tmp_path / "numpy"
    numpy.mkdir()
    (numpy / "__init__.py").write_text(
        "__version__ = '2.5.3'\n"
        "def show_config(mode):\n"
        "    return {'Build Dependencies': {'blas': {'name': 'scipy-openblas', "
        "'found': True, 'version': '0.3.34.106.0'}}}\n"
    )
    libraries = tmp_path / "numpy.libs"
    libraries.mkdir()
    files = {
        "libscipy_openblas64_-test.so": b"openblas",
        "libgfortran-test.so.5.0.0": b"fortran",
        "libquadmath-test.so.0.0.0": b"quadmath",
        "unrecorded.so": b"not in any distribution manifest",
    }
    for name, content in files.items():
        (libraries / name).write_bytes(content)

    def probe():
        return subprocess.run(
            [sys.executable, "-S", "-c", scan.PYTHON_INVENTORY],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
        )

    result = probe()
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)["bundled_libraries"]
    assert len(observed) == len(files)
    for library in observed:
        path = Path(library["path"])
        assert path.parent == libraries
        assert library["parent"] == PARENT
        assert library["sha256"] == hashlib.sha256(files[path.name]).hexdigest()
        if library["name"] == "OpenBLAS":
            assert library["version"] == "0.3.34.106.0"
        else:
            assert "version" not in library
        if library["name"] in {"libgfortran", "libquadmath"}:
            assert library["abi"] == ("5" if library["name"] == "libgfortran" else "0")
    (libraries / "unrecorded.so").write_bytes(b"changed")
    changed = json.loads(probe().stdout)["bundled_libraries"]
    assert changed != observed
    (libraries / "linked.so").symlink_to(libraries / "unrecorded.so")
    assert probe().returncode != 0
