# Issue tracker: GitHub

Issues and product requirement documents (PRDs) live in
[`sediment-ai/sediment`](https://github.com/sediment-ai/sediment). Use the `gh`
CLI; it infers the repository from the clone remote.

## Community contributors

- Read issues with `gh issue view <number> --comments` and `gh issue list`.
- Comment with missing information, reproduction evidence, or an approach.
  Claim a `ready-for-agent` issue with a comment before you start.
- Open a pull request against `main` with `Closes #<n>` in its description.
- Write standalone problems, scope, acceptance criteria, and dependencies.
  Keep credentials, raw transcripts, personal data, and private repository
  content out of issues and pull requests.

A maintainer reviews evidence before labeling, assigning milestones or project
state, closing another person's issue, or merging. Contributors don't do these.

## Maintainers

Create implementation issues with one triage label. Group related deliverables
under an appropriate milestone; GitHub milestone titles are canonical. Use
`gh issue edit` for labels and milestones. Close with `gh issue close --comment`;
merge after review and a maintainer's approval.

A `tracker` issue can hold a deliverable checklist. Architectural decisions use
an ADR; vocabulary uses `CONTEXT.md`; semantics use the relevant documentation.
A closure or consolidation must cite implementation or reproduction evidence.

## Triage labels

The five triage roles are exact GitHub label names:

- `needs-triage` — a maintainer evaluates it.
- `needs-info` — it waits on the reporter.
- `ready-for-agent` — fully specified and actionable.
- `ready-for-human` — it needs human implementation or judgment.
- `wontfix`.

Extras are `tracker`, `security`, `legal`, and `enterprise-tier`. The last marks
the open-core boundary ([Licensing](../../AGENTS.md#licensing)).

An issue earns `ready-for-agent` only when it names design decisions, files,
tests, and acceptance criteria. If anything is missing, a contributor comments
with the gap and stops. A maintainer moves it to `needs-info`.

## Pull requests as a triage surface

**PRs as a request surface: no.** Automated external-pull-request triage requires
these checks before a maintainer enables it:

1. A maintainer audits fork workflows, token scope, secrets, labels, milestones,
   project state, and merge permissions.
2. A maintainer runs a fork-based pull request through templates, continuous
   integration (CI), review comments, and the contributor-visible failure path.
3. A maintainer approves a reviewed pull request that changes this flag to `yes`.

The [review contract](review.md#reviewer-isolation-staged-rule) defines the
isolation required for automated review of untrusted code. Until that isolation
exists, maintainers review external code without an agent on the host.

When the flag is `yes`, `/triage` uses the `gh pr` equivalents. Triage lists
restrict `authorAssociation` to `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE`.
GitHub shares one issue/pull-request number space. Resolve a number with
`gh pr view <number>`, then `gh issue view <number>`. Read the relevant comments
with `gh issue view <number> --comments` before acting.
