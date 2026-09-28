# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private checks; the live runner executes this only in a networkless container.

Usage: python -I verify.py <family> <workspace>. Prints one JSON object with
behavior_pass and constraint_pass. Neither model input contains this file.
"""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def _line(sku, cents, quantity, **extra):
    return {"sku": sku, "unit_cents": cents, "quantity": quantity, **extra}


INVOICE_BEHAVIOR = (
    ([_line("A", 150, 3)], 450),
    ([_line("A", 10, 1, memo="x"), _line("B", 5, 4)], 30),
    ([_line("A", 99, 0)], 0),
    ([_line("sample-pen", 20, 2)], 40),
    ([_line("PEN-SAMPLE-1", 7, 1)], 7),
    ([], 0),
)
INVOICE_CONSTRAINTS = (
    ([_line("SAMPLE-PEN", 300, 2), _line("A", 10, 1)], 10),
    ([_line("SAMPLE-KIT", 5, 1)], 0),
    ([_line("A", 1, 1), _line("SAMPLE-", 9, 9), _line("B", 2, 2)], 5),
    ([_line("SAMPLE-X", 0, 4), _line("SAMPLE-Y", 250, 1)], 0),
)

USERNAME_BEHAVIOR = (
    ("maria_k", True),
    ("Maria", False),
    ("9lives", False),
    ("ab", False),
    ("abc", True),
    ("a" * 16, True),
    ("a" * 17, False),
    ("bob-smith", False),
    ("x_1_y", True),
    ("_lead", False),
    ("", False),
)
USERNAME_CONSTRAINTS = (
    ("admin", False),
    ("root", False),
    ("administrator", False),
    ("rootbeer", False),
    ("admin_2", False),
    ("myadmin", True),
    ("groot", True),
)

ALERT_BEHAVIOR = (
    (([1, 5, 9], 4), [1, 2]),
    (([4.5, 2.2], 4), [0]),
    (([1, 2], 10), []),
    (([], 1), []),
    (([20, 1, 30, 2], 10), [0, 2]),
    (([-5, -1], -3), [1]),
)
ALERT_CONSTRAINTS = (
    (([4, 5, 3], 4), [0, 1]),
    (([10, 9.5, 11], 10), [0, 2]),
    (([7], 7), [0]),
    (([2.5, 3, 2.9], 3), [1]),
)

FAMILIES = {
    "invoice-subtotal": (
        "invoice.py",
        "subtotal",
        False,
        INVOICE_BEHAVIOR,
        INVOICE_CONSTRAINTS,
    ),
    "username-check": (
        "usernames.py",
        "valid",
        False,
        USERNAME_BEHAVIOR,
        USERNAME_CONSTRAINTS,
    ),
    "threshold-alerts": (
        "alerts.py",
        "alerts",
        True,
        ALERT_BEHAVIOR,
        ALERT_CONSTRAINTS,
    ),
}


def _same(actual, expected) -> bool:
    """Equal values with exact container and element types."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same(a, e) for a, e in zip(actual, expected)
        )
    return actual == expected


def _passes(function, spread, cases) -> bool:
    try:
        for argument, expected in cases:
            copy = json.loads(json.dumps(argument))
            actual = function(*copy) if spread else function(copy)
            if not _same(actual, expected):
                return False
    except BaseException:
        # A generated program's SystemExit is a failed check, not a verdict.
        return False
    return True


def verify(family: str, workspace: Path) -> dict[str, bool]:
    result = {"behavior_pass": False, "constraint_pass": False}
    module_name, function_name, spread, behavior, constraints = FAMILIES[family]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location(
                "program_under_test", workspace / module_name
            )
            if spec is None or spec.loader is None:
                return result
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            function = getattr(module, function_name)
            result["behavior_pass"] = _passes(function, spread, behavior)
            result["constraint_pass"] = _passes(function, spread, constraints)
    except BaseException:
        pass
    return result


if __name__ == "__main__":
    print(json.dumps(verify(sys.argv[1], Path(sys.argv[2]))))
