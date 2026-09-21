# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-model outcome report — attribution, CI, and acceptance by model.

Prints ``sediment_export.generate_model_report`` for one org: an aligned
table on stdout by default, or ``--json`` for machines. An empty result (no
data for the org/window) is a valid answer — it prints "no data" and exits 0,
not an error.

    sediment report model --org acme-corp
    sediment report model --org acme-corp --since-days 30
    sediment report model --org acme-corp --json
    sediment report model --org acme-corp --trend
    sediment report model --org acme-corp \
        --compare claude-sonnet-5 claude-opus-4-8
    sediment report model --org acme-corp \
        --compare-all claude-sonnet-5 claude-opus-4-8 gpt-5-codex --regret

``--compare MODEL_A MODEL_B`` adds a two-proportion z-test and power
analysis on CI pass rate and attribution rate between the two named models, over
the same ``--org``/``--since-days`` window as the descriptive table — Cohen's
h effect size, 95% confidence interval, z-statistic, raw p-value,
Bonferroni-corrected significance, and current minimum detectable effect,
plus a separate Bayesian Beta-Binomial posterior companion reporting
``P(A>B)`` and a credible interval for the rate difference. Both are
stdlib-only (``sediment_export.significance``, no scipy). See
``docs/agents/statistics.md`` for the honest
caveats around treating a p-value as more than it is, especially the warning
against repeated peeking as data accumulates. ``--peek-fraction`` adds a
diagnostic O'Brien-Fleming sequential boundary read alongside the naive
fixed-sample call. ``--effect-decay``, available only with ``--compare``,
adds Cohen's h checkpoint tables at 25/50/75/100% prefixes to flag early
effects that shrink as more outcomes arrive.

``--bootstrap-check``, with ``--compare``, adds a fixed-seed
nonparametric bootstrap percentile CI beside the analytic CI for the same
counts and flags substantial disagreement. It is a reproducible diagnostic,
not persisted state.

``--compare-all MODEL1 MODEL2 MODEL3 ...`` runs that same
comparison for every unordered model pair and applies Benjamini-Hochberg FDR
control across the full family of p-values: both metrics times every pair.
``--regret`` adds the expected regret, in raw proportion points, of shipping
the apparent-best observed model if a compared alternative is actually better.

Storage is read the same way the operator CLI reads it — ``--database-url``/
``SEDIMENT_DATABASE_URL`` and ``--mirror-path``/``SEDIMENT_MIRROR_PATH`` — but this
script does NOT import ``sediment_api.config.settings``: that model requires
``SEDIMENT_ORG_ID`` (and, outside dev mode, real auth secrets) just to
construct, which would defeat the point of this script's own ``--org`` flag
(report any org a deployment holds, without touching API auth config). A
missing ``--mirror-path`` is not fatal: ``MirrorManager`` degrades to "no
mirror for this repo" per-repo (an unmirrored repo's commits are skipped,
never guessed at, same as ``assemble_attributed_completions`` — see ``attributed_completions.py``), so an
operator who only wants git-notes-derived attributed completions never needs one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from sediment_core import CIResult, normalize_org_id
from sediment_derive import (
    AttributionShareAlert,
    RepoAttributionShare,
    Provenance,
    MirrorManager,
    InferenceCall,
    inference_fact_id,
    inference_model,
    inference_observed_at,
    derive_fate_result,
    four_gram_containment,
    RepositoryContext,
    read_repository_context,
)
from sediment_export import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    AbandonmentSummary,
    BayesianProportionComparison,
    BootstrapIntervalDiagnostic,
    CIGrain,
    EffectDecayReport,
    ModelComparison,
    ModelOutcomeReport,
    ModelOutcomeReportResult,
    ModelTemporalTrend,
    MultiModelComparison,
    NonInferiorityResult,
    OutcomeReportPolicy,
    OperationalReportScope,
    ProportionComparison,
    RegretReport,
    SignalFunnelReport,
    StratificationCheck,
    StratificationStatus,
    TrendStatus,
    AttributedCompletion,
    assemble_attributed_completions_result,
    bootstrap_two_proportion_interval_diagnostic,
    build_abandonment_summary,
    build_model_report_result,
    compare_all_models,
    compare_models,
    effect_decay_check,
    expected_regret,
    derive_model_report_attribution_share,
    non_inferiority_test,
    required_days_for_mde,
    required_n_per_arm_for_mde,
)

from sediment_export.outcome_report import (
    model_report_inputs,
    model_ci_trials,
    prepare_report_ci,
    with_model_assembly_diagnostics,
)

from .attribution_share_report import format_repository_label

from ..database import add_database_url_argument, one_shot_fact_store
from ..services.operational_reports import (
    base_model_report_payload,
    generate_operational_model_report,
)


_DEFAULT_MIRROR_PATH = "./mirrors"
_DEFAULT_ALPHA = 0.05
_DEFAULT_POWER = 0.80


def _format_breakdown(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items()) or "-"


def _format_rate_ci(rate: float, ci: tuple[float, float]) -> str:
    lower, upper = ci
    return f"{rate:.1%} [{lower:.1%}, {upper:.1%}]"


def _print_table(rows: list[ModelOutcomeReport]) -> None:
    if not rows:
        print("no data")
        return
    header = (
        f"{'model':<28} {'completions':>11} {'attributed':>10} {'attribution_rate':>23} "
        f"{'ci_linked':>9} {'ci_passed':>9} {'ci_pass':>23} {'accepts':>8} "
        f"{'rejects':>8} {'mean_sim':>9}"
    )
    print(header)
    for r in rows:
        print(
            f"{r.model:<28} {r.completions:>11} {r.attributed_inference_calls:>10} "
            f"{_format_rate_ci(r.attribution_rate, r.attribution_rate_ci):>23} "
            f"{r.ci_linked:>9} {r.ci_passed:>9} "
            f"{_format_rate_ci(r.ci_pass_rate, r.ci_pass_rate_ci):>23} "
            f"{r.explicit_accepts:>8} "
            f"{r.explicit_rejects:>8} {r.mean_similarity:>9.3f}"
        )
        print(
            f"{'':<28} {'session_commit_unobserved':>31}: {r.session_commit_unobserved}"
        )
        print(
            f"{'':<28} {'ci_failures_by_workflow':>31}: "
            f"{_format_breakdown(r.ci_failures_by_workflow)}"
        )
        print(
            f"{'':<28} {'explicit_rejects_by_agent_harness':>31}: "
            f"{_format_breakdown(r.explicit_rejects_by_agent_harness)}"
        )
        print(f"{'':<28} {'fates':>31}: {_format_breakdown(r.fates)}")
        print(
            f"{'':<28} {'human-explicit accept fates':>31}: "
            f"{_format_breakdown(r.explicit_accept_fates)}"
        )
        print(
            f"{'':<28} {'fates with external changes':>31}: "
            f"{_format_breakdown(r.fates_with_external_changes)}"
        )


def _print_fate_diagnostics(result: ModelOutcomeReportResult) -> None:
    print()
    print("Fate diagnostic")
    print(f"derivation_skipped: {_format_breakdown(result.fate_skipped)}")
    print(f"provenance: {result.fate_provenance or '-'}")


def _print_attribution_share(
    rows: list[RepoAttributionShare], alerts: list[AttributionShareAlert]
) -> None:
    print("attribution share")
    total = sum(row.agent_plausible_commits for row in rows)
    if total == 0:
        print("no agent-plausible commits in this window")
        return

    git_notes = sum(row.git_notes_attributed for row in rows)
    share = git_notes / total
    print(
        f"{share:.1%} of agent-plausible commits in this window carry a "
        "session stamp. "
        f"Token-overlap matches among the remaining {1 - share:.1%} carry a "
        "similarity discount in downstream confidence."
    )
    for row in rows:
        print(
            f"  {format_repository_label(row.repo, row.repository_identity)}: {row.git_notes_share:.1%} session-stamped "
            f"({row.git_notes_attributed}/{row.agent_plausible_commits}); "
            f"token_overlap={row.jaccard_attributed} "
            f"unattributed={row.unattributed}"
        )
    for alert in alerts:
        print(
            f"  alert {format_repository_label(alert.repo, alert.repository_identity)}: {alert.kind.value} -- {alert.reason}"
        )


def _print_signal_funnel(rows: list[SignalFunnelReport]) -> None:
    print()
    print("signal funnel")
    if not rows:
        print("no data")
        return
    header = (
        f"{'model':<28} {'completions':>11} {'attributed':>19} "
        f"{'ci_linked':>19} {'has_decision':>19} {'training_eligible':>21}"
    )
    print(header)
    for row in rows:
        print(
            f"{row.model:<28} {row.completions_total:>11} "
            f"{_fmt_rate(row.attributed, row.completions_total, row.attribution_rate):>19} "
            f"{_fmt_rate(row.ci_linked, row.attributed, row.ci_linked_retention_rate):>19} "
            f"{_fmt_rate(row.has_decision, row.attributed, row.has_decision_retention_rate):>19} "
            f"{_fmt_rate(row.training_row_eligible, row.attributed, row.training_row_eligible_retention_rate):>21}"
        )


def _print_abandonment(summary: AbandonmentSummary) -> None:
    print()
    print("abandonment")
    print(f"abandoned_sessions:        {summary.abandoned_sessions}")
    print(f"grade_eligible_sessions:   {summary.grade_eligible_sessions}")
    print(f"implicit_only_sessions:    {summary.implicit_only_sessions}")
    print(f"negative_completions:      {summary.negative_completions}")
    print(f"explicit_accepts_unjoined: {summary.explicit_accepts_unjoined}")
    print(f"derivation_skipped:       {_format_breakdown(summary.derivation_skipped)}")
    print(f"provenance:                {summary.provenance or '-'}")


def _row_for_model(
    rows: list[ModelOutcomeReport], model: str, since_days: int | None
) -> ModelOutcomeReport:
    """The named model's row, or an all-zero row if it has no data in this
    org/window. Mirrors the report's own "no data is a valid answer, not an
    error" convention (see the module docstring): a typo'd or silent model
    name does not crash ``--compare``, it flows into ``compare_models`` as
    ``n=0`` on every metric, which ``two_proportion_z_test`` already turns
    into an explicit ``insufficient_data=True`` rather than a fabricated
    comparison.
    """
    for r in rows:
        if r.model == model:
            return r
    provenance = (
        rows[0].provenance
        if rows
        else Provenance(
            policy_version=OutcomeReportPolicy().policy_version,
            quarantine_revision=0,
        )
    )
    grain = rows[0].grain if rows else CIGrain.COMMIT
    return ModelOutcomeReport(
        model=model,
        since_days=since_days,
        completions=0,
        attributed_inference_calls=0,
        attribution_rate=0.0,
        attribution_rate_ci=(0.0, 0.0),
        ci_linked=0,
        ci_passed=0,
        ci_pass_rate=0.0,
        ci_pass_rate_ci=(0.0, 0.0),
        explicit_accepts=0,
        explicit_rejects=0,
        mean_similarity=0.0,
        provenance=provenance,
        grain=grain,
    )


def _print_proportion_row(
    name: str, pc: ProportionComparison, show_sequential: bool = False
) -> None:
    if pc.insufficient_data:
        placeholder = (
            f"{name:<14} {'--':>8} {'--':>8} {'--':>8} {'--':>7} "
            f"{'--':>8} {'--':>8} {'--':>7} {'--':>9} {'--':>10}"
        )
        if show_sequential:
            placeholder += f" {'--':>8} {'--':>9} {'--':>9}"
        print(
            f"{placeholder} {pc.n_a:>6} {pc.n_b:>6}  "
            "insufficient data (n=0 in at least one group)"
        )
        return
    note = "small n, less reliable" if pc.small_n else ""
    if pc.significant_after_bonferroni:
        sig = "corrected"
    elif pc.significant_at_alpha:
        sig = "raw_only"
    else:
        sig = "no"
    sequential = pc.sequential_boundary
    if sequential is None:
        seq_columns = ""
    else:
        seq_sig = (
            "yes" if sequential.significant_after_sequential_boundary else "not_yet"
        )
        seq_columns = (
            f"{sequential.critical_z:>8.2f} {sequential.alpha_spent:>9.4f} "
            f"{seq_sig:>9} "
        )
    print(
        f"{name:<14} {pc.proportion_a:>8.1%} {pc.proportion_b:>8.1%} "
        f"{pc.difference:>+8.1%} {pc.cohens_h:>+7.3f} "
        f"{pc.ci_low:>+8.1%} {pc.ci_high:>+8.1%} "
        f"{pc.z_stat:>7.2f} {pc.p_value:>9.4f} {sig:>10} "
        f"{seq_columns}{pc.n_a:>6} {pc.n_b:>6}  {note}"
    )


def _format_percentage_points(value: float) -> str:
    return f"{value * 100:.1f} percentage points"


def _target_n_per_arm(pc: ProportionComparison, target_mde: float | None) -> int | None:
    if target_mde is None or pc.insufficient_data or pc.degenerate_se:
        return None
    return required_n_per_arm_for_mde(target_mde, pc.baseline_p)


def _target_days(
    pc: ProportionComparison,
    target_mde: float | None,
    completions_per_day: tuple[float, float] | None,
) -> float | None:
    if (
        target_mde is None
        or pc.insufficient_data
        or pc.degenerate_se
        or completions_per_day is None
    ):
        return None
    completions_per_day_a, completions_per_day_b = completions_per_day
    return required_days_for_mde(
        target_mde,
        pc.baseline_p,
        completions_per_day_a,
        completions_per_day_b,
    )


def _print_power_row(
    name: str,
    pc: ProportionComparison,
    target_mde: float | None,
    completions_per_day: tuple[float, float] | None,
) -> None:
    if pc.insufficient_data:
        print(f"{name}: MDE unavailable because at least one group has n=0.")
        return
    if pc.degenerate_se:
        print(
            f"{name}: MDE unavailable because both arms shared a boundary "
            "outcome (all-pass or all-fail); the normal-approximation power "
            "formula is undefined at a pooled rate of 0 or 1."
        )
        return
    print(
        f"{name}: at your current sample size (n_a={pc.n_a}, n_b={pc.n_b}), "
        "this comparison can detect a difference of at least "
        f"{_format_percentage_points(pc.minimum_detectable_effect)} "
        f"with {_DEFAULT_POWER:.0%} power."
    )
    target_n = _target_n_per_arm(pc, target_mde)
    if target_n is not None:
        print(
            f"{name}: to detect a target difference of "
            f"{_format_percentage_points(target_mde)} with {_DEFAULT_POWER:.0%} "
            f"power, plan for n={target_n} per arm."
        )
        target_days = _target_days(pc, target_mde, completions_per_day)
        if target_days is not None:
            completions_per_day_a, completions_per_day_b = completions_per_day
            print(
                f"{name}: at {completions_per_day_a:g}/{completions_per_day_b:g} "
                "completions/day for model A/model B, estimated duration is "
                f"{target_days:.2f} days."
            )


def _print_bayesian_row(name: str, comparison: BayesianProportionComparison) -> None:
    if comparison.insufficient_data:
        print(f"{name:<14} insufficient data (n=0 in at least one group)")
        return
    note = "small n, posterior is prior-sensitive" if comparison.small_n else ""
    print(
        f"{name:<14} {comparison.posterior_mean_a:>8.1%} "
        f"{comparison.posterior_mean_b:>8.1%} "
        f"{comparison.mean_difference:>+9.1%} "
        f"{comparison.credible_interval_low:>+9.1%} "
        f"{comparison.credible_interval_high:>+9.1%} "
        f"{comparison.probability_a_gt_b:>8.1%} "
        f"{comparison.n_a:>6} {comparison.n_b:>6}  {note}"
    )


def _print_bayesian_comparison(comparison: ModelComparison) -> None:
    bayes = comparison.bayesian_ci_pass_rate
    print()
    print("bayesian posterior")
    print(
        "method: Beta-Binomial posterior with flat "
        f"Beta({bayes.prior_alpha:g}, {bayes.prior_beta:g}) prior; "
        f"Monte Carlo draws={bayes.sample_count}, seed={bayes.seed}. "
        "P(A>B) is posterior probability, not a p-value."
    )
    header = (
        f"{'metric':<14} {'post_a':>8} {'post_b':>8} {'mean_diff':>9} "
        f"{'cred_low':>9} {'cred_high':>9} {'P(A>B)':>8} "
        f"{'n_a':>6} {'n_b':>6}"
    )
    print(header)
    _print_bayesian_row("ci_pass_rate", comparison.bayesian_ci_pass_rate)
    _print_bayesian_row("attribution_rate", comparison.bayesian_attribution_rate)


def _non_inferiority_results(
    report_a: ModelOutcomeReport, report_b: ModelOutcomeReport, margin: float
) -> dict[str, NonInferiorityResult]:
    return {
        "ci_pass_rate": non_inferiority_test(
            report_a.ci_passed,
            report_a.ci_linked,
            report_b.ci_passed,
            report_b.ci_linked,
            margin,
        ),
        "attribution_rate": non_inferiority_test(
            report_a.attributed_inference_calls,
            report_a.completions,
            report_b.attributed_inference_calls,
            report_b.completions,
            margin,
        ),
    }


def _print_non_inferiority_row(name: str, result: NonInferiorityResult) -> None:
    if result.insufficient_data:
        print(f"{name:<14} insufficient data (n=0 in at least one group)")
        return
    note = "small n, less reliable" if result.small_n else ""
    print(
        f"{name:<14} {result.proportion_a:>8.1%} {result.proportion_b:>8.1%} "
        f"{result.point_estimate:>+8.1%} {result.ci_lower_bound:>+11.1%} "
        f"{-result.margin:>+10.1%} {str(result.is_non_inferior):>14} "
        f"{result.n_a:>6} {result.n_b:>6}  {note}"
    )


def _print_non_inferiority(
    comparison: ModelComparison,
    results: dict[str, NonInferiorityResult],
) -> None:
    example = next(iter(results.values()))
    print()
    print(
        "non-inferiority: "
        f"baseline={comparison.model_a}; candidate={comparison.model_b}; "
        f"margin={_format_percentage_points(example.margin)}; "
        f"one-sided alpha={example.alpha:.2f}"
    )
    print(
        "convention: estimate is candidate minus baseline (p_b - p_a); "
        f"lower bound uses the equivalent two-sided "
        f"{example.equivalent_two_sided_confidence:.0%} CI; "
        "non-inferior means lower_bound > -margin."
    )
    header = (
        f"{'metric':<14} {'prop_a':>8} {'prop_b':>8} {'b_minus_a':>8} "
        f"{'lower':>11} {'threshold':>10} {'non_inferior':>14} "
        f"{'n_a':>6} {'n_b':>6}"
    )
    print(header)
    _print_non_inferiority_row("ci_pass_rate", results["ci_pass_rate"])
    _print_non_inferiority_row("attribution_rate", results["attribution_rate"])


def _print_effect_decay_row(report: EffectDecayReport) -> None:
    flag = "yes" if report.decay_detected else "no"
    trend = report.decay_trend
    trend_p = "n/a" if trend.p_value is None else f"{trend.p_value:.4f}"
    print(
        "decay_detected="
        f"{flag} trend={trend.status} p_value={trend_p} "
        f"drop={report.total_abs_drop:.3f} small_n={report.small_n}"
    )
    print(
        f"{'checkpoint':>10} {'n_a':>6} {'n_b':>6} {'prop_a':>8} "
        f"{'prop_b':>8} {'h':>8} {'|h|':>8} {'p_value':>9}"
    )
    for checkpoint in report.checkpoints:
        comparison = checkpoint.comparison
        if comparison.insufficient_data:
            print(
                f"{checkpoint.fraction:>9.0%} {checkpoint.n_a:>6} "
                f"{checkpoint.n_b:>6} insufficient data"
            )
            continue
        print(
            f"{checkpoint.fraction:>9.0%} {checkpoint.n_a:>6} "
            f"{checkpoint.n_b:>6} {checkpoint.proportion_a:>8.1%} "
            f"{checkpoint.proportion_b:>8.1%} {checkpoint.cohens_h:>+8.3f} "
            f"{checkpoint.abs_cohens_h:>8.3f} {comparison.p_value:>9.4f}"
        )


def _print_effect_decay(
    reports: dict[str, EffectDecayReport],
) -> None:
    print()
    print(
        "effect decay diagnostic: Cohen's h over accumulating prefixes (display only)"
    )
    print(
        "flag rule: Mann-Kendall trend test over disjoint sequential blocks "
        "of the same outcomes (trend=significant_decrease) -- the "
        "checkpoints above are nested prefixes and not what drives the flag."
    )
    for metric, report in reports.items():
        print()
        print(metric)
        _print_effect_decay_row(report)


def _bh_threshold_for_display(comparison: MultiModelComparison) -> float:
    for pair in comparison.comparisons:
        for pc in (pair.ci_pass_rate, pair.attribution_rate):
            if not pc.insufficient_data:
                return pc.benjamini_hochberg_alpha
    return 0.0


def _regret_cell(
    model_a: str,
    model_b: str,
    regret_reports: dict[str, RegretReport] | None,
    metric: str,
) -> str | None:
    if regret_reports is None:
        return None
    report = regret_reports[metric]
    best = report.apparent_best_model
    if best is None:
        return "n/a"
    if model_a == best:
        alternative = model_b
    elif model_b == best:
        alternative = model_a
    else:
        return "-"
    for regret in report.alternatives:
        if regret.model == alternative:
            return (
                "n/a" if regret.insufficient_data else f"{regret.expected_regret:.2%}"
            )
    return "-"


def _print_fdr_proportion_row(
    model_a: str,
    model_b: str,
    name: str,
    pc: ProportionComparison,
    regret: str | None = None,
) -> None:
    regret_suffix = "" if regret is None else f" {regret:>9}"
    if pc.insufficient_data:
        print(
            f"{model_a:<22} {model_b:<22} {name:<14} "
            "insufficient data (n=0 in at least one group)"
            f"{regret_suffix}"
        )
        return
    note = "small n, less reliable" if pc.small_n else ""
    fdr = "yes" if pc.significant_after_benjamini_hochberg else "no"
    bh_alpha = pc.benjamini_hochberg_alpha or 0.0
    print(
        f"{model_a:<22} {model_b:<22} {name:<14} "
        f"{pc.proportion_a:>8.1%} {pc.proportion_b:>8.1%} "
        f"{pc.difference:>+8.1%} {pc.cohens_h:>+7.3f} "
        f"{pc.p_value:>9.4f} {bh_alpha:>9.4f} {fdr:>5}"
        f"{regret_suffix} {pc.n_a:>6} {pc.n_b:>6}  {note}"
    )


def _print_compare_all(
    comparison: MultiModelComparison,
    regret_reports: dict[str, RegretReport] | None = None,
) -> None:
    print()
    print(f"compare-all: {', '.join(comparison.models)}")
    regret_header = "" if regret_reports is None else f" {'regret':>9}"
    header = (
        f"{'model_a':<22} {'model_b':<22} {'metric':<14} "
        f"{'prop_a':>8} {'prop_b':>8} {'diff':>8} {'h':>7} "
        f"{'p_value':>9} {'bh_alpha':>9} {'fdr':>5}"
        f"{regret_header} {'n_a':>6} {'n_b':>6}"
    )
    print(header)
    for pair in comparison.comparisons:
        _print_fdr_proportion_row(
            pair.model_a,
            pair.model_b,
            "ci_pass_rate",
            pair.ci_pass_rate,
            _regret_cell(pair.model_a, pair.model_b, regret_reports, "ci_pass_rate"),
        )
        _print_fdr_proportion_row(
            pair.model_a,
            pair.model_b,
            "attribution_rate",
            pair.attribution_rate,
            _regret_cell(
                pair.model_a, pair.model_b, regret_reports, "attribution_rate"
            ),
        )
    print(
        f"multiple comparisons: raw alpha={comparison.alpha:.2f}; "
        "Benjamini-Hochberg FDR threshold="
        f"{_bh_threshold_for_display(comparison):.4f} "
        f"over {comparison.tested_family_size} tested p-values "
        f"({comparison.comparison_family_size} total pair-metric slots). "
        "fdr=yes is the BH-controlled rejection set."
    )
    if regret_reports is not None:
        bests = ", ".join(
            f"{metric}={report.apparent_best_model or 'n/a'}"
            for metric, report in regret_reports.items()
        )
        print(
            "regret: expected loss from shipping each metric's apparent best "
            f"if the compared alternative is actually better ({bests})."
        )
    print(
        "repeated-testing caveat: run --compare-all once on a pre-committed "
        "sample window; peeking as data accumulates inflates false positives "
        "beyond the nominal alpha."
    )


def _regret_reports_for_rows(
    rows: list[ModelOutcomeReport],
) -> dict[str, RegretReport]:
    return {
        "ci_pass_rate": expected_regret(
            {row.model: (row.ci_passed, row.ci_linked) for row in rows},
            "ci_pass_rate",
        ),
        "attribution_rate": expected_regret(
            {
                row.model: (row.attributed_inference_calls, row.completions)
                for row in rows
            },
            "attribution_rate",
        ),
    }


def _target_mde_payload(
    comparison: ModelComparison,
    target_mde: float | None,
    completions_per_day: tuple[float, float] | None,
) -> dict[str, object] | None:
    if target_mde is None:
        return None
    payload: dict[str, object] = {
        "target_mde": target_mde,
        "power": _DEFAULT_POWER,
        "alpha": _DEFAULT_ALPHA,
        "required_n_per_arm": {
            "ci_pass_rate": _target_n_per_arm(comparison.ci_pass_rate, target_mde),
            "attribution_rate": _target_n_per_arm(
                comparison.attribution_rate, target_mde
            ),
        },
    }
    if completions_per_day is not None and all(
        rate > 0 for rate in completions_per_day
    ):
        payload["completions_per_day"] = {
            "model_a": completions_per_day[0],
            "model_b": completions_per_day[1],
        }
        payload["required_days"] = {
            "ci_pass_rate": _target_days(
                comparison.ci_pass_rate,
                target_mde,
                completions_per_day,
            ),
            "attribution_rate": _target_days(
                comparison.attribution_rate,
                target_mde,
                completions_per_day,
            ),
        }
    return payload


def _bootstrap_checks_for_reports(
    report_a: ModelOutcomeReport,
    report_b: ModelOutcomeReport,
    comparison: ModelComparison,
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> dict[str, BootstrapIntervalDiagnostic]:
    return {
        "ci_pass_rate": bootstrap_two_proportion_interval_diagnostic(
            report_a.ci_passed,
            report_a.ci_linked,
            report_b.ci_passed,
            report_b.ci_linked,
            alpha=comparison.alpha,
            comparison_family_size=comparison.comparison_family_size,
            bootstrap_iterations=bootstrap_iterations,
            seed=bootstrap_seed,
        ),
        "attribution_rate": bootstrap_two_proportion_interval_diagnostic(
            report_a.attributed_inference_calls,
            report_a.completions,
            report_b.attributed_inference_calls,
            report_b.completions,
            alpha=comparison.alpha,
            comparison_family_size=comparison.comparison_family_size,
            bootstrap_iterations=bootstrap_iterations,
            seed=bootstrap_seed,
        ),
    }


def _format_ci(low: float, high: float) -> str:
    return f"[{low:+.1%}, {high:+.1%}]"


def _print_bootstrap_row(name: str, check: BootstrapIntervalDiagnostic) -> None:
    if check.analytic.insufficient_data:
        print(f"{name:<14} bootstrap unavailable (n=0 in at least one group)")
        return
    status = "disagree" if check.substantial_disagreement else "agree"
    reasons = ",".join(check.disagreement_reasons) or "-"
    print(
        f"{name:<14} {_format_ci(check.analytic.ci_low, check.analytic.ci_high):>20} "
        f"{_format_ci(check.bootstrap_ci_low, check.bootstrap_ci_high):>20} "
        f"{check.ci_overlap_fraction:>7.2f} "
        f"{str(check.analytic_point_in_bootstrap_ci):>8} "
        f"{status:>9}  {reasons}"
    )


def _print_bootstrap_checks(
    checks: dict[str, BootstrapIntervalDiagnostic],
) -> None:
    if not checks:
        return
    first = next(iter(checks.values()))
    print()
    print(
        "bootstrap interval diagnostic: "
        f"B={first.bootstrap_iterations}, seed={first.seed}, "
        f"confidence={first.confidence:.0%}"
    )
    print(
        f"{'metric':<14} {'analytic_ci':>20} {'bootstrap_ci':>20} "
        f"{'overlap':>7} {'point_in':>8} {'status':>9}  reasons"
    )
    for name, check in checks.items():
        _print_bootstrap_row(name, check)


def _print_comparison(
    comparison: ModelComparison,
    target_mde: float | None = None,
    completions_per_day: tuple[float, float] | None = None,
    bootstrap_checks: dict[str, BootstrapIntervalDiagnostic] | None = None,
) -> None:
    print()
    print(f"compare: {comparison.model_a} vs {comparison.model_b}")
    if comparison.information_fraction is None:
        sequential_header = ""
    else:
        sequential_header = f" {'seq_z':>8} {'seq_alpha':>9} {'seq_sig':>9}"
    header = (
        f"{'metric':<14} {'prop_a':>8} {'prop_b':>8} {'diff':>8} {'h':>7} "
        f"{'ci_low':>8} {'ci_high':>8} {'z':>7} {'p_value':>9} "
        f"{'sig':>10}{sequential_header} {'n_a':>6} {'n_b':>6}"
    )
    print(header)
    show_sequential = comparison.information_fraction is not None
    _print_proportion_row("ci_pass_rate", comparison.ci_pass_rate, show_sequential)
    _print_proportion_row(
        "attribution_rate", comparison.attribution_rate, show_sequential
    )
    print(
        f"multiple comparisons: raw alpha={comparison.alpha:.2f}; "
        "Bonferroni-adjusted threshold="
        f"{comparison.bonferroni_alpha:.4f} "
        f"for {comparison.comparison_family_size} tests. "
        "sig=corrected clears both thresholds; sig=raw_only clears only raw alpha."
    )
    if comparison.information_fraction is not None:
        print(
            "sequential correction: O'Brien-Fleming "
            f"information_fraction={comparison.information_fraction:.4f}; "
            "seq_sig=yes crosses the peeking-corrected boundary, "
            "seq_sig=not_yet does not."
        )
    print(
        "repeated-testing caveat: run --compare once on a pre-committed "
        "sample window; peeking as data accumulates inflates false positives "
        "beyond the nominal alpha."
    )
    print()
    print(f"power analysis: alpha={_DEFAULT_ALPHA:.2f}, power={_DEFAULT_POWER:.0%}")
    _print_power_row(
        "ci_pass_rate",
        comparison.ci_pass_rate,
        target_mde,
        completions_per_day,
    )
    _print_power_row(
        "attribution_rate",
        comparison.attribution_rate,
        target_mde,
        completions_per_day,
    )
    _print_bayesian_comparison(comparison)
    if bootstrap_checks is not None:
        _print_bootstrap_checks(bootstrap_checks)


def _attribution_outcomes_by_model(
    completions: list[InferenceCall],
    attributed_completions: list[AttributedCompletion],
    since_days: int | None,
    now: datetime,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population=None,
) -> dict[str, list[bool]]:
    windowed_calls, windowed_rows = model_report_inputs(
        completions,
        attributed_completions,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    surviving_ids = {
        row.inference_call_id for row in windowed_rows if row.abandonment is None
    }
    outcomes: dict[str, list[bool]] = {}
    for completion in sorted(
        windowed_calls,
        key=lambda completion: (
            inference_observed_at(completion),
            inference_fact_id(completion),
        ),
    ):
        model = inference_model(completion)
        if model is not None:
            outcomes.setdefault(model, []).append(
                inference_fact_id(completion) in surviving_ids
            )
    return outcomes


def _ci_outcomes_by_model(
    attributed_completions: list[AttributedCompletion],
    completions: list[InferenceCall],
    since_days: int | None,
    now: datetime,
    grain: CIGrain,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population=None,
) -> dict[str, list[bool]]:
    ci_population = prepare_report_ci(
        ci_population, attributed_completions, repository_context, now
    )
    calls, source_rows = model_report_inputs(
        completions,
        attributed_completions,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    call_by_id = {inference_fact_id(call): call for call in calls}
    groups: dict[str, list[AttributedCompletion]] = {}
    for row in source_rows:
        model = inference_model(call_by_id[row.inference_call_id])
        if model is not None:
            groups.setdefault(model, []).append(row)
    outcomes = {
        model: [
            trial.resolution.verdict == CIResult.PASSED
            for trial in model_ci_trials(
                members,
                grain,
                repository_context=repository_context,
                ci_population=ci_population,
            )
        ]
        for model, members in sorted(groups.items())
    }
    return {model: values for model, values in outcomes.items() if values}


def _effect_decay_reports(
    completions: list[InferenceCall],
    attributed_completions: list[AttributedCompletion],
    model_a: str,
    model_b: str,
    since_days: int | None,
    now: datetime,
    grain: CIGrain,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population=None,
) -> dict[str, EffectDecayReport]:
    attribution = _attribution_outcomes_by_model(
        completions,
        attributed_completions,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    ci = _ci_outcomes_by_model(
        attributed_completions,
        completions,
        since_days,
        now,
        grain,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    return {
        "ci_pass_rate": effect_decay_check(
            ci.get(model_a, []),
            ci.get(model_b, []),
        ),
        "attribution_rate": effect_decay_check(
            attribution.get(model_a, []),
            attribution.get(model_b, []),
        ),
    }


def _fmt_rate(numerator: int, denominator: int, rate: float) -> str:
    return f"{rate:.1%} ({numerator}/{denominator})"


def _fmt_raw_to_shrunk(
    numerator: int, denominator: int, raw_rate: float, shrunk_rate: float
) -> str:
    return f"{raw_rate:.1%} -> {shrunk_rate:.1%} ({numerator}/{denominator})"


def _fmt_delta(delta: float) -> str:
    return f"{delta:+.1%}"


def _print_adjusted_comparisons(check: StratificationCheck) -> None:
    if not check.adjusted_comparisons:
        return
    print("  Mantel-Haenszel adjusted risk difference (CI/p-value not computed):")
    for comparison in check.adjusted_comparisons:
        left = comparison.aggregate_left
        right = comparison.aggregate_right
        print(
            f"  - {comparison.left_model} vs {comparison.right_model}: "
            f"naive {_fmt_delta(comparison.naive_delta)} "
            f"[{_fmt_rate(left.numerator, left.denominator, left.rate)} vs "
            f"{_fmt_rate(right.numerator, right.denominator, right.rate)}], "
            f"adjusted {_fmt_delta(comparison.adjusted_delta)} "
            f"({comparison.compared_repos} repo strata, "
            f"{comparison.skipped_repos} skipped)"
        )


def _print_shrunk_rates(check: StratificationCheck) -> None:
    if not check.shrunk_rates:
        return
    print("  shrunk repo rates:")
    for entry in check.shrunk_rates:
        label = format_repository_label(entry.repo, entry.repository_identity)
        cells = ", ".join(
            f"{model}: {_fmt_raw_to_shrunk(rate.numerator, rate.denominator, rate.raw_rate, rate.shrunk_rate)}"
            for model, rate in sorted(entry.model_rates.items())
        )
        print(f"    {label}: {cells}")


def _print_stratification(checks: list[StratificationCheck]) -> None:
    print()
    print("stratification")
    for check in checks:
        if check.status == StratificationStatus.REVERSAL_DETECTED:
            print(f"{check.metric}: reversal detected")
            for comparison in check.comparisons:
                if not comparison.reversals:
                    continue
                left = comparison.aggregate_left
                right = comparison.aggregate_right
                print(
                    f"  {comparison.left_model} vs {comparison.right_model}: "
                    f"aggregate delta {_fmt_delta(comparison.aggregate_delta)} "
                    f"[{_fmt_rate(left.numerator, left.denominator, left.rate)} vs "
                    f"{_fmt_rate(right.numerator, right.denominator, right.rate)}]"
                )
                for reversal in comparison.reversals:
                    repo_left = reversal.left
                    repo_right = reversal.right
                    print(
                        f"  - {reversal.repo}: repo delta "
                        f"{_fmt_delta(reversal.repo_delta)} "
                        f"[{_fmt_rate(repo_left.numerator, repo_left.denominator, repo_left.rate)} "
                        f"vs "
                        f"{_fmt_rate(repo_right.numerator, repo_right.denominator, repo_right.rate)}]"
                    )
            _print_shrunk_rates(check)
        elif check.status == StratificationStatus.NO_REVERSAL_DETECTED:
            print(
                f"{check.metric}: no reversal detected "
                f"({check.checked_repos} repos checked)"
            )
            _print_shrunk_rates(check)
        else:
            print(f"{check.metric}: {check.status} ({check.reason})")
            _print_shrunk_rates(check)
        _print_adjusted_comparisons(check)


def _trend_payload(trend: ModelTemporalTrend) -> dict[str, object]:
    payload = asdict(trend)
    for window in payload["windows"]:
        window["window_start"] = window["window_start"].isoformat()
        window["window_end"] = window["window_end"].isoformat()
    return payload


def _fmt_trend_p_value(p_value: float | None) -> str:
    return "-" if p_value is None else f"{p_value:.4f}"


def _print_trends(trends: list[ModelTemporalTrend]) -> None:
    print()
    print("trend")
    if not trends:
        print("no data")
        return
    header = (
        f"{'model':<28} {'metric':<14} {'windows':>7} {'status':<24} "
        f"{'S':>5} {'z':>7} {'p_value':>9} {'latest':>17}"
    )
    print(header)
    for trend in trends:
        latest = trend.windows[-1] if trend.windows else None
        latest_text = (
            _fmt_rate(latest.numerator, latest.denominator, latest.rate)
            if latest is not None
            else "-"
        )
        print(
            f"{trend.model:<28} {trend.metric:<14} "
            f"{trend.test.sample_size:>7} {trend.test.status:<24} "
            f"{trend.test.s_statistic:>5} {trend.test.z_statistic:>7.2f} "
            f"{_fmt_trend_p_value(trend.test.p_value):>9} {latest_text:>17}"
        )
        if trend.test.status == TrendStatus.INSUFFICIENT_DATA:
            print(f"{'':<28} {'':<14} {trend.test.reason}")


def build_parser() -> argparse.ArgumentParser:
    """The argv contract for this report, extracted so the generated
    CLI reference can walk it."""
    parser = argparse.ArgumentParser(
        prog="model_report", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="window inference calls (and their attributed completions) to the last N days by "
        "capture time; default: all history",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON rows instead of a table"
    )
    parser.add_argument(
        "--trend",
        action="store_true",
        help="also bucket attribution_rate and ci_pass_rate into 7-day "
        "captured-at windows and run a Mann-Kendall monotonic trend test",
    )
    parser.add_argument(
        "--target-mde",
        type=float,
        default=None,
        help="with --compare, also report the equal-allocation n per arm "
        "needed to detect this target effect, expressed as raw proportion "
        "points (0.05 means five percentage points)",
    )
    parser.add_argument(
        "--completions-per-day",
        type=float,
        nargs=2,
        metavar=("MODEL_A_PER_DAY", "MODEL_B_PER_DAY"),
        default=None,
        help="with --target-mde, also estimate calendar days using per-arm "
        "completions/day in the same order as --compare; zero or negative "
        "rates omit the duration line",
    )
    parser.add_argument(
        "--bootstrap-check",
        action="store_true",
        help="with --compare, also run a fixed-seed bootstrap percentile CI "
        "diagnostic against the analytic two-proportion CI",
    )
    parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=None,
        help="resamples for --bootstrap-check "
        f"(default: {DEFAULT_BOOTSTRAP_ITERATIONS})",
    )
    parser.add_argument(
        "--bootstrap-seed",
        type=int,
        default=None,
        help="deterministic RNG seed for --bootstrap-check "
        f"(default: {DEFAULT_BOOTSTRAP_SEED})",
    )
    parser.add_argument(
        "--non-inferiority-margin",
        type=float,
        default=None,
        help="with --compare, test whether MODEL_B is not worse than MODEL_A "
        "by more than this raw proportion-point margin (0.05 means five "
        "percentage points)",
    )
    parser.add_argument(
        "--peek-fraction",
        type=float,
        default=None,
        help="with --compare, also report an O'Brien-Fleming sequential "
        "boundary read for this information fraction (0 < t <= 1)",
    )
    parser.add_argument(
        "--ci-grain",
        choices=[g.value for g in CIGrain],
        default=CIGrain.COMMIT.value,
        help="unit of analysis for ci_linked/ci_passed: 'commit' "
        "(default, correct) dedups a commit's file-grained attributed completions to one trial "
        "before counting; 'attributed_completion' is the legacy per-attributed-completion count, "
        "kept only as an escape hatch for explicit comparison — it "
        "inflates sample size and is not recommended for inference",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("MODEL_A", "MODEL_B"),
        help="two-proportion z-test of MODEL_A vs MODEL_B on CI pass rate "
        "and attribution rate, over the same --org/--since-days window; "
        "a model with no data in this window compares as insufficient data, "
        "not an error",
    )
    parser.add_argument(
        "--compare-all",
        nargs="+",
        metavar="MODEL",
        help="pairwise two-proportion z-tests for 2+ models with "
        "Benjamini-Hochberg FDR control across both metrics and all pairs; "
        "a model with no data in this window compares as "
        "insufficient data, not an error",
    )
    parser.add_argument(
        "--effect-decay",
        action="store_true",
        help="with --compare, print a regression-to-the-mean diagnostic: "
        "Cohen's h at 25/50/75/100%% prefixes for CI pass rate and attribution "
        "rate, using natural arrival order",
    )
    parser.add_argument(
        "--regret",
        action="store_true",
        help="with --compare-all, add expected regret for the apparent-best "
        "model on each metric",
    )
    add_database_url_argument(parser)
    parser.add_argument(
        "--mirror-path",
        default=os.environ.get("SEDIMENT_MIRROR_PATH", _DEFAULT_MIRROR_PATH),
        help="base dir for git mirrors (default: $SEDIMENT_MIRROR_PATH or "
        f"{_DEFAULT_MIRROR_PATH!r})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.since_days is not None and args.since_days <= 0:
        print("error: --since-days must be a positive integer", file=sys.stderr)
        return 2
    if args.target_mde is not None and not 0 < args.target_mde <= 1:
        print("error: --target-mde must be > 0 and <= 1", file=sys.stderr)
        return 2
    if args.compare and args.compare_all:
        print("error: use --compare or --compare-all, not both", file=sys.stderr)
        return 2
    if args.compare and args.compare[0] == args.compare[1]:
        print(
            "error: --compare requires two different model names",
            file=sys.stderr,
        )
        return 2
    if args.compare_all is not None and len(args.compare_all) < 2:
        print("error: --compare-all requires at least two models", file=sys.stderr)
        return 2
    if args.compare_all is not None and len(set(args.compare_all)) != len(
        args.compare_all
    ):
        print("error: --compare-all requires unique model names", file=sys.stderr)
        return 2
    if (
        args.bootstrap_check
        and args.bootstrap_iterations is not None
        and args.bootstrap_iterations < 2
    ):
        print("error: --bootstrap-iterations must be at least 2", file=sys.stderr)
        return 2
    if args.non_inferiority_margin is not None and not (
        0 < args.non_inferiority_margin <= 1
    ):
        print("error: --non-inferiority-margin must be > 0 and <= 1", file=sys.stderr)
        return 2
    if args.peek_fraction is not None and not 0 < args.peek_fraction <= 1:
        print("error: --peek-fraction must be > 0 and <= 1", file=sys.stderr)
        return 2
    for flag, given, requires, present in (
        ("--target-mde", args.target_mde is not None, "--compare", args.compare),
        (
            "--completions-per-day",
            args.completions_per_day is not None,
            "--target-mde",
            args.target_mde is not None,
        ),
        ("--bootstrap-check", args.bootstrap_check, "--compare", args.compare),
        (
            "--bootstrap-iterations",
            args.bootstrap_iterations is not None,
            "--bootstrap-check",
            args.bootstrap_check,
        ),
        (
            "--bootstrap-seed",
            args.bootstrap_seed is not None,
            "--bootstrap-check",
            args.bootstrap_check,
        ),
        (
            "--non-inferiority-margin",
            args.non_inferiority_margin is not None,
            "--compare",
            args.compare,
        ),
        ("--peek-fraction", args.peek_fraction is not None, "--compare", args.compare),
        ("--effect-decay", args.effect_decay, "--compare", args.compare),
        ("--regret", args.regret, "--compare-all", args.compare_all),
    ):
        if given and not present:
            print(f"error: {flag} requires {requires}", file=sys.stderr)
            return 2
    completions_per_day = (
        tuple(args.completions_per_day)
        if args.completions_per_day is not None
        else None
    )
    bootstrap_iterations = (
        args.bootstrap_iterations
        if args.bootstrap_iterations is not None
        else DEFAULT_BOOTSTRAP_ITERATIONS
    )
    bootstrap_seed = (
        args.bootstrap_seed
        if args.bootstrap_seed is not None
        else DEFAULT_BOOTSTRAP_SEED
    )

    try:
        org_id = normalize_org_id(args.org)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report_policy = OutcomeReportPolicy(ci_grain=CIGrain(args.ci_grain))
    now = datetime.now(UTC)
    try:
        with one_shot_fact_store(
            args.database_url, operation="generate model report"
        ) as store:
            mirrors = MirrorManager(args.mirror_path)
            if args.since_days is not None:
                operational = generate_operational_model_report(
                    store,
                    mirrors,
                    org_id,
                    OperationalReportScope.trailing_days(args.since_days, as_of=now),
                    report_policy=report_policy,
                    include_trends=args.trend,
                )
                ci_population = operational.ci_population
                repository_context = operational.repository_context
                result = operational.result
                completions = operational.completions
                attributed_completions = operational.attributed_completions
            else:
                with store.read_snapshot() as snapshot:
                    repository_context = read_repository_context(
                        snapshot, org_id, as_of=now
                    )
                    ci_population = snapshot.read_ci_outcome_projections(
                        org_id, captured_through=now, limit=50_000
                    )
                    completions = snapshot.read_rollout_inference_calls(org_id)
                    assembly = assemble_attributed_completions_result(
                        snapshot,
                        mirrors,
                        org_id,
                        ci_population=ci_population,
                        repository_context=repository_context,
                        as_of=now,
                    )
                    attributed_completions = assembly.rows
                    attribution_share, attribution_alerts = (
                        derive_model_report_attribution_share(
                            snapshot,
                            mirrors,
                            org_id,
                            None,
                            now=now,
                            repository_context=repository_context,
                        )
                    )
                    quarantine_revision = snapshot.quarantine_revision(org_id)
                    fate_result = derive_fate_result(
                        [
                            observation
                            for observation in snapshot.read_edit_observation_projections(
                                org_id
                            )
                            if observation.captured_at <= now
                        ],
                        four_gram_containment,
                        quarantine_revision=quarantine_revision,
                    )
                    result = build_model_report_result(
                        completions,
                        attributed_completions,
                        ci_population=ci_population,
                        since_days=None,
                        now=now,
                        decisions=snapshot.read_decision_projections(org_id),
                        policy=report_policy,
                        include_trends=args.trend,
                        attribution_share=attribution_share,
                        attribution_alerts=attribution_alerts,
                        abandonment=build_abandonment_summary(assembly),
                        quarantine_revision=quarantine_revision,
                        fate_result=fate_result,
                        repository_context=repository_context,
                    )
                    result = with_model_assembly_diagnostics(result, assembly)
    except Exception as exc:
        print(f"error: couldn't generate model report: {exc}", file=sys.stderr)
        return 1
    rows = result.rows

    comparison = None
    bootstrap_checks = None
    comparison_all = None
    non_inferiority = None
    effect_decay = None
    regret_reports = None
    if args.compare:
        model_a, model_b = args.compare
        report_a = _row_for_model(rows, model_a, args.since_days)
        report_b = _row_for_model(rows, model_b, args.since_days)
        comparison = compare_models(
            report_a, report_b, information_fraction=args.peek_fraction
        )
        if args.bootstrap_check:
            bootstrap_checks = _bootstrap_checks_for_reports(
                report_a,
                report_b,
                comparison,
                bootstrap_iterations=bootstrap_iterations,
                bootstrap_seed=bootstrap_seed,
            )
        if args.non_inferiority_margin is not None:
            non_inferiority = _non_inferiority_results(
                report_a,
                report_b,
                args.non_inferiority_margin,
            )
        if args.effect_decay:
            effect_decay = _effect_decay_reports(
                completions,
                attributed_completions,
                model_a,
                model_b,
                args.since_days,
                now,
                report_policy.ci_grain,
                ci_population=ci_population,
                repository_context=repository_context,
            )
    elif args.compare_all:
        compare_all_rows = [
            _row_for_model(rows, model, args.since_days) for model in args.compare_all
        ]
        comparison_all = compare_all_models(compare_all_rows)
        if args.regret:
            regret_reports = _regret_reports_for_rows(compare_all_rows)

    if args.json:
        payload = base_model_report_payload(result)
        if args.trend:
            payload["trends"] = [_trend_payload(t) for t in result.trends]
        if comparison is not None:
            payload["comparison"] = asdict(comparison)
            target_payload = _target_mde_payload(
                comparison,
                args.target_mde,
                completions_per_day,
            )
            if target_payload is not None:
                payload["target_mde"] = target_payload
            if bootstrap_checks is not None:
                payload["bootstrap_interval_diagnostic"] = {
                    name: asdict(check) for name, check in bootstrap_checks.items()
                }
            if non_inferiority is not None:
                payload["non_inferiority"] = {
                    metric: asdict(result) for metric, result in non_inferiority.items()
                }
            if effect_decay is not None:
                payload["effect_decay"] = {
                    metric: asdict(report) for metric, report in effect_decay.items()
                }
        if comparison_all is not None:
            payload["comparison_all"] = asdict(comparison_all)
        if regret_reports is not None:
            payload["regret"] = {
                metric: asdict(report) for metric, report in regret_reports.items()
            }
        print(json.dumps(payload, indent=2))
    else:
        window = f"last {args.since_days} day(s)" if args.since_days else "all history"
        print(f"org: {org_id}  window: {window}")
        _print_attribution_share(result.attribution_share, result.attribution_alerts)
        _print_table(rows)
        for name, populations in (
            ("repository", result.repository_skipped),
            ("CI", result.ci_skipped),
        ):
            for unit, reasons in sorted(populations.items()):
                print(f"{name} skips ({unit}): {_format_breakdown(reasons)}")
        _print_fate_diagnostics(result)
        _print_signal_funnel(result.signal_funnel)
        _print_abandonment(result.abandonment)
        if comparison is not None:
            _print_comparison(
                comparison,
                args.target_mde,
                completions_per_day,
                bootstrap_checks,
            )
            if non_inferiority is not None:
                _print_non_inferiority(comparison, non_inferiority)
            if effect_decay is not None:
                _print_effect_decay(effect_decay)
        if comparison_all is not None:
            _print_compare_all(comparison_all, regret_reports)
        if rows:
            _print_stratification(result.stratification)
        if args.trend:
            _print_trends(result.trends)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
