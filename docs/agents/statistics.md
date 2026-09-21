# Statistics semantics

`sediment report model` can print p-values, confidence intervals,
posteriors, and a regret number. This playbook is authoritative for what
each of them does and doesn't license a reader to conclude. Change a
statistic's behavior and you change a claim on this page, so update both
in the same pull request.

Calibration, sensitivity, and dataset diagnostics partition rows by
`recipe_id`, integer `recipe_version`, and label or eligibility source. A
pooled result would misdescribe heterogeneous evidence, so these tools never
produce one. Consumers must preserve the strata for statistics or weights.

## The caveat that outranks every number

**This is observational, not a controlled experiment — and statistical
significance doesn't change that.** Nothing holds task difficulty,
developer, repo, or time-of-day constant across the arms. The report
can't warn you when one arm skews harder.

A p-value from `--compare` answers exactly one question: is this gap
larger than pure sampling noise would predict? The null hypothesis has
nothing to say about confounding. A p<0.001 on a badly confounded
comparison is at most evidence that *some* difference between the arms
is real.

So read a gap as "a difference existed under these real, uncontrolled
conditions," and a low p-value as "worth investigating further" — check
traffic comparability, read the Attribution-share section, and consider a
longer or re-randomized window. Never as "the model caused this and the
math proves it." The Bayesian posterior and the regret number inherit
this caveat unchanged. A single summary number makes it more important,
not less.

Assignment is also per-request, not per-user. LiteLLM's weighted router
picks a deployment on every request, so one developer's — or one
Session's — completions can land on either model. Sediment reports
whichever model served each completion. If mid-task switching
confounds the comparison you want, pin assignment upstream.

Factual CI comparisons use qualified Session-to-commit observations through the boundary.
A model label identifies calls inferred within that Session, not causal effects on CI.
`session_commit_unobserved` counts missing edges. Direct decisions and labeled Attribution
coverage remain available ([ADR 0014](../adr/0014-factual-outcomes-and-training-evidence.md)).
Repository trial keys include organization and provider identity. Renames retain one
trial; reused names never pool lifetimes. Complete CI run qualification precedes
cohort, model, and commit selection, so omitted early failures cannot make a retry
look clean. The report preserves source, run, and commit skip counts as separate units.
## What the Attribution rate under-counts

`attribution_rate` is a lower bound. A completion that genuinely shipped
but was heavily hand-edited before commit can fall under the jaccard
threshold and never become an Attributed completion, even though the work
landed.

The under-count runs the same direction for every model, which is why
the number still works for an A/B comparison and not as an absolute
retention figure. The report's git-notes Attribution share sizes the Jaccard fallback. A
lower share means more token-overlap matches carry a similarity discount in downstream Confidence.

Attributed inference calls also cluster within Sessions. The denominator is
per-completion. A Session's completions aren't independent — developer
style, task difficulty, and repo carry across them. The report applies
no cluster-aware correction, so treat p-values and intervals over
survival counts as somewhat optimistic. The commit grain fixes
`ci_pass_rate`'s pseudo-replication. Commit-level trials cluster within
Sessions too. Same grain of salt.
## The z-test, Cohen's h, and the interval

Stdlib only (`math`, `statistics.NormalDist`; no scipy). Per metric,
`difference = p_a - p_b`, positive meaning MODEL_A's rate is higher.
Cohen's h is the standardized effect size for two proportions — `|h|`
near 0.2/0.5/0.8 reads small/medium/large, interpretation aids rather
than product thresholds.

Two standard errors appear, deliberately. The null estimates one shared
proportion, so the z-statistic and its two-tailed p-value use the
**pooled** SE. The 95% confidence interval isn't conditioned on the
null, so it uses the **unpooled** SE. The formulas and the reasoning
behind each choice live in
`packages/export/sediment_export/significance.py`.

The report handles two edges rather than hiding them. **n=0 in either
group** reports "insufficient data" explicitly — a model with no window
traffic isn't an error. **n below 30** still runs the same z-test,
annotated "small n, less reliable": a subtly-wrong Fisher's exact test
would be worse than an honestly-flagged approximation.

## Why a high p-value isn't evidence of no difference

The minimum detectable effect inverts the planning question: at the
current sample sizes, alpha=0.05, and 80% power, how large a true
difference could this comparison likely detect?

This matters more than it sounds. A three-point survival gap reported at
p=0.35 alongside an MDE of nine percentage points isn't evidence the
gap is absent — the window couldn't have found a gap that small either
way. Read the power line before concluding anything from a high
p-value. The two honest responses are to gather more traffic or to
decide effects that small aren't worth chasing.

`--target-mde` solves the same normal-approximation relationship for the
required `n` per arm. `--completions-per-day` turns that into calendar
time. Both are planning guidance, not a promise that uncontrolled
traffic is causal or independent.

When both arms share a boundary outcome — every CI passes on both sides,
or every CI fails on both sides — the pooled rate is exactly 0 or 1 and
the pooled SE collapses to zero. The z-test's "no difference" read is
correct, but the normal-approximation power formula has no meaningful
value at `p*(1-p)=0`: it would print "MDE = 0.0 percentage points" and
"plan for n=0 per arm" as degenerate arithmetic, not honest answers. The
report flags this as `degenerate_se` and prints MDE/required-n
**unavailable** for that metric rather than the degenerate values. This
is the modal case when two well-built models are compared at high CI pass
rates, so the unavailable message is the trigger to collect more data,
not a sign the comparison broke.

**Non-inferiority** asks a different question from significance:
is MODEL_B not meaningfully worse than MODEL_A by more than a margin you
name? The convention is a one-sided TOST at alpha=0.05, declaring
non-inferiority only when the lower bound of the two-sided 90% interval
is strictly greater than `-margin`. A 95% interval would spend the alpha
twice.

## Corrections, and the one you have to make yourself

Within one `--compare` call, the report judges its two-test family
against the nominal alpha=0.05 and a Bonferroni-adjusted 0.025 alongside
the raw p-value: `sig=corrected` clears both, `sig=raw_only` only raw
0.05. Across models, `--compare-all` applies Benjamini-Hochberg FDR
control over the whole family — both metrics times every unordered pair.

**Across time, correction is on you.** These corrections control the
family inside one call, not the practice of re-running the same
comparison as more completions arrive. That is optional stopping. It
inflates the true false-positive rate beyond the nominal alpha even
though every individual run prints `alpha=0.05`. Pre-commit the sample
size or time window, run the comparison once, and read that result.
Don't monitor until a p-value crosses a threshold.

For an intentional early look, `--peek-fraction` keeps the naive p-value
visible and adds an O'Brien-Fleming boundary read, built from the same
Bonferroni alpha so the two can never contradict each other. At `t=1.0`
it reduces to the fixed-sample critical value. Earlier looks require a
larger `|z|`. It is a single-peek approximation: it corrects the one
`t` you supply, peeking at several still inflates false positives, and
it is no substitute for a pre-registered group-sequential design.

## The Bayesian posterior says something different

The report models each arm as `Beta(successes + 1, failures + 1)` with a
flat prior. `P(A>B)` and the credible interval come from 100,000 paired
Monte Carlo draws with a fixed seed, so the number is reproducible. It
is a different statistical framing from the z-test, not a replacement.

"94% probability A is better" means 94% of the posterior mass has
MODEL_A's underlying rate above MODEL_B's, *given this prior and this
window's counts*. It doesn't mean "94% chance the p-value is
significant," and it doesn't mean the model caused the gap.

## Expected regret, and the winner's curse

`--regret` selects the apparent-best observed model per metric, then
reports the expected proportion-point loss against alternatives that
might truly be better. Its standard error uses the Agresti-Coull
plus-four smoothed rate rather than the raw rate. A boundary rate — p at
0 or 1, common at small n — would otherwise force regret to exactly
zero.

It carries a **winner's-curse** bias: conditioning on the observed
argmax understates regret in close races, where the apparent winner is
more likely to be an upward fluctuation. When models are close, treat a
reported regret as a floor rather than a precise estimate.

## Effect decay, and why the checkpoint table isn't the verdict

`--effect-decay` re-runs the z-test on arrival-ordered prefixes at 25%,
50%, 75%, and 100%, and prints each checkpoint.

The report deliberately does **not** derive `decay_detected` from that
table. The checkpoints are nested, overlapping prefixes, so any
threshold or trend test over them inherits the early checkpoints'
sampling noise — in simulation, that fires on 26–30% of *constant real
effects*. The flag instead runs a Mann-Kendall trend test over Cohen's h
on 10 **disjoint** sequential blocks, which is what the test requires,
firing only on a significant decrease with at least 30 observations in
both arms.

A detected decrease is a prompt to inspect regression to the mean or
overfitting to early noise. It doesn't override the full-sample z-test
or the earlier caveats. It is also distinct from `--trend` (calendar
windows, one model's rate) and from the peeking correction: a
peeking-corrected "significant" beside "no decay detected" isn't a
contradiction.

## Trend detection isn't root-cause analysis

`--trend` says whether a model's windowed rate drifts monotonically, not
why. A significant decrease might be a model regression, a harder work
mix, flaky CI, or a deployment change Sediment can't see. It is an
early-warning signal to investigate, subject to the same comparability
checks as everything else here.

Mann-Kendall is nonparametric and stdlib-only — a better fit for bounded
rates with uneven denominators than a linear-regression slope test.

## Where this is implemented

`sediment_export.significance` (`two_proportion_z_test`,
`minimum_detectable_effect`, `beta_binomial_probability_of_improvement`,
`bootstrap_two_proportion_interval_diagnostic`, `non_inferiority_test`,
`obrien_fleming_boundary`, `effect_decay_check`, `compare_models`,
`compare_all_models`, `expected_regret`). Its module docstring carries
the same reasoning in more depth.
