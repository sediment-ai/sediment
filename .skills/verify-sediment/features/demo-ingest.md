# Demo ingest

Demo posts one synthetic inference call and one developer decision through the
same ingest doors clients use, then prints Fact counts so the quickstart ends
on captured Facts rather than empty tables.

## Sub-features

- `demo-post` posts one gateway inference call and one OTLP decision.
- `demo-counts` prints org Fact table totals after the post.
- `demo-session` leaves Facts under the demo session id for a follow-up read.
- `demo-dedup` collapses a repeated demo on UNIQUE indexes (`stored: false`).

## How to get to it (user POV)

- Run `sediment demo` while logged in to a loopback server.
- Pass `sediment demo --force` only when intentionally seeding a non-loopback server.

## Driving it with verify-sediment

Preconditions:

- Launch, Doctor, and Login have passed for this run.
- The logged-in URL is loopback (`127.0.0.1` or `localhost`).

- **Plant Facts.** Run `.skills/verify-sediment/bin/cli -- demo`. Exit code `0`. Stdout includes `posted 1 inference call and 1 decision` and ends with `These are synthetic facts, not your agent's. They prove the ingest path works end to end.`
- **Confirm counts.** Run `.skills/verify-sediment/bin/cli -- facts`. Exit code `0`. The `inference_calls` and `developer_decisions` rows each show a total of at least `1`.
- **Dedup.** Run `.skills/verify-sediment/bin/cli -- demo` again. Exit code `0`. Counts stay at the same totals (redelivery collapsed).
- **Proof.** Save both demo and facts transcripts under `$SEDIMENT_VERIFY_EVIDENCE/demo-ingest/`. Require the success sentence and non-zero inference and decision counts.

## Gotchas

- Demo refuses a non-loopback URL without `--force`. That guard is intentional: synthetic Facts are real once stored.
- A success line without a follow-up `facts` read is incomplete proof.
- Repeating demo must not grow counts; growth means dedup failed.
- These Facts are synthetic. Do not treat them as agent capture proof.
