# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated CLI reference.

The page's whole promise is that a flag cannot ship undocumented, so the
checks are: every non-plumbing verb has a section, every flag on a verb
appears in it, and ``--check`` actually fails on a stale page.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_cli_docs.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_cli_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_cli_docs"] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }


def _command_section(page: str, path: tuple[str, ...]) -> str:
    heading = f"## sediment {' '.join(path)}\n"
    start = page.index(heading)
    end = page.find("\n## ", start + len(heading))
    return page[start:] if end == -1 else page[start:end]


def test_every_command_and_flag_is_on_the_page() -> None:
    page = mod.render()
    from sediment_cli import attribution, cli

    def walk(parser, path=()):
        if path:
            assert f"sediment {' '.join(path)}" in page, path
            section = _command_section(page, path)
            for flag in _flags(parser):
                assert flag in section, f"{path}: {flag} undocumented"
        action = mod._subparsers(parser)
        if action is None:
            return
        for name, child in action.choices.items():
            if name in mod._PLUMBING or (not path and name in mod._STUBS):
                continue
            walk(child, (*path, name))

    walk(cli.build_parser())
    # install/uninstall/doctor carry their flags only in the attribution tree.
    attribution_sub = mod._subparsers(attribution.build_parser())
    for name in ("install", "uninstall", "doctor"):
        walk(attribution_sub.choices[name], (name,))
    # mirror-gc and each report own their parser too.
    walk(mod._module_parser("sediment_api.mirror_gc"), ("mirror-gc",))
    walk(mod._module_parser("sediment_cli.delivery"), ("delivery",))
    for name, (module_path, _) in cli._REPORTS.items():
        walk(mod._module_parser(module_path), ("report", name))


def test_stubbed_verbs_render_their_real_flags() -> None:
    # The trap this guards: cli.py's `report`/`mirror-gc`/`install` entries
    # are flagless help stubs, so rendering those would produce a section
    # that looks complete and documents nothing.
    page = mod.render()
    for flag in ("--retention-days", "--compare-all", "--target-margin", "--fleet"):
        assert flag in page, flag


def test_report_registry_uses_canonical_attribution_vocabulary() -> None:
    page = mod.render()

    assert "per-model attribution/CI/acceptance" in page
    assert "per-model survival/CI/acceptance" not in page


def test_env_defaults_do_not_leak_into_the_page(monkeypatch) -> None:
    # A generator run on a configured machine must produce the same page as
    # one on a bare shell — otherwise CI's --check flips on whoever ran it.
    bare = mod.render()
    monkeypatch.setenv("SEDIMENT_ORG_ID", "acme")
    monkeypatch.setenv(
        "SEDIMENT_DATABASE_URL", "postgresql://leaked:secret@localhost/leaked"
    )
    monkeypatch.setenv("SEDIMENT_MIRROR_PATH", "/tmp/mirrors")
    assert mod.render() == bare


def test_plumbing_verbs_stay_off_the_page() -> None:
    # Hooks invoke these; documenting them invites hand-running them.
    page = mod.render()
    for verb in ("sediment mark", "sediment stamp", "sediment push-notes"):
        assert verb not in page


def test_check_fails_on_a_stale_page(tmp_path, monkeypatch, capsys) -> None:
    stale = tmp_path / "cli.md"
    stale.write_text("# CLI reference\n")
    monkeypatch.setattr(mod, "OUT_PATH", stale)
    assert mod.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert mod.main([]) == 0  # regenerate
    assert mod.main(["--check"]) == 0


def test_committed_page_is_current() -> None:
    # The same assertion CI makes; fails here first, with the fix named.
    assert mod.main(["--check"]) == 0
