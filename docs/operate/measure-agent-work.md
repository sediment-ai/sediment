# Measure agent work

Use Sediment's read-only reports to compare model outcomes, find accepted work
with missing commit evidence, check Attribution coverage, and measure attributed
changes through pull-request merge. These reports read immutable Facts and
derive each result under a stated policy.

[Why Sediment?](../explanation/operational-value.md) summarizes the questions
these reports answer and their limits.

## Inspect accepted work and coverage gaps

Generate the accepted-work lifecycle artifact:

```bash
sediment report lifecycle --org acme --json > lifecycle.json
```

The artifact keeps four measurements separate:

- accepted Inference calls progressing through Attribution, pull-request
  membership, and CI;
- Edit observations with Session-end retention and final Fate;
- attributed commit-file contributions at final-head and merged-commit
  boundaries; and
- accepted Sessions with an observed commit relationship or missing evidence
  reported as Attribution unavailable. Missing observations don't establish
  abandonment or an in-flight state.

Use each panel's denominator, coverage, skips, policy, and Provenance. Rework
components remain separate because a reject, Retry linkage, modified edit,
abandoned Session, and CI failure don't represent the same event.

Merge durability is qualified as `partial_pull_request_history` until
pull-request revision capture preserves membership across rebases. Don't use
that partial stage to rank repositories, models, or agent harnesses.

## Compare model outcomes

Run the model outcome report for one organization:

```bash
sediment report model --org acme
```

The report shows captured and attributed Inference calls, CI linkage and pass
rates, explicit accepts and rejects, workflow failures, final Fate, and a
signal funnel by model. The funnel begins with captured Inference calls. Its
later stages show Attribution, CI linkage, explicit decisions, and training-row
eligibility.

Explicit accepts, rejects, and their final Fate remain measurable when a Session
has no observed commit. These counts use uniquely attached Developer decisions.
The signal funnel counts decision coverage among attributed Inference calls.
CI outcomes still require matching Session-to-commit observations. Repository
identity keeps renamed repositories together and reused names separate. Reports
qualify complete CI runs before narrowing the cohort or model; a failed attempt
followed by a pass retains its flake evidence.

If you need a machine-readable result, add `--json`:

```bash
sediment report model --org acme --json > model-outcomes.json
```

For an authenticated remote read, call
`GET /v1/reports/model-outcomes` with timezone-aware `cohort_start`,
`cohort_end`, and `as_of` query parameters. The half-open cohort can span at
most 31 days. The response wraps the same base report in a version 1 envelope
with its explicit scope. The server fixes the cohort cap at 50,000 Inference
calls and the response cap at 64 MiB. Each API process admits two query/report
jobs without a waiting queue. A capacity rejection or 30-second deadline returns
503; the server stops the child process before releasing its slot.

Model rows, the signal funnel, repository stratification, and temporal trends
use the declared cohort. A later `as_of` admits supporting evidence without
moving that cohort. Trend buckets start at `cohort_start`; model and funnel
rows display the cohort duration rounded up to a whole day.

Before attaching Decisions, a scoped report reads all visible call identities in
the organization through `as_of`, including calls before the cohort. This read
rejects more than 50,000 rows; HTTP returns 409 and the model CLI exits 1.
Narrowing the cohort cannot remove older ambiguity witnesses. The cap limits
rows, not output-message bytes or database scan work. Metrics count only cohort
calls after the shared uniqueness check.

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

`--since-days` fixes `as_of` when the command starts. It reads evidence related
to the selected Inference-call cohort plus complete organization-wide identity
witnesses through `as_of`. If you omit `--since-days`, the command reads all
retained history for offline analysis.

For a bounded report, the Attribution-share window ends at `as_of` and spans
the cohort duration rounded up to a whole day. Facts captured after
`cohort_end` and no later than `as_of` can therefore supply outcome evidence
for Inference calls in the cohort.

Repository strata and Attribution-share rows include provider, host, and repository
ID beside the representative name. JSON `shrunk_rates` is an ordered list of
repository strata. Inspect `repository_skipped` for source evidence declines and
`ci_skipped` for source-outcome, run, and commit declines. These populations have
separate units and must not be added. Source loss does not erase direct captured
usage or Developer decision counts.

## Record a model price policy

Cost analysis takes an explicit version 1 price manifest. Record provider and
model identifiers exactly as the corresponding Inference calls report them.
Declare exact input and output prices per million tokens as decimal strings:

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
quality-adjusted cost report and its CLI input are not implemented.

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
  --header "Authorization: Bearer $SEDIMENT_API_BEARER_TOKEN" \
  --data-urlencode "provider=github_actions" \
  --data-urlencode "run_id=<provider-run-id>" \
  --data-urlencode "run_attempt=1" \
  "https://sediment.example.com/query/ci/outcome"
```

If you know the repository and failure window, list a bounded page of failures:

```bash
curl --get \
  --header "Authorization: Bearer $SEDIMENT_API_BEARER_TOKEN" \
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

Query a commit through the authenticated API:

```bash
curl \
  --header "Authorization: Bearer $SEDIMENT_API_BEARER_TOKEN" \
  "https://sediment.example.com/query/commit/<commit-sha>"
```

The commit response groups candidate Attributions by repository and includes
Inference-call identity, Session, model, provider, Attribution source,
decision count, and CI outcomes.

For each candidate `session_id`, retrieve its metadata dossier:

```bash
curl \
  --header "Authorization: Bearer $SEDIMENT_API_BEARER_TOKEN" \
  "https://sediment.example.com/query/session/<session-id>"
```

The dossier orders content-free Fact metadata, summarizes observed Session commits,
and attaches exact-head Push receipts and matching CI outcomes. Coverage shows
visible and quarantined counts. Gaps name evidence that the deployment didn't
observe; they don't classify the Session as unsuccessful. Compatibility
`attribution_sources` stays empty. Similarity extrema and `attributed_files`
are unavailable for these observed edges, so the response omits them.

The failure search returns metadata, structured error evidence, and provider
locations. It doesn't return CI logs or captured Inference-call content.
Attribution records evidence of contribution; it doesn't prove that an
Inference call caused a CI failure. The dossier omits prompts, responses,
reasoning, tool arguments, edit content, user identity, and file paths.

## Preserve an operational result

When you use a report to support a decision, preserve:

- the JSON output;
- the command and time window;
- policy and Provenance fields;
- quarantine revision;
- repository and workflow scope;
- capture and skip counts; and
- any external price or experiment manifest used by downstream analysis.

These inputs let another operator reproduce the Derivation and understand its
coverage. They don't reconstruct treatment assignment or missing agent-harness
configuration.
