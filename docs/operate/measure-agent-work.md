# Measure agent work

Use the read-only reports to compare models, to find accepted agent work that
never shipped, and to measure how much agent code survives review. Each
report writes a table, or JSON with `--json`.

Run the `sediment report` commands from the operator shell.
[Set up an operator shell](deploy-ec2.md#set-up-an-operator-shell) shows it
for EC2, and [the same section](deploy.md#set-up-an-operator-shell) for your
own host.

Reports count only the evidence that capture recorded. Missing evidence is
never a negative outcome. Before you compare two groups, check that they have
the same agents and capture coverage. See
[Know what each agent captures](run-pilot.md#know-what-each-agent-captures).

## Compare model outcomes

```bash
sediment report model --org acme
```

For each model, the report counts captured and attributed Inference calls,
explicit Developer decisions, CI outcomes, final Fate, and training
eligibility. A CI outcome counts only when a Session-to-commit observation
links the call to the commit.

To test whether two models differ over the last 30 days, run:

```bash
sediment report model \
  --org acme \
  --since-days 30 \
  --compare model-a model-b \
  --non-inferiority-margin 0.05
```

The comparison tests CI pass rate and Attribution rate. Attribution rate is a
lower bound on retention, not a measurement of it. Before you act on a
difference, check the sample sizes and the per-repository rows. The
`repository_skipped` and `ci_skipped` counts use separate populations, so
don't add them.

Without `--since-days`, the report reads all history.

## Find where accepted work stops

```bash
sediment report lifecycle --org acme --json > lifecycle.json
```

The lifecycle report follows accepted work from decision to commit, pull
request, CI, and merge. Each part has its own unit and denominator, so don't
add one part's counts to another's.

| Panel | Counts |
| --- | --- |
| `accepted_work` | Inference calls with a human-explicit accept, through commit, pull request, and CI |
| `edit_retention` | Edit observations and their final Fate |
| `merge_durability` | Attributed files through the final pull-request head and the merged commit |
| `session_attrition` | Accepted Sessions by status. Without a commit observation, a Session stays `attribution_unavailable`, so `abandoned` and `in_flight` stay zero. |
| `rework` | Explicit rejects, Retry linkages, modified or deleted edits, external line changes, abandoned Sessions, and failed CI resolutions, each as a separate component |

Read these qualifications before you draw a conclusion:

- Cursor and pi accepts, and automatic Codex approvals, are implicit. They
  don't enter `accepted_work`. Investigate them through `session_attrition`.
- `session_commit_unobserved` counts accepted work with no observed commit.
  Missing commit evidence doesn't prove abandonment.
- External line counts in `edit_retention` show that something other than the
  agent changed the file. They don't identify who.
- If `merge_durability` carries `partial_pull_request_history`, use it to
  investigate, not to rank repositories, models, or agents.

## Find accepted Sessions without a commit

```bash
sediment report abandonment --org acme
```

`accepted_session_outcomes` marks a Session committed when capture observed a
commit. A Session without that evidence stays `attribution_unavailable`. It
isn't counted as abandoned.

## Check Attribution coverage

```bash
sediment report attribution-share --org acme --target-margin 0.1
```

For each repository, the report measures the share of Attribution that comes
from git notes, and flags a decline against the preceding window. Run it before
you interpret a change in the model report. Fewer Session notes lower observed
Attribution without any change in the agents.

## Measure retention through merge

After the deployment has received pull-request merge webhooks, run:

```bash
sediment report merge-retention --org acme
```

The report scores how much attributed added text remains at the final
pull-request head and at the merged commit. It also reports membership
coverage and skip counts. Low retention means that the text didn't survive. It
doesn't mean that the original response was wrong, and it doesn't identify who
changed it.

A rebase can lower membership coverage when the original commit isn't an
ancestor of the final head. Read the membership counts before you compare
groups.

## Investigate a failed CI run

These HTTP routes require the operator token. Capture tokens can't read them.

1. List failures for a repository and time window:

   ```bash
   curl --get \
     --header "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
     --data-urlencode "repo=acme/backend" \
     --data-urlencode "captured_after=2026-09-05T11:00:00Z" \
     --data-urlencode "captured_before=2026-09-05T13:00:00Z" \
     "https://sediment.example.com/query/ci/failures"
   ```

   To page, pass `next_cursor` as the next request's `cursor`, with the same
   filters. If you know the provider's run, fetch it from
   `/query/ci/outcome` with `provider`, `run_id`, and `run_attempt` instead.

2. Each outcome has a `commit_query` path. Follow it to list the Inference
   calls that Attribution links to the commit, with their Session, model, and
   decisions:

   ```bash
   curl \
     --header "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
     "https://sediment.example.com/query/commit/<commit-sha>"
   ```

3. For each `session_id`, fetch the Session's metadata, commit observations,
   Pushes, and CI outcomes:

   ```bash
   curl \
     --header "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
     "https://sediment.example.com/query/session/<session-id>"
   ```

The responses contain metadata, not CI logs or captured content. Attribution
links a call to a commit. It doesn't establish that the call caused the
failure.

## Query reports over HTTP

Two report routes serve bounded windows without shell access:
[`GET /v1/reports/model-outcomes`](../reference/api.md#get-v1reportsmodel-outcomes)
and
[`GET /v1/reports/accepted-work-lifecycle`](../reference/api.md#get-v1reportsaccepted-work-lifecycle).
Both require the operator token and timezone-aware `cohort_start`,
`cohort_end`, and `as_of` parameters.

| Limit | Value |
| --- | --- |
| Cohort window | Half-open, at most 31 days |
| Inference calls in the cohort | 50,000 |
| Distinct Decision identifiers | 30,000; more returns 409 |
| Response size | 64 MiB |
| Deadline | 30 seconds; a timeout returns 503 |

The API serves two report or evidence reads at a time. A third concurrent read
returns 503.

## Keep a result reproducible

With each result, keep the command, its window and `as_of`, the Sediment
version, and the result's policy, Provenance, quarantine revision, and skip
counts. To reproduce it later, you also need the database backup and the
mirrors from that time.
