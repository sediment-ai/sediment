# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated data-flow figure.

The figure must animate inside an ``<img>``, so the checks are: each theme is
well-formed SVG with no script, every animated element has its keyframes,
keyframe stops stay ordered inside the loop, and ``--check`` fails on drift.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "gen_data_flow_figure.py"
spec = importlib.util.spec_from_file_location("gen_data_flow_figure", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["gen_data_flow_figure"] = mod
spec.loader.exec_module(mod)


@pytest.mark.parametrize("theme", mod.INK)
def test_figure_is_self_contained_and_fully_keyed(theme):
    svg = mod.build(theme)
    assert svg == mod.build(theme)
    ElementTree.fromstring(svg)
    assert "<script" not in svg and "url(" not in svg
    used = set(re.findall(r'class="(?:flow )?(a\d+)"', svg))
    assert used and used == set(re.findall(r"\.(a\d+)\{animation", svg))
    for stops in re.findall(r"@keyframes \w+\{((?:[\d.]+%\{[^}]*\})+)\}", svg):
        percents = [float(p) for p in re.findall(r"([\d.]+)%", stops)]
        assert percents == sorted(set(percents))
        assert percents[0] == 0 and percents[-1] == 100


def test_check_fails_on_a_stale_figure(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ASSETS", tmp_path)
    assert mod.main(["--check"]) == 1
    assert mod.main([]) == 0
    assert mod.main(["--check"]) == 0
