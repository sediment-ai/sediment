# SPDX-License-Identifier: AGPL-3.0-or-later
"""The coding-agent guide ships inside the wheel and is published under
docs/. One file is the source; the docs copy is pinned byte-identical so a
drift is a red test rather than a stale page."""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_COPY = REPO_ROOT / "docs" / "capture" / "agent-guide.md"
MANIFEST = REPO_ROOT / "docs" / "published-pages.json"

# check_docs.py skips absolute URLs, and this is the one page that links the
# docs site by absolute URL (it has to: `sediment guide` prints it offline,
# where a relative link resolves to nothing). So the destinations are pinned
# here instead.
_DOCS_LINK = re.compile(r"https://docs\.sediment\.so(/[^)\s#]*)?(?:#([^)\s]+))?")


def _packaged() -> str:
    return resources.files("sediment_cli").joinpath("agent_guide.md").read_text("utf-8")


def _heading_anchors(path: Path) -> set[str]:
    anchors = set()
    for line in path.read_text("utf-8").splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            anchors.add(re.sub(r"[^a-z0-9 -]", "", title).replace(" ", "-"))
    return anchors


def _unresolved(text: str) -> list[str]:
    """Every docs.sediment.so link in ``text`` whose page is not a published
    destination, or whose fragment is not a heading on that page's source."""
    pages = {
        page["destination"]: REPO_ROOT / page["source"]
        for page in json.loads(MANIFEST.read_text("utf-8"))["pages"]
    }
    missing = []
    for match in _DOCS_LINK.finditer(text):
        path, fragment = (match.group(1) or "").strip("/"), match.group(2)
        if path and path not in pages:
            missing.append(match.group(0))
        elif fragment and fragment not in _heading_anchors(pages[path]):
            missing.append(match.group(0))
    return missing


def test_docs_copy_matches_packaged_guide() -> None:
    assert DOCS_COPY.read_text("utf-8") == _packaged()


def test_guide_is_static_text() -> None:
    text = _packaged()
    assert text.startswith("# ")
    for forbidden in (
        "http://localhost",
        "SEDIMENT_INGEST_TOKEN=",
        "Bearer ",
        "/Users/",
        "/home/",
    ):
        assert forbidden not in text


def test_guide_links_resolve_to_published_pages() -> None:
    text = _packaged()
    assert _DOCS_LINK.search(text), "the guide should link the published docs"
    assert _unresolved(text) == []


def test_link_check_flags_unpublished_pages_and_missing_headings() -> None:
    assert _unresolved("see https://docs.sediment.so/capture/nope") == [
        "https://docs.sediment.so/capture/nope"
    ]
    bad_fragment = "https://docs.sediment.so/capture/local-capture#no-such-heading"
    assert _unresolved(f"see {bad_fragment}") == [bad_fragment]
    good = "https://docs.sediment.so/capture/local-capture#what-the-installer-changes"
    assert _unresolved(f"see {good} and https://docs.sediment.so") == []
