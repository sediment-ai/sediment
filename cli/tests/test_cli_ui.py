# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ui module's one contract: styled on a TTY, byte-identical plain text
everywhere else (pipes, NO_COLOR, TERM=dumb) — the property every existing
CLI assertion and golden relies on."""

from __future__ import annotations

import io

from sediment_cli import ui


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _clean_env(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


def test_plain_stream_is_byte_identical(monkeypatch) -> None:
    _clean_env(monkeypatch)
    plain = io.StringIO()  # not a TTY
    assert ui.style("hello", "sandstone", "bold", stream=plain) == "hello"
    assert ui.glyph("✓", "phosphor", plain) == ""


def test_tty_gets_codes_and_reset(monkeypatch) -> None:
    _clean_env(monkeypatch)
    tty = _Tty()
    styled = ui.style("hello", "sandstone", stream=tty)
    assert styled.startswith("\x1b[38;5;") and styled.endswith("hello\x1b[0m")
    assert ui.glyph("✓", "phosphor", tty).startswith("\x1b[")


def test_truecolor_when_colorterm_says_so(monkeypatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert "\x1b[38;2;196;147;90m" in ui.style("x", "sandstone", stream=_Tty())


def test_line_helpers_and_help_are_plain_when_piped(monkeypatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setattr("sys.stderr", io.StringIO())  # not a TTY
    assert ui.error_line("boom") == "error: boom"
    assert ui.warn_line("careful") == "warning: careful"
    help_text = "USAGE:\n  sediment facts [-h]\n\nOPTIONS:\n  -h, --help  exit\n"
    assert ui.style_help(help_text) == help_text  # stdout not a TTY either


def test_style_help_colors_only_listing_cells(monkeypatch) -> None:
    _clean_env(monkeypatch)
    text = "USAGE:\n  sediment <command>\n\nCOMMANDS:\n  facts   counts\n"
    styled = ui.style_help(text, stream=_Tty())
    lines = styled.splitlines()
    assert lines[1] == "  sediment <command>"  # usage body untouched
    assert lines[4].startswith("  \x1b[")  # listing cell accented
    assert "USAGE:" in lines[0] and lines[0] != "USAGE:"  # heading styled


def test_no_color_and_dumb_term_beat_tty(monkeypatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.style("x", "sandstone", stream=_Tty()) == "x"
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert ui.style("x", "sandstone", stream=_Tty()) == "x"
