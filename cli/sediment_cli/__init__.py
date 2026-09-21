# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sediment CLI — the top-level `cli/` workspace member.

Remote verbs speak HTTP through `.client`; operator verbs run where the
facts live (ADR 0001); `.attribution` is the stdlib-only stamper. The
version is single-sourced from ``sediment_api`` (the string ``/v1/me``
and ``/health`` report)."""

from sediment_api import __version__ as __version__


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
