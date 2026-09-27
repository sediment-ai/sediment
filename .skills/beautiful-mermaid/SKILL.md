---
name: beautiful-mermaid
description: Author, clean up, or verify a Mermaid diagram in this repo's docs using the beautiful-mermaid renderer's conventions. Use whenever a task touches a ```mermaid fence anywhere under docs/ or README.md — adding a diagram, editing one, reviewing a PR that changes one, or making a diagram render well on the published site — even if the request only says "diagram", "flowchart", or "architecture picture" and never names Mermaid.
---

Read `docs/agents/mermaid-diagrams.md` and follow it: stay inside the
beautiful-mermaid syntax subset, apply the style rules (no color in
source, semantic dashed edges, CONTEXT.md terms, column-width layout),
and render-verify every touched fence with the real parser before
committing.

The `<!-- render: beautiful-mermaid -->` marker is a site contract.
Before adding it to another block, coordinate the matching renderer
change with the documentation-site maintainer.
