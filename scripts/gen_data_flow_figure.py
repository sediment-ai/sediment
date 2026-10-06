# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate `.github/assets/data-flow-{dark,light}.svg` — the animated figure.

Each theme is one self-contained SVG: a static diagram plus CSS keyframes
compiled from the timeline in ``FACTS`` and ``OUTPUTS``. There is no script
and no external font, so the figure animates anywhere an ``<img>`` renders,
GitHub included. Under ``prefers-reduced-motion`` it shows its final state.

Nothing in the SVGs is hand-authored; CI can run ``--check`` to fail on drift.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ASSETS = Path(__file__).resolve().parent.parent / ".github" / "assets"
INK = {"dark": "#FFFFFF", "light": "#111111"}
PAPER = {"dark": "#000000", "light": "#FFFFFF"}
W, H = 780, 234
LOOP = 14.0  # seconds
RESET = 13.2  # every revealed element hides here, so the loop restarts clean
GAP, TRAVEL, FLASH = 1.4, 0.8, 0.6
# ponytail: system monospace stack, no embedded font. Values are end-anchored,
# so no layout depends on glyph width. Embed a subset if the brand face matters.
MONO = "'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace"

AGENT, FORGE = 34, 122  # y of each source's edge into Facts
# (source, Fact it sends, the Attributed completion row that Fact completes)
FACTS = [
    (AGENT, "Inference call", None),
    (AGENT, "Developer decision", ("Developer decision", "accepted")),
    (AGENT, "Edit observation", ("Edit retention score", "0.92")),
    (FORGE, "Push", ("Commit", "a1b2c3d")),
    (FORGE, "CI outcome", ("CI outcome", "passed")),
]
DERIVE = ("M388 78H438", 50)  # the edge from Facts to the Derivation
# (pulse start, edge, edge length, arrowhead, box x, box width, label)
OUTPUTS = [
    (8.0, "M520 156V190", 34, "m-4-5 4 5 4-5", 438, 164, "Reports"),
    (8.9, "M698 156V190", 34, "m-4-5 4 5 4-5", 616, 164, "Training rows"),
    (9.8, "M308 156V212H178", 186, "m5-4-5 4 5 4", 0, 178, "Agent context"),
]
ALT = (
    "Evidence about one piece of agent output arrives from separate systems. "
    "A coding agent sends an Inference call, a Developer decision, and an Edit "
    "observation. Git and CI send a Push and a CI outcome. Sediment appends "
    "each one as a Fact in PostgreSQL, and the stored count rises from 0 to 5. "
    "From those Facts, Sediment derives an Attributed completion: Developer "
    "decision accepted, Edit retention score 0.92, Commit a1b2c3d, CI outcome "
    "passed. Reports and Training rows read the Derivation. Agent context "
    "reads the Facts."
)

Track = tuple[str, str, list[tuple[float, str]]]


def _pct(seconds: float) -> str:
    return f"{seconds / LOOP * 100:.2f}".rstrip("0").rstrip(".") + "%"


class Sheet:
    """One CSS class per animated element, each on the shared loop."""

    def __init__(self) -> None:
        self.css: list[str] = []

    def add(self, *tracks: Track) -> str:
        name = f"a{len(self.css)}"
        frames, uses = [], []
        for i, (timing, prop, keys) in enumerate(tracks):
            held = dict([(0.0, keys[0][1]), *keys, (LOOP, keys[-1][1])])
            stops = "".join(f"{_pct(t)}{{{prop}:{v}}}" for t, v in held.items())
            frames.append(f"@keyframes {name}k{i}{{{stops}}}")
            uses.append(f"{name}k{i} {LOOP:g}s {timing} infinite")
        self.css.append(f"{''.join(frames)}.{name}{{animation:{','.join(uses)}}}")
        return name


def shown(*windows: tuple[float, float]) -> Track:
    """Visible inside each [start, end) window, hidden elsewhere."""
    keys = [(0.0, "0")]
    for start, end in windows:
        keys.append((start, "1"))
        if end < LOOP:  # a window that reaches the loop end stays on into the restart
            keys.append((end, "0"))
    return ("step-end", "opacity", keys)


def slide(start: float, x0: int, x1: int, y: int) -> Track:
    keys = [
        (start, f"translate({x0}px,{y}px)"),
        (start + TRAVEL, f"translate({x1}px,{y}px)"),
    ]
    return ("linear", "transform", keys)


def _cls(name: str, transient: bool) -> str:
    """Transient elements hide in the static state; the rest show their end state."""
    if not name:
        return ""
    return f' class="flow {name}" opacity="0"' if transient else f' class="{name}"'


def text(
    x: int,
    y: int,
    body: str,
    name: str = "",
    *,
    size: int = 13,
    bold: bool = False,
    dim: bool = False,
    end: bool = False,
    transient: bool = False,
) -> str:
    attrs = f' font-size="{size}"'
    attrs += ' font-weight="600"' if bold else ""
    attrs += ' fill-opacity=".55"' if dim else ""
    attrs += ' text-anchor="end"' if end else ""
    return f'<text x="{x}" y="{y}"{attrs}{_cls(name, transient)}>{escape(body)}</text>'


def build(theme: str) -> str:
    ink, sheet = INK[theme], Sheet()
    boxes = [(0, 0, 178, 68), (0, 88, 178, 68), (228, 0, 160, 156), (438, 0, 342, 156)]
    head = "m-5-4 5 4-5 4"
    arrows = [f"M178 {AGENT}H228{head}M178 {FORGE}H228{head}{DERIVE[0]}{head}"]
    labels = [
        text(16, 28, "Coding agent", size=14, bold=True),
        text(16, 116, "Git and CI", size=14, bold=True),
        text(244, 28, "Facts", size=14, bold=True),
        text(244, 48, "PostgreSQL", dim=True),
        text(454, 28, "Attributed completion", size=14, bold=True),
    ]
    flashes, pulses, chips = [], [], []

    def pulse(start: float, path: str, length: int) -> float:
        """Send a dash along an edge; return the time it arrives."""
        end = start + 0.3 + length / 450
        travel = (
            "linear",
            "stroke-dashoffset",
            [(start, "14px"), (end, f"{-length}px")],
        )
        name = sheet.add(travel, shown((start, end)))
        pulses.append(
            f'<path d="{path}" stroke-dasharray="14 {length + 14}"{_cls(name, True)}/>'
        )
        return end

    def flash(
        start: float, held: float, x: int, y: int, width: int, height: int
    ) -> None:
        name = sheet.add(shown((start, start + held)))
        flashes.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}"{_cls(name, True)}/>'
        )

    emits = [0.5 + i * GAP for i in range(len(FACTS))]
    lands = [t + TRAVEL for t in emits]
    rows = 0
    for i, (emit, land, (source, fact, row)) in enumerate(zip(emits, lands, FACTS)):
        naming = sheet.add(shown((emit, emit + GAP)))
        labels.append(text(16, source + 16, fact, naming, transient=True))
        chip = sheet.add(slide(emit, 178, 241, source), shown((emit, land)))
        chips.append(
            f'<g{_cls(chip, True)}><rect x="-13" y="-9" width="26" height="18" fill="{ink}"/>'
            f'<path d="M-8-4h14M-8 0h10M-8 4h12" stroke="{PAPER[theme]}" stroke-width="2"/></g>'
        )
        # Facts settle as strata: one bar per stored Fact, oldest at the bottom.
        bar = sheet.add(shown((land, RESET)))
        flashes.append(
            f'<rect x="244" y="{136 - i * 10}" width="128" height="6" fill-opacity=".45"{_cls(bar, False)}/>'
        )
        before = (0.0, lands[0]) if i == 0 else (lands[i - 1], land)
        count = sheet.add(shown(before, *([(RESET, LOOP)] if i == 0 else [])))
        labels.append(text(244, 70, f"{i} stored", count, transient=True))
        if row is None:
            continue
        y, reveal = 64 + rows * 24, pulse(land, *DERIVE)
        rows += 1
        flash(reveal, FLASH, 442, y - 15, 334, 22)
        derived = sheet.add(shown((reveal, RESET)))
        labels.append(
            f"<g{_cls(derived, False)}>{text(454, y, row[0], dim=True)}"
            f"{text(764, y, row[1], bold=True, end=True)}</g>"
        )
    total = sheet.add(shown((lands[-1], RESET)))
    labels.append(text(244, 70, f"{len(FACTS)} stored", total))

    for start, edge, length, tip, x, width, label in OUTPUTS:
        boxes.append((x, 190, width, 44))
        arrows.append(edge + tip)
        labels.append(text(x + 16, 217, label, size=14, bold=True))
        flash(pulse(start, edge, length), 0.9, x, 190, width, 44)

    rects = "".join(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}"/>' for x, y, w, h in boxes
    )
    stroke = (
        f'fill="none" stroke="{ink}" stroke-linecap="round" stroke-linejoin="round"'
    )
    still = "@media (prefers-reduced-motion:reduce){.flow{display:none}*{animation:none!important}}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 {W + 2} {H + 2}" '
        f'width="{W + 2}" height="{H + 2}" role="img" aria-label={quoteattr(ALT)}>'
        f"<title>{escape(ALT)}</title><style>{''.join(sheet.css)}{still}</style>"
        f'<g {stroke} stroke-width="1.5" stroke-opacity=".4"><path d="{"".join(arrows)}"/></g>'
        f'<g fill="{ink}" fill-opacity=".05" stroke="{ink}" stroke-opacity=".3">{rects}</g>'
        f'<g fill="{ink}" fill-opacity=".14">{"".join(flashes)}</g>'
        f'<g fill="{ink}" font-family="{MONO}">{"".join(labels)}</g>'
        f'<g {stroke} stroke-width="2">{"".join(pulses)}</g>{"".join(chips)}</svg>\n'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on a stale figure")
    args = parser.parse_args(argv)
    stale = []
    for theme in INK:
        path, svg = ASSETS / f"data-flow-{theme}.svg", build(theme)
        if not args.check:
            path.write_text(svg)
        elif not path.exists() or path.read_text() != svg:
            stale.append(path.name)
    if stale:
        print(f"stale: {', '.join(stale)}; run scripts/gen_data_flow_figure.py")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
