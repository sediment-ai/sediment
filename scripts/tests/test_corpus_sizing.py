# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/corpus_sizing.py.

``scripts/`` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "corpus_sizing.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("corpus_sizing", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_module().main


def test_cli_prints_assumed_and_worst_case_counts(capsys) -> None:
    code = main(["--target-margin", "0.1", "--assumed-rate", "0.8"])

    assert code == 0
    out = capsys.readouterr().out
    assert "target_margin: 0.1" in out
    assert "confidence: 0.95" in out
    assert "assumed_rate: 0.8" in out
    assert "assumed_rate_required_n: 62" in out
    assert "worst_case_rate: 0.5" in out
    assert "worst_case_required_n: 97" in out


def test_cli_defaults_to_worst_case_when_rate_is_unknown(capsys) -> None:
    code = main(["--target-margin", "0.1"])

    assert code == 0
    out = capsys.readouterr().out
    assert "assumed_rate:" not in out
    assert "worst_case_required_n: 97" in out


def test_cli_reports_clean_error_for_bad_margin(capsys) -> None:
    code = main(["--target-margin", "0"])

    assert code == 2
    err = capsys.readouterr().err
    assert "error: target_margin must be positive" in err
