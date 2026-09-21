# ADR 0011 — Training objectives own evidence interpretation

Status: accepted

## Context

Sediment captures immutable facts once, including inference calls, developer
decisions, edit observations, retry linkages, pushes, and CI outcomes. Several post-training
methods can learn from the same history, but they require different dataset
contracts:

- Supervised fine-tuning (SFT) imitates labeled input/output examples.
- Direct preference optimization (DPO) compares a chosen response with a
  rejected response for the same prompt. Each side needs directional evidence.
- Kahneman-Tversky optimization (KTO) can consume one desirable or undesirable
  response without constructing a pair.
- Reinforcement learning from verifiable rewards (RLVR) evaluates a generated
  response or trajectory with an automated reward function or environment.
- Agentic tuning can imitate tool-bearing trajectories or optimize them against
  an environment. The trajectory shape alone doesn't select SFT or RLVR.
- Continued pre-training (CPT) learns from unlabeled domain text rather than
  labeled developer-workflow examples.

A fact can inform more than one objective without carrying one universal
quality label. An explicit developer accept can select an SFT target, label one
side of a DPO pair, or supply one unary KTO example. A resolved CI pass can
verify a historical RLVR trajectory, raise confidence in an accepted SFT
target, or define an outcome-derived comparison. Those uses aren't
interchangeable. Combining them behind one unlabeled classification hides
whether a row represents human judgment, retained work, or a verifier outcome.

## Decision

The durable rule is:

> Capture facts once, then let each exporter interpret only the evidence
> appropriate to its training objective. Facts may support several projections,
> but each training row names one evidence recipe and preserves the source of
> every label, eligibility decision, and reward.

An **evidence recipe** is a named, versioned projection policy. It selects the
evidence that can create a training target for one objective. Canonical
artifacts may carry all available evidence, but they don't collapse that
evidence into a universal good-or-bad judgment. Training framework, weight
update strategy, and compute topology remain consumer choices because they
don't change evidence semantics.

Sediment uses these method-specific recipe families:

- `sft_curated` requires either a human-explicit accepted developer decision or
  an edit retention score that meets the recipe's versioned strong-retention
  threshold. A human-explicit reject, abandonment, or resolved CI failure
  vetoes the row. A resolved CI pass can change confidence or reliability, but
  it doesn't create eligibility alone.
- `sft_verified` may use a clean resolved CI pass without a human-explicit
  accept. It remains a separate, non-default dataset rather than silently
  widening `sft_curated`. Diff-SFT applies the corresponding SFT recipe to the
  retained patch target.
- `dpo_human` requires a human-explicit accept for the chosen member and a
  human-explicit reject for the rejected member. The prompt and model must be
  comparable under DPO's pairing contract. The row states that two human
  judgments supplied its directions; it doesn't claim that the developer
  directly compared the two completions. `dpo_outcome` requires resolved CI
  verdicts for both members. One pair never mixes the two label sources.
- `kto_human` is the future unary projection for one human-explicit accept or
  reject. Developer decisions map to this shape without fabricating a second
  response. This decision reserves the recipe semantics but doesn't commit
  Sediment to a KTO exporter.
- `rlvr_ci` uses the resolved CI verdict as the sole numeric reward source. A
  pass maps to 1.0, a failure maps to 0.0, and a non-verdict maps to no reward.
  Developer decisions, edit retention, attribution, and CI reliability remain
  evidence beside the reward; they never become a fractional reward.
- `recovery_ci` remains the fact-derived red-to-green recipe from ADR 0004. It
  uses clean resolved CI lineages and doesn't become a general DPO, KTO, or SFT
  label source.

An RLVR row keeps three claims separate. The resolved CI verdict records the
historical reward. Verification configuration explains how a verifier can run
again. An environment exposes that verifier to a training framework. Sediment
never infers the latter two from a CI workflow name or file. A row without an
executable verification configuration or environment remains an audit rollout;
it isn't an executable online RLVR task.

Agentic data follows the objective rather than receiving a universal agentic
label. A reviewed tool-bearing demonstration uses an SFT recipe. A trajectory
that an environment can score uses an RLVR recipe. CPT remains outside the
fact export pipeline because inference calls and workflow outcomes aren't
an unlabeled domain-text corpus.

Every row carries a recipe identifier. SFT and diff-SFT rows preserve an
`eligibility_source`. DPO rows preserve `chosen_label_source` and
`rejected_label_source`. KTO rows preserve a `label_source`. RLVR rows preserve
a `reward_source`. The source vocabulary is closed and includes
`explicit_accept`, `explicit_reject`, `edit_retention`, `resolved_ci_pass`,
`resolved_ci_fail`, and `abandonment` as the applicable evidence enters a
recipe.

The categorical label, eligibility source, reward, label confidence, CI
reliability, and downstream sample weight remain distinct. An exporter reports
what its recipe derived. A trainer decides how reliability affects sample
weight and must not infer that mapping from a reward or confidence value.

Training-row schemas include recipe and source metadata. A change to default
eligibility must preserve explicit recipe identity and its versioned semantics.

Shared CI resolution is objective-neutral and needs no
redesign under this decision. It produces recomputable evidence without
choosing a training objective. Any RLVR target rewrite
consumes that resolution. A rewrite that changes the RLVR row contract also
identifies `rlvr_ci` and the resolved-CI reward source while keeping
verification configuration and exact verifier evidence separate.

## Consequences

- Capture and canonical derivation remain shared. Sediment doesn't duplicate
  facts or construct method-specific fact stores.
- Dataset names and row metadata state whether human judgment, verifier
  outcomes, or another closed source created the target.
- Unary developer decisions remain available for KTO without weakening DPO's
  pairwise contract.
- Historical verifier results don't masquerade as executable RLVR
  environments.
- Conservative default datasets have lower yield. Operators can export broader
  verified or outcome-derived datasets without mixing their meaning with
  human-curated data.
- Calibration and sensitivity tooling compare evidence recipes explicitly
  instead of treating a mixed dataset as one homogeneous label source.
- Changing recipe semantics requires a version bump and recomputation over the
  same immutable facts.
- A canonical schema id and version identify row shape and field semantics.
  They don't identify the evidence recipe. A compatibility-profile id and
  version identify one downstream adapter, while an Alembic revision identifies
  only the physical PostgreSQL schema.

## DPO recipe version 2

Decision: `dpo_human` and `dpo_outcome` version 2 preserve their Label sources
and require distinct complete mapped responses after row representation
validation. Strict JSON equality ignores object key order and preserves all
response values and scalar types. Each equality decline consumes one slot in
the existing bounded candidate prefix. It doesn't change Facts or canonical
Attribution Provenance.

The recipe-version literal requires DPO pair and metadata schema v4 successors.
Consumer profiles `hf-trl-dpo-v2` and `fireworks-dpo-v2` identify those schemas.
Retired v1 profile names and retained recipe-v1/schema-v3 rows require re-export
of canonical evidence. Published schemas remain immutable; runtime profiles
don't select historical algorithms. `DPOPolicy` retains its separate policy
version gap.
