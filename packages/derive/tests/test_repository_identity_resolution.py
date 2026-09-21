# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository identity joins depend on captured proof, never mutable locations."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from itertools import permutations
from zoneinfo import ZoneInfo

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    FactTable,
    ForgeProvider,
    Push,
    PullRequestMerge,
    PullRequestRevision,
    RepositoryIdentityEvidence,
    RepositoryRename,
    SessionCommitObservation,
)

ORG = "acme-corp"
SLUG_A = "acme-corp/alpha"
SLUG_B = "acme-corp/beta"
T0 = datetime(2026, 9, 12, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
SHA = "a" * 40


@pytest.fixture
def identity_api():
    try:
        return import_module("sediment_derive.repository_identity")
    except ModuleNotFoundError:
        pytest.fail("repository identity resolution is not implemented")


def evidence(source_id, *, repo=SLUG_A, repository_id="101", **changes):
    return replace(
        RepositoryIdentityEvidence(
            source_table=FactTable.PUSHES,
            source_fact_id=source_id,
            role="repo",
            org_id=ORG,
            repo=repo,
            repository_provider=(
                ForgeProvider.GITHUB if repository_id is not None else None
            ),
            repository_host="github.com" if repository_id is not None else None,
            repository_id=repository_id,
            captured_at=T0,
        ),
        **changes,
    )


def test_same_captured_identity_joins_across_names_without_rename(identity_api):
    rows = (evidence("old"), evidence("renamed", repo=SLUG_B))
    outputs = []
    for population in permutations(rows):
        context = identity_api.build_repository_context(population, (), ORG, as_of=T1)
        old = context.resolve_source(FactTable.PUSHES, "old")
        renamed = context.resolve_source(FactTable.PUSHES, "renamed")
        assert old.key == renamed.key
        assert old.repo == renamed.repo == SLUG_A
        assert old.reason is renamed.reason is None
        assert context.observed_repo_slugs(old.key) == (SLUG_A, SLUG_B)
        assert context.repo_for(old.key) == SLUG_A
        assert old.evidence == (rows[0],)
        assert renamed.evidence == (rows[1],)
        outputs.append((old, renamed, dict(context.skipped)))
    assert outputs[0] == outputs[1]


def test_same_slug_and_commit_never_join_distinct_repository_ids(identity_api):
    rows = (evidence("original"), evidence("fork", repository_id="202"))
    context = identity_api.build_repository_context(rows, (), ORG, as_of=T1)
    original = context.resolve_source(FactTable.PUSHES, "original")
    fork = context.resolve_source(FactTable.PUSHES, "fork")
    assert original.key != fork.key
    assert identity_api.CommitKey(original.key, SHA) != identity_api.CommitKey(
        fork.key, SHA
    )
    assert (
        context.resolve_reference(ORG, SLUG_A).reason
        == "repository_identity_unresolved"
    )


def test_legacy_only_island_remains_literal_and_never_promotes(identity_api):
    legacy = evidence("legacy", repository_id=None)
    context = identity_api.build_repository_context((legacy,), (), ORG, as_of=T1)
    result = context.resolve_source(FactTable.PUSHES, "legacy")
    assert result.key == identity_api.LegacyRepositoryKey(ORG, SLUG_A)
    assert result.evidence == (legacy,)
    assert context.commit_key(ORG, SLUG_A, SHA).repository == result.key

    mixed = identity_api.build_repository_context(
        (legacy, evidence("identified")), (), ORG, as_of=T1
    )
    assert mixed.resolve_source(FactTable.PUSHES, "legacy").reason == (
        "repository_identity_unresolved"
    )
    assert dict(mixed.skipped) == {"repository_identity_unresolved": 1}


def rename(rename_id, old_repo=SLUG_A, new_repo=SLUG_B, **changes):
    return RepositoryRename(
        rename_id=rename_id,
        org_id=ORG,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        old_repo=old_repo,
        new_repo=new_repo,
        captured_at=T0,
        **changes,
    )


def push(source_id="push", **changes):
    values = dict(
        push_id=source_id,
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=SLUG_A,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        clone_url="https://github.com/acme-corp/alpha.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=SHA,
        captured_at=T0,
    )
    values.update(changes)
    return Push(**values)


def test_exact_fact_projection_must_agree_with_declared_source(identity_api):
    row = evidence("push")
    context = identity_api.build_repository_context((row,), (), ORG, as_of=T1)
    assert context.resolve_fact(push()) == context.resolve_source(
        FactTable.PUSHES, "push"
    )
    for changes in (
        {"repo": SLUG_B},
        {"repository_id": "202"},
        {"captured_at": T1},
        {"org_id": "other-corp"},
    ):
        assert (
            context.resolve_fact(push(**changes)).reason
            == "repository_identity_conflict"
        )
    # Fetch location is not an identity projection. Receipt ownership validates it.
    assert context.resolve_fact(
        push(clone_url="https://github.com/acme-corp/beta.git")
    ).key
    assert context.resolve_fact(push("absent")).reason == "repository_source_absent"


@pytest.mark.parametrize("identified", [False, True])
def test_observation_uses_exact_identified_source_push(identity_api, identified):
    source = evidence("push", repo=SLUG_B)
    observation = evidence(
        "observation",
        repository_id="101" if identified else None,
        source_table=FactTable.SESSION_COMMIT_OBSERVATIONS,
        source_push_id="push",
    )
    for population in permutations((observation, source)):
        context = identity_api.build_repository_context(population, (), ORG, as_of=T1)
        result = context.resolve_source(
            FactTable.SESSION_COMMIT_OBSERVATIONS, "observation"
        )
        assert result.key == context.resolve_source(FactTable.PUSHES, "push").key
        assert result.repo == SLUG_A
        assert set(result.evidence) == {source, observation}
        assert context.skipped == {}


@pytest.mark.parametrize(
    ("source", "identified", "reason"),
    [
        (None, True, "repository_source_absent"),
        (evidence("push", repository_id=None), True, "repository_identity_unresolved"),
        (evidence("push", repository_id="202"), True, "repository_identity_conflict"),
        (evidence("push", org_id="other-corp"), True, "repository_source_absent"),
    ],
)
def test_identified_observation_declines_unproved_sources(
    identity_api, source, identified, reason
):
    observation = evidence(
        "observation",
        source_table=FactTable.SESSION_COMMIT_OBSERVATIONS,
        source_push_id="push",
        repository_id="101" if identified else None,
    )
    rows = (observation,) if source is None else (observation, source)
    context = identity_api.build_repository_context(rows, (), ORG, as_of=T1)
    assert (
        context.resolve_source(
            FactTable.SESSION_COMMIT_OBSERVATIONS, "observation"
        ).reason
        == reason
    )
    expected_count = 2 if source is not None and source.repository_id is None else 1
    assert context.skipped == {reason: expected_count}


def test_legacy_observation_preserves_absent_source_semantics(identity_api):
    observation = evidence(
        "observation",
        source_table=FactTable.SESSION_COMMIT_OBSERVATIONS,
        source_push_id="missing",
        repository_id=None,
    )
    context = identity_api.build_repository_context((observation,), (), ORG, as_of=T1)
    assert context.resolve_source(
        FactTable.SESSION_COMMIT_OBSERVATIONS, "observation"
    ).key == (identity_api.LegacyRepositoryKey(ORG, SLUG_A))
    context = identity_api.build_repository_context(
        (observation, evidence("different-source")), (), ORG, as_of=T1
    )
    assert context.resolve_source(
        FactTable.SESSION_COMMIT_OBSERVATIONS, "observation"
    ).reason == ("repository_identity_unresolved")


def test_conflicting_source_copies_never_select_a_winner_or_unblock_legacy(
    identity_api,
):
    rows = (
        evidence("same"),
        evidence("same", repository_id="202"),
        evidence("legacy", repository_id=None),
    )
    for population in permutations(rows):
        context = identity_api.build_repository_context(population, (), ORG, as_of=T1)
        assert (
            context.resolve_source(FactTable.PUSHES, "same").reason
            == "repository_identity_conflict"
        )
        assert (
            context.resolve_source(FactTable.PUSHES, "legacy").reason
            == "repository_identity_unresolved"
        )
        assert context.skipped == {
            "repository_identity_conflict": 1,
            "repository_identity_unresolved": 1,
        }
    context = identity_api.build_repository_context(
        (rows[0], rows[0]), (), ORG, as_of=T1
    )
    assert context.resolve_source(FactTable.PUSHES, "same").key
    assert context.skipped == {}


def test_conflicting_source_push_cannot_be_ignored_by_legacy_observation(identity_api):
    observation = evidence(
        "observation",
        source_table=FactTable.SESSION_COMMIT_OBSERVATIONS,
        source_push_id="push",
        repository_id=None,
    )
    rows = (observation, evidence("push"), evidence("push", repository_id="202"))
    context = identity_api.build_repository_context(rows, (), ORG, as_of=T1)
    assert context.resolve_source(
        FactTable.SESSION_COMMIT_OBSERVATIONS, "observation"
    ).reason == ("repository_identity_conflict")
    assert context.skipped == {"repository_identity_conflict": 2}


def test_rename_chain_cycle_is_claim_evidence_not_current_name(identity_api):
    slug_c = "acme-corp/gamma"
    changes = (
        rename("one"),
        rename("two", SLUG_B, slug_c),
        rename("cycle", slug_c, SLUG_A),
    )
    row = evidence("push", repo=slug_c)
    for population in permutations(changes):
        context = identity_api.build_repository_context(
            (row,), population, ORG, as_of=T1
        )
        result = context.resolve_source(FactTable.PUSHES, "push")
        assert result.repo == SLUG_A
        assert context.observed_repo_slugs(result.key) == (SLUG_A, SLUG_B, slug_c)
        assert (
            context.resolve_reference(ORG, SLUG_B).reason
            == "repository_identity_unresolved"
        )
        assert (
            context.resolve_reference(
                ORG, SLUG_B, repository_identity=result.key.identity
            ).key
            == result.key
        )
        assert context.rename_skipped == {}


@pytest.mark.parametrize("same_primary", [False, True])
def test_conflicting_rename_copies_do_not_authorize_new_labels(
    identity_api, same_primary
):
    changes = (
        rename("one", source_event_id="event"),
        rename(
            "one" if same_primary else "two",
            SLUG_A,
            "acme-corp/gamma",
            source_event_id="event",
        ),
    )
    for population in permutations(changes):
        context = identity_api.build_repository_context(
            (evidence("push"),), population, ORG, as_of=T1
        )
        resolved = context.resolve_source(FactTable.PUSHES, "push")
        assert context.observed_repo_slugs(resolved.key) == (SLUG_A,)
        assert context.resolve_reference(
            ORG, SLUG_B, repository_identity=resolved.key.identity
        ).reason == ("repository_identity_unresolved")
        assert (
            context.resolve_reference(ORG, SLUG_B).reason
            == "repository_identity_unresolved"
        )
        assert context.rename_skipped == {
            "repository_identity_conflict": 1 if same_primary else 2
        }


def test_boundary_and_tenancy_filter_claims_before_resolution(identity_api):
    legacy = evidence("legacy", repository_id=None)
    typed = evidence("typed", captured_at=T1)
    foreign = evidence("foreign", org_id="other-corp")
    before = identity_api.build_repository_context(
        (legacy, typed, foreign), (), ORG, as_of=T0
    )
    assert before.resolve_source(FactTable.PUSHES, "legacy").key
    assert (
        before.resolve_source(FactTable.PUSHES, "typed").reason
        == "repository_source_absent"
    )
    assert (
        before.resolve_source(FactTable.PUSHES, "foreign").reason
        == "repository_source_absent"
    )
    assert (
        before.resolve_reference("other-corp", SLUG_A).reason
        == "repository_identity_unresolved"
    )
    at = identity_api.build_repository_context((legacy, typed), (), ORG, as_of=T1)
    assert (
        at.resolve_source(FactTable.PUSHES, "legacy").reason
        == "repository_identity_unresolved"
    )


def test_boundary_compares_dst_instants_and_rejects_naive_datetime(identity_api):
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    rows = (
        evidence("early", repository_id=None, captured_at=first),
        evidence("late", captured_at=second),
    )
    context = identity_api.build_repository_context(rows, (), ORG, as_of=first)
    assert context.resolve_source(FactTable.PUSHES, "early").key
    assert (
        context.resolve_source(FactTable.PUSHES, "late").reason
        == "repository_source_absent"
    )
    with pytest.raises(ValueError, match="aware"):
        identity_api.build_repository_context(
            rows, (), ORG, as_of=T0.replace(tzinfo=None)
        )


def test_mirror_refresh_refuses_known_reused_slug(identity_api):
    original = evidence("original")
    renamed = evidence("renamed", repo=SLUG_B)
    replacement = evidence("replacement", repository_id="303")
    context = identity_api.build_repository_context(
        (original, renamed, replacement), (), ORG, as_of=T1
    )
    assert context.mirror_refresh_resolution(
        FactTable.PUSHES, "original", repo=SLUG_A
    ).reason == ("repository_mirror_identity_unresolved")
    assert context.mirror_refresh_resolution(
        FactTable.PUSHES, "renamed", repo=SLUG_B
    ).key == (context.resolve_source(FactTable.PUSHES, "original").key)
    assert context.mirror_refresh_resolution(
        FactTable.PUSHES, "original", repo=SLUG_B
    ).key == (context.resolve_source(FactTable.PUSHES, "original").key)
    assert context.mirror_refresh_resolution(
        FactTable.PUSHES, "original", repo="acme-corp/absent"
    ).reason == ("repository_mirror_identity_unresolved")


def test_identity_namespace_includes_host_and_tenant_and_sort_is_total(identity_api):
    original = identity_api.RepositoryIdentity(
        ForgeProvider.GITHUB, "github.com", "101"
    )
    enterprise = identity_api.RepositoryIdentity(
        ForgeProvider.GITHUB, "forge.example.com", "101"
    )
    keys = (
        identity_api.IdentifiedRepositoryKey(ORG, original),
        identity_api.IdentifiedRepositoryKey(ORG, enterprise),
        identity_api.IdentifiedRepositoryKey("other-corp", original),
        identity_api.LegacyRepositoryKey(ORG, SLUG_A),
    )
    assert len(set(keys)) == 4
    assert len({identity_api.repository_sort_key(key) for key in keys}) == 4
    expected = sorted(keys, key=identity_api.repository_sort_key)
    for ordering in permutations(keys):
        assert sorted(ordering, key=identity_api.repository_sort_key) == expected
        commits = [identity_api.CommitKey(key, SHA) for key in ordering]
        assert [
            key.repository for key in sorted(commits, key=identity_api.commit_sort_key)
        ] == expected


def test_resolution_cannot_register_unknown_identity_or_mutate_diagnostics(
    identity_api,
):
    context = identity_api.build_repository_context(
        (evidence("legacy", repository_id=None), evidence("push")), (), ORG, as_of=T1
    )
    unknown = identity_api.RepositoryIdentity(ForgeProvider.GITHUB, "github.com", "404")
    before = dict(context.skipped)
    for _ in range(3):
        assert context.resolve_reference(
            ORG, SLUG_A, repository_identity=unknown
        ).reason == ("repository_identity_unresolved")
        context.resolve_source(FactTable.PUSHES, "legacy")
    assert context.skipped == before
    with pytest.raises(TypeError):
        context.skipped["repository_identity_conflict"] = 100


@pytest.mark.parametrize("fact_type", [PullRequestMerge, PullRequestRevision])
def test_pull_request_head_role_is_independent_proof(identity_api, fact_type):
    values = dict(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=SLUG_A,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        pr_number=1,
        head_repo=SLUG_B,
        head_repository_provider=ForgeProvider.GITHUB,
        head_repository_host="github.com",
        head_repository_id="202",
        head_ref="feature",
        head_sha=SHA,
        base_ref="main",
        base_sha="b" * 40,
        captured_at=T0,
    )
    if fact_type is PullRequestMerge:
        values.update(merge_id="pr", merge_commit_sha="c" * 40, merged_at=T0)
        table = FactTable.PULL_REQUEST_MERGES
    else:
        values.update(revision_id="pr")
        table = FactTable.PULL_REQUEST_REVISIONS
    fact = fact_type(**values)
    rows = (
        evidence("pr", source_table=table),
        evidence(
            "pr", source_table=table, role="head_repo", repo=SLUG_B, repository_id="202"
        ),
    )
    context = identity_api.build_repository_context(rows, (), ORG, as_of=T1)
    assert (
        context.resolve_fact(fact).key
        != context.resolve_fact(fact, role="head_repo").key
    )
    assert (
        identity_api.repository_identity_of(fact, role="head_repo").repository_id
        == "202"
    )
    assert context.resolve_fact(fact, role="head_repo").evidence == (rows[1],)


def test_real_ci_and_observation_facts_share_projection_owner(identity_api):
    ci = CIOutcome(
        outcome_id="ci",
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run",
        repo=SLUG_A,
        commit_sha=SHA,
        branch="main",
        result=CIResult.PASSED,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        captured_at=T0,
    )
    observation = SessionCommitObservation(
        observation_id="observation",
        org_id=ORG,
        repo=SLUG_A,
        commit_sha=SHA,
        session_id="session",
        source_push_id="push",
        captured_at=T0,
    )
    rows = (
        evidence("push"),
        evidence("ci", source_table=FactTable.CI_OUTCOMES),
        evidence(
            "observation",
            source_table=FactTable.SESSION_COMMIT_OBSERVATIONS,
            repository_id=None,
            source_push_id="push",
        ),
    )
    context = identity_api.build_repository_context(rows, (), ORG, as_of=T1)
    assert context.resolve_fact(ci).key == context.resolve_fact(observation).key
    altered = observation.model_copy(update={"source_push_id": "other"})
    assert context.resolve_fact(altered).reason == "repository_identity_conflict"


def test_fold_equivalent_fact_matches_but_distinct_fold_copies_conflict(identity_api):
    first = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
    second = first.replace(fold=1)
    row = evidence("push", captured_at=first)
    context = identity_api.build_repository_context((row,), (), ORG, as_of=second)
    assert context.resolve_fact(push(captured_at=first.astimezone(UTC))).key
    assert (
        context.resolve_fact(push(captured_at=second)).reason
        == "repository_identity_conflict"
    )
    context = identity_api.build_repository_context(
        (row, replace(row, captured_at=second)), (), ORG, as_of=second
    )
    assert (
        context.resolve_source(FactTable.PUSHES, "push").reason
        == "repository_identity_conflict"
    )


def test_future_rename_does_not_poison_legacy_and_input_mutation_cannot_change_context(
    identity_api,
):
    event = rename("rename")
    event.captured_at = T1
    legacy = evidence("legacy", repository_id=None)
    before = identity_api.build_repository_context((legacy,), (event,), ORG, as_of=T0)
    assert before.resolve_source(FactTable.PUSHES, "legacy").key
    at = identity_api.build_repository_context((legacy,), (event,), ORG, as_of=T1)
    assert (
        at.resolve_source(FactTable.PUSHES, "legacy").reason
        == "repository_identity_unresolved"
    )
    event.old_repo = "acme-corp/mutated"
    assert (
        at.resolve_source(FactTable.PUSHES, "legacy").reason
        == "repository_identity_unresolved"
    )
    assert at.resolve_reference(ORG, "acme-corp/mutated").key


@pytest.mark.parametrize(
    "changes",
    [
        {"repository_host": None},
        {"repository_id": "001"},
        {"repository_host": "https://github.com"},
        {"role": "head_repo"},
        {"source_push_id": "not-an-observation"},
        {"captured_at": T0.replace(tzinfo=None)},
    ],
)
def test_malformed_projection_rejects_population_without_error_content(
    identity_api, changes
):
    with pytest.raises(ValueError, match="^invalid repository identity evidence$"):
        identity_api.build_repository_context(
            (evidence("bad", **changes),), (), ORG, as_of=T1
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda api: api.RepositoryIdentity(ForgeProvider.GITHUB, "github.com", "001"),
        lambda api: api.RepositoryIdentity(ForgeProvider.GITHUB, "github.com:443", "1"),
        lambda api: api.RepositoryIdentity("invented-provider", "github.com", "1"),
        lambda api: api.LegacyRepositoryKey(ORG, ""),
        lambda api: api.IdentifiedRepositoryKey(
            "", api.RepositoryIdentity(ForgeProvider.GITHUB, "github.com", "1")
        ),
        lambda api: api.CommitKey(api.LegacyRepositoryKey(ORG, SLUG_A), "not-a-sha"),
    ],
)
def test_repository_keys_validate_join_components(identity_api, factory):
    with pytest.raises(ValueError):
        factory(identity_api)


def test_empty_repo_is_counted_absence_and_unknown_label_is_not_registered(
    identity_api,
):
    row = evidence("empty", repo="")
    context = identity_api.build_repository_context((row,), (), ORG, as_of=T1)
    assert (
        context.resolve_source(FactTable.PUSHES, "empty").reason
        == "repository_identity_absent"
    )
    assert context.skipped == {"repository_identity_absent": 1}
    assert context.resolve_reference(
        ORG, SLUG_A, repository_identity=identity_api.repository_identity_of(row)
    ).reason == ("repository_identity_unresolved")


def test_mirror_refresh_allows_proved_legacy_and_refuses_absent_source(identity_api):
    context = identity_api.build_repository_context(
        (evidence("legacy", repository_id=None),), (), ORG, as_of=T1
    )
    assert context.mirror_refresh_resolution(
        FactTable.PUSHES, "legacy", repo=SLUG_A
    ).key == identity_api.LegacyRepositoryKey(ORG, SLUG_A)
    assert (
        context.mirror_refresh_resolution(
            FactTable.PUSHES, "absent", repo=SLUG_A
        ).reason
        == "repository_mirror_identity_unresolved"
    )


@pytest.mark.parametrize("population", ["evidence", "renames"])
def test_population_cap_refuses_incomplete_context(identity_api, population):
    from sediment_core import OperationalReportLimitExceeded, REPOSITORY_IDENTITY_LIMIT

    row = evidence("push") if population == "evidence" else rename("rename")
    oversized = (row for _ in range(REPOSITORY_IDENTITY_LIMIT + 1))
    with pytest.raises(OperationalReportLimitExceeded):
        identity_api.build_repository_context(
            oversized if population == "evidence" else (),
            oversized if population == "renames" else (),
            ORG,
            as_of=T1,
        )


def test_rename_proves_refresh_location_but_competing_identity_refuses_it(identity_api):
    row = evidence("push")
    event = rename("rename")
    context = identity_api.build_repository_context((row,), (event,), ORG, as_of=T1)
    assert (
        context.mirror_refresh_resolution(FactTable.PUSHES, "push", repo=SLUG_B).key
        == context.resolve_source(FactTable.PUSHES, "push").key
    )
    competing = evidence("fork", repo=SLUG_B, repository_id="202")
    context = identity_api.build_repository_context(
        (row, competing), (event,), ORG, as_of=T1
    )
    assert (
        context.mirror_refresh_resolution(FactTable.PUSHES, "push", repo=SLUG_B).reason
        == "repository_mirror_identity_unresolved"
    )


def test_equivalent_source_copy_representations_have_deterministic_evidence(
    identity_api,
):
    local = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1)
    rows = (
        evidence("push", captured_at=local),
        evidence("push", captured_at=local.astimezone(UTC)),
    )
    results = []
    for population in permutations(rows):
        context = identity_api.build_repository_context(
            population, (), ORG, as_of=local
        )
        resolved = context.resolve_source(FactTable.PUSHES, "push")
        assert resolved.key
        results.append(tuple(row.captured_at.isoformat() for row in resolved.evidence))
    assert results[0] == results[1]


def test_ci_projection_and_fact_resolve_identically(identity_api):
    from sediment_core.store import CIOutcomeProjection

    fact = CIOutcome(
        outcome_id="ci",
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run",
        repo=SLUG_A,
        commit_sha=SHA,
        branch="main",
        result=CIResult.PASSED,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        captured_at=T0,
    )
    projection = CIOutcomeProjection(**fact.model_dump(exclude={"raw"}))
    context = identity_api.build_repository_context(
        (evidence("ci", source_table=FactTable.CI_OUTCOMES),), (), ORG, as_of=T1
    )
    assert context.resolve_fact(projection) == context.resolve_fact(fact)
    malformed = replace(projection, repository_id="001")
    assert context.resolve_fact(malformed).reason == "repository_identity_conflict"


def test_legacy_observation_cannot_ignore_present_different_slug_source(identity_api):
    rows = (
        evidence("push", repo=SLUG_B, repository_id=None),
        evidence(
            "observation",
            repository_id=None,
            source_table=FactTable.SESSION_COMMIT_OBSERVATIONS,
            source_push_id="push",
        ),
    )
    context = identity_api.build_repository_context(rows, (), ORG, as_of=T1)
    assert (
        context.resolve_source(
            FactTable.SESSION_COMMIT_OBSERVATIONS, "observation"
        ).reason
        == "repository_identity_conflict"
    )


def test_package_exports_frozen_resolver_contract(identity_api):
    import sediment_derive

    names = (
        "RepositoryIdentity",
        "IdentifiedRepositoryKey",
        "LegacyRepositoryKey",
        "RepositoryKey",
        "CommitKey",
        "RepositoryContext",
        "RepositoryResolution",
        "RepositoryIdentitySkipReason",
        "build_repository_context",
        "repository_identity_of",
        "repository_sort_key",
        "commit_sort_key",
        "REPOSITORY_IDENTITY_SKIP_REASONS",
        "REPOSITORY_IDENTITY_POLICY_VERSION",
    )
    for name in names:
        assert getattr(sediment_derive, name) is getattr(identity_api, name)
        assert name in sediment_derive.__all__
    assert identity_api.REPOSITORY_IDENTITY_POLICY_VERSION == "1"
    assert identity_api.REPOSITORY_IDENTITY_SKIP_REASONS == frozenset(
        (
            "repository_identity_absent",
            "repository_identity_unresolved",
            "repository_identity_conflict",
            "repository_source_absent",
            "repository_mirror_identity_unresolved",
        )
    )


@pytest.mark.parametrize("repo", [SLUG_A.upper(), f"  {SLUG_A}  "])
def test_reference_normalization_cannot_bypass_an_identified_claim(identity_api, repo):
    declared = identity_api.build_repository_context(
        (evidence("push"),), (), ORG, as_of=T1
    )
    result = declared.resolve_reference(ORG, repo)
    assert result.key is None and result.reason == "repository_identity_unresolved"
    assert declared.commit_key(ORG, repo, SHA) is None


def test_reference_normalization_precedes_tenancy_and_captured_name_checks(
    identity_api,
):
    declared = identity_api.build_repository_context(
        (evidence("push"), evidence("rename", repo=SLUG_B)), (), ORG, as_of=T1
    )
    original = declared.resolve_source(FactTable.PUSHES, "push")
    result = declared.resolve_reference(
        ORG.upper(),
        f" {SLUG_B.upper()} ",
        repository_identity=original.key.identity,
    )
    assert result.key == original.key and result.repo == SLUG_A
    assert (
        declared.commit_key(
            ORG, f" {SLUG_B.upper()} ", SHA, repository_identity=original.key.identity
        ).repository
        == original.key
    )
    assert (
        declared.mirror_refresh_resolution(
            FactTable.PUSHES, "push", repo=f" {SLUG_B.upper()} "
        ).key
        == original.key
    )


@pytest.mark.parametrize("repo", [None, True, 42, [], {}, "invalid\x00repo"])
def test_invalid_reference_and_mirror_target_decline_without_content(
    identity_api, repo
):
    declared = identity_api.build_repository_context(
        (evidence("push"),), (), ORG, as_of=T1
    )
    result = declared.resolve_reference(ORG, repo)
    assert result.key is None and result.reason == "repository_identity_unresolved"
    assert declared.commit_key(ORG, repo, SHA) is None
    mirror = declared.mirror_refresh_resolution(FactTable.PUSHES, "push", repo=repo)
    assert (
        mirror.key is None and mirror.reason == "repository_mirror_identity_unresolved"
    )


def test_absent_reference_stays_counted_after_normalization(identity_api):
    declared = identity_api.build_repository_context((), (), ORG, as_of=T1)
    assert declared.resolve_reference(ORG, "   ").reason == "repository_identity_absent"
    assert (
        declared.resolve_reference(None, SLUG_A).reason
        == "repository_identity_unresolved"
    )
    assert (
        declared.resolve_reference(f" {ORG} ", SLUG_A).reason
        == "repository_identity_unresolved"
    )
    assert (
        declared.resolve_reference(ORG, SLUG_A, repository_identity={}).reason
        == "repository_identity_unresolved"
    )
    assert declared.commit_key(ORG, SLUG_A, "invalid-sha") is None


def test_public_evidence_helper_uses_the_exact_validated_projection(identity_api):
    projected = identity_api.repository_identity_evidence_of(push())
    assert projected == evidence("push")
    with pytest.raises(ValueError, match="repository role"):
        identity_api.repository_identity_evidence_of(push(), role="unsupported")


def test_valid_legacy_reference_label_does_not_invent_captured_names(identity_api):
    declared = identity_api.build_repository_context((), (), ORG, as_of=T1)
    key = declared.resolve_reference(ORG, SLUG_B).key
    assert declared.repo_for(key) == SLUG_B
    assert declared.observed_repo_slugs(key) == ()
    claimed = identity_api.build_repository_context(
        (evidence("push", repo=SLUG_B),), (), ORG, as_of=T1
    )
    with pytest.raises(KeyError):
        claimed.repo_for(key)
