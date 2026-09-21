# Simulation and capture checks

A fake company's development history, run through the real Sediment
pipeline, with the right answers written down in advance. It exists to
answer one question: does the pipeline produce the Attributions, pairs,
and exports it should, against known ground truth?

Two halves:

- **Scenarios** (`scenarios.py`) — scripted traffic replayed through the
  real pipeline in-process: API → Facts → Derivations → exports, against an
  isolated migrated PostgreSQL database. `gen_repo.py` deterministically generates the
  synthetic repo (simcorp-billing: near-duplicate modules, a renamed
  subtree, a vendored directory, five red→green executable pairs — same
  bytes, same SHAs, every run). Each scenario appends the expected outcome
  to a ground-truth manifest and asserts exact counts. `precision_report.py`
  scores notes/jaccard Attribution precision and recall against that
  manifest and exits nonzero below the pinned floors. This half runs in
  CI in a few minutes.
- **Driver** (`driver/`) — real agents (Claude Code, Codex) working on the
  sim repo through a real deployment: live gateway traffic, real webhooks,
  the stamper installed. The scenarios replay frozen wire captures, so
  they cannot notice when a new agent build changes its telemetry — the
  driver can, and `drift_report.py` diffs what it captured against the
  frozen fixtures. Operator-gated: it needs a seeded remote, deployment
  credentials, and the agents on PATH.

What it proves: the pipeline is correct against known ground truth, and
regressions surface as a red scenario or a breached precision floor in
ordinary CI. What it cannot prove: real-repo performance — the sim's
vocabulary separation is designed, a real codebase's near-duplicates are
nastier. Sim precision is a mechanics check, not the exam.

## Run it

Scenario suite + precision floors (what CI runs):

```bash
uv run pytest sim/tests -q
```

The precision report standalone (both axes + the threshold sweep):

```bash
uv run python sim/precision_report.py
```

Regenerate the sim repo alone:

```bash
uv run python sim/gen_repo.py --out /tmp/sim
```

For capacity measurements, `capacity_workload.py` builds one gateway envelope at
a time from the frozen LiteLLM fixture. Profiles in `sim/profiles/` declare
developer identities, Sessions, calls, complete repeated histories, and byte
sizes. They don't infer calls or storage from lines of code. Token and cost
measurements remain absent because byte sizes don't establish either.

`scripts/capacity_rehearsal.py` seeds the semantic catalog before starting a real
loopback API and concurrent capture. It checks reports, canonical builds,
positive training exports, receipt conservation, and fixed-input determinism.
The smoke profile contains 24 historical calls; the pilot profile contains
1,600 calls in 160 Sessions. Both qualify their declared synthetic populations.
Follow [Profile reports and Derivations](../docs/operate/profile-derivations.md#rehearse-capture-alongside-batch-work)
for database ownership, commands, profile fields, and measurement limits.

`capacity_push_probe.py` adds one commit and note to the seeded repository. It
requires a matching Session-to-commit observation after a signed HTTP Push receipt,
then checks redelivery. This distinguishes stored capture from completed mirror
work during a Derivation.

Conventions this half holds to (ADR 0001 — Facts, not derived state):
Facts enter **only** through
the ingest doors; reads go only through `FactStore` and the public
`sediment_derive` / `sediment_export` APIs; no direct database writes;
fixed seeds and injected timestamps throughout. Every wire shape is
parameterized from the wire-verified fixtures in
`packages/capture/tests/fixtures` — never invented — and notes stamps
use the stamper's exact format. Synthetic CI definitions have distinct stable
provider IDs in both workflow identity fields. Reordering deliveries or retrying
a run preserves its definition; display names don't substitute for that ID.

For the live driver's required deployment inputs and flags:

```bash
uv run python sim/driver/run.py --help
```

### Credential transport

The live driver requires HTTPS for a remote `--api` endpoint. Loopback HTTP
remains available for local runs. The driver rejects redirects, including
synthetic webhook and stub-agent requests, so a redirect cannot forward bearer
credentials or signed payloads to another destination.
