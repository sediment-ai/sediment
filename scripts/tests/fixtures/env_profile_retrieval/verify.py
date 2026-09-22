# SPDX-License-Identifier: AGPL-3.0-or-later
"""Independent profile checks; run outside the agent without its credentials."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def verify(workspace: Path) -> dict[str, bool]:
    result = {"behavior_pass": False, "constraint_pass": False}
    # The controller imports generated Python only in a disposable, networkless
    # validator container, never in its credential-bearing host process.
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location(
                "profile_under_test", workspace / "env_profile.py"
            )
            if spec is None or spec.loader is None:
                return result
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            parse = module.parse_profile
            behavior = [
                ("ALPHA=one\nBETA=two\n", {"ALPHA": "one", "BETA": "two"}),
                ("SECRET=a=b=c==\n", {"SECRET": "a=b=c=="}),
                ("\n # ignored\n  NAME = value \n\t\n", {"NAME": "value"}),
                ("EMPTY=\n", {"EMPTY": ""}),
                ("\n# comment only\n", {}),
            ]
            constraints = [
                ("MODE=first\nMODE=last\n", {"MODE": "first"}),
                (
                    "ALPHA=one\nBETA=two\nALPHA=three\nGAMMA=four\nBETA=five\nALPHA=six\n",
                    {"ALPHA": "one", "BETA": "two", "GAMMA": "four"},
                ),
                (
                    " TOKEN = first=part \n# comment\nTOKEN=later\n\nOTHER=single\nTOKEN=last\n",
                    {"TOKEN": "first=part", "OTHER": "single"},
                ),
                ("VALUE=\nVALUE=replacement\n", {"VALUE": ""}),
                ("UPPER=a\nupper=b\n", {"UPPER": "a", "upper": "b"}),
                (
                    "SINGLE=alone\nSECOND=other\n",
                    {"SINGLE": "alone", "SECOND": "other"},
                ),
            ]
            result["behavior_pass"] = all(
                parse(text) == want for text, want in behavior
            )
            result["constraint_pass"] = all(
                parse(text) == want for text, want in constraints
            )
    except BaseException:
        # SystemExit is a failed generated program, not a completed check.
        pass
    return result


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]))))
