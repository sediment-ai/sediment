# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scanner errors, malformed output, and findings all close the static gate."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "security_static", ROOT / "scripts/security_static.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"results": [], "errors": [{"message": "parse failure"}]},
        {"results": [{"check_id": "unsafe"}], "errors": []},
        {"results": [], "errors": [], "paths": {"scanned": []}},
    ],
)
def test_missing_malformed_or_failed_static_evidence_blocks(report, tmp_path):
    script = module()
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    with pytest.raises(script.StaticFailure):
        script.validate_report(path)


def test_complete_clean_static_evidence_passes(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "results": [],
                "errors": [],
                "paths": {"scanned": ["apps/api/sediment_api/main.py"]},
            }
        )
    )
    module().validate_report(path)


def test_missing_scanner_fails_closed(tmp_path):
    script = module()
    with pytest.raises(script.StaticFailure):
        script.run([str(tmp_path / "absent-scanner")])
