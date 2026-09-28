# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sediment CLI — the top-level `cli/` workspace member.

Remote verbs speak HTTP through `.client`; operator verbs run where the
facts live (ADR 0001); `.attribution` is the stdlib-only stamper. The
version is single-sourced from ``sediment_api`` (the string ``/v1/me``
and ``/health`` report)."""

import re

from sediment_api import __version__ as __version__

# The release policy's two shapes, X.Y.Z and X.Y.ZrcN, in ASCII digits.
_RELEASE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?")
# A release candidate pins these at the same pre-release; uv admits a
# pre-release only when a direct requirement names it.
_FIRST_PARTY_DEPENDENCIES = (
    "sediment-api",
    "sediment-capture",
    "sediment-core",
    "sediment-derive",
    "sediment-export",
)


def install_command(version: str) -> str:
    """The installer's uv command pinned to *version*.

    A pinned install replaces the tool in either direction. `uv tool upgrade`
    keeps an earlier pin and otherwise moves only to the newest release.
    """
    command = f"uv tool install --python 3.12 --upgrade 'sediment-cli=={version}'"
    if re.fullmatch(r"[0-9.]+rc[0-9]+", version):
        command += "".join(
            f" --with '{name}=={version}'" for name in _FIRST_PARTY_DEPENDENCIES
        )
    return command


def version_skew_advice(server_version: object, client_version: str) -> str | None:
    """How to match the server's version, or None when it matches or is absent.

    The server controls *server_version*; only a release version reaches the
    copyable command, so a server can't put shell syntax or terminal control
    characters into it.
    """
    if server_version is None or server_version == client_version:
        return None
    if not isinstance(server_version, str) or not _RELEASE_VERSION.fullmatch(
        server_version
    ):
        return (
            f"server reported an unrecognized version; client is {client_version}. "
            f"Install your deployment's release: {install_command('<version>')}"
        )
    return (
        f"server version {server_version} differs from client {client_version}; "
        f"install the server's version: {install_command(server_version)}"
    )


def main(argv: list[str] | None = None) -> int:
    """Refuse unsupported capture installation before importing POSIX operators."""
    import os
    import sys

    args = sys.argv[1:] if argv is None else argv
    if os.name == "nt" and args[:1] == ["install"]:
        from .attribution import main as run
    else:
        from .cli import main as run
    return run(args)
