# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private checks; the live controller runs this only in a networkless container."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


BEHAVIOR = (
    ("North,2,api\nSouth,5,manual\nNorth,3,web\n", {"North": 5, "South": 5}),
    ("North,4,web\nNorth,-2,api\nSouth,0,manual\n", {"North": 2, "South": 0}),
    ('"Port, West",4,api\n"Port, West",7,web\n', {"Port, West": 11}),
    ('"Dock ""East""",6,manual\n', {'Dock "East"': 6}),
    ('"Dock\nEast",3,web\n"Dock\nEast",2,api\n', {"Dock\nEast": 5}),
    ('"Dock\r\nEast",3,web\r\n', {"Dock\r\nEast": 3}),
    ('"Dock\n\nEast",3,web\n', {"Dock\n\nEast": 3}),
    ('"Dock,\n""East""",3,web\n', {'Dock,\n"East"': 3}),
    ("\n \t\n  East  ,2, api \n\nEast,3, web \n", {"East": 5}),
    ("\r\nEast,2,api\r\nEast,3,web\r\n", {"East": 5}),
    ("", {}),
    ("\n \t\r\n", {}),
)
CONSTRAINTS = (
    ("A,3,api\nA,40,replay\nA,2,manual\n", {"A": 5}),
    ("A,40, replay \nB,5,web\n", {"B": 5}),
    ("A,3,Replay\nA,4,REPLAY\nA,2,replay\n", {"A": 7}),
    ('"Port, West",8,replay\n"Port, West",6,api\n', {"Port, West": 6}),
    ("A,9,replay\nB,4,replay\n", {}),
    ("A,2,replay-import\nA,3,api\n", {"A": 5}),
    (" A ,3,\t replay \t\n A ,2, manual \n", {"A": 2}),
)


def _passes(total, cases) -> bool:
    try:
        for text, expected in cases:
            actual = total(text)
            if (
                type(actual) is not dict
                or any(
                    type(k) is not str or type(v) is not int for k, v in actual.items()
                )
                or actual != expected
            ):
                return False
    except BaseException:
        # A generated program's SystemExit is a failed check, not a verdict.
        return False
    return True


def verify(workspace: Path) -> dict[str, bool]:
    result = {"behavior_pass": False, "constraint_pass": False}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location(
                "shipments_under_test", workspace / "shipment_totals.py"
            )
            if spec is None or spec.loader is None:
                return result
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            total = module.totals_by_destination
            result["behavior_pass"] = _passes(total, BEHAVIOR)
            result["constraint_pass"] = _passes(total, CONSTRAINTS)
    except BaseException:
        pass
    return result


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]))))
