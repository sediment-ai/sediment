# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bundle release security evidence and rescan retained assets as data only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    (a, arch) for a in ("api", "postgres", "gateway") for arch in ("amd64", "arm64")
} | {("client", "portable"), ("pi", "portable")}
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_RELEASE_BYTES = 2 * 1024 * 1024 * 1024


class ReleaseFailure(Exception):
    """A release lacks verifiable, complete security evidence."""


def run(command: list[str], *, stdout=None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseFailure(
            f"release operation failed ({type(exc).__name__})"
        ) from None
    if result.returncode:
        sys.stderr.write(result.stderr.decode(errors="replace")[-8192:])
        raise ReleaseFailure(f"{Path(command[0]).name} exit {result.returncode}")
    return result.stdout.decode() if result.stdout is not None else ""


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError):
        raise ReleaseFailure(f"invalid release evidence: {path.name}") from None


def filename(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name
    ):
        raise ReleaseFailure("release asset has an unsafe filename")
    return name


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseFailure(f"missing regular release asset: {path.name}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory_set(directory: Path, commit: str) -> list[str]:
    found = set()
    names = []
    for path in sorted(directory.glob("*.inventory.json")):
        inventory = read_json(path)
        try:
            artifact, arch = inventory["artifact"], inventory["architecture"]
            key = (artifact, "portable" if artifact in {"client", "pi"} else arch)
            if (
                key not in EXPECTED
                or key in found
                or inventory["source_commit"] != commit
            ):
                raise ValueError
            if inventory["sbom"] not in inventory["files"] or not inventory["files"]:
                raise ValueError
            for name, expected in inventory["files"].items():
                if digest(directory / filename(name)) != expected:
                    raise ValueError
            gate = read_json(
                directory / f"{filename(artifact)}-{filename(arch)}.gate.json"
            )
            if gate.get("errors") != []:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ReleaseFailure(
                f"incomplete or failed inventory: {path.name}"
            ) from None
        found.add(key)
        names.append(path.name)
    if found != EXPECTED:
        raise ReleaseFailure(
            "release must contain all eight artifact/platform inventories"
        )
    return names


def bundle(directory: Path, tag: str, commit: str, *, today: date) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseFailure("release commit must be a full Git SHA")
    inventories = inventory_set(directory, commit)
    wheel_hashes = read_json(directory / "release-wheel-hashes.json")
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 6 or wheel_hashes != {p.name: digest(p) for p in wheels}:
        raise ReleaseFailure("published wheels differ from the scanned installation")
    static = directory / "source.semgrep.json"
    report = read_json(static)
    scanner = read_json(directory / "source.static-metadata.json")
    if (
        report.get("results") != []
        or report.get("errors") != []
        or not report.get("paths", {}).get("scanned")
        or scanner.get("scanner") != "semgrep"
        or scanner.get("version") != "1.177.0"
        or scanner.get("report_sha256") != digest(static)
    ):
        raise ReleaseFailure("release static evidence is incomplete or has findings")
    support = {
        "owner": "Sediment maintainers",
        "starts_on": today.isoformat(),
        "ends_on": (today + timedelta(days=90)).isoformat(),
    }
    # Avoid extending an inventory's original support period during a rerun.
    if any(
        date.fromisoformat(read_json(directory / name)["support"]["ends_on"])
        < date.fromisoformat(support["ends_on"])
        for name in inventories
    ):
        raise ReleaseFailure("inventory support ends before the release support period")
    metadata = {
        "schema_version": 1,
        "tag": tag,
        "source_commit": commit,
        "support": support,
        "inventories": inventories,
    }
    (directory / "security-support.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    lines = [
        f"{digest(path)}  {filename(path.name)}\n"
        for path in sorted(directory.iterdir())
        if path.name != "SHA256SUMS"
    ]
    (directory / "SHA256SUMS").write_text("".join(lines))


def verify_release(directory: Path, tag: str, *, today: date) -> list[Path]:
    try:
        entries = {}
        for line in (directory / "SHA256SUMS").read_text().splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if match is None:
                raise ReleaseFailure("malformed release checksum file")
            expected, name = match.groups()
            filename(name)
            if name in entries or name == "SHA256SUMS":
                raise ReleaseFailure("duplicate or recursive release checksum")
            if digest(directory / name) != expected:
                raise ReleaseFailure(f"release checksum mismatch: {name}")
            entries[name] = expected
        actual = {p.name for p in directory.iterdir()} - {"SHA256SUMS"}
        if set(entries) != actual or "security-support.json" not in entries:
            raise ReleaseFailure("release checksum coverage is incomplete")
        metadata = read_json(directory / "security-support.json")
        start = date.fromisoformat(metadata["support"]["starts_on"])
        end = date.fromisoformat(metadata["support"]["ends_on"])
        if (
            metadata["schema_version"] != 1
            or metadata["tag"] != tag
            or end - start != timedelta(days=90)
            or not start <= today <= end
        ):
            raise ReleaseFailure("release support metadata is invalid or expired")
        names = inventory_set(directory, metadata["source_commit"])
        if metadata["inventories"] != names or not set(names) <= set(entries):
            raise ReleaseFailure("release inventory manifest is incomplete")
        return [directory / name for name in names]
    except (OSError, KeyError, ValueError, TypeError):
        raise ReleaseFailure("missing or malformed retained release evidence") from None


def list_releases(repository: str) -> list[dict]:
    pages = json.loads(
        run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/releases?per_page=100",
            ]
        )
    )
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ReleaseFailure("invalid GitHub release listing")
    return [release for page in pages for release in page]


def download_release(repository: str, release: dict, directory: Path) -> None:
    assets = release["assets"]
    names = [filename(asset["name"]) for asset in assets]
    if len(set(names)) != len(names) or not {
        "SHA256SUMS",
        "security-support.json",
    } <= set(names):
        raise ReleaseFailure("supported release is missing security assets")
    if (
        any(not 0 <= asset["size"] <= MAX_ASSET_BYTES for asset in assets)
        or sum(asset["size"] for asset in assets) > MAX_RELEASE_BYTES
    ):
        raise ReleaseFailure("release assets exceed download limit")
    for asset in assets:
        if type(asset["id"]) is not int or asset["id"] <= 0:
            raise ReleaseFailure("invalid release asset identity")
        target = directory / asset["name"]
        with target.open("xb") as stream:
            run(
                [
                    "gh",
                    "api",
                    f"repos/{repository}/releases/assets/{asset['id']}",
                    "-H",
                    "Accept: application/octet-stream",
                ],
                stdout=stream,
            )
        if target.stat().st_size != asset["size"]:
            raise ReleaseFailure("release asset download is incomplete")


def scan(repository: str, out: Path, trivy: str, *, today: date) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseFailure("invalid repository name")
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "checked_on": today.isoformat(),
        "supported": [],
        "outside_support": [],
        "failures": [],
    }
    for release in list_releases(repository):
        if release.get("draft"):
            continue
        tag = release.get("tag_name", "<missing tag>")
        try:
            published = datetime.fromisoformat(release["published_at"]).date()
            if today > published + timedelta(days=90):
                result["outside_support"].append(
                    {"tag": tag, "published_on": published.isoformat()}
                )
                continue
            if published > today or type(release["id"]) is not int:
                raise ReleaseFailure("invalid release metadata")
            directory = out / f"release-{release['id']}"
            directory.mkdir()
            download_release(repository, release, directory)
            inventories = verify_release(directory, tag, today=today)
            errors = []
            for inventory in inventories:
                try:
                    run(
                        [
                            sys.executable,
                            str(ROOT / "scripts/security_scan.py"),
                            "rescan",
                            "--inventory",
                            str(inventory.resolve()),
                            "--out",
                            str((out / f"rescan-{release['id']}").resolve()),
                            "--trivy",
                            trivy,
                        ]
                    )
                except ReleaseFailure as exc:
                    errors.append(f"{inventory.name}: {exc}")
            if errors:
                raise ReleaseFailure("; ".join(errors))
            result["supported"].append({"tag": tag, "inventories": len(inventories)})
        except (ReleaseFailure, KeyError, ValueError, TypeError, OSError) as exc:
            result["failures"].append({"tag": tag, "error": str(exc)})
    (out / "retained-releases.json").write_text(json.dumps(result, indent=2) + "\n")
    return int(bool(result["failures"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    package = subparsers.add_parser("bundle")
    package.add_argument("--directory", type=Path, required=True)
    package.add_argument("--tag", required=True)
    package.add_argument("--commit", required=True)
    rescan = subparsers.add_parser("scan")
    rescan.add_argument("--repository", required=True)
    rescan.add_argument("--out", type=Path, required=True)
    rescan.add_argument("--trivy", default="trivy")
    args = parser.parse_args()
    try:
        today = datetime.now(UTC).date()
        if args.mode == "bundle":
            bundle(args.directory, args.tag, args.commit, today=today)
            return 0
        return scan(args.repository, args.out, args.trivy, today=today)
    except (ReleaseFailure, OSError, KeyError, ValueError, TypeError) as exc:
        print(f"release security gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
