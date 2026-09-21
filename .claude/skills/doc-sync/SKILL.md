---
name: doc-sync
description: Sync the agent docs with this branch's changes before opening or updating a PR — map the diff to affected docs via the AGENTS.md router, update them in the same PR, then run the deterministic checker. Use before opening or updating any PR; doc-only and pure-test PRs still run the checker step.
---

Read `docs/agents/doc-sync.md` and execute it end to end — it is the
authoritative procedure (diff → router mapping → repo-wide grep → in-place
updates → `uv run python scripts/check_docs.py`).

Do not defer doc updates to a follow-up PR, and re-run the checklist after
review-feedback pushes that change behavior.
