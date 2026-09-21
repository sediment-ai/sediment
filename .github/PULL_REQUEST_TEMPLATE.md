## Summary

What does this pull request change, and why?

Include `Closes #<n>` for an issue in this pull request's repository.

Check the applicable items and explain any item that doesn't apply. Leave
unverified checks unchecked and describe the validation that ran.

## Architecture check

- [ ] No derived state persisted as Fact; no mutation of Fact tables (ADR 0001)
- [ ] Derivations stay pure functions of (Facts, policy) (ADR 0001)
- [ ] No placeholder Session ids; Sessions upserted at the storage seam (ADR 0002)
- [ ] Dedup via UNIQUE indexes, not application code (ADR 0003)
- [ ] Fact shapes (if any) added to `packages/core/sediment_core/models.py` first
- [ ] Training exports consume Attributed completions or Rollouts; Recovery remains the sanctioned Fact-derived exception (ADR 0004)

## Checklist

- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run python scripts/add_spdx.py --check` passes
- [ ] `uv run pytest -q` passes
- [ ] Docs synced in this PR per `docs/agents/doc-sync.md` — or N/A
      (doc-only / pure-test PR); `uv run python scripts/check_docs.py`
      passes either way
- [ ] Generated references pass `dump_openapi.py --check`,
      `gen_cli_docs.py --check`, `gen_api_docs.py --check`, and
      `gen_schema_docs.py --check`
- [ ] Changes under `shims/pi/` pass `npm ci --no-audit --no-fund`,
      `npm run typecheck`, and `npm test`
- [ ] Library and service code uses structured logging; operator CLIs and scripts may print
- [ ] No cloud SDK imports outside `packages/export`

## Notes for reviewers

Anything to focus on, or follow-ups deferred (file an issue for each).
