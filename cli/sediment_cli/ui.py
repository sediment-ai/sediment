# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Terminal styling for the operator CLI, in the Sediment brand palette.

Stdlib-only ANSI. Styling is gated per stream: on only for a TTY, never
under ``NO_COLOR`` or ``TERM=dumb`` — piped output, tests, and CI read
exactly the bytes they read before this module existed. Truecolor when
``COLORTERM`` advertises it, 256-color approximations otherwise.

The palette is the site design system (the ``--sds-*`` tokens): sandstone
is the one accent, phosphor means success, iron oxide means failure,
bleached is for headings, and hierarchy below that is dim — never a fourth
color. Flat color only (the brand allows no gradients), so the strata mark
is three flat bands, light to dark.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

# name -> (truecolor RGB, 256-color fallback), from the brand kit.
_PALETTE = {
    "sandstone": ((196, 147, 90), 173),  # --sds-sandstone · primary accent
    "phosphor": ((95, 216, 149), 78),  # positive / success
    "iron-oxide": ((184, 73, 43), 130),  # negative / errors
    "bleached": ((240, 237, 232), 255),  # --sds-bleached · headings
    "ochre": ((139, 115, 85), 95),  # --sds-ochre · muted
}
_ATTRS = {"bold": "\x1b[1m", "dim": "\x1b[2m"}
_RESET = "\x1b[0m"


def on(stream: TextIO | None = None) -> bool:
    """Whether *stream* (default stdout) gets styled output."""
    stream = sys.stdout if stream is None else stream
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def _fg(name: str) -> str:
    rgb, fallback = _PALETTE[name]
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return "\x1b[38;2;%d;%d;%dm" % rgb
    return f"\x1b[38;5;{fallback}m"


def style(text: str, *names: str, stream: TextIO | None = None) -> str:
    """*text* wrapped in the named palette colors / attrs, unchanged when the
    stream is not a styled TTY. Pad before styling — ANSI codes break
    ``str.format`` field widths."""
    if not on(stream):
        return text
    codes = "".join(_ATTRS.get(n) or _fg(n) for n in names)
    return f"{codes}{text}{_RESET}"


def glyph(char: str, name: str, stream: TextIO | None = None) -> str:
    """A styled ``✓ ``-style prefix on a styled TTY, ``""`` otherwise, so
    plain output keeps its exact historical bytes."""
    return f"{style(char, name, stream=stream)} " if on(stream) else ""


def error_line(msg: str) -> str:
    """The CLI's one stderr error shape: ``✗ error: <msg>`` styled on a
    TTY, exactly ``error: <msg>`` piped — every verb routes through this so
    the vocabulary and the bytes stay uniform."""
    err = sys.stderr
    label = glyph("✗", "iron-oxide", err) + style("error:", "iron-oxide", stream=err)
    return f"{label} {msg}"


def warn_line(msg: str) -> str:
    """The matching stderr warning shape: ``⚠ warning: <msg>`` styled on a
    TTY, exactly ``warning: <msg>`` piped."""
    err = sys.stderr
    label = glyph("⚠", "sandstone", err) + style("warning:", "sandstone", stream=err)
    return f"{label} {msg}"


# Listing sections of help output whose first cell (command / flag) gets the
# accent; USAGE bodies and description text stay uncolored.
_HELP_LISTINGS = frozenset(
    {"COMMANDS:", "FORMATS:", "OPTIONS:", "ARGUMENTS:", "REPORTS:"}
)


def style_help(text: str, stream: TextIO | None = None) -> str:
    """TTY-gated color pass over already-formatted help text: bold
    headings, accent on the invocation cell of listing rows. Plain text
    passes through untouched, so goldens and pipes never see codes."""
    if not on(stream):
        return text
    out: list[str] = []
    listing = False
    for line in text.splitlines():
        if line and not line.startswith(" ") and line == line.upper():
            listing = line.endswith(":") and line in _HELP_LISTINGS
            out.append(style(line, "bleached", "bold", stream=stream))
        elif listing and line.startswith("  ") and len(line) > 2 and line[2] != " ":
            cell, gap, rest = line[2:].partition("  ")
            out.append("  " + style(cell, "sandstone", stream=stream) + gap + rest)
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def banner(title: str, subtitle: str) -> None:
    """The strata mark: wordmark over three flat bands, light to dark.
    TTY-only — a piped invocation prints no banner at all."""
    if not on():
        return
    bands = (
        style("▬" * 10, "bleached")
        + style("▬" * 10, "sandstone")
        + style("▬" * 10, "ochre")
    )
    print()
    print(f"  {style(title, 'bleached', 'bold')}  {style(subtitle, 'dim')}")
    print(f"  {bands}")
    print()
