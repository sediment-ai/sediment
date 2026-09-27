# Measure agent work

Compare model outcomes, investigate missing commit evidence, and measure
retention with read-only reports. Run CLI reports on the deployment host with
operator database access configured through
[Deploy Sediment](deploy.md#5-verify-the-deployment).

Remote API examples require `SEDIMENT_OPERATOR_TOKEN`. Capture tokens don't
authorize reports or queries.

## Inspect accepted work and coverage gaps

Generate the accepted-work lifecycle artifact:

```bash
sediment report lifecycle --org acme --json > lifecycle.json
```

Read the [lifecycle panels](lifecycle-report.md#read-the-panels) separately.
They measure accepted Inference calls, Edit observations, attributed file
contributions, and accepted Sessions. Use each panel's denominator, coverage,
skips, policy, and Provenance. Missing commit evidence doesn't prove abandonment.

Don't rank repositories, models, or harnesses from merge durability qualified
as `partial_pull_request_history`.

## Compare model outcomes

Run the model outcome report for one organization:

```bash
sediment report model --org acme
```

The report groups captured and attributed Inference calls, CI linkage, explicit
decisions, final Fate, and training eligibility by model. Uniquely attached
explicit decisions remain measurable without an observed commit; CI linkage
requires a matching Session-to-commit observation.

If you need a machine-readable result, add `--json`:

```bash
sediment report model --org acme --json > model-outcomes.json
```

For remote access, use [`GET /v1/reports/model-outcomes`](../reference/api.md#get-v1reportsmodel-outcomes)
with timezone-aware `cohort_start`, `cohort_end`, and `as_of` values.

| Bound | Limit |
| --- | --- |
| Cohort interval | Half-open, at most 31 days |
| Cohort Inference calls | 50,000 |
| Distinct Decision identifiers checked against history through `as_of` | 30,000 |
| Response size | 64 MiB |
| Read deadline | 30 seconds |

Capacity saturation or timeout returns 503. More than 30,000 distinct Decision
identifiers returns 409 from HTTP and exit 1 from the CLI. The attachment check
searches visible organization history through `as_of` without reading message
content, so narrowing the cohort doesn't hide older collisions.

Metrics use the declared cohort. A later `as_of` admits supporting evidence
without moving it. Trend buckets start at `cohort_start`; model and funnel rows
round the cohort duration up to whole days.

Compare two models under the same report window:

```bash
sediment report model \
  --org acme \
  --since-days 30 \
  --compare model-a model-b \
  --non-inferiority-margin 0.05
```

The comparison covers CI pass rate and Attribution rate. Attribution rate is a
lower bound, not an absolute retention measurement. Before you act on an
aggregate difference, inspect the report's sample sizes and repository
stratification.

`--since-days` sets `as_of` when the command starts. Without it, the CLI reads all
retained history. Supporting evidence through `as_of` can include Facts captured
after the cohort ends and organization-wide ambiguity witnesses for the cohort's
Decision identifiers. Attribution-share windows use the rounded cohort duration.

Inspect repository strata, `repository_skipped`, and `ci_skipped`. They use
separate populations and aren't additive. Repository identity keeps renames
together and reused names separate. Complete CI runs retain retry and flake
evidence before cohort filtering.

## Record a model price policy

Sediment has no cost-report command. For downstream cost analysis, record a
version 1 price manifest. Record provider and
model identifiers exactly as the corresponding Inference calls report them.
Declare input and output prices per million tokens as decimal strings.
The following prices are illustrative; replace them with your contract values:

```json
{
  "version": 1,
  "manifest_id": "enterprise-contract-2026",
  "prices": [
    {
      "model_provider": "openai",
      "model": "gpt-5",
      "currency": "USD",
      "effective_from": "2026-01-01T00:00:00Z",
      "effective_until": null,
      "input_per_million_tokens": "1.25",
      "output_per_million_tokens": "10"
    }
  ]
}
```

Ranges are half-open: the start applies and the end does not. You may leave
either bound null. Entries for one provider, model, and currency must not
overlap. Preserve the immutable file and its canonical digest with the
analysis. Sediment does not download prices or infer discounts. The
quality-adjusted cost report and its CLI input are unsupported.

## Find accepted Sessions without a commit

Run the abandonment report:

```bash
sediment report abandonment --org acme
```

Inspect `accepted_session_outcomes` and the `session_commit_unobserved` skip count.
A captured observation establishes committed status. An accepted Session without
that evidence stays `attribution_unavailable`; it does not prove abandonment.
The legacy grace-horizon option remains accepted but cannot establish a negative
outcome. See [the factual evidence boundary](../adr/0014-factual-outcomes-and-training-evidence.md).

## Check deterministic Attribution coverage

Run the Attribution-share report to measure git-notes Attribution by
repository and detect a decline against a trailing baseline:

```bash
sediment report attribution-share \
  --org acme \
  --target-margin 0.1
```

Use this report before interpreting changes in the model outcome report. A
decline in Session-stamped Attribution coverage can lower observed Attribution
without a change in agent performance.

## Measure retention through pull-request merge

Run the merge-retention report after the deployment has captured pull-request
merge webhooks and mirrored the relevant repository state:

```bash
sediment report merge-retention --org acme
```

The report shows:

- attributed file candidates and pull-request membership coverage;
- scored files and pull requests;
- distributions at the final head and merged commit;
- threshold counts for all scored rows and human-explicit accepts;
- membership, scoring, Attribution, and decision-attachment skips; and
- Attribution and merge-retention Provenance.

Use `--json` to retain the aggregate result:

```bash
sediment report merge-retention \
  --org acme \
  --json > merge-retention.json
```

Low merge retention means that the attributed added text doesn't remain at the
measured boundary. It doesn't establish that the original response was wrong
or identify who changed it.

Pull-request rebases can reduce membership coverage when the original commit
isn't an ancestor of the final head and no exact CI membership speaks. Read the
membership counters before you compare cohorts.

## Investigate a failed CI run

If you know the provider run identity, retrieve the exact attempt:

```bash
curl --get \
  --header "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
  --data-urlencode "provider=github_actions" \
  --data-urlencode "run_id=<provider-run-id>" \
  --data-urlencode "run_attempt=1" \
  "https://sediment.example.com/query/ci/outcome"
```

If you know the repository and failure window, list a bounded page of failures:

```bash
curl --get \
  --header "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
  --data-urlencode "repo=acme/backend" \
  --data-urlencode "captured_after=2026-09-05T11:00:00Z" \
  --data-urlencode "captured_before=2026-09-05T13:00:00Z" \
  "https://sediment.example.com/query/ci/failures"
```

The search defaults to the normalized `failed` result. Pass `result=passed` or
another normalized result only when you need comparison data. Use
`next_cursor` as the next request's `cursor` value and preserve every filter.
Sediment rejects a cursor when its repository, window, result, workflow, or
pull-request filter changes.

Each outcome contains `commit_query`. Follow that path to retrieve candidate
Attributions:

```bash
curl \
  --header "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
  "https://sediment.example.com/query/commit/<commit-sha>"
```

The commit response groups candidate Attributions by repository and includes
Inference-call identity, Session, model, provider, Attribution source,
decision count, and CI outcomes.

For each candidate `session_id`, retrieve its metadata dossier:

```bash
curl \
  --header "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
  "https://sediment.example.com/query/session/<session-id>"
```

The dossier returns content-free Fact metadata, Session-to-commit observations,
Push receipts, and CI outcomes. Coverage distinguishes visible and quarantined
counts. Missing observations don't establish failure. Dossiers omit captured
content, user identity, and file paths.

Failure searches return metadata, structured errors, and provider locations, not
CI logs. Attribution links contributions; it doesn't establish the cause of a
CI failure.

## Preserve an operational result

Preserve the JSON result, command, timezone-aware window and `as_of`, repository
and workflow scope, policy, Provenance, quarantine revision, and skip counts.
Keep the software revision, capture configuration, Fact backup, required mirror
objects and refs, and any external price or experiment manifest.

Use a fixed evidence boundary when reproducing a result.

These inputs let another operator reproduce the Derivation and understand its
coverage. They don't reconstruct treatment assignment or missing agent-harness
configuration.
