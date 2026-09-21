# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated HTTP API reference.

The page's promise is that a route cannot ship undocumented, so the checks
are: every path in the spec has a section with its auth and status codes,
an unmapped route fails loudly instead of rendering a blank contract, and
``--check`` actually fails on a stale page.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_api_docs.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_api_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_api_docs"] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _spec() -> dict:
    return yaml.safe_load((REPO_ROOT / "openapi.yaml").read_text(encoding="utf-8"))


def _sections(page: str) -> dict[str, str]:
    """Page split per `## <METHOD> <path>` heading, so an assertion about one
    route cannot be satisfied by another route's table."""
    out, name, buf = {}, None, []
    for line in page.splitlines():
        if line.startswith("## ") and (" /" in line):
            if name:
                out[name] = "\n".join(buf)
            name, buf = line[3:].strip(), []
        elif name:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return out


def test_every_route_has_a_section_with_auth_and_status_codes() -> None:
    page = mod.render()
    for path, operations in _spec()["paths"].items():
        for method in operations:
            heading = f"## {method.upper()} {path}"
            assert heading in page, heading
        assert mod.CONTRACTS[path][1] in page, path
    # Auth is stated per route, from dump_openapi.py's own map.
    assert page.count("**Auth:**") == len(_spec()["paths"])
    for label in (
        "$SEDIMENT_API_BEARER_TOKEN",
        "X-Hub-Signature-256",
        "unauthenticated",
    ):
        assert label in page, label


def test_request_schemas_are_inlined() -> None:
    # The $ref'd envelopes and their enum members, resolved into tables — a
    # reader must never have to open openapi.yaml to learn a field.
    page = mod.render()
    for token in (
        "GatewayIngestRequest",
        "VendorCIRequest",
        "`run_url`",
        "`litellm`",
        "`buildkite`",
    ):
        assert token in page, token


def test_router_raised_status_codes_are_documented() -> None:
    # The whole reason CONTRACTS exists: FastAPI's schema never sees these.
    page = mod.render()
    for code in ("`400`", "`413`", "`503`"):
        assert code in page, code
    # An input-free route must not advertise a validation error it cannot
    # return; the doors that read a body must advertise the 25 MiB cap.
    me = page.split("## GET /v1/me")[1].split("## GET /v1/facts")[0]
    assert "`422`" not in me
    assert "`413`" in page.split("## POST /ingest/gateway")[1].split("## POST")[0]


def test_every_webhook_door_documents_its_inherited_400() -> None:
    # The three HMAC doors call deps.py::read_verified_webhook, which answers
    # a signature-valid but non-object body with 400. The page listed only
    # 200/401/413/422 for all three — a completeness claim it did not have. A
    # route's codes include the ones its dependencies raise, and CONTRACTS'
    # path check cannot see that; only a per-route assertion can.
    page = mod.render()
    sections = _sections(page)
    hmac_paths = mod._auth_map()
    for path, (label, _) in hmac_paths.items():
        if label != "HMAC signature":
            continue
        assert "`400`" in sections[f"POST {path}"], f"{path} omits its 400"


def test_an_unmapped_route_fails_the_run(monkeypatch) -> None:
    contracts = dict(mod.CONTRACTS)
    contracts.pop("/health")
    monkeypatch.setattr(mod, "CONTRACTS", contracts)
    with pytest.raises(SystemExit, match="/health"):
        mod.render()


def test_env_does_not_leak_into_the_page(monkeypatch) -> None:
    # A generator run on a configured machine must produce the same page as
    # one on a bare shell — otherwise CI's --check flips on whoever ran it.
    bare = mod.render()
    for key in (
        "SEDIMENT_ORG_ID",
        "SEDIMENT_DATABASE_URL",
        "SEDIMENT_API_BEARER_TOKEN",
    ):
        monkeypatch.setenv(key, "leaked")
    assert mod.render() == bare


def test_check_fails_on_a_stale_page(tmp_path, monkeypatch, capsys) -> None:
    stale = tmp_path / "api.md"
    stale.write_text("# HTTP API reference\n")
    monkeypatch.setattr(mod, "OUT_PATH", stale)
    assert mod.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert mod.main([]) == 0  # regenerate
    assert mod.main(["--check"]) == 0


def test_committed_page_is_current() -> None:
    # The same assertion CI makes; fails here first, with the fix named.
    assert mod.main(["--check"]) == 0


def test_generated_reference_separates_operator_and_capture_authority():
    sections = _sections(mod.render())
    assert "operator" in sections["GET /v1/facts"].lower()
    assert "$SEDIMENT_OPERATOR_TOKEN" in sections["GET /v1/facts"]
    assert "`403`" in sections["GET /v1/facts"]
    assert "ingest" in sections["POST /ingest/gateway"].lower()
    assert "authority" in sections["GET /v1/me"]
    assert "client_id" in sections["GET /v1/me"]
    assert "`503`" in sections["GET /query/ci/outcome"]
