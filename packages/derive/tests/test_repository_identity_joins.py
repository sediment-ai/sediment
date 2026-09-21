# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared joins preserve captured repository lifetimes and CI attempt evidence."""

from datetime import UTC, datetime, timedelta
from itertools import permutations
from zoneinfo import ZoneInfo

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    ForgeProvider,
    Push,
    SessionCommitObservation,
)
from sediment_derive import repository_identity as identity
from sediment_derive import attachment, ci_resolution, session_commit

ORG = "acme-corp"
A = "acme-corp/alpha"
B = "acme-corp/beta"
SHA = "a" * 40
AT = datetime(2026, 9, 12, tzinfo=UTC)
BOUNDARY = AT + timedelta(hours=1)


def triple(repository_id="101", host="github.com"):
    return dict(
        repository_provider=ForgeProvider.GITHUB if repository_id else None,
        repository_host=host if repository_id else None,
        repository_id=repository_id,
    )


def push(source_id="push", repo=A, repository_id="101"):
    return Push(
        push_id=source_id,
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=repo,
        clone_url="https://github.com/acme-corp/alpha.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=SHA,
        captured_at=AT,
        **triple(repository_id),
    )


def observation(source_id="observation", repo=A, repository_id="101", **changes):
    values = dict(
        observation_id=source_id,
        org_id=ORG,
        repo=repo,
        commit_sha=SHA,
        session_id="session",
        source_push_id="push",
        captured_at=AT,
        **triple(repository_id),
    )
    values.update(changes)
    return SessionCommitObservation(**values)


def outcome(source_id="ci", repo=A, repository_id="101", **changes):
    values = dict(
        outcome_id=source_id,
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        repo=repo,
        commit_sha=SHA,
        branch="main",
        run_id="run",
        run_attempt=1,
        result=CIResult.PASSED,
        workflow_id="workflow",
        workflow_name="CI",
        workflow_path=".github/workflows/ci.yml",
        captured_at=AT,
        **triple(repository_id),
    )
    values.update(changes)
    return CIOutcome(**values)


def context(facts, *, as_of=BOUNDARY):
    return identity.build_repository_context(
        [identity.repository_identity_evidence_of(fact) for fact in facts],
        (),
        ORG,
        as_of=as_of,
    )


def test_shared_binding_and_ci_index_join_rename_but_isolate_fork():
    captured = observation()
    renamed_ci = outcome(repo=B)
    fork_ci = outcome("fork-ci", repository_id="202", run_id="fork-run")
    facts = (push(), captured, renamed_ci, fork_ci)
    for population in permutations(facts):
        declared = context(population)
        bindings = session_commit.bind_session_commit_keys_result(
            (captured,),
            ORG,
            as_of=BOUNDARY,
            repository_context=declared,
        )
        indexed = attachment.index_ci_outcomes_by_commit_key_result(
            (fork_ci, renamed_ci),
            repository_context=declared,
        )
        (commit_key, session_id), sources = next(iter(bindings.bindings.items()))
        assert sources == (captured,) and sources[0] is captured
        assert session_id == captured.session_id
        assert indexed.outcomes_by_commit[commit_key] == (renamed_ci,)
        assert indexed.outcomes_by_commit[commit_key][0] is renamed_ci
        assert len(indexed.outcomes_by_commit) == 2
        assert bindings.skipped == indexed.skipped == {}
    assert captured.repo == A and renamed_ci.repo == B


def test_qualified_binding_requires_exact_context_boundary_and_org():
    captured = observation()
    declared = context((push(), captured))
    for org_id, boundary in ((ORG, AT), ("other-corp", BOUNDARY)):
        with pytest.raises(ValueError):
            session_commit.bind_session_commit_keys_result(
                (captured,),
                org_id,
                as_of=boundary,
                repository_context=declared,
            )


def test_binding_preserves_legacy_and_counts_missing_identified_source():
    legacy = observation("legacy", repository_id=None, repo="acme-corp/legacy")
    unidentified = observation("unproved", source_push_id="missing")
    declared = context((legacy, unidentified))
    result = session_commit.bind_session_commit_keys_result(
        (legacy, unidentified),
        ORG,
        as_of=BOUNDARY,
        repository_context=declared,
    )
    assert list(result.bindings.values()) == [(legacy,)]
    assert result.skipped == {"repository_source_absent": 1}


def test_shared_indexes_scope_before_lookup_and_count_declined_sources():
    eligible = outcome()
    altered = eligible.model_copy(update={"repository_id": "202"})
    future = outcome("future", captured_at=BOUNDARY + timedelta(microseconds=1))
    foreign = outcome("foreign", org_id="other-corp")
    result = attachment.index_ci_outcomes_by_commit_key_result(
        (altered, future, foreign),
        repository_context=context((eligible,)),
    )
    assert result.outcomes_by_commit == {}
    assert result.skipped == {"repository_identity_conflict": 1}


def test_ci_retry_across_rename_preserves_attempt_and_reliability_semantics():
    failed = outcome(
        "failed", result=CIResult.FAILED, run_attempt=1, captured_at=BOUNDARY
    )
    passed = outcome("passed", repo=B, run_attempt=3)
    timeout = outcome("timeout", repo=B, result=CIResult.TIMED_OUT, run_attempt=2)
    facts = (failed, passed, timeout)
    snapshots = []
    for population in permutations(facts):
        result = ci_resolution.derive_ci_resolution_result(
            population,
            repository_context=context(population),
            quarantine_revision=3,
        )
        assert result.skipped == {}
        assert len(result.resolutions) == 1
        resolved = result.resolutions[0]
        assert resolved.repo == A
        assert resolved.repository_identity == identity.repository_identity_of(failed)
        assert resolved.verdict == CIResult.PASSED
        assert resolved.reliability == 0.0 and resolved.suspected_flake
        assert resolved.verdict_outcome_ids == ("passed",)
        assert resolved.non_verdict_outcome_ids == ("timeout",)
        assert resolved.source_outcome_ids == ("failed", "passed", "timeout")
        assert resolved.provenance.quarantine_revision == 3
        snapshots.append(result)
    assert all(item == snapshots[0] for item in snapshots)


def test_ci_same_run_different_host_stays_separate():
    public = outcome("public")
    private = outcome(
        "private", repository_host="forge.example.com", result=CIResult.FAILED
    )
    result = ci_resolution.derive_ci_resolution_result(
        (public, private),
        repository_context=context((public, private)),
    )
    assert len(result.resolutions) == 2
    assert {
        row.repository_identity.host: row.verdict for row in result.resolutions
    } == {
        "github.com": CIResult.PASSED,
        "forge.example.com": CIResult.FAILED,
    }
    assert result.skipped == {}


@pytest.mark.parametrize(
    "changes", [{"repository_id": "202"}, {"commit_sha": "b" * 40}]
)
def test_ci_same_host_run_cannot_change_repository_or_commit(changes):
    first = outcome("one", result=CIResult.FAILED)
    second = outcome("two", run_attempt=2, **changes)
    for population in permutations((first, second)):
        result = ci_resolution.derive_ci_resolution_result(
            population,
            repository_context=context(population),
        )
        assert result.resolutions == []
        assert result.skipped == {"conflicting_run_identity": 1}
        assert result.outcomes_by_commit == {}
        declared = context(population)
        assert result.conflicting_commit_keys == frozenset(
            identity.CommitKey(declared.resolve_fact(row).key, row.commit_sha)
            for row in population
        )


def test_unproved_attempt_cannot_leave_a_false_clean_lineage():
    failed = outcome("unproved", result=CIResult.FAILED)
    passed = outcome("passed", run_attempt=2)
    declared = context((passed,))
    result = ci_resolution.derive_ci_resolution_result(
        (passed, failed),
        repository_context=declared,
    )
    assert result.resolutions == []
    assert result.skipped == {"repository_source_absent": 1}
    assert result.conflicting_commit_keys == frozenset()
    assert result.outcomes_by_commit == {}


def test_qualified_run_population_preserves_original_non_verdict_outcome():
    captured = outcome("cancelled", result=CIResult.CANCELLED)
    declared = context((captured,))
    result = ci_resolution.derive_ci_resolution_result(
        (captured,), repository_context=declared
    )
    key = identity.CommitKey(declared.resolve_fact(captured).key, captured.commit_sha)
    assert result.outcomes_by_commit == {key: (captured,)}
    assert result.outcomes_by_commit[key][0] is captured
    assert result.resolutions[0].verdict is None


def test_declined_copy_cannot_borrow_an_accepted_same_id_lookup():
    retained = outcome("shared-id")
    contradictory = outcome(
        "shared-id", repository_id="202", run_attempt=2, result=CIResult.FAILED
    )
    declared = context((retained,))
    for population in permutations((retained, contradictory)):
        result = ci_resolution.derive_ci_resolution_result(
            population,
            repository_context=declared,
        )
        assert result.resolutions == []
        assert result.skipped == {"repository_identity_conflict": 1}


def test_contradictory_same_attempt_declines_instead_of_using_input_order():
    failed = outcome("failed", result=CIResult.FAILED)
    passed = outcome("passed")
    for population in permutations((failed, passed)):
        result = ci_resolution.derive_ci_resolution_result(
            population,
            repository_context=context(population),
        )
        assert result.resolutions == []
        assert result.skipped == {"conflicting_run_identity": 1}


def test_legacy_entrypoints_visibly_decline_identified_rows(caplog):
    captured = observation()
    assert session_commit.bind_session_commits((captured,), ORG, as_of=BOUNDARY) == {}
    ci = outcome()
    assert attachment.index_ci_outcomes_by_repo_commit((ci,)) == {}
    result = ci_resolution.derive_ci_resolution_result((ci,))
    assert result.resolutions == []
    assert result.skipped == {"repository_identity_unresolved": 1}
    assert "repository_identity_unresolved" in caplog.text
    assert ci.repo not in caplog.text


def test_legacy_ci_index_cannot_flatten_tenant_collision(caplog):
    first = outcome("one", repository_id=None)
    second = outcome("two", repository_id=None, org_id="other-corp")
    assert attachment.index_ci_outcomes_by_repo_commit((first, second)) == {}
    assert "repository_identity_unresolved" in caplog.text


def test_qualified_bindings_and_ci_index_order_sources_by_utc_instant():
    local = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"))
    later = local.replace(fold=1)
    early_observation = observation("z-early", captured_at=local)
    late_observation = observation("a-late", repo=B, captured_at=later)
    early_ci = outcome("z-early-ci", captured_at=local)
    late_ci = outcome("a-late-ci", repo=B, captured_at=later, run_attempt=2)
    facts = (push(), early_observation, late_observation, early_ci, late_ci)
    declared = context(facts, as_of=later)
    snapshots = []
    for observations in permutations((late_observation, early_observation)):
        for outcomes in permutations((late_ci, early_ci)):
            bindings = session_commit.bind_session_commit_keys_result(
                observations,
                ORG,
                as_of=later.astimezone(UTC),
                repository_context=declared,
            )
            indexed = attachment.index_ci_outcomes_by_commit_key_result(
                outcomes, repository_context=declared
            )
            assert next(iter(bindings.bindings.values())) == (
                early_observation,
                late_observation,
            )
            assert next(iter(indexed.outcomes_by_commit.values())) == (
                early_ci,
                late_ci,
            )
            snapshots.append((bindings, indexed))
    assert all(item == snapshots[0] for item in snapshots)


def test_ci_resolution_preserves_scope_and_non_verdict_projection_evidence():
    from sediment_core.store import CIOutcomeProjection

    non_verdict = outcome("timeout", result=CIResult.TIMED_OUT)
    projection = CIOutcomeProjection(**non_verdict.model_dump(exclude={"raw"}))
    future = outcome(
        "future", run_attempt=2, captured_at=BOUNDARY + timedelta(microseconds=1)
    )
    foreign = outcome("foreign", org_id="other-corp")
    result = ci_resolution.derive_ci_resolution_result(
        (projection, future, foreign),
        repository_context=context((non_verdict,)),
    )
    assert result.skipped == {}
    assert len(result.resolutions) == 1
    assert result.resolutions[0].verdict is None
    assert result.resolutions[0].source_outcome_ids == ("timeout",)
    assert result.resolutions[0].non_verdict_outcome_ids == ("timeout",)


def test_mixed_claims_do_not_promote_legacy_ci_or_assign_false_verdict():
    identified = outcome("identified", result=CIResult.FAILED)
    legacy = outcome("legacy", repository_id=None, run_id="legacy-run")
    qualified = ci_resolution.derive_ci_resolution_result(
        (legacy, identified),
        repository_context=context((legacy, identified)),
    )
    assert qualified.skipped == {"repository_identity_unresolved": 1}
    assert [row.verdict for row in qualified.resolutions] == [CIResult.FAILED]
    legacy_only = ci_resolution.derive_ci_resolution_result((legacy, identified))
    assert legacy_only.resolutions == []
    assert legacy_only.skipped == {"repository_identity_unresolved": 2}


def test_shared_repository_join_contract_is_exported():
    import sediment_derive

    for module, names in (
        (
            session_commit,
            ("SessionCommitBindingResult", "bind_session_commit_keys_result"),
        ),
        (
            attachment,
            ("CIOutcomeIndexResult", "index_ci_outcomes_by_commit_key_result"),
        ),
    ):
        for name in names:
            assert getattr(sediment_derive, name) is getattr(module, name)
            assert name in sediment_derive.__all__
