# Measure accepted-work lifecycle

Use the lifecycle report to find where accepted agent work stops progressing
and which Sessions need investigation. The report measures observed evidence;
it does not claim that an agent caused a continuous integration (CI) failure or
that an external edit came from a human.

## Generate the report

If Sediment can read the Fact store and repository mirrors, run:

```shell
sediment report lifecycle --org acme --json
```

The command requires `--json`. It writes one canonical JSON object to standard
output and sends failures to standard error.

The command reads all retained history. Programmatic callers can pass an
`OperationalReportScope` to bound the Inference-call cohort and related evidence
through `as_of`; the scoped assembly rejects over-cap populations instead of
returning a partial report. The API application service carries that scope with
the canonical report. Its pure serializer returns the same report payload that
the all-history CLI prints.

For an authenticated remote read, call
`GET /v1/reports/accepted-work-lifecycle` with timezone-aware `cohort_start`,
`cohort_end`, and `as_of` query parameters. The half-open cohort can span at
most 31 days. The response wraps the canonical report in a version 1 envelope
with its explicit scope. The server fixes the cohort cap at 50,000 Inference
calls and the response cap at 64 MiB. Each API process admits two query/report
jobs without a waiting queue. A capacity rejection or 30-second deadline returns
503; the server stops the child process before releasing its slot.

Before attaching Decisions, a scoped report checks every requested identifier
against visible organization history through `as_of`, including older calls.
Two matching Facts establish ambiguity without reading their message content.
More than 30,000 distinct non-null Decision identifiers refuses the complete
report with HTTP 409. Narrowing the cohort doesn't hide
older collisions. Metrics count only cohort calls after this check. Other cohort,
supporting-evidence, and execution limits still apply.

## Read the panels

`accepted_work` counts uniquely attached Inference calls from a human-explicit
accept through an observed Session-to-commit relationship, pull-request membership,
and CI. Similarity-selected calls within that Session remain inferred. Several
attributed files for one call still count as one call. Missing qualified edges
count under `session_commit_unobserved`; direct decisions remain available.

Stock pi and Cursor accepts, and automatic Codex approvals, are implicit. They
don't enter `accepted_work`. Use the separate Session observations and
`session_attrition` panel when investigating those populations; don't relabel
automatic success as human approval. The
[Pilot evidence contract](run-pilot.md#use-the-pilot-evidence) describes the
capture limits for those harnesses.

`edit_retention` counts Edit observations and their final Fate. External line
counts distinguish missing evidence from an observed zero. They do not identify
the actor that changed the file. After partial Edit observation quarantine,
these counts can understate change without a partial-coverage flag; follow the
[quarantine procedure](deploy.md#83-quarantine-and-wholesale-deletion) before
using external-change totals or rates.

`merge_durability` counts attributed commit-file contributions through the
final pull-request head and merged commit. Its candidate denominator counts
observation-qualified files. `session_commit_unobserved` counts distinct
repository/commit/Session edges; these units aren't additive. Joined pull requests
count distinct repository/pull-request-number pairs. Its
`partial_pull_request_history` qualification means the report can support an
investigation but must not rank repositories, models, or agent harnesses by
merge retention.

`session_attrition` classifies accepted Sessions with captured observations as
committed. A Session without an observation stays Attribution unavailable. Missing
evidence cannot prove abandonment and does not enter the abandonment-rate denominator.

Repository strata preserve provider identity across renames and separate reused
names. The report preserves original Session observation IDs. Its
`repository_skipped` maps separate Session observations, Attribution sources, and
CI population outcomes; those counts are not additive. Missing repository or
source evidence cannot establish CI success or merge retention.

## Decide

Use accepted-call progression and Session examples to investigate work that
disappears before CI. Use the independent `rework` components to compare
workflows or configurations only when their grains and capture coverage match.
Use model and agent-harness strata only when the report emits a unique supported
join. Do not add the rework components or divide one component by another
component's denominator.
