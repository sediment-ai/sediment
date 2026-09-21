# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract checks for the accepted-work lifecycle artifact."""

from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from sediment_core import (
    AgentHarness,
    CIProvider,
    CIOutcome,
    PullRequestMerge,
    CIResult,
    DeveloperDecision,
    EditObservation,
    GatewayProvider,
    InferenceCall,
    InteractionMode,
    SessionCommitObservation,
)
from sediment_derive import (
    Provenance,
    MergeRetentionResult,
    MergeMembershipOutcome,
    AttributionSource,
)

from sediment_derive import MirrorManager
from sediment_export import accepted_work_lifecycle as lifecycle
from sediment_export import (
    LifecycleCoverage,
    LifecycleStage,
    LifecycleReportPolicy,
    generate_accepted_work_lifecycle_report,
    OperationalReportScope,
)


def test_lifecycle_policy_rejects_unbounded_example_population() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        LifecycleReportPolicy(example_session_limit=0)


def test_lifecycle_metrics_reject_invalid_counts_and_rates() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        LifecycleStage(-1, 1, 0.0, 1, 0.0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        LifecycleStage(1, 1, 1.1, 1, 1.0)
    with pytest.raises(ValueError, match="coverage missing reason"):
        LifecycleCoverage(observed=0, missing={"guessed": 1})
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        lifecycle.LifecycleRetentionDistribution(1, 1.1, None, None, None)
    with pytest.raises(ValueError, match="non-negative"):
        lifecycle.LifecycleRetentionThreshold(0.8, -1, 1, 0.0)


def test_session_examples_sort_deduplicate_and_cap_deterministically() -> None:
    forward = lifecycle._examples(
        "accepted", {"session-c", "session-a", "session-b"}, 2
    )
    reverse = lifecycle._examples(
        "accepted", {"session-b", "session-c", "session-a"}, 2
    )
    assert forward == reverse
    assert forward.session_ids == ("session-a", "session-b")


def test_call_ci_linkage_distinguishes_missing_non_verdict_and_conflict() -> None:
    commit = {("acme/backend", "a" * 40)}
    assert lifecycle._call_ci_status(commit, set(), set(), {}) == "missing"
    assert lifecycle._call_ci_status(commit, commit, set(), {}) == "non_verdict"
    assert lifecycle._call_ci_status(commit, commit, commit, {}) == "ambiguous"


def test_call_ci_verdict_is_conservative_across_attributed_commits() -> None:
    first = ("acme/backend", "a" * 40)
    second = ("acme/backend", "b" * 40)

    def resolution(verdict, workflows=()):
        return SimpleNamespace(verdict=verdict, workflow_resolutions=workflows)

    passed = resolution(CIResult.PASSED)
    failed = resolution(CIResult.FAILED)
    non_verdict = resolution(None)
    assert (
        lifecycle._call_ci_status(
            {first, second},
            {first, second},
            set(),
            {first: passed, second: non_verdict},
        )
        == "passed"
    )
    assert (
        lifecycle._call_ci_status(
            {first, second}, {first, second}, set(), {first: passed, second: failed}
        )
        == "failed"
    )
    workflows = (
        SimpleNamespace(verdict=CIResult.PASSED),
        SimpleNamespace(verdict=CIResult.FAILED),
    )
    assert (
        lifecycle._call_ci_status(
            {first}, {first}, set(), {first: resolution(None, workflows)}
        )
        == "ambiguous"
    )


def test_lifecycle_schema_excludes_captured_content() -> None:
    schema = json.loads(
        Path("schemas/derived-artifacts/accepted-work-lifecycle/v3.json").read_text()
    )
    serialized = json.dumps(schema, sort_keys=True)
    for forbidden in (
        "input_messages",
        "output_messages",
        "applied_text",
        "observed_file_text",
        "raw",
    ):
        assert forbidden not in serialized


def test_empty_fact_snapshot_produces_a_complete_zero_report(
    tmp_path: Path, postgres_store
) -> None:
    report = generate_accepted_work_lifecycle_report(
        postgres_store, MirrorManager(tmp_path / "mirrors"), "acme"
    )

    assert report.provenance.attribution.policy_version == "3"
    assert report.provenance.merge_retention.policy_version == "3"
    assert report.accepted_work.accepted_calls == 0
    assert report.edit_retention.observations == 0
    assert report.merge_durability.coverage_qualification == (
        "partial_pull_request_history"
    )
    assert report.session_attrition.eligible_sessions == 0
    assert all(component.rate is None for component in report.rework)

    schema = json.loads(
        Path("schemas/derived-artifacts/accepted-work-lifecycle/v3.json").read_text()
    )
    Draft202012Validator(schema).validate(json.loads(json.dumps(asdict(report))))


def test_scoped_lifecycle_uses_only_bounded_authoritative_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = OperationalReportScope.trailing_days(
        30, as_of=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    )
    calls: list[tuple[str, dict]] = []

    class Snapshot:
        def quarantine_revision(self, org_id):
            return 0

        def __getattr__(self, name):
            if not name.startswith("read_"):
                raise AttributeError(name)

            def read(*args, **kwargs):
                calls.append((name, kwargs))
                return []

            return read

    class Store:
        @contextmanager
        def read_snapshot(self):
            yield Snapshot()

    class Mirrors:
        @contextmanager
        def read_repository_snapshot(self, keys):
            yield self

    provenance = Provenance("1", 0)
    captured: dict[str, dict] = {}

    def attribution(*args, **kwargs):
        captured["attribution"] = kwargs
        return SimpleNamespace(attributions=[], skipped=Counter())

    def abandonment(*args, **kwargs):
        captured["abandonment"] = kwargs
        return SimpleNamespace(outcomes=[], skipped=Counter(), provenance=provenance)

    def retention(*args, **kwargs):
        captured["retention"] = kwargs
        return MergeRetentionResult(
            membership_outcomes=[],
            rows=[],
            skipped=Counter(),
            membership=Counter(),
            attributed_candidates=0,
            joined_candidates=0,
            joined_pull_requests=0,
            provenance=provenance,
        )

    monkeypatch.setattr(lifecycle, "derive_attribution_result", attribution)
    monkeypatch.setattr(lifecycle, "derive_abandonment", abandonment)
    monkeypatch.setattr(lifecycle, "derive_merge_retention_result", retention)
    monkeypatch.setattr(
        lifecycle,
        "derive_fate_result",
        lambda *a, **k: SimpleNamespace(
            fates=[], skipped=Counter(), provenance=provenance
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "derive_ci_resolution_result",
        lambda *a, **k: SimpleNamespace(
            resolutions=[], skipped=Counter(), conflicting_commit_keys=frozenset()
        ),
    )

    generate_accepted_work_lifecycle_report(Store(), Mirrors(), "acme", scope=scope)

    reads = dict(calls)
    assert "read_inference_calls" not in reads
    assert reads["read_inference_call_summaries"] == {
        "observed_between": (scope.cohort_start, scope.cohort_end),
        "limit": scope.max_inference_calls,
    }
    assert reads["read_decisions"]["captured_through"] == scope.as_of
    assert reads["read_session_commit_observations"]["as_of"] == scope.as_of
    assert captured["attribution"]["pushes"] == []
    assert captured["attribution"]["note_session_ids_by_commit"] == {}
    assert captured["abandonment"]["as_of"] == scope.as_of
    assert captured["abandonment"]["session_commits"] == {}
    assert captured["retention"]["merges"] == []
    assert captured["retention"]["revisions"] == []
    assert captured["retention"]["ci_outcomes"] == []


def test_scoped_lifecycle_rejects_oversized_derived_pr_keys_before_read(
    tmp_path, postgres_store, monkeypatch
):
    from sediment_core import store as store_module

    scope = OperationalReportScope.trailing_days(
        30, as_of=datetime(2026, 9, 6, 12, tzinfo=UTC)
    )
    for index, repo in enumerate(("acme/one", "acme/two"), 1):
        postgres_store.store_pull_request_merge(
            PullRequestMerge(
                org_id="acme",
                provider="github",
                repo=repo,
                pr_number=index,
                head_repo=repo,
                head_ref="topic",
                head_sha="a" * 40,
                base_ref="main",
                base_sha="b" * 40,
                merge_commit_sha="c" * 40,
                merged_at=scope.as_of,
                captured_at=scope.as_of,
            )
        )
    attributions = [
        SimpleNamespace(
            org_id="acme", repo=repo, commit_sha="a" * 40, repository_identity=None
        )
        for repo in ("acme/one", "acme/two")
    ]
    monkeypatch.setattr(store_module, "REPOSITORY_FILTER_KEY_LIMIT", 1)
    monkeypatch.setattr(
        lifecycle,
        "derive_attribution_result",
        lambda *a, **k: SimpleNamespace(attributions=attributions, skipped=Counter()),
    )
    with pytest.raises(ValueError, match="Repository filter exceeds"):
        generate_accepted_work_lifecycle_report(
            postgres_store, MirrorManager(tmp_path / "mirrors"), "acme", scope=scope
        )


def test_scoped_lifecycle_excludes_other_cohorts_and_late_evidence(
    tmp_path: Path, postgres_store
) -> None:
    as_of = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    included = InferenceCall(
        inference_call_id="inference-included",
        org_id="acme",
        session_id="session-included",
        gateway_provider=GatewayProvider.PORTKEY,
        model="model-a",
        input_messages=[],
        output_messages=[],
        model_call_id="call-included",
        observed_at=as_of.replace(day=4),
    )
    outside = included.model_copy(
        update={
            "inference_call_id": "inference-outside",
            "session_id": "session-outside",
            "model_call_id": "call-outside",
            "observed_at": as_of.replace(month=7),
        }
    )
    postgres_store.store_inference_call(outside)
    postgres_store.store_inference_call(included)
    base_decision = DeveloperDecision(
        decision_id="decision-included",
        org_id="acme",
        session_id="session-included",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="service.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id="call-included",
        occurred_at=as_of.replace(day=4),
        captured_at=as_of,
    )
    postgres_store.store_decision(base_decision)
    postgres_store.store_decision(
        base_decision.model_copy(
            update={
                "decision_id": "decision-late",
                "accepted": False,
                "occurred_at": as_of.replace(day=7),
                "captured_at": as_of.replace(day=7),
            }
        )
    )
    postgres_store.store_decision(
        base_decision.model_copy(
            update={
                "decision_id": "decision-outside",
                "session_id": "session-outside",
                "call_id": "call-outside",
            }
        )
    )

    report = generate_accepted_work_lifecycle_report(
        postgres_store,
        MirrorManager(tmp_path / "mirrors"),
        "acme",
        scope=OperationalReportScope(
            cohort_start=as_of.replace(day=1),
            cohort_end=as_of.replace(day=5),
            as_of=as_of,
        ),
    )

    assert report.accepted_work.accepted_calls == 1
    explicit_rejects = next(
        item for item in report.rework if item.name == "explicit_rejects"
    )
    assert explicit_rejects.count == 0
    assert explicit_rejects.denominator == 1


def test_scoped_lifecycle_rejects_inference_cohort_overflow(
    tmp_path: Path, postgres_store
) -> None:
    as_of = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    for suffix in ("a", "b"):
        postgres_store.store_inference_call(
            InferenceCall(
                inference_call_id=f"inference-{suffix}",
                org_id="acme",
                session_id=f"session-{suffix}",
                gateway_provider=GatewayProvider.PORTKEY,
                model="model-a",
                input_messages=[],
                output_messages=[],
                observed_at=as_of.replace(day=4),
            )
        )

    with pytest.raises(ValueError, match="Inference call cohort exceeds 1"):
        generate_accepted_work_lifecycle_report(
            postgres_store,
            MirrorManager(tmp_path / "mirrors"),
            "acme",
            scope=OperationalReportScope(
                cohort_start=as_of.replace(day=1),
                cohort_end=as_of.replace(day=5),
                as_of=as_of,
                max_inference_calls=1,
            ),
        )


def test_lifecycle_schema_rejects_invalid_metrics_vocabularies_and_content(
    tmp_path: Path, postgres_store
) -> None:
    report = generate_accepted_work_lifecycle_report(
        postgres_store, MirrorManager(tmp_path / "mirrors"), "acme"
    )
    schema = json.loads(
        Path("schemas/derived-artifacts/accepted-work-lifecycle/v3.json").read_text()
    )
    validator = Draft202012Validator(schema)
    valid = json.loads(json.dumps(asdict(report)))
    invalid_rows = []

    negative_count = deepcopy(valid)
    negative_count["accepted_work"]["accepted_calls"] = -1
    invalid_rows.append(negative_count)

    invalid_rate = deepcopy(valid)
    invalid_rate["accepted_work"]["attributed"]["rate"] = 1.1
    invalid_rows.append(invalid_rate)

    invalid_qualification = deepcopy(valid)
    invalid_qualification["merge_durability"]["coverage_qualification"] = "complete"
    invalid_rows.append(invalid_qualification)

    invalid_skip = deepcopy(valid)
    invalid_skip["stratum_skips"] = {"guessed_identity": 1}
    invalid_rows.append(invalid_skip)

    invalid_coverage = deepcopy(valid)
    invalid_coverage["accepted_work"]["coverage"]["missing"] = {"guessed_evidence": 1}
    invalid_rows.append(invalid_coverage)

    invalid_distribution = deepcopy(valid)
    invalid_distribution["merge_durability"]["head"]["distribution"]["mean"] = 1.1
    invalid_rows.append(invalid_distribution)

    invalid_threshold_count = deepcopy(valid)
    invalid_threshold_count["merge_durability"]["head"]["thresholds"][0]["below"] = -1
    invalid_rows.append(invalid_threshold_count)

    invalid_threshold_rate = deepcopy(valid)
    invalid_threshold_rate["merge_durability"]["head"]["thresholds"][0]["rate"] = 2.0
    invalid_rows.append(invalid_threshold_rate)

    invalid_policy = deepcopy(valid)
    invalid_policy["policy"]["example_session_limit"] = 0
    invalid_rows.append(invalid_policy)

    captured_content = deepcopy(valid)
    captured_content["prompt"] = "secret"
    invalid_rows.append(captured_content)

    for row in invalid_rows:
        assert list(validator.iter_errors(row))


def test_external_line_evidence_survives_unscoreable_fate(
    tmp_path: Path, postgres_store, monkeypatch
) -> None:
    observed_at = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    for suffix, added, removed in (
        ("known-zero", 0, 0),
        ("changed", 3, 2),
        ("absent", None, None),
    ):
        postgres_store.store_edit_observation(
            EditObservation(
                observation_id=f"observation-{suffix}",
                org_id="acme",
                session_id=f"session-{suffix}",
                agent_harness=AgentHarness.CLAUDE_CODE,
                file_path="service.py",
                call_id=f"call-{suffix}",
                applied_text="return 1",
                observed_file_text="return 1",
                external_lines_added=added,
                external_lines_removed=removed,
                occurred_at=observed_at,
            )
        )

    def fail(*args, **kwargs):
        raise ValueError("unscoreable")

    monkeypatch.setattr(lifecycle, "four_gram_containment", fail)
    report = generate_accepted_work_lifecycle_report(
        postgres_store, MirrorManager(tmp_path / "mirrors"), "acme"
    )

    assert report.edit_retention.derived_fates == 0
    assert report.edit_retention.known_external_line_counts == 2
    assert report.edit_retention.external_lines_added == 3
    assert report.edit_retention.external_lines_removed == 2
    external = next(
        item for item in report.rework if item.name == "external_line_changes"
    )
    assert (external.count, external.denominator, external.rate) == (1, 2, 0.5)


def test_generator_hand_computed_progression_attrition_strata_and_cursor(
    monkeypatch, tmp_path: Path
) -> None:
    observed_at = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    stages = (
        "accepted",
        "attributed",
        "pull-request",
        "non-verdict",
        "failed",
        "passed",
    )
    calls = [
        InferenceCall(
            inference_call_id=f"inference-{stage}",
            org_id="acme",
            session_id=f"session-{stage}",
            gateway_provider=GatewayProvider.PORTKEY,
            model=None if stage == "accepted" else "model-a",
            input_messages=[],
            output_messages=[],
            model_call_id=f"call-{stage}",
            observed_at=observed_at,
        )
        for stage in stages
    ]
    decisions = [
        DeveloperDecision(
            decision_id=f"decision-{stage}",
            org_id="acme",
            session_id=f"session-{stage}",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="service.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id=f"call-{stage}",
            occurred_at=observed_at,
        )
        for stage in stages
    ]
    decisions.append(
        decisions[-1].model_copy(
            update={
                "decision_id": "decision-passed-codex",
                "agent_harness": AgentHarness.CODEX,
            }
        )
    )
    decisions.append(
        DeveloperDecision(
            decision_id="cursor-implicit",
            org_id="acme",
            session_id="session-cursor-implicit",
            agent_harness=AgentHarness.CURSOR,
            file_path="cursor.py",
            accepted=True,
            explicit=False,
            interaction_mode=InteractionMode.AGENT,
            call_id=None,
            occurred_at=observed_at,
        )
    )

    commits = {stage: (str(index) * 40) for index, stage in enumerate(stages[1:], 1)}
    attrs = [
        SimpleNamespace(
            org_id="acme",
            repository_identity=None,
            repo="acme/backend",
            commit_sha=commits[stage],
            file_path="service.py",
            inference_call_id=f"inference-{stage}",
            session_id=f"session-{stage}",
        )
        for stage in stages[1:]
    ]
    attrs.extend(
        (
            SimpleNamespace(**{**attrs[-1].__dict__, "file_path": "second.py"}),
            SimpleNamespace(
                org_id="acme",
                repository_identity=None,
                repo="acme/frontend",
                commit_sha="f" * 40,
                file_path="ui.py",
                inference_call_id="inference-passed",
                session_id="session-passed",
            ),
            SimpleNamespace(
                org_id="acme",
                repository_identity=None,
                repo="acme/backend",
                commit_sha="e" * 40,
                file_path="tab.py",
                inference_call_id="cursor-tab-attribution",
                session_id="session-cursor-tab",
            ),
        )
    )
    joined_calls = {"pull-request", "non-verdict", "failed", "passed"}
    memberships = [
        MergeMembershipOutcome(
            org_id="acme",
            repository_identity=None,
            repo="acme/backend",
            source_commit_sha=commits[stage],
            source_file_path="file.py",
            attribution_source=AttributionSource.JACCARD,
            attribution_similarity_score=1.0,
            provenance=Provenance("1", 0),
            status="joined",
            inference_call_id=f"inference-{stage}",
            session_id=f"session-{stage}",
            pr_number=index,
            merge_id=f"merge-{index}",
        )
        for index, stage in enumerate(sorted(joined_calls), 1)
    ]
    memberships.extend(
        [
            next(
                item
                for item in memberships
                if item.inference_call_id == "inference-passed"
            )
        ]
        * 2
    )

    def workflow(verdict, name):
        return SimpleNamespace(
            provider=CIProvider.GITHUB_ACTIONS,
            workflow_id=name,
            workflow_path=f".github/{name}.yml",
            workflow_name=name,
            verdict=verdict,
        )

    resolutions = [
        SimpleNamespace(
            org_id="acme",
            repository_identity=None,
            repo="acme/backend",
            commit_sha=commits[stage],
            verdict=verdict,
            workflow_resolutions=(workflow(verdict, name),),
            provenance=Provenance("1", 0),
        )
        for stage, verdict, name in (
            ("non-verdict", None, "checks"),
            ("failed", CIResult.FAILED, "test"),
            ("passed", CIResult.PASSED, "test"),
        )
    ]
    resolutions.append(
        SimpleNamespace(
            org_id="acme",
            repository_identity=None,
            repo="acme/frontend",
            commit_sha="f" * 40,
            verdict=CIResult.PASSED,
            workflow_resolutions=(workflow(CIResult.PASSED, "lint"),),
            provenance=Provenance("1", 0),
        )
    )
    ci_outcomes = [
        CIOutcome(
            outcome_id=f"ci-{stage}",
            result=resolution.verdict or CIResult.UNKNOWN,
            captured_at=observed_at,
            org_id="acme",
            provider=CIProvider.GITHUB_ACTIONS,
            run_id=f"run-{stage}",
            repo=resolution.repo,
            commit_sha=resolution.commit_sha,
            branch="main",
            workflow_id=resolution.workflow_resolutions[0].workflow_id,
            workflow_name=resolution.workflow_resolutions[0].workflow_name,
            workflow_path=resolution.workflow_resolutions[0].workflow_path,
        )
        for stage, resolution in zip(
            ("non-verdict", "failed", "passed", "lint"), resolutions, strict=True
        )
    ]
    outcomes = [
        SimpleNamespace(status=status, session_id=f"session-{status}")
        for status in ("committed", "abandoned", "in_flight", "attribution_unavailable")
    ]

    class Snapshot:
        def read_repository_identities(self, org_id, **kwargs):
            from sediment_derive import repository_identity_evidence_of

            return [
                repository_identity_evidence_of(item)
                for item in (
                    *self.read_session_commit_observations(org_id),
                    *ci_outcomes,
                )
            ]

        def read_repository_renames(self, org_id, **kwargs):
            return []

        def read_ci_outcome_projections(self, org_id, **kwargs):
            return ci_outcomes

        def read_session_commit_observations(self, org_id, **kwargs):
            return [
                SessionCommitObservation(
                    observation_id=f"obs/{item.inference_call_id}/{item.commit_sha}",
                    org_id=org_id,
                    repo=item.repo,
                    commit_sha=item.commit_sha,
                    session_id=item.session_id,
                    source_push_id="push",
                    captured_at=datetime(2026, 9, 1, tzinfo=UTC),
                )
                for item in attrs
            ]

        def read_pushes(self, org_id, **kwargs):
            return []

        def read_pull_request_merges(self, org_id, **kwargs):
            return []

        def read_pull_request_revisions(self, org_id, **kwargs):
            return []

        def read_report_inference_calls(self, org_id, *, observed_through):
            return [
                call for call in reversed(calls) if call.observed_at <= observed_through
            ]

        def read_decisions(self, org_id, **kwargs):
            return list(reversed(decisions))

        def read_edit_observations(self, org_id, **kwargs):
            return []

        def read_retry_linkages(self, org_id, **kwargs):
            return [SimpleNamespace()]

        def read_ci_outcomes(self, org_id, **kwargs):
            return list(reversed(ci_outcomes))

        def quarantine_revision(self, org_id):
            return 0

        @contextmanager
        def read_snapshot(self):
            yield self

    class Store:
        @contextmanager
        def read_snapshot(self):
            yield Snapshot()

    class Mirrors:
        @contextmanager
        def read_repository_snapshot(self, keys):
            yield self

    provenance = Provenance("1", 0)
    monkeypatch.setattr(
        lifecycle,
        "derive_attribution_result",
        lambda *a, **k: SimpleNamespace(
            attributions=list(reversed(attrs)), skipped=Counter()
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "derive_abandonment",
        lambda *a, **k: SimpleNamespace(
            outcomes=list(reversed(outcomes)), skipped=Counter(), provenance=provenance
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "derive_fate_result",
        lambda *a, **k: SimpleNamespace(
            fates=[], skipped=Counter(), provenance=provenance
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "derive_ci_resolution_result",
        lambda *a, **k: SimpleNamespace(
            resolutions=list(reversed(resolutions)),
            skipped=Counter(),
            conflicting_commit_keys=frozenset(),
        ),
    )
    retention = MergeRetentionResult(
        session_commit_observations=tuple(
            Snapshot().read_session_commit_observations("acme")
        ),
        membership_outcomes=list(reversed(memberships)),
        rows=[],
        skipped=Counter(),
        membership=Counter(),
        attributed_candidates=len(attrs),
        joined_candidates=len(memberships),
        joined_pull_requests=4,
        provenance=provenance,
    )
    monkeypatch.setattr(
        lifecycle, "derive_merge_retention_result", lambda *a, **k: retention
    )

    report = generate_accepted_work_lifecycle_report(
        Store(),
        Mirrors(),
        "acme",
        policy=LifecycleReportPolicy(example_session_limit=2),
    )
    assert report.accepted_work.accepted_calls == 6
    assert report.accepted_work.attributed.count == 5
    assert report.accepted_work.pull_request_membership.count == 4
    assert report.accepted_work.ci_linked.count == 3
    assert (report.accepted_work.ci_passed, report.accepted_work.ci_failed) == (1, 1)
    assert report.accepted_work.skips["ci_non_verdict"] == 1
    assert [
        report.session_attrition.committed,
        report.session_attrition.abandoned,
        report.session_attrition.in_flight,
        report.session_attrition.attribution_unavailable,
    ] == [1, 1, 1, 1]
    assert (
        next(item for item in report.rework if item.name == "retry_linkages").count == 1
    )
    assert report.stratum_skips == {
        "missing_model": 1,
        "missing_repository": 1,
        "missing_workflow": 3,
        "multiple_agent_harnesses": 1,
        "multiple_repositories": 1,
        "multiple_workflows": 1,
    }
    assert report.accepted_work.coverage.unsupported_by_integration == {
        "cursor_human_explicit_inference_capture": 1
    }
    assert all(
        "session-cursor-tab" not in example.session_ids
        for example in report.accepted_work.examples
    )

    calls.reverse()
    decisions.reverse()
    ci_outcomes.reverse()
    attrs.reverse()
    outcomes.reverse()
    resolutions.reverse()
    retention.membership_outcomes.reverse()
    shuffled = generate_accepted_work_lifecycle_report(
        Store(),
        Mirrors(),
        "acme",
        policy=LifecycleReportPolicy(example_session_limit=2),
    )
    assert shuffled == report


def test_lifecycle_counts_cross_session_attachment_without_accepting_call(
    tmp_path: Path, postgres_store_factory
) -> None:
    call = InferenceCall(
        org_id="acme",
        session_id="call-session",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[],
        model_call_id="unique",
    )
    decision = DeveloperDecision(
        org_id="acme",
        session_id="decision-session",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="a.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id="unique",
        occurred_at=datetime.now(UTC),
    )
    _, first = postgres_store_factory()
    _, shuffled = postgres_store_factory()
    first.store_inference_call(call)
    first.store_decision(decision)
    shuffled.store_decision(decision)
    shuffled.store_inference_call(call)
    mirrors = MirrorManager(tmp_path / "mirrors")
    report = generate_accepted_work_lifecycle_report(first, mirrors, "acme")
    assert report.accepted_work.accepted_calls == 0
    assert report.accepted_work.skips == {"decision_session_mismatch": 1}
    assert report.accepted_work.coverage.missing == {"decision_session_mismatch": 1}
    assert report.policy.policy_version == "3"
    assert generate_accepted_work_lifecycle_report(first, mirrors, "acme") == report
    assert generate_accepted_work_lifecycle_report(shuffled, mirrors, "acme") == report
    schema = json.loads(
        Path("schemas/derived-artifacts/accepted-work-lifecycle/v3.json").read_text()
    )
    Draft202012Validator(schema).validate(json.loads(json.dumps(asdict(report))))
