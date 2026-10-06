# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated data-flow figure.

The figure must animate inside an ``<img>``, so the checks are: each theme is
well-formed SVG with no script, every animated element has its keyframes, and
keyframe stops stay ordered inside the loop. Without animation the figure must
show its complete final state. ``--check`` must fail on a missing or edited
file, and the README must show the files the generator writes.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "gen_data_flow_figure.py"
SVG = "{http://www.w3.org/2000/svg}"
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


@pytest.mark.parametrize("theme", mod.INK)
def test_static_state_is_the_complete_final_diagram(theme):
    """What reduced motion, or a renderer that drops the style block, shows."""
    svg = mod.build(theme)
    root = ElementTree.fromstring(svg)
    assert "@media (prefers-reduced-motion:reduce){.flow{display:none}" in svg
    assert root.get("aria-label") == root.findtext(f"{SVG}title") == mod.ALT
    assert len(svg.encode()) < 20_000
    transient = [e for e in root.iter() if "flow" in e.get("class", "").split()]
    assert transient and all(e.get("opacity") == "0" for e in transient)
    still = [
        e.text
        for e in root.iter(f"{SVG}text")
        if "flow" not in e.get("class", "").split()
    ]
    assert [t for t in still if t.endswith(" stored")] == [f"{len(mod.FACTS)} stored"]
    assert all(cell in still for _, _, row in mod.FACTS if row for cell in row)


def test_check_fails_on_a_missing_or_edited_figure(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ASSETS", tmp_path)
    assert mod.main(["--check"]) == 1
    assert mod.main([]) == 0
    assert mod.main(["--check"]) == 0
    edited = tmp_path / "data-flow-dark.svg"
    edited.write_text(edited.read_text().replace("5 stored", "6 stored"))
    assert mod.main(["--check"]) == 1


def test_readme_shows_both_generated_themes():
    readme = (SCRIPT.parents[1] / "README.md").read_text()
    for theme in mod.INK:
        assert f'".github/assets/data-flow-{theme}.svg"' in readme
