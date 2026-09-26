# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for layered key-value configuration."""

from layered_config import resolve

CASES = (
    (
        "later layer overrides",
        ["host = alpha\nport = 80\n", "port = 8080\n"],
        {"host": "alpha", "port": "8080"},
    ),
    (
        "comments, blank lines, and whitespace",
        ["# defaults\n\n   # indented comment\n  mode  =  fast  \n"],
        {"mode": "fast"},
    ),
    (
        "value may contain equals",
        ["url = https://example.test/?a=b=c\n"],
        {"url": "https://example.test/?a=b=c"},
    ),
    (
        "case-sensitive keys and CRLF",
        ["Region=west\r\nregion=east\r\n"],
        {"Region": "west", "region": "east"},
    ),
    ("no layers", [], {}),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = resolve(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
