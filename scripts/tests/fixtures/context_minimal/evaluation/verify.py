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


def _row(account, cents, status="posted", **extra):
    return {"account": account, "amount_cents": cents, "status": status, **extra}


LEDGER_BEHAVIOR = (
    ([_row("a", 5), _row("a", -2), _row("b", 7)], {"a": 3, "b": 7}),
    ([_row(" a ", 1), _row("a\t", 2), _row("A", 4)], {"a": 3, "A": 4}),
    ([_row("z", 0)], {"z": 0}),
    ([_row("a", 10), _row("a", -10)], {"a": 0}),
    ([_row("a", 1, memo="m", entered_by="u")], {"a": 1}),
    ([_row("a", -3, "reconciled"), _row("a", 2, "posted")], {"a": -1}),
    ([], {}),
)
LEDGER_CONSTRAINTS = (
    ([_row("a", 5), _row("a", 7, "pending")], {"a": 5}),
    ([_row("a", 5), _row("b", 7, "pending")], {"a": 5}),
    ([_row("a", 9, "pending")], {}),
    ([_row("a", 4, "pending"), _row("a", -4)], {"a": -4}),
    ([_row(" b ", 3, "pending"), _row("b", 1)], {"b": 1}),
)

TAG_BEHAVIOR = (
    (["Red", "red", "Blue"], ["red", "blue"]),
    (["  dark   blue ", "dark blue", "DARK\tBLUE"], ["dark-blue"]),
    (["", "   ", "x"], ["x"]),
    (["b", "a", "b", "c"], ["b", "a", "c"]),
    (["Trail  Running", "trail-running"], ["trail-running"]),
    (["café", "CAFÉ"], ["café"]),
    ([], []),
)
TAG_CONSTRAINTS = (
    (["red", "~red", "blue"], ["blue"]),
    (["~red", "red", "blue"], ["blue"]),
    (["~ Dark  Blue", "dark blue", "x"], ["x"]),
    (["~missing", "a"], ["a"]),
    (["a", "~a", "a"], []),
)

WINDOW_BEHAVIOR = (
    (([1, 2, 3], 2), [1.5, 2.5]),
    (([1, 1, 2], 3), [1.33]),
    (([5], 2), []),
    (([4, 6], 1), [4.0, 6.0]),
    (([2.5, 3.5, 4.5, 5.5], 2), [3.0, 4.0, 5.0]),
    (([], 1), []),
)
WINDOW_CONSTRAINTS = (
    (([1, None, 3], 2), [2.0]),
    (([None, 4, 6, None], 2), [5.0]),
    (([None, None], 1), []),
    (([2, None, 2, 5], 3), [3.0]),
)

FAMILIES = {
    "ledger-balance": (
        "ledger.py",
        "balances",
        False,
        LEDGER_BEHAVIOR,
        LEDGER_CONSTRAINTS,
    ),
    "tag-normalize": ("tags.py", "normalize", False, TAG_BEHAVIOR, TAG_CONSTRAINTS),
    "sensor-window": (
        "window.py",
        "moving_average",
        True,
        WINDOW_BEHAVIOR,
        WINDOW_CONSTRAINTS,
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
