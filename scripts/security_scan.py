# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collect exact-artifact security evidence and rescan retained release SBOMs."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from security_policy import (
    check_inventory_hashes,
    check_bundled_libraries,
    check_maintenance,
    check_latest_release,
    check_release_window,
    check_upstream,
    check_vulnerabilities,
)

ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY = {
    f"sediment-{p}" for p in ("api", "capture", "cli", "core", "derive", "export")
}
TOOLS = {
    "trivy": "0.74.0",
    "pip-audit": "2.10.1",
    "cyclonedx-bom": "7.3.1",
    "semgrep": "1.177.0",
    "npm": "12.0.2",
}
PYTHON_INVENTORY = """
import importlib.metadata as m, importlib.util, hashlib, json, platform, ssl
from pathlib import Path
runtimes = {'python':platform.python_version(), 'openssl':ssl.OPENSSL_VERSION.split()[1]}
if importlib.util.find_spec('cryptography'):
    from cryptography.hazmat.bindings.openssl.binding import Binding
    binding = Binding()
    runtimes['openssl-cryptography'] = binding.ffi.string(binding.lib.OpenSSL_version(0)).decode().split()[1]
if importlib.util.find_spec('psycopg'):
    try:
        from psycopg import pq
    except ImportError:
        pass  # Capture-only client installations do not require system libpq.
    else:
        version = pq.version()
        runtimes['libpq'] = f'{version // 10000}.{version % 10000}'
bundled = []
if importlib.util.find_spec('numpy'):
    import numpy
    config = numpy.show_config(mode='dicts')
    blas = config['Build Dependencies']['blas']
    if blas['name'] != 'scipy-openblas' or not blas['found']:
        raise ValueError('unrecognized NumPy BLAS build')
    root = Path(numpy.__file__).parent.parent / 'numpy.libs'
    if root.is_symlink() or not root.is_dir():
        raise ValueError('NumPy bundled library directory absent or unsafe')
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('NumPy bundled library is not a regular file')
        if path.is_dir():
            continue
        item = {'name':path.name, 'path':str(path), 'parent':
                {'ecosystem':'pypi','name':'numpy','version':numpy.__version__}}
        with path.open('rb') as stream:
            item['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        if path.name.startswith('libscipy_openblas'):
            item.update(name='OpenBLAS', version=blas['version'])
        elif path.name.startswith(('libgfortran-', 'libquadmath-')):
            item.update(name=path.name.split('-')[0], abi=path.name.split('.so.')[1].split('.')[0])
        bundled.append(item)
    if not bundled:
        raise ValueError('NumPy bundled library inventory is empty')
print(json.dumps({
 'bundled_libraries': bundled,
 'components': sorted([{'ecosystem':'pypi','name':d.metadata['Name'].lower().replace('_','-'),
                       'version':d.version} for d in m.distributions()], key=lambda d:d['name']),
 'runtimes': runtimes
}))
"""

# Downloaded server binaries are separate from the wheel inventory. Observe the
# installed wheel's pinned installer; retain every installed file and host link.
POSTGRES_INVENTORY = """
import contextlib, hashlib, json, platform, subprocess, sys, tempfile
from pathlib import Path
from sediment_cli.local_postgres import _install_postgres, VERSION, _PACKAGES
with tempfile.TemporaryDirectory(prefix='sediment-native-postgres-') as temporary:
    with contextlib.redirect_stdout(sys.stderr):
        binaries = _install_postgres(Path(temporary))
    version = subprocess.check_output([str(binaries/'postgres'), '--version'], text=True).strip().split()[-1]
    if version != VERSION.removesuffix('.0'):
        raise ValueError('downloaded PostgreSQL version differs from pin')
    target, digest = _PACKAGES[(platform.system(), platform.machine())]
    files = []
    for path in sorted(binaries.parent.rglob('*')):
        if path.is_dir():
            continue
        item = {'path':str(path.relative_to(binaries.parent))}
        if path.is_symlink():
            item['target'] = str(path.readlink())
        with path.open('rb') as stream:
            item['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        files.append(item)
    if not files:
        raise ValueError('empty PostgreSQL installation')
    print(json.dumps({'postgresql':version, 'archive_sha256':digest,
      'archive_url':f'https://github.com/theseus-rs/postgresql-binaries/releases/download/{VERSION}/postgresql-{VERSION}-{target}.tar.gz',
      'files':files, 'host_libraries':'operator-maintained; not a host OS attestation'}))
"""


class ScanFailure(Exception):
    """A required scanner, inventory, or policy check did not succeed."""


def run(
    command: list[str], *, cwd: Path = ROOT, allowed: tuple[int, ...] = (0,)
) -> str:
    """A missing tool or execution error must not become an empty clean report."""
    try:
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=1800
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScanFailure(
            f"{Path(command[0]).name}: scanner execution failed ({type(exc).__name__})"
        ) from None
    if completed.returncode not in allowed:
        # Preserve diagnostics for CI without reproducing subprocess arguments,
        # which may contain local paths or environment-specific configuration.
        if completed.stderr:
            print(completed.stderr[-8192:], file=sys.stderr)
        raise ScanFailure(
            f"{Path(command[0]).name}: scanner exit {completed.returncode}"
        )
    return completed.stdout


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError) as exc:
        raise ScanFailure(
            f"invalid evidence {path.name}: {type(exc).__name__}"
        ) from None


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def require_components(components: list[dict], artifact: str) -> None:
    names = {c["name"] for c in components}
    if artifact in {"api", "client"} and not FIRST_PARTY <= names:
        raise ScanFailure("installed inventory is missing first-party distributions")
    if artifact == "postgres" and "postgresql-17" not in names:
        raise ScanFailure("PostgreSQL inventory is missing its server package")
    if artifact == "gateway" and "litellm" not in names:
        raise ScanFailure("gateway inventory is missing LiteLLM")


def components_from_trivy(report: dict) -> list[dict]:
    ecosystems = {
        "python-pkg": "pypi",
        "node-pkg": "npm",
        "debian": "deb",
        "wolfi": "apk",
        "gobinary": "golang",
    }
    components = {}
    try:
        for result in report["Results"]:
            ecosystem = ecosystems[result["Type"]]
            for package in result["Packages"]:
                item = {
                    "ecosystem": ecosystem,
                    "name": re.sub(r"[-_.]+", "-", package["Name"]).lower()
                    if ecosystem == "pypi"
                    else package["Name"],
                    "version": package["ID"].removeprefix(package["Name"] + "@")
                    if ecosystem == "deb"
                    else package["Version"],
                }
                components[(ecosystem, item["name"], item["version"])] = item
    except (KeyError, TypeError):
        raise ScanFailure("unsupported or incomplete Trivy package inventory") from None
    return list(components.values())


def finalize(
    out: Path,
    artifact: str,
    architecture: str,
    components: list[dict],
    runtimes: dict,
    files: list[Path],
    *,
    image: dict | None = None,
    assurance: dict | None = None,
    native_probe: Path | None = None,
    sbom: Path,
) -> Path:
    require_components(components, artifact)
    observed = out / f"{artifact}-{architecture}.runtimes.json"
    write_json(observed, runtimes)
    run(
        [
            "uv",
            "tool",
            "run",
            "--from",
            f"cyclonedx-bom=={TOOLS['cyclonedx-bom']}",
            "python",
            str(ROOT / "scripts/security_sbom.py"),
            str(sbom),
            str(observed),
            *([str(native_probe)] if native_probe else []),
        ]
    )
    files = [*files, observed, *([native_probe] if native_probe else [])]
    today = datetime.now(UTC).date()
    manifest = {
        "schema_version": 1,
        "artifact": artifact,
        "architecture": architecture,
        "created_at": datetime.now(UTC).isoformat(),
        "source_commit": run(["git", "rev-parse", "HEAD"]).strip(),
        "build_inputs": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                ROOT / "Dockerfile",
                ROOT / "uv.lock",
                ROOT / "pyproject.toml",
                ROOT / "shims/pi/package-lock.json",
                *sorted((ROOT / "docker").rglob("*")),
            ]
            if p.is_file()
        },
        "tools": TOOLS,
        "components": components,
        "runtimes": runtimes,
        "support": {
            "owner": "Sediment maintainers",
            "starts_on": today.isoformat(),
            "ends_on": (today + timedelta(days=90)).isoformat(),
        },
        "sbom": sbom.name,
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }
    if native_probe is not None:
        manifest["native_probe"] = native_probe.name
    if image is not None:
        manifest["image"] = image
    if assurance is not None:
        manifest["assurance"] = assurance
    path = out / f"{artifact}-{architecture}.inventory.json"
    write_json(path, manifest)
    return path


def require_image_source(labels: dict, revision: str, digest: str) -> None:
    if (
        labels.get("org.opencontainers.image.revision") != revision
        or labels.get("io.sediment.source-digest") != digest
    ):
        raise ScanFailure("image source labels do not match the reviewed checkout")


def require_scan_image(report: dict, image_id: str) -> None:
    if report.get("Metadata", {}).get("ImageID") != image_id:
        raise ScanFailure("scan does not identify the inspected image")


def require_python_scan_coverage(observed: list[dict], scanned: list[dict]) -> None:
    installed = {
        (re.sub(r"[-_.]+", "-", c["name"]).lower(), c["version"]) for c in observed
    }
    covered = {(c["name"], c["version"]) for c in scanned if c["ecosystem"] == "pypi"}
    if not installed or not installed <= covered:
        raise ScanFailure("image scan omits an executed Python distribution or version")


def postgres_version(output: str) -> str:
    match = re.fullmatch(
        r"postgres \(PostgreSQL\) ([0-9]+\.[0-9]+)(?: \([^()\n]+\))?", output.strip()
    )
    if match is None:
        raise ScanFailure("PostgreSQL runtime returned an unrecognized version")
    return match.group(1)


def collect_image(
    image_ref: str, artifact: str, architecture: str, out: Path, trivy: str
) -> Path:
    info = json.loads(run(["docker", "image", "inspect", image_ref]))[0]
    if info["Architecture"] != architecture or info["Os"] != "linux":
        raise ScanFailure("image platform does not match requested inventory")
    from security_image_assurance import collect_assurance, source_digest

    image_id = info["Id"]
    require_image_source(
        info.get("Config", {}).get("Labels") or {},
        run(["git", "rev-parse", "HEAD"]).strip(),
        source_digest(ROOT),
    )
    scan = out / f"{artifact}-{architecture}.trivy.json"
    sbom = out / f"{artifact}-{architecture}.cdx.json"
    # The immutable local image ID binds the scan and runtime probe to one build.
    run(
        [
            trivy,
            "image",
            "--scanners",
            "vuln",
            "--list-all-pkgs",
            "--format",
            "json",
            "--output",
            str(scan),
            image_id,
        ]
    )
    run([trivy, "convert", "--format", "cyclonedx", "--output", str(sbom), str(scan)])
    report = read_json(scan)
    require_scan_image(report, image_id)
    components = components_from_trivy(report)
    prefix = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
    ]
    native_probe = None
    if artifact == "postgres":
        version = postgres_version(run([*prefix, "postgres", image_id, "--version"]))
        openssl = run([*prefix, "openssl", image_id, "version"]).split()[1]
        runtimes = {"postgresql": version, "openssl": openssl}
    else:
        probe = json.loads(run([*prefix, "python", image_id, "-c", PYTHON_INVENTORY]))
        require_python_scan_coverage(probe["components"], components)
        native_probe = out / f"{artifact}-{architecture}.native.json"
        write_json(native_probe, probe)
        runtimes = probe["runtimes"]
        if artifact == "gateway":
            runtimes["litellm"] = next(
                c["version"] for c in components if c["name"] == "litellm"
            )
    if artifact == "api" and "libpq" not in runtimes:
        raise ScanFailure("API image is missing its system libpq runtime")
    if report.get("Metadata", {}).get("OS", {}).get("Family") == "debian":
        runtimes["openssl-debian"] = runtimes.pop("openssl")
        if "libpq" in runtimes:
            runtimes["libpq-debian"] = runtimes.pop("libpq")
    for name in ("grpcio", "protobuf"):
        observed = [
            c["version"]
            for c in components
            if c["ecosystem"] == "pypi" and c["name"] == name
        ]
        if observed:
            runtimes[name] = observed[0]
    for component in components:
        if component["ecosystem"] == "apk" and component["name"].startswith("nodejs-"):
            runtimes["node"] = component["version"].split("-r")[0]
        if component["ecosystem"] == "golang":
            # Go build versions must receive their own review, including tools
            # hidden in vendor images. An unrecognized version fails closed.
            if component["name"] == "stdlib":
                runtimes["go"] = (
                    component["version"].removeprefix("v").removeprefix("go")
                )
    assurance = collect_assurance(image_id, artifact, architecture, out, root=ROOT)
    return finalize(
        out,
        artifact,
        architecture,
        components,
        runtimes,
        [scan, sbom, out / f"{artifact}-{architecture}.assurance.json"],
        assurance=assurance,
        native_probe=native_probe,
        image={
            "id": image_id,
            "repo_digests": info.get("RepoDigests", []),
            "platform": f"linux/{architecture}",
        },
        sbom=sbom,
    )


def require_python_audit(report: dict, components: list[dict]) -> None:
    """An empty or partial audit cannot attest to an installed wheel set."""
    expected = {
        (c["name"], c["version"])
        for c in components
        if c["ecosystem"] == "pypi" and c["name"] not in FIRST_PARTY
    }
    dependencies = report.get("dependencies", [])
    observed = {
        (re.sub(r"[-_.]+", "-", d.get("name", "")).lower(), d.get("version"))
        for d in dependencies
    }
    if (
        not expected
        or expected != observed
        or any("skip_reason" in d or d.get("vulns") != [] for d in dependencies)
    ):
        raise ScanFailure(
            "resolved wheel installation has vulnerabilities or incomplete audit coverage"
        )


def collect_client(wheels: Path, out: Path) -> Path:
    """Inventory installed wheels and observed host runtime prerequisites."""
    artifacts = sorted(wheels.glob("*.whl"))
    if len(artifacts) != 6:
        raise ScanFailure("release inventory requires the exact six release wheels")
    with tempfile.TemporaryDirectory(prefix="sediment-security-client-") as temporary:
        venv = Path(temporary) / "venv"
        run(["uv", "venv", "--python", "3.12.14", str(venv)])
        python = venv / "bin/python"
        run(["uv", "pip", "install", "--python", str(python), *map(str, artifacts)])
        run(["uv", "pip", "check", "--python", str(python)])
        probe = json.loads(
            run([str(python), "-c", PYTHON_INVENTORY], cwd=Path(temporary))
        )
        require_components(probe["components"], "client")
        postgres = json.loads(
            run([str(python), "-c", POSTGRES_INVENTORY], cwd=Path(temporary))
        )
        postgres_evidence = out / "client-native-postgres.json"
        write_json(postgres_evidence, postgres)
        probe["runtimes"]["postgresql"] = postgres["postgresql"]
        native_probe = out / f"client-{platform.machine()}.native.json"
        write_json(native_probe, probe)
        sbom = out / "client.cdx.json"
        run(
            [
                "uv",
                "tool",
                "run",
                "--from",
                f"cyclonedx-bom=={TOOLS['cyclonedx-bom']}",
                "cyclonedx-py",
                "environment",
                str(python),
                "--short-PURLs",
                "--output-file",
                str(sbom),
            ]
        )
        requirements = out / "client-resolved-requirements.txt"
        requirements.write_text(
            "".join(
                f"{c['name']}=={c['version']}\n"
                for c in probe["components"]
                if c["name"] not in FIRST_PARTY
            )
        )
        audit = out / "client.pip-audit.json"
        run(
            [
                "uv",
                "tool",
                "run",
                "--from",
                f"pip-audit=={TOOLS['pip-audit']}",
                "pip-audit",
                "--strict",
                "--no-deps",
                "--disable-pip",
                "--progress-spinner",
                "off",
                "-r",
                str(requirements),
                "--format",
                "json",
                "--output",
                str(audit),
            ],
            allowed=(0, 1),
        )
        report = read_json(audit)
        require_python_audit(report, probe["components"])
        wheel_hashes = out / "release-wheel-hashes.json"
        write_json(
            wheel_hashes,
            {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts},
        )
        return finalize(
            out,
            "client",
            platform.machine(),
            probe["components"],
            probe["runtimes"],
            [sbom, requirements, audit, wheel_hashes, postgres_evidence],
            native_probe=native_probe,
            sbom=sbom,
        )


def npm_components(tree: dict) -> list[dict]:
    """Read installed production packages, including distinct nested versions."""
    result = {}
    try:
        if tree.get("problems"):
            raise ValueError
        for name, child in tree.get("dependencies", {}).items():
            version = child["version"]
            if not isinstance(version, str) or not version:
                raise ValueError
            result[(name, version)] = {
                "ecosystem": "npm",
                "name": name,
                "version": version,
            }
            for component in npm_components(child):
                result[(component["name"], component["version"])] = component
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ScanFailure("incomplete installed npm inventory") from None
    return list(result.values())


def collect_pi(out: Path) -> Path:
    # npm's SBOM implementation checks omitted development edges before filtering
    # them. Generate its production projection from a complete installation, then
    # prune and compare it with the actual remaining production dependency tree.
    # Every Node command stays inside the scoped shim tree.
    source = ROOT / "shims/pi"
    if run(["npm", "--version"], cwd=source).strip() != TOOLS["npm"]:
        raise ScanFailure("npm version does not match the reviewed generator pin")
    with tempfile.TemporaryDirectory(prefix=".security-", dir=source) as temporary:
        directory = Path(temporary)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(source / name, directory / name)
        run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=directory,
        )
        sbom = out / "pi.cdx.json"
        sbom.write_text(
            run(["npm", "sbom", "--omit=dev", "--sbom-format=cyclonedx"], cwd=directory)
        )
        run(
            [
                "npm",
                "prune",
                "--omit=dev",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ],
            cwd=directory,
        )
        installed = npm_components(
            json.loads(
                run(["npm", "ls", "--omit=dev", "--all", "--json"], cwd=directory)
            )
        )
        generated = set()
        for component in read_json(sbom).get("components", []):
            purl = component["purl"]
            if not purl.startswith("pkg:npm/"):
                raise ScanFailure("unexpected component in native npm inventory")
            name, version = purl.removeprefix("pkg:npm/").rsplit("@", 1)
            generated.add((unquote(name), unquote(version)))
        if generated != {(c["name"], c["version"]) for c in installed}:
            raise ScanFailure(
                "native npm SBOM differs from installed production dependencies"
            )
        audit = out / "pi.npm-audit.json"
        audit.write_text(
            run(["npm", "audit", "--omit=dev", "--json"], cwd=directory, allowed=(0, 1))
        )
        report = read_json(audit)
        if (
            report.get("error")
            or report.get("metadata", {}).get("vulnerabilities", {}).get("total") != 0
        ):
            raise ScanFailure(
                "production shim has vulnerabilities or incomplete audit coverage"
            )
        manifest = read_json(source / "package.json")
        components = [
            {
                "ecosystem": "npm",
                "name": manifest["name"],
                "version": manifest["version"],
            }
        ]
        components.extend(installed)
        version = run(["node", "--version"], cwd=directory).strip().removeprefix("v")
        if not re.fullmatch(r"(?:22|24)\.\d+\.\d+", version):
            raise ScanFailure("unsupported shim Node release line")
        return finalize(
            out,
            "pi",
            platform.machine(),
            components,
            {"node": version},
            [sbom, audit],
            sbom=sbom,
        )


def _json_url(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Sediment-maintenance-check"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def live_maintenance(components: list[dict], catalog: dict, out: Path) -> list[str]:
    """Verify public metadata only; no private source or captured data is sent."""
    items = {
        (c["ecosystem"], c["name"], c["version"])
        for c in components
        if c["ecosystem"] in {"pypi", "npm"}
        and c["name"] not in FIRST_PARTY | {"sediment-pi"}
    }
    repositories = {}
    evidence = []
    errors = []
    for ecosystem, name, version in sorted(items):
        entry = catalog["packages"].get(f"{ecosystem}/{name}", {})
        try:
            if ecosystem == "pypi":
                package = _json_url(
                    f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json"
                )["info"]
            else:
                package = _json_url(
                    f"https://registry.npmjs.org/{quote(name, safe='')}/{quote(version)}"
                )
                package["yanked"] = (
                    False  # npm withdrawal returns a failing HTTP request.
                )
            repository = None
            source = urlsplit(entry.get("source", ""))
            if source.hostname == "github.com":
                repository_name = "/".join(source.path.strip("/").split("/")[:2])
                if repository_name not in repositories:
                    repositories[repository_name] = json.loads(
                        run(["gh", "api", f"repos/{repository_name}"])
                    )
                repository = repositories[repository_name]
            problems = check_upstream(entry, package, repository)
            window = {"litellm": 4, "grpcio": 2, "grpcio-status": 2}.get(name)
            if ecosystem == "pypi" and (window or entry.get("support_window")):
                releases = _json_url(f"https://pypi.org/pypi/{quote(name)}/json")[
                    "releases"
                ]
                if window:
                    problems.extend(check_release_window(version, releases, window))
                if entry.get("support_window"):
                    problems.extend(check_latest_release(version, releases, entry))
            evidence.append(
                {
                    "ecosystem": ecosystem,
                    "name": name,
                    "version": version,
                    "source": entry.get("source"),
                    "yanked": package.get("yanked"),
                    "deprecated": package.get("deprecated"),
                    "archived": repository.get("archived") if repository else None,
                    "disabled": repository.get("disabled") if repository else None,
                    "errors": problems,
                }
            )
            errors.extend(f"{name} {version}: {problem}" for problem in problems)
        except (OSError, ValueError, KeyError, ScanFailure) as exc:
            errors.append(
                f"{name} {version}: upstream check failed ({type(exc).__name__})"
            )
    write_json(
        out,
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "packages": evidence,
            "errors": errors,
        },
    )
    return errors


def live_runtime_maintenance(runtimes: dict[str, str], out: Path) -> list[str]:
    """Require the latest released patch on a separately reviewed support line."""
    errors, evidence = [], []
    for name, installed in sorted(runtimes.items()):
        if name not in {
            "python",
            "node",
            "postgresql",
            "openssl",
            "openssl-cryptography",
            "libpq",
        }:
            continue
        try:
            version = tuple(map(int, installed.split(".")))
            upstream_name = {
                "openssl-cryptography": "openssl",
                "libpq": "postgresql",
            }.get(name, name)
            if upstream_name == "node":
                records = _json_url("https://nodejs.org/dist/index.json")
                candidates = [r["version"].removeprefix("v") for r in records]
                source = "https://nodejs.org/dist/index.json"
                width = 1
            else:
                repository, prefix, replacement = {
                    "python": ("python/cpython", f"v{version[0]}.{version[1]}.", "."),
                    "postgresql": ("postgres/postgres", f"REL_{version[0]}_", "_"),
                    "openssl": (
                        "openssl/openssl",
                        f"openssl-{version[0]}.{version[1]}.",
                        ".",
                    ),
                }[upstream_name]
                path = f"repos/{repository}/git/matching-refs/tags/{prefix}"
                records = json.loads(run(["gh", "api", path]))
                candidates = [
                    r["ref"]
                    .removeprefix("refs/tags/")
                    .removeprefix(
                        "REL_"
                        if upstream_name == "postgresql"
                        else "openssl-"
                        if upstream_name == "openssl"
                        else "v"
                    )
                    .replace(replacement, ".")
                    for r in records
                ]
                source = f"https://api.github.com/{path}"
                width = 1 if upstream_name == "postgresql" else 2
            stable = [
                tuple(map(int, v.split(".")))
                for v in candidates
                if re.fullmatch(r"\d+(?:\.\d+){1,2}", v)
            ]
            stable = [v for v in stable if v[:width] == version[:width]]
            latest = max(stable)
            evidence.append(
                {
                    "runtime": name,
                    "installed": installed,
                    "latest": ".".join(map(str, latest)),
                    "source": source,
                }
            )
            if version != latest:
                errors.append(
                    f"{name} {installed}: a later upstream patch is available"
                )
        except (OSError, ValueError, KeyError, TypeError, ScanFailure) as exc:
            errors.append(
                f"{name} {installed}: runtime release check failed ({type(exc).__name__})"
            )
    write_json(
        out,
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "runtimes": evidence,
            "errors": errors,
        },
    )
    return errors


def rescan_inventory(source: Path, out: Path, trivy: str) -> tuple[dict, Path]:
    """Use retained SBOM bytes; never rebuild or execute a historical release."""
    inventory = read_json(source)
    if check_inventory_hashes(source.parent, inventory):
        raise ScanFailure("retained inventory integrity failed")
    sbom_name = inventory["sbom"]
    if (
        not isinstance(sbom_name, str)
        or Path(sbom_name).name != sbom_name
        or sbom_name not in inventory["files"]
    ):
        raise ScanFailure("SBOM is not bound to the retained inventory")
    sbom = source.parent / sbom_name
    out.mkdir(parents=True, exist_ok=True)
    report = out / f"{inventory['artifact']}-{inventory['architecture']}.trivy.json"
    run(
        [
            trivy,
            "sbom",
            "--scanners",
            "vuln",
            "--list-all-pkgs",
            "--format",
            "json",
            "--output",
            str(report),
            str(sbom),
        ]
    )
    return inventory, report


def evaluate(
    inventory: dict,
    directory: Path,
    out: Path,
    catalog: dict,
    dispositions: list,
    *,
    rescan: Path | None = None,
    online: bool = True,
) -> list[str]:
    today = datetime.now(UTC).date()
    errors = check_inventory_hashes(directory, inventory)
    errors.extend(check_bundled_libraries(inventory, directory, catalog))
    errors.extend(
        check_maintenance(
            inventory["components"], inventory["runtimes"], catalog, today
        )
    )
    if today > date.fromisoformat(inventory["support"]["ends_on"]):
        errors.append("Sediment release support period has ended")
    reports = (
        [rescan]
        if rescan
        else [directory / p for p in inventory["files"] if p.endswith(".trivy.json")]
    )
    if not reports and inventory["artifact"] in {"api", "postgres", "gateway"}:
        errors.append("missing required scanner evidence for image")
    if not rescan and inventory["artifact"] == "client":
        audits = [
            directory / p for p in inventory["files"] if p.endswith(".pip-audit.json")
        ]
        if len(audits) != 1:
            errors.append("missing required scanner evidence for client")
        else:
            try:
                require_python_audit(read_json(audits[0]), inventory["components"])
            except ScanFailure as exc:
                errors.append(str(exc))
    if not rescan and inventory["artifact"] == "pi":
        audits = [
            directory / p for p in inventory["files"] if p.endswith(".npm-audit.json")
        ]
        if (
            len(audits) != 1
            or read_json(audits[0])
            .get("metadata", {})
            .get("vulnerabilities", {})
            .get("total")
            != 0
        ):
            errors.append(
                "missing required scanner evidence or vulnerabilities for shim"
            )
    for report in reports:
        errors.extend(
            check_vulnerabilities(
                read_json(report),
                inventory["artifact"],
                inventory["architecture"],
                dispositions,
                today,
                assurance=inventory.get("assurance"),
            )
        )
    if online:
        path = (
            out / f"{inventory['artifact']}-{inventory['architecture']}.upstream.json"
        )
        errors.extend(live_maintenance(inventory["components"], catalog, path))
        errors.extend(
            live_runtime_maintenance(
                inventory["runtimes"],
                out
                / f"{inventory['artifact']}-{inventory['architecture']}.runtime-upstream.json",
            )
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["image", "client", "pi", "rescan", "evaluate"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image")
    parser.add_argument("--artifact", choices=["api", "postgres", "gateway"])
    parser.add_argument("--architecture", choices=["arm64", "amd64"])
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--trivy", default="trivy")
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        catalog = read_json(ROOT / "security/maintenance.json")
        dispositions = read_json(ROOT / "security/dispositions.json")["dispositions"]
        if args.mode in {"image", "rescan"}:
            version = json.loads(run([args.trivy, "--version", "--format", "json"]))
            if version["Version"] != TOOLS["trivy"]:
                raise ScanFailure("Trivy version does not match the reviewed tool pin")
        if args.mode == "image":
            if not all((args.image, args.artifact, args.architecture)):
                raise ScanFailure("image, artifact, and architecture are required")
            source = collect_image(
                args.image, args.artifact, args.architecture, args.out, args.trivy
            )
        elif args.mode == "client":
            if not args.wheels:
                raise ScanFailure("release wheels are required")
            source = collect_client(args.wheels.resolve(), args.out)
        elif args.mode == "pi":
            source = collect_pi(args.out)
        else:
            if not args.inventory:
                raise ScanFailure("retained inventory is required")
            source = args.inventory.resolve()
        inventory = read_json(source)
        report = None
        if args.mode == "rescan":
            inventory, report = rescan_inventory(source, args.out, args.trivy)
        errors = evaluate(
            inventory, source.parent, args.out, catalog, dispositions, rescan=report
        )
        result = (
            args.out / f"{inventory['artifact']}-{inventory['architecture']}.gate.json"
        )
        write_json(
            result,
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "errors": errors,
                "maintenance_policy": catalog,
                "dispositions": dispositions,
            },
        )
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(
            f"security gates passed: {inventory['artifact']}/{inventory['architecture']}"
        )
        return 0
    except (ScanFailure, KeyError, ValueError, OSError) as exc:
        print(f"security gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
