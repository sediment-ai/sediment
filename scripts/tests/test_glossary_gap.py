# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/glossary_gap.py.

``scripts/`` is not a package, so the module is loaded by path. A synthetic
graph.json stands in for graphify output — the coverage/matching logic is what
these lock down, not the extraction.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "glossary_gap.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("glossary_gap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolves string annotations (from
    # `from __future__ import annotations`) via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gg = _load_module()


def _node(nid: str, label: str, source_file: str, file_type: str = "code") -> dict:
    return {
        "id": nid,
        "label": label,
        "source_file": source_file,
        "file_type": file_type,
    }


def _edges_to(nid: str, n: int) -> list[dict]:
    # n edges giving node `nid` degree n (each edge adds 1 to source and target)
    return [{"source": nid, "target": f"other{i}"} for i in range(n)]


CONTEXT = """# Context
## Glossary

### Fact
Something that happened.

### Policy (`*Policy`, `policy_version`)
A tunable knob.

### Push (`Push`)
A git push.

### Training row (`DPOPair`, `SFTSample`)
An exported row.
"""


def _graph() -> dict:
    nodes = [
        _node(
            "factstore", "FactStore", "packages/core/store.py"
        ),  # covered by title "Fact"
        _node(
            "rewardpolicy",
            "LabelConfidencePolicy",
            "packages/export/label_confidence.py",
        ),  # covered by *Policy glob
        _node("push", "Push", "packages/core/models.py"),  # covered by `Push` exact
        _node("scorer", "Scorer", "packages/derive/scoring.py"),  # GAP
        _node(
            "testthing", "TestFake", "packages/core/tests/test_x.py"
        ),  # excluded: test dir
        _node("simthing", "SimWorld", "sim/scenarios.py"),  # excluded: sim
        _node("priv", "_helper()", "packages/core/store.py"),  # excluded: private func
        _node(
            "func",
            "assemble_attributed_completions()",
            "packages/export/attributed_completions.py",
        ),  # func: gap only with flag
        _node("mod", "store.py", "packages/core/store.py"),  # excluded: module node
        _node("ext", "Path", ""),  # excluded: no source_file
    ]
    links: list[dict] = []
    for nid in ["factstore", "rewardpolicy", "push", "scorer", "func", "simthing"]:
        links.extend(_edges_to(nid, 20))
    return {"nodes": nodes, "links": links}


def test_covered_entities_are_not_gaps() -> None:
    gaps = gg.find_gaps(_graph(), CONTEXT, min_degree=5, include_functions=False)
    names = {g.name for g in gaps}
    # exact / glob / title coverage all suppress
    assert "Push" not in names
    assert "LabelConfidencePolicy" not in names
    assert "FactStore" not in names


def test_uncovered_high_degree_class_is_flagged() -> None:
    gaps = gg.find_gaps(_graph(), CONTEXT, min_degree=5, include_functions=False)
    assert [g.name for g in gaps] == ["Scorer"]
    assert gaps[0].kind == "class"


def test_functions_excluded_by_default_included_on_flag() -> None:
    without = gg.find_gaps(_graph(), CONTEXT, min_degree=5, include_functions=False)
    assert "assemble_attributed_completions" not in {g.name for g in without}
    with_funcs = gg.find_gaps(_graph(), CONTEXT, min_degree=5, include_functions=True)
    assert "assemble_attributed_completions" in {g.name for g in with_funcs}


def test_tests_sim_private_and_modules_excluded() -> None:
    gaps = gg.find_gaps(_graph(), CONTEXT, min_degree=5, include_functions=True)
    names = {g.name for g in gaps}
    assert names.isdisjoint({"TestFake", "SimWorld", "_helper", "store.py", "Path"})


def test_min_degree_floor() -> None:
    # Scorer has degree 20; a floor above it yields nothing.
    assert gg.find_gaps(_graph(), CONTEXT, min_degree=50, include_functions=False) == []


def test_path_filter_scopes_to_substring() -> None:
    gaps = gg.find_gaps(
        _graph(),
        CONTEXT,
        min_degree=5,
        include_functions=False,
        path_filters=["packages/export"],
    )
    assert gaps == []  # Scorer lives under packages/derive, filtered out


def test_stale_flags_missing_glossary_class() -> None:
    # `DPOPair` and `SFTSample` are in CONTEXT but absent from the graph; `Push` is present.
    stale = gg.find_stale(_graph(), CONTEXT)
    assert set(stale) == {"DPOPair", "SFTSample"}
    assert "Push" not in stale


def test_main_json_output(capsys, tmp_path) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_graph()), encoding="utf-8")
    context_path = tmp_path / "CONTEXT.md"
    context_path.write_text(CONTEXT, encoding="utf-8")
    code = gg.main(
        [
            "--graph",
            str(graph_path),
            "--context",
            str(context_path),
            "--min-degree",
            "5",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [g["name"] for g in payload["gaps"]] == ["Scorer"]


def test_glob_tier_covers_without_title_masking() -> None:
    # Heading title ("Exports") shares no substring with the class name, so
    # only the glob tier can cover it — pins the tier the 3-tier test can't
    # isolate (its `*Policy` classes are also title-covered by "Policy").
    terms = gg.parse_defined_terms("### Exports (`*Sample`)\n")
    assert gg.is_covered("RecoverySample", terms)
    assert not gg.is_covered("RecoveryReport", terms)
