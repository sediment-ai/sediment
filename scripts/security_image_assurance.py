# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure the exact image and deployment predicates behind security dispositions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("packages", "apps", "cli", "litellm", "docker", "shims")
SOURCE_FILES = (
    "Dockerfile",
    "uv.lock",
    "pyproject.toml",
    ".env.example",
    "docker-compose.yml",
)
EXCLUDED = {
    "tests",
    "test",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "docs",
    ".git",
    "dist",
    "build",
}
PG_CAPS = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"}
HBA_TARGET = "/etc/postgresql/sediment-pg_hba.conf"
DEPLOYMENT_KEYS = (
    "database_isolated",
    "database_credentials_separated",
    "no_host_data_mounts",
    "resource_limits",
    "capabilities_confined",
    "read_only_roots",
    "scram_configured",
)


class AssuranceFailure(Exception):
    """A required observation could not be established."""


# zlib's convenience gzFile write API (CVE-2026-85091 requires a caller to
# reach gzprintf/gzvprintf, or the gzwrite/gzputc/gzputs/gzflush/gzsetparams
# paths that share its gz_write()/gz_vacate() implementation) on a
# non-blocking descriptor. The low-level deflate/inflate/checksum API is
# unaffected.
GZ_WRITE_API_SYMBOLS = frozenset(
    {
        "gzprintf",
        "gzvprintf",
        "gzwrite",
        "gzwrite64",
        "gzputc",
        "gzputs",
        "gzflush",
        "gzsetparams",
    }
)
# libxml2 is optionally built with its own bundled zlib for gzip-file I/O.
# These reviewed gateway extensions statically embed that API for SAML XML
# processing. Their Python callers require a separate exact-image review
# (docs/operate/security.md); symbol inspection cannot establish whether Python
# opens a gzip filename. Any other static embed is unreviewed and revokes the
# predicate.
REVIEWED_STATIC_GZ_WRITE_EMBEDS = frozenset(
    {
        "app/.venv/lib/python3.13/site-packages/lxml/etree.cpython-313-aarch64-linux-gnu.so",
        "app/.venv/lib/python3.13/site-packages/lxml/etree.cpython-313-x86_64-linux-gnu.so",
        "app/.venv/lib/python3.13/site-packages/lxml/objectify.cpython-313-aarch64-linux-gnu.so",
        "app/.venv/lib/python3.13/site-packages/lxml/objectify.cpython-313-x86_64-linux-gnu.so",
        "app/.venv/lib/python3.13/site-packages/xmlsec.cpython-313-aarch64-linux-gnu.so",
        "app/.venv/lib/python3.13/site-packages/xmlsec.cpython-313-x86_64-linux-gnu.so",
    }
)


def _elf_symbol_names(readelf_output: str, *, undefined_only: bool) -> set[str]:
    names: set[str] = set()
    for line in readelf_output.splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[3] != "FUNC":
            continue
        is_undefined = fields[6] == "UND"
        if is_undefined != undefined_only:
            continue
        names.add(fields[7].split("@", 1)[0])
    return names


def gzip_write_api_reachability(
    symbols: dict[str, tuple[set[str], set[str]]],
) -> dict[str, list[str]]:
    """Classify observed ELF files against zlib's non-blocking gzip write API.

    `symbols` maps each file's path (relative to the image root) to a pair of
    (undefined dynamic-import names, all defined symbol names) drawn from its
    ELF symbol tables.
    """
    dynamic_importers = sorted(
        path
        for path, (undefined, _) in symbols.items()
        if undefined & GZ_WRITE_API_SYMBOLS
    )
    unexpected_static_embeds = sorted(
        path
        for path, (_, defined) in symbols.items()
        if defined & GZ_WRITE_API_SYMBOLS
        and path not in REVIEWED_STATIC_GZ_WRITE_EMBEDS
        and not Path(path).name.startswith("libz.so")
    )
    return {
        "dynamic_importers": dynamic_importers,
        "unexpected_static_embeds": unexpected_static_embeds,
    }


def gzip_write_api_unreachable(image_id: str) -> bool:
    """Check native imports and static embeds of the vulnerable write API.

    Extracts the immutable image filesystem and inspects every ELF file's
    symbol tables with readelf: no file may dynamically import the gzip
    convenience write API from the system zlib, and no file outside the
    reviewed static-embed set may define it either. Python callers of the
    permitted static embeds require a separate manual review.
    """
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise AssuranceFailure("exact image identifier required")
    name = "sediment-assurance-elf-" + uuid4().hex
    try:
        run(
            ["docker", "create", "--name", name, "--network", "none", image_id],
            timeout=60,
        )
        with tempfile.TemporaryDirectory(prefix="sediment-assurance-elf-") as temporary:
            extracted = Path(temporary) / "rootfs"
            extracted.mkdir()
            try:
                export = subprocess.Popen(
                    ["docker", "export", name], stdout=subprocess.PIPE
                )
                tar = subprocess.run(
                    ["tar", "-x", "-C", str(extracted)],
                    stdin=export.stdout,
                    capture_output=True,
                    timeout=180,
                )
                if export.stdout is not None:
                    export.stdout.close()
                export.wait(timeout=60)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AssuranceFailure(
                    f"required observation failed ({type(error).__name__})"
                ) from None
            del tar  # Device-node extraction failures under an unprivileged runner are expected.
            symbols: dict[str, tuple[set[str], set[str]]] = {}
            for base, _, files in os.walk(extracted, followlinks=False):
                for filename in files:
                    path = Path(base) / filename
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        with path.open("rb") as handle:
                            if handle.read(4) != b"\x7fELF":
                                continue
                    except OSError:
                        continue
                    dyn = run(
                        ["readelf", "--dyn-syms", "-W", str(path)],
                        check=False,
                        timeout=30,
                    )
                    full = run(["readelf", "-sW", str(path)], check=False, timeout=30)
                    relative = path.relative_to(extracted).as_posix()
                    symbols[relative] = (
                        _elf_symbol_names(dyn, undefined_only=True),
                        _elf_symbol_names(full, undefined_only=False),
                    )
            if not symbols:
                raise AssuranceFailure("no ELF files were observed in the image")
    finally:
        run(["docker", "rm", "--force", "--volumes", name], check=False)
    classification = gzip_write_api_reachability(symbols)
    return (
        not classification["dynamic_importers"]
        and not classification["unexpected_static_embeds"]
    )


def source_digest(root: Path = ROOT) -> str:
    """Hash deployed source paths, content, and executable bits in stable order."""
    paths = {root / name for name in SOURCE_FILES}
    if not all(path.is_file() for path in paths):
        raise AssuranceFailure("required production source input is missing")
    for name in SOURCE_DIRS:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise AssuranceFailure("required production source directory is missing")
        for base, directories, files in os.walk(directory, followlinks=False):
            directories[:] = sorted(
                d
                for d in directories
                if d not in EXCLUDED and not d.endswith(".egg-info")
            )
            if any((Path(base) / d).is_symlink() for d in directories):
                raise AssuranceFailure("production source directory contains a symlink")
            for filename in files:
                path = Path(base) / filename
                if path.suffix.lower() in {".md", ".rst", ".pyc", ".pyo"}:
                    continue
                paths.add(path)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink() or not path.is_file():
            raise AssuranceFailure("production source must contain regular files")
        name = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(bytes([bool(path.stat().st_mode & 0o111)]))
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    check: bool = True,
) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, env=env, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AssuranceFailure(
            f"required observation failed ({type(error).__name__})"
        ) from None
    if check and result.returncode:
        raise AssuranceFailure(f"required observation exited {result.returncode}")
    return result.stdout


def _object(text: str) -> dict:
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise AssuranceFailure("required observation returned invalid JSON") from None


def render_deployment(root: Path = ROOT) -> dict:
    """Render the checked-in recipe with synthetic values and no ambient secrets."""
    values = {
        "POSTGRES_PASSWORD": "assurance-bootstrap-secret",
        "SEDIMENT_MIGRATOR_PASSWORD": "assurance-migrator-secret",
        "SEDIMENT_RUNTIME_PASSWORD": "assurance-runtime-secret",
        "SEDIMENT_OPERATOR_PASSWORD": "assurance-operator-db-secret",
        "SEDIMENT_OPERATOR_TOKEN": "assurance-operator-api-secret",
        "SEDIMENT_INGEST_TOKENS": '{"gateway":"assurance-gateway-ingest","capture":"assurance-capture-ingest"}',
        "SEDIMENT_GATEWAY_INGEST_TOKEN": "assurance-gateway-ingest",
        "SEDIMENT_API_BEARER_TOKEN": "assurance-legacy-ingest",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET": "assurance-webhook-secret",
        "SEDIMENT_ORG_ID": "assurance",
        "SEDIMENT_ALLOWED_CLONE_HOSTS": '["github.com"]',
        "SEDIMENT_DEV_MODE": "false",
        "SEDIMENT_ENABLE_DOCS": "false",
        "SEDIMENT_DELIVERY_DIR": "",
        "SEDIMENT_CAPTURE_DIR": "",
        "ANTHROPIC_API_KEY": "assurance-provider-secret",
        "LITELLM_MASTER_KEY": "sk-assurance-gateway-key",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "XDG_RUNTIME_DIR",
        }
    }
    with tempfile.TemporaryDirectory(prefix="sediment-assurance-env-") as temporary:
        env_file = Path(temporary) / "synthetic.env"
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items())
        )
        env_file.chmod(0o600)
        return _object(
            run(
                [
                    "docker",
                    "compose",
                    "--project-directory",
                    str(root),
                    "--env-file",
                    str(env_file),
                    "--profile",
                    "*",
                    "-f",
                    str(root / "docker-compose.yml"),
                    "config",
                    "--format",
                    "json",
                    "--no-env-resolution",
                ],
                env=environment,
            )
        )


def _positive(value: object) -> bool:
    return not isinstance(value, bool) and bool(
        re.fullmatch(
            r"(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)(?:[kmg]b?)?", str(value).lower()
        )
    )


def _database_target(value: object, user: str, password: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"postgresql", "postgresql+psycopg"}
            and parsed.hostname == "postgres"
            and parsed.port in {None, 5432}
            and parsed.path == "/sediment"
            and parsed.username == user
            and unquote(parsed.password or "") == password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def deployment_predicates(config: dict, root: Path = ROOT) -> dict[str, bool]:
    """Validate the fixed supported service topology, never an arbitrary deployment."""
    failed = dict.fromkeys(DEPLOYMENT_KEYS, False)
    try:
        services = config["services"]
        if set(services) != {
            "postgres",
            "migrate",
            "api",
            "operator",
            "gateway",
        } or not all(isinstance(s, dict) for s in services.values()):
            return failed
        pg, migration, api, operator, gateway = (
            services[n] for n in ("postgres", "migrate", "api", "operator", "gateway")
        )
        networks = config["networks"]
        isolated = (
            set(pg.get("networks", {})) == {"database"}
            and networks["database"].get("internal") is True
            and not networks["database"].get("external")
            and not pg.get("ports")
            and "database" not in gateway.get("networks", {})
            and all(
                "database" in services[n].get("networks", {})
                for n in ("api", "migrate", "operator")
            )
            and not any(s.get("network_mode") for s in services.values())
        )
        pg_env, migration_env, api_env, operator_env, gateway_env = (
            s.get("environment", {}) for s in (pg, migration, api, operator, gateway)
        )
        passwords = [
            pg_env.get("POSTGRES_PASSWORD"),
            *(
                migration_env.get(f"SEDIMENT_{role}_PASSWORD")
                for role in ("MIGRATOR", "RUNTIME", "OPERATOR")
            ),
        ]
        separated = (
            all(isinstance(p, str) and p for p in passwords)
            and len(set(passwords)) == 4
        )
        separated = (
            separated
            and pg_env.get("POSTGRES_USER") == "sediment"
            and pg_env.get("POSTGRES_DB") == "sediment"
            and migration.get("command") == ["sediment", "db", "provision"]
        )
        separated = (
            separated
            and _database_target(
                migration_env.get("SEDIMENT_BOOTSTRAP_DATABASE_URL"),
                "sediment",
                passwords[0],
            )
            and _database_target(
                api_env.get("SEDIMENT_DATABASE_URL"), "sediment_runtime", passwords[2]
            )
            and _database_target(
                operator_env.get("SEDIMENT_DATABASE_URL"),
                "sediment_operator",
                passwords[3],
            )
        )
        allowed = (
            {"postgres", "migrate"},
            {"migrate"},
            {"migrate", "api"},
            {"migrate", "operator"},
        )
        for secret, consumers in zip(passwords, allowed, strict=True):
            if secret and any(
                secret in unquote(str(s))
                for name, s in services.items()
                if name not in consumers
            ):
                separated = False
        token = api_env.get("SEDIMENT_OPERATOR_TOKEN")
        ingest = json.loads(api_env.get("SEDIMENT_INGEST_TOKENS", "{}"))
        separated = (
            separated
            and isinstance(token, str)
            and bool(token)
            and isinstance(ingest, dict)
            and gateway_env.get("SEDIMENT_API_BEARER_TOKEN") in ingest.values()
            and token not in ingest.values()
            and token != api_env.get("SEDIMENT_API_BEARER_TOKEN")
            and all(token not in str(s) for n, s in services.items() if n != "api")
            and str(api_env.get("SEDIMENT_DEV_MODE")).lower() == "false"
        )
        bind_paths = {
            "postgres": {
                (str((root / "docker/postgres/pg_hba.conf").resolve()), HBA_TARGET)
            },
            "gateway": {
                (str((root / "litellm/config.yaml").resolve()), "/app/config.yaml"),
                (
                    str((root / "litellm/sediment_callback.py").resolve()),
                    "/app/sediment_callback.py",
                ),
                (
                    str((root / "cli/sediment_cli/delivery.py").resolve()),
                    "/app/sediment_delivery.py",
                ),
            },
        }
        named_paths = {
            "postgres": {("sediment-postgres", "/var/lib/postgresql/data")},
            "api": {("sediment-mirror", "/data/mirror")},
            "operator": {
                ("sediment-mirror", "/data/mirror"),
                ("sediment-export", "/data/export"),
                ("sediment-staging", "/data/staging"),
            },
            "gateway": {("sediment-delivery", "/data/delivery")},
        }
        mounts = not any(
            any(
                s.get(key) for key in ("env_file", "secrets", "configs", "volumes_from")
            )
            for s in services.values()
        )
        volumes = config.get("volumes", {})
        mounts = mounts and all(
            not (v or {}).get("external") and not (v or {}).get("driver_opts")
            for v in volumes.values()
        )
        for name, service in services.items():
            for mount in service.get("volumes", []):
                source, target = mount.get("source"), mount.get("target")
                if mount.get("type") == "bind":
                    permitted = mount.get("read_only") is True and (
                        str(Path(source).resolve()),
                        target,
                    ) in bind_paths.get(name, set())
                else:
                    permitted = (
                        mount.get("type") == "volume"
                        and source in volumes
                        and (source, target) in named_paths.get(name, set())
                    )
                mounts = mounts and permitted
        resources = all(
            _positive(s.get("mem_limit"))
            and _positive(s.get("cpus"))
            and _positive(s.get("pids_limit"))
            and s.get("logging", {}).get("driver") in {"local", "json-file"}
            and _positive(s.get("logging", {}).get("options", {}).get("max-size"))
            and _positive(s.get("logging", {}).get("options", {}).get("max-file"))
            for s in services.values()
        )
        capabilities = all(
            set(s.get("cap_drop", [])) == {"ALL"}
            and set(s.get("cap_add", [])) == (PG_CAPS if name == "postgres" else set())
            and bool(s.get("security_opt"))
            and set(s["security_opt"])
            <= {"no-new-privileges:true", "no-new-privileges"}
            and not any(
                s.get(key) for key in ("privileged", "devices", "device_cgroup_rules")
            )
            and not s.get("user")
            and s.get("pid") != "host"
            and s.get("ipc") != "host"
            for name, s in services.items()
        )
        scram = f"hba_file={HBA_TARGET}" in pg.get("command", []) and any(
            str(Path(m.get("source", "")).resolve())
            == str((root / "docker/postgres/pg_hba.conf").resolve())
            and m.get("target") == HBA_TARGET
            and m.get("read_only") is True
            for m in pg.get("volumes", [])
        )
        return dict(
            zip(
                DEPLOYMENT_KEYS,
                (
                    bool(isolated),
                    bool(separated),
                    bool(mounts),
                    bool(resources),
                    bool(capabilities),
                    all(s.get("read_only") is True for s in services.values()),
                    bool(scram),
                ),
                strict=True,
            )
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return failed


# Fixed shell programs inspect only the owned container. No caller text enters shell code.
FILESYSTEM_PROBE = r"""
set -eu
printf '{'
find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -print > /tmp/suid
if [ ! -s /tmp/suid ]; then printf '"no_suid_sgid":true'; else printf '"no_suid_sgid":false'; fi
absent() { key="$1"; shift; matches=$(find / -xdev "$@" -print); if [ -z "$matches" ]; then printf ',"%s":true' "$key"; else printf ',"%s":false' "$key"; fi; }
absent infocmp_absent -name infocmp
absent systemd_homed_absent -name systemd-homed
absent minizip_absent -iname '*minizip*'
absent libxml_python_bindings_absent \( -name 'libxml2.py*' -o -name 'libxml2mod*' \)
absent curl_cli_absent -type f -name curl
fstab=""; if [ -e /etc/fstab ]; then fstab=$(cat /etc/fstab); fi
if printf '%s\n' "$fstab" | grep -Eq '^[[:space:]]*[^#[:space:]]'; then printf ',"empty_fstab":false'; else printf ',"empty_fstab":true'; fi
if command -v perl >/dev/null 2>&1 && [ "$(perl -MConfig -e 'print join q{:}, @Config{qw(ptrsize sizesize ivsize)}')" = '8:8:8' ]; then printf ',"perl_64bit":true'; else printf ',"perl_64bit":false'; fi
printf '}\n'
"""
USER_PROBE = r"""
set -eu
printf '{"uid":%s,"git_default_config":' "$(id -u)"
if command -v git >/dev/null 2>&1 && config=$(git config --list) && [ -z "$config" ]; then printf true; else printf false; fi
printf '}\n'
"""


def _container_probe(image_id: str, script: str, *, root_user: bool = False) -> dict:
    # Read-search permits a complete file inventory through private directories.
    # It is exclusive to this networkless, read-only inventory container.
    name = "sediment-assurance-" + uuid4().hex
    try:
        run(
            [
                "docker",
                "create",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--pids-limit",
                "32",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=16m,mode=1777",
                *(
                    ["--user", "0:0", "--cap-add", "DAC_READ_SEARCH"]
                    if root_user
                    else []
                ),
                "--workdir",
                "/",
                "--entrypoint",
                "/bin/sh",
                image_id,
                "-ec",
                script,
            ]
        )
        return _object(run(["docker", "start", "--attach", name]))
    finally:
        run(["docker", "rm", "--force", "--volumes", name], check=False)


def probe_image(image_id: str, artifact: str, architecture: str) -> dict:
    if (
        not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
        or artifact not in {"api", "postgres", "gateway"}
        or architecture not in {"amd64", "arm64"}
    ):
        raise AssuranceFailure(
            "exact image identifier, artifact, and architecture required"
        )
    try:
        image = json.loads(run(["docker", "image", "inspect", image_id]))[0]
        if image["Id"] != image_id or image["Architecture"] != architecture:
            raise ValueError
    except (ValueError, KeyError, TypeError, IndexError):
        raise AssuranceFailure(
            "image identity or architecture does not match"
        ) from None
    filesystem = _container_probe(image_id, FILESYSTEM_PROBE, root_user=True)
    user = _container_probe(image_id, USER_PROBE)
    expected = {
        "no_suid_sgid",
        "infocmp_absent",
        "systemd_homed_absent",
        "minizip_absent",
        "libxml_python_bindings_absent",
        "curl_cli_absent",
        "empty_fstab",
        "perl_64bit",
    }
    if (
        set(filesystem) != expected
        or any(type(v) is not bool for v in filesystem.values())
        or type(user.get("uid")) is not int
        or type(user.get("git_default_config")) is not bool
    ):
        raise AssuranceFailure("native image predicate evidence is incomplete")
    return {
        "image_id": image_id,
        "architecture": architecture,
        "uid": user["uid"],
        "labels": {
            key: (image.get("Config", {}).get("Labels") or {}).get(key)
            for key in (
                "org.opencontainers.image.revision",
                "io.sediment.source-digest",
            )
        },
        "predicates": {
            **filesystem,
            "git_default_config": user["git_default_config"],
            "nonroot_user": user["uid"] != 0,
            "gzip_write_api_unreachable": gzip_write_api_unreachable(image_id),
        },
    }


def probe_postgres(image_id: str, root: Path = ROOT) -> dict:
    """Boot an owned ephemeral database with SCRAM, no network, and bounded resources."""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise AssuranceFailure("exact PostgreSQL image identifier required")
    hba = root / "docker/postgres/pg_hba.conf"
    if not hba.is_file() or hba.is_symlink():
        raise AssuranceFailure("checked PostgreSQL authentication file is missing")
    name = "sediment-assurance-pg-" + uuid4().hex
    password = "assurance-" + uuid4().hex
    try:
        run(
            [
                "docker",
                "create",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                *[arg for cap in sorted(PG_CAPS) for arg in ("--cap-add", cap)],
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "1g",
                "--cpus",
                "1",
                "--pids-limit",
                "256",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,nosuid,nodev,size=512m",
                "--tmpfs",
                "/var/run/postgresql:rw,nosuid,nodev,size=16m,uid=999,gid=999,mode=0775",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs",
                "/etc/postgresql:rw,nosuid,nodev,size=1m,mode=0755",
                "--entrypoint",
                "/bin/sh",
                "-e",
                "SEDIMENT_ASSURANCE_HBA=" + hba.read_text(),
                "-e",
                "POSTGRES_USER=sediment",
                "-e",
                "POSTGRES_DB=sediment",
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-e",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256",
                image_id,
                "-ec",
                'printf "%s\n" "$SEDIMENT_ASSURANCE_HBA" > /etc/postgresql/sediment-pg_hba.conf; '
                'unset SEDIMENT_ASSURANCE_HBA; exec docker-entrypoint.sh "$@"',
                "assurance",
                "postgres",
                "-c",
                f"hba_file={HBA_TARGET}",
                "-c",
                "max_connections=40",
            ]
        )
        run(["docker", "start", name])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = _object(
                run(["docker", "inspect", "--format", "{{json .State}}", name])
            )
            if not state.get("Running"):
                raise AssuranceFailure(
                    "PostgreSQL assurance container stopped during initialization"
                )
            ready = run(
                [
                    "docker",
                    "exec",
                    "-e",
                    f"PGPASSWORD={password}",
                    name,
                    "psql",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "sediment",
                    "-d",
                    "sediment",
                    "-Atqc",
                    "select json_build_object('scram_only', count(*) = 3 and bool_and(auth_method = 'scram-sha-256' and error is null)) from pg_hba_file_rules",
                ],
                check=False,
                timeout=5,
            )
            if ready.strip().startswith("{"):
                result = _object(ready)
                result["postgres_uid"] = int(
                    run(["docker", "exec", name, "stat", "-c", "%u", "/proc/1"]).strip()
                )
                result["hba_sha256"] = hashlib.sha256(hba.read_bytes()).hexdigest()
                return result
            time.sleep(0.5)
        raise AssuranceFailure("PostgreSQL assurance readiness deadline exceeded")
    finally:
        run(["docker", "rm", "--force", "--volumes", name], check=False)


def collect_assurance(
    image_id: str, artifact: str, architecture: str, out: Path, *, root: Path = ROOT
) -> dict:
    observed = probe_image(image_id, artifact, architecture)
    deployment = render_deployment(root)
    predicates = {**observed["predicates"], **deployment_predicates(deployment, root)}
    postgres = probe_postgres(image_id, root) if artifact == "postgres" else None
    predicates["scram_only"] = bool(
        postgres
        and postgres.get("scram_only") is True
        and postgres.get("postgres_uid") == 999
        and predicates["scram_configured"]
    )
    record = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "artifact": artifact,
        "architecture": architecture,
        "image_id": image_id,
        "source_digest": source_digest(root),
        "predicates": predicates,
        "image": observed,
        "postgres": postgres,
        "deployment": deployment,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{artifact}-{architecture}.assurance.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n"
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("source-digest", help="print the deployed production source digest")
    collect = sub.add_parser(
        "collect", help="measure an exact image and checked-in deployment"
    )
    collect.add_argument("image_id")
    collect.add_argument("artifact", choices=("api", "postgres", "gateway"))
    collect.add_argument("architecture", choices=("amd64", "arm64"))
    collect.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "source-digest":
            print(source_digest())
        else:
            collect_assurance(args.image_id, args.artifact, args.architecture, args.out)
    except AssuranceFailure as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
