# Mermaid diagrams — authoring and cleanup

How to author, clean up, and verify a Mermaid diagram anywhere under
`docs/` or in `README.md` (Claude Code: `/beautiful-mermaid`). The rules
exist because the published site renders the marked Architecture diagram
with [beautiful-mermaid](https://github.com/lukilabs/beautiful-mermaid)
1.1.3 at sync time, and every other block with Mintlify's default
renderer. A diagram that stays inside beautiful-mermaid's subset renders
on GitHub, on the site, and — if it later gains the marker — through the
site's build-time renderer, without edits.

The source diagram stays canonical. The site owns its renderer dependency,
lockfile, palettes, and generated light/dark SVG assets; marked diagrams need no
browser renderer. Sync preserves unmarked
fences, replaces marked fences with centered theme-aware images and meaningful
alternative text, and fails on a renderer error. Repeated sync is deterministic;
site verification covers both themes, narrow layouts, and an invalid marked block.

## The renderer marker

`<!-- render: beautiful-mermaid -->` immediately before a fence routes
that one block through the build-time renderer. Only the Architecture
diagram carries it. Before adding the marker to another block, coordinate
the matching renderer change with the documentation-site maintainer.
An unmarked block keeps Mintlify's renderer.

## Supported syntax

beautiful-mermaid parses flowcharts, state, sequence, class, and ER
diagrams, plus `xychart-beta` charts. For flowcharts it supports:

- directions `TB`, `TD`, `LR`, `BT`, `RL`, and a `direction` override
  inside a subgraph;
- node shapes `[rect]`, `(rounded)`, `([stadium])`, `[(cylinder)]`,
  `((circle))`, `{diamond}`, `{{hexagon}}`, `[[subroutine]]`;
- edges `-->`, `---`, `-.->`, `-.-`, `==>`, `===`, bidirectional `<-->`
  variants, labels as `-->|text|` or `-- text -->`, and chained edges;
- `subgraph id[Label]` … `end`, nested subgraphs;
- `<br/>` inside a label for a line break;
- `linkStyle` with `stroke` and `stroke-width` only.

Anything else — `click`, `:::class` shorthand, icon shapes, markdown
strings — is outside the subset. Keep it out of `docs/`.

## Style rules

1. **No color in source.** Delete `classDef`, `class`, `style`, and
   `linkStyle` lines from docs diagrams. The renderer theme owns
   presentation, and hard-coded colors break dark mode on every surface.
2. **Line meaning is semantic.** Solid arrows carry the normal flow. A
   dashed edge marks an exception or bypass and says why in its label —
   for example, `CI -. "Recovery · ADR 0004" .-> Z[Recovery rows]` in
   [How Sediment works](../explanation/how-sediment-works.md).
   Don't use dashed edges as decoration.
3. **One diagram, one claim.** Draw the boundary or flow the section
   argues for and stop. Component inventories belong in a stage node's
   label lines, not in six sibling nodes.
4. **Labels use CONTEXT.md terms exactly.** Spell an abbreviation out in
   the surrounding prose, then abbreviate in the diagram. Keep each
   label line short; break with `<br/>` rather than letting one line
   set the diagram's width.
5. **No redundant edges.** If `A --> B --> C` is drawn, add `A --> C`
   only when a real second path exists — and then label what travels on
   it.
6. **Fit the article column.** Prefer `flowchart TB` for page-width
   diagrams; give a wide `LR` diagram `direction TB` inside its
   subgraphs. A marked block publishes at native width, so it keeps
   the 460 px source-width target. Mintlify scales an unmarked
   block into the article column, so its constraint is legibility
   after scaling: keep the native width at or below ~850 px, and
   restructure past that. Verify the width; don't eyeball it.

## Verify before committing

Render every touched fence with the real parser. Work outside the repo
— the Node toolchain is scoped to `shims/`, so `package.json`,
`node_modules`, and helper scripts never land here:

```bash
work=$(mktemp -d) && cd "$work"
npm init -y >/dev/null && npm install beautiful-mermaid@1.1.3 --no-audit --no-fund
node --input-type=module -e '
import { readFileSync } from "node:fs";
import { renderMermaidSVG } from "beautiful-mermaid";
for (const f of process.argv.slice(1)) {
  const fences = [...readFileSync(f, "utf8").matchAll(/```mermaid\n([\s\S]*?)```/g)];
  fences.forEach((m, i) => {
    const opts = { padding: 24, nodeSpacing: 36, layerSpacing: 56, componentSpacing: 36 };
    const svg = renderMermaidSVG(m[1], opts); // the site's render settings
    const w = svg.match(/width="([\d.]+)"/)[1];
    console.log(`${f} [${i}]: OK, width ${Math.round(w)}px`);
  });
}
' /path/to/repo/docs/explanation/architecture.md
```

Version 1.1.3 exposes only `padding`, `nodeSpacing`, `layerSpacing`,
and `componentSpacing` as layout options and silently ignores anything
else — crossing-minimization thoroughness included. Check
`RenderOptions` in the package's `dist/index.d.ts` before relying on an
option the README of a newer commit documents.

A parse error fails the site sync for a marked block, so treat a FAIL
on any block as a blocker. Check the printed width against rule 6. For
a quick shape check without leaving the terminal, swap in
`renderMermaidASCII` from the same package.

Afterward run `uv run python scripts/check_docs.py` as usual; the
checker validates the surrounding page, not the diagram.
