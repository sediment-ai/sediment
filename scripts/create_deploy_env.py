# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create private, separate deployment credentials without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def create_environment(path: Path, *, ingest_clients: tuple[str, ...] = ()) -> None:
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
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise PermissionError(
            "environment directory must be owned by you and not writable by others"
        )
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
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        create_environment(args.output, ingest_clients=tuple(args.ingest_client))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot create private environment file: {exc}\n")
    print(f"Created private environment file: {args.output}")
    print("Set SEDIMENT_ORG_ID and review SEDIMENT_ALLOWED_CLONE_HOSTS before startup.")
    print(
        "Share only each client's entry in SEDIMENT_INGEST_TOKENS, never the whole file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
