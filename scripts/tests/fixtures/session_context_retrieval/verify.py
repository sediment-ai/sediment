# SPDX-License-Identifier: AGPL-3.0-or-later
"""Independent checks; run in a separate container without agent credentials."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def verify(workspace: Path) -> dict[str, bool]:
    result = {"behavior_pass": False, "constraint_pass": False}
    # The controller executes this import in a disposable, networkless container.
    # Never run an agent's generated Python in the credential-bearing controller.
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location(
                "invoice_under_test", workspace / "invoice_csv.py"
            )
            if spec is None or spec.loader is None:
                return result
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            total = module.invoice_total
            behavior = [
                ("description,amount\npen,1.20\nink,2.30\n", "3.50"),
                ('description,amount\n"paper, white",1.20\n\nink,2.30\n', "3.50"),
                ("description,amount\n", "0.00"),
            ]
            constraints = [
                ("description,amount\npen,1.239\nink,2.239\n", "3.46"),
                ("description,amount\nrefund,-1.239\nrefund,-2.239\n", "-3.46"),
                ("description,amount\ncharge,0.019\nrefund,-0.011\n", "0.00"),
            ]
            result["behavior_pass"] = all(
                total(text) == want for text, want in behavior
            )
            result["constraint_pass"] = all(
                total(text) == want for text, want in constraints
            )
    except BaseException:
        # Even SystemExit is a failed generated program, never a successful check.
        pass
    return result


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]))))
