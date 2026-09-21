# Why Sediment?

Sediment connects coding-agent activity to Developer decisions, commits, and
continuous integration (CI) results. Each answer depends on what your
[Agent integrations](../capture/agent-integrations.md) capture.
Missing evidence stays unknown; it isn't rejection or failure.

A Cursor, pi, and Codex pilot has uneven capture coverage. Cursor supplies
decision metadata and commit Attribution. Codex native decisions can also
contain patch/tool arguments. pi and Codex offer separately opted-in Edit
observations and gateway calls. The
[Pilot evidence contract](../operate/run-pilot.md#use-the-pilot-evidence)
maps these populations to the seven questions.

Run report commands on your Sediment host with `SEDIMENT_ORG_ID`,
`SEDIMENT_DATABASE_URL`, and `SEDIMENT_MIRROR_PATH` set in your shell.

## Which model works better?

Sediment compares CI pass rates and the share of captured Inference calls linked to
commits (Attribution rate). Reports include sample sizes and uncertainty.
Observed differences don't prove that a model caused an outcome. The comparison
doesn't cover agent harnesses.

```bash
sediment report model
```

[Compare model outcomes](../operate/measure-agent-work.md#compare-model-outcomes).

## Did accepted work reach a commit?

The lifecycle report's `accepted_work` section follows explicitly accepted work
through Attribution, observed commits, merged pull requests, and CI results.
A missing commit observation doesn't prove abandonment.
Stock pi and Cursor accepts are implicit, as are automatic Codex approvals.
Those events don't enter `accepted_work`; an empty section doesn't mean that
their work failed to reach a commit. Observed Session commits support the
separate Session progression measurement.

```bash
sediment report lifecycle --json
```

[Measure accepted-work lifecycle](../operate/lifecycle-report.md).

## How much code survived?

Sediment measures how much of an edit remains at Session end and, separately,
how much of an attributed addition remains after review and merge. Low retention
doesn't identify a defect or who changed the code. Reports with incomplete
pull-request history can't support rankings.

For review and merge retention:

```bash
sediment report merge-retention
```

[Measure retention through pull-request merge](../operate/measure-agent-work.md#measure-retention-through-pull-request-merge).

## Where does work get rejected or changed?

The lifecycle report's `rework` section shows explicit rejects, Retry linkages,
modified or deleted edits, external line changes, and CI failures separately.
These measure different events, so Sediment doesn't combine them into one
rework score. External changes don't establish human authorship.

```bash
sediment report lifecycle --json
```

[Read the panels](../operate/lifecycle-report.md#read-the-panels).

## What work is linked to a CI failure?

Sediment traces a failed run to its commit, observed Sessions, and likely
contributing Inference calls. The Session dossier shows a timeline of recorded
events, without CI logs or call content. These links don't establish
responsibility for the failure.

After [`sediment login`](../reference/cli.md#sediment-login), replace
`COMMIT_SHA` with the failed commit's full hash:

```bash
sediment commit COMMIT_SHA
```

[Investigate a failed CI run](../operate/measure-agent-work.md#investigate-a-failed-ci-run).

## Can I compare model costs?

Sediment captures token usage and duration when the gateway supplies them.
You can use these in your own analysis with the prices that applied at the
time. Sediment has no cost-report command and doesn't infer discounts.

[Record a model price policy](../operate/measure-agent-work.md#record-a-model-price-policy).

## Can I reproduce an analysis?

Preserving the report, its scope, policy, and inputs lets you repeat the analysis
with matching software. Inputs include the Facts, repository data, and any
external price or experiment records. No single command replays a report.

[Preserve an operational result](../operate/measure-agent-work.md#preserve-an-operational-result).
