# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create private, separate deployment credentials without printing secrets."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate_domain(domain: str) -> str:
    """Accept a DNS hostname, never a URL, IP address, or proxy rule."""
    labels = domain.split(".")
    if (
        len(domain) > 253
        or len(labels) < 2
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", labels[-1])
        or labels[-1].lower() in {"local", "localhost", "internal"}
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        )
    ):
        raise ValueError(
            "domain must be a public DNS hostname without a scheme or path"
        )
    return domain.lower()


def create_environment(
    path: Path,
    *,
    domain: str | None = None,
    email: str = "",
    provider_key: str = "",
    ingest_clients: tuple[str, ...] = (),
) -> None:
    """Create a 0600 environment file, refusing existing paths and shared writers."""
    # Keep this bootstrap script stdlib-only. The Settings contract test checks
    # that these names and generated secrets satisfy the server's authority rules.
    seen = {"gateway", "operator", "legacy", "retrieval"}
    for client in ingest_clients:
        if (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", client)
            or client in seen
        ):
            raise ValueError(
                "ingest client names must be unique, unreserved, and 1–64 ASCII "
                "letters, digits, dots, underscores, or hyphens, starting with "
                "a letter or digit (gateway is generated automatically)"
            )
        seen.add(client)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid():
        raise PermissionError("environment directory must be owned by you")
    if parent.st_mode & 0o022:
        raise PermissionError(
            "environment directory is writable by others; run chmod go-w "
            + shlex.quote(str(path.parent))
            + " before retrying"
        )
    pilot = {}
    if domain is not None:
        domain = validate_domain(domain)
        if not re.fullmatch(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+", email):
            raise ValueError("provide a certificate contact email address")
        validate_domain(email.rsplit("@", 1)[1])
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", provider_key):
            raise ValueError("provide an Anthropic API key without whitespace")
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                stderr=subprocess.PIPE,
                text=True,
            ).strip()
            digest = subprocess.check_output(
                [
                    sys.executable,
                    str(ROOT / "scripts/security_image_assurance.py"),
                    "source-digest",
                ],
                cwd=ROOT,
                stderr=subprocess.PIPE,
                text=True,
            ).strip()
        except subprocess.CalledProcessError:
            raise ValueError(
                "cannot identify deployment source; use a Git checkout"
            ) from None
        pilot = {
            "COMPOSE_PROFILES": "https",
            "SEDIMENT_DOMAIN": domain,
            "SEDIMENT_ACME_EMAIL": email,
            "ANTHROPIC_API_KEY": provider_key,
            "SEDIMENT_SOURCE_REVISION": revision,
            "SEDIMENT_SOURCE_DIGEST": digest,
        }
    values = {
        name: secrets.token_hex(32)
        for name in (
            "POSTGRES_PASSWORD",
            "SEDIMENT_MIGRATOR_PASSWORD",
            "SEDIMENT_RUNTIME_PASSWORD",
            "SEDIMENT_OPERATOR_PASSWORD",
            "SEDIMENT_OPERATOR_TOKEN",
            "SEDIMENT_GATEWAY_INGEST_TOKEN",
            "SEDIMENT_GITHUB_WEBHOOK_SECRET",
        )
    }
    values["LITELLM_MASTER_KEY"] = "sk-" + secrets.token_hex(32)
    values["SEDIMENT_INGEST_TOKENS"] = json.dumps(
        {
            "gateway": values["SEDIMENT_GATEWAY_INGEST_TOKEN"],
            **{client: secrets.token_hex(32) for client in ingest_clients},
        },
        separators=(",", ":"),
    )
    values.update(pilot)
    content = (ROOT / ".env.example").read_text()
    for name, value in values.items():
        content, count = re.subn(
            rf"^{name}=.*$", lambda _: f"{name}={value}", content, flags=re.MULTILINE
        )
        if count != 1:
            raise ValueError(
                f"environment template must contain exactly one {name} setting"
            )
    fd = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument(
        "--ingest-client",
        action="append",
        default=[],
        metavar="NAME",
        help="generate a separate ingest-only token for this client; repeat per client",
    )
    parser.add_argument("--domain", help="public hostname; enables LiteLLM and HTTPS")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        email = key = ""
        if args.domain is not None:
            args.domain = validate_domain(args.domain)
            if args.output.exists() or args.output.is_symlink():
                raise FileExistsError(
                    "environment file already exists; keep its credentials"
                )
            email = os.environ.get("SEDIMENT_ACME_EMAIL", "")
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not email or not key:
                if not sys.stdin.isatty():
                    raise ValueError(
                        "use an interactive terminal or set SEDIMENT_ACME_EMAIL "
                        "and ANTHROPIC_API_KEY in the process environment"
                    )
                if not email:
                    email = input("Certificate contact email: ").strip()
                if not key:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        key = getpass.getpass("Anthropic API key (hidden): ")
        create_environment(
            args.output,
            domain=args.domain,
            email=email,
            provider_key=key,
            ingest_clients=tuple(args.ingest_client),
        )
    except (OSError, ValueError, EOFError, getpass.GetPassWarning) as exc:
        parser.exit(1, f"Cannot create private environment file: {exc}\n")
    print(f"Created private environment file: {args.output}")
    if args.domain:
        print("Point the hostname at the host and allow inbound TCP ports 80 and 443.")
        env_option = (
            ""
            if args.output == Path(".env")
            else " --env-file " + shlex.quote(str(args.output))
        )
        print(f"Start: docker compose{env_option} up -d --build --wait")
        print(f"Verify HTTPS: https://{args.domain}/health")
        print(f"Gateway base URL: https://{args.domain}/llm")
    else:
        print(
            "Set the deployment organization and optional Anthropic provider key before startup."
        )
    print(
        "Share only each client's entry in SEDIMENT_INGEST_TOKENS, never the whole file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
