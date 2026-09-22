# SPDX-License-Identifier: AGPL-3.0-or-later
"""Independent shipment checks outside the agent and its credentials."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def verify(workspace: Path) -> dict[str, bool]:
    result = {"behavior_pass": False, "constraint_pass": False}
    # The controller imports generated code only in a networkless validator.
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
            behavior = [
                ("A,2,api\nB,5,manual\nA,3,web\n", {"A": 5, "B": 5}),
                ('"Port, West",4,api\n"Port, West",7,web\n', {"Port, West": 11}),
                ('"Dock ""East""",6,manual\n', {'Dock "East"': 6}),
                ("\n  East  ,2, api \n\nEast,3, web \n", {"East": 5}),
                ("", {}),
            ]
            constraints = [
                ("A,3,api\nA,40,replay\nA,2,manual\n", {"A": 5}),
                ("A,40, replay \nB,5,web\n", {"B": 5}),
                ("A,3,Replay\nA,4,REPLAY\nA,2,replay\n", {"A": 7}),
                ('"Port, West",8,replay\n"Port, West",6,api\n', {"Port, West": 6}),
                ("A,9,replay\nB,4,replay\n", {}),
                ("A,2,replay-import\nA,3,api\n", {"A": 5}),
            ]
            result["behavior_pass"] = all(
                total(text) == want for text, want in behavior
            )
            result["constraint_pass"] = all(
                total(text) == want for text, want in constraints
            )
    except BaseException:
        # SystemExit is a failed generated program, not a completed check.
        pass
    return result


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]))))
