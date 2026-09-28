# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared formatting helpers for invoice totals reports; not used by invoice.py."""


def banner(title: str, width: int = 60) -> str:
    """Return a centered banner line for plain-text reports."""
    return title.center(width, "=")


def table(rows: list[tuple[str, str]]) -> str:
    """Render two-column rows as an aligned plain-text table."""
    if not rows:
        return ""
    left = max(len(a) for a, _ in rows)
    return "\n".join(f"{a.ljust(left)}  {b}" for a, b in rows)


def percent(part: float, whole: float) -> str:
    """Format a ratio as a percentage with one decimal place."""
    return "n/a" if not whole else f"{100 * part / whole:.1f}%"


def truncate(text: str, limit: int = 40) -> str:
    """Shorten text for narrow report columns."""
    return text if len(text) <= limit else text[: limit - 3] + "..."
