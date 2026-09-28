# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bundled guide matches the published source and links to real procedures."""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_COPY = REPO_ROOT / "docs" / "operate" / "agent-guide.md"
MANIFEST = REPO_ROOT / "docs" / "published-pages.json"

_DOCS_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _packaged() -> str:
    return (
        resources.files("sediment_cli").joinpath("agent_guide.txt").read_text("utf-8")
    )


def _heading_anchors(path: Path) -> set[str]:
    anchors = set()
    for line in path.read_text("utf-8").splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            anchors.add(re.sub(r"[^a-z0-9 -]", "", title).replace(" ", "-"))
    return anchors


def _unresolved(text: str) -> list[str]:
    """Links without a published source page or a matching source heading."""
    pages = {
        (REPO_ROOT / page["source"]).resolve()
        for page in json.loads(MANIFEST.read_text("utf-8"))["pages"]
    }
    missing = []
    for match in _DOCS_LINK.finditer(text):
        link = match.group(1)
        parts = urlsplit(link)
        if parts.scheme or parts.netloc:
            continue
        path = (DOCS_COPY.parent / parts.path).resolve()
        if path not in pages:
            missing.append(link)
        elif parts.fragment and parts.fragment not in _heading_anchors(path):
            missing.append(link)
    return missing


def test_docs_copy_matches_packaged_guide() -> None:
    assert DOCS_COPY.read_text("utf-8") == _packaged()


def test_guide_is_static_text() -> None:
    text = _packaged()
    assert text.startswith("# ")
    assert len(text.splitlines()) <= 150
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
    assert _unresolved("[Missing](../capture/nope.md)") == ["../capture/nope.md"]
    bad_fragment = "../capture/local-capture.md#no-such-heading"
    assert _unresolved(f"[Missing heading]({bad_fragment})") == [bad_fragment]
    good = "../capture/local-capture.md#what-the-installer-changes"
    assert _unresolved(f"[Install]({good}) [External](https://example.com)") == []
