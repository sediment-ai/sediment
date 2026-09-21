# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AttributedCompletion-assembly tests over real facts and real git fixtures — never mocked
(per AGENTS.md). Every commit-binding case builds an actual git repository,
stamps a real ``refs/notes/sediment`` note, mirrors it through
``MirrorManager``, writes real completion/decision/CI facts to a real
``FactStore``, and runs ``assemble_attributed_completions`` as a consumer would.

Git helpers are inline rather than imported from
``packages/derive/tests/gitfixtures.py``: a shared file would need a unique
module basename to avoid a pytest collection collision with derive's own
copy (see ``test_rlvr.py``'s identical note) — the sibling RLVR suite already
established this convention for ``packages/export/tests``.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    InteractionMode,
    FactTable,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
)
from sediment_core.store import FactStore
from sediment_derive import (
    AbandonmentPolicy,
    AbandonmentResult,
    AttributionSource,
    AttributionPolicy,
    MirrorManager,
    Provenance,
    SessionAbandonment,
    SimilarityPolicy,
    split_of,
)

from sediment_export import (
    AttributedCompletion,
    AttributedCompletionPolicy,
    assemble_attributed_completion_result,
    assemble_attributed_completions,
)
from sediment_export import attributed_completions as attributed_completions_module
from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
OTHER_REPO = "other-corp/fork"

FIB = "def fibonacci(n):\n    return n if n <= 1 else fibonacci(n - 1)\n"


def _provenance(version: str, revision: int = 0) -> Provenance:
    return Provenance(policy_version=version, quarantine_revision=revision)


# Real-git helpers, kept inline; see the module docstring.


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _work_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    return work


def _commit(work: Path, message: str, *, when: str | None = None) -> str:
    _git(work, "add", "-A")
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return _git(work, "rev-parse", "HEAD").strip()


def _make_remote(tmp_path: Path, work: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    return remote


def _note(session_id: str) -> str:
    import json

    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": session_id,
                    "stamped_at": "2026-07-15T00:00:00+00:00",
                }
            ],
        }
    )


def _recent() -> datetime:
    # Within the attribution lookback, anchored shortly before the push whose
    # captured_at defaults to now (matches the derive/export suites' convention).
    return datetime.now(UTC) - timedelta(minutes=5)


def _completion(
    session_id: str,
    messages: list[InferenceMessage],
    completion: str,
    *,
    call_id: str | None = None,
    captured_at: datetime | None = None,
    provider: GatewayProvider = GatewayProvider.LITELLM,
) -> InferenceCall:
    return inference_call(
        inference_call_id=None,
        org_id=ORG,
        session_id=session_id,
        gateway_provider=provider,
        model="claude-sonnet-5",
        input_messages=messages,
        output=completion,
        model_call_id=call_id,
        observed_at=captured_at if captured_at is not None else _recent(),
    )


def _decision(
    session_id: str,
    call_id: str | None,
    *,
    accepted: bool = True,
    explicit: bool = True,
    file_path: str = "math_utils.py",
    agent_harness: AgentHarness = AgentHarness.CLAUDE_CODE,
    occurred_at: datetime | None = None,
    edit_retention_score: float | None = None,
) -> DeveloperDecision:
    decision_time = occurred_at if occurred_at is not None else _recent()
    return DeveloperDecision(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        agent_harness=agent_harness,
        file_path=file_path,
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        call_id=call_id,
        occurred_at=decision_time,
        captured_at=decision_time,
        edit_retention_score=edit_retention_score,
    )


def _edit_observation(
    session_id: str,
    call_id: str,
    *,
    applied_text: str,
    observed_file_text: str,
) -> EditObservation:
    return EditObservation(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="math_utils.py",
        call_id=call_id,
        applied_text=applied_text,
        observed_file_text=observed_file_text,
        occurred_at=_recent(),
    )


def _push_and_mirror(
    tmp_path: Path,
    store: FactStore,
    work: Path,
    before: str,
    after: str,
    *,
    repo: str = REPO,
) -> MirrorManager:
    remote = _make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=repo,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=before,
        after_sha=after,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    store.store_push(push)
    return mirrors


def _store_ci(
    store: FactStore, commit_sha: str, result: CIResult, *, repo: str = REPO, run: str
) -> None:
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id=run,
            repo=repo,
            commit_sha=commit_sha,
            branch="main",
            result=result,
            run_url=run,
        )
    )


def _stamped_scenario(
    postgres_store_factory, tmp_path: Path, *, session_id: str = "sess-1"
):
    """A single-commit, notes-stamped, mirror-backed fixture: one file
    (``math_utils.py``) whose content matches a completion's output closely
    enough to attribute through git notes. Returns
    ``(store, mirrors, head)``."""
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _completion(session_id, [message(role="user", content="write fib")], FIB)
    )
    return store, mirrors, head


def _only(
    attributed_completions: list[AttributedCompletion], inference_call_id: str
) -> AttributedCompletion:
    matches = [
        t for t in attributed_completions if t.inference_call_id == inference_call_id
    ]
    assert len(matches) == 1, (
        f"expected one attributed completion for {inference_call_id}"
    )
    return matches[0]


def _abandonment(
    *,
    org_id: str = ORG,
    session_id: str = "sess-abandoned",
    explicit_accepted_decisions: int = 1,
) -> SessionAbandonment:
    return SessionAbandonment(
        org_id=org_id,
        session_id=session_id,
        accepted_decisions=1,
        explicit_accepted_decisions=explicit_accepted_decisions,
        last_decision_at=_recent() - timedelta(days=20),
        as_of=_recent(),
        provenance=_provenance("2"),
    )


def _attributed_completion_variant(
    *,
    abandonment: SessionAbandonment | None = None,
    org_id: str = ORG,
    session_id: str = "sess-abandoned",
    attributed: bool = False,
    ci_outcomes: list[CIOutcome] | None = None,
    decisions: list[DeveloperDecision] | None = None,
) -> AttributedCompletion:
    row_decisions = decisions
    if row_decisions is None:
        row_decisions = (
            [_decision(session_id, "call-variant")] if abandonment is not None else []
        )
    return AttributedCompletion(
        org_id=org_id,
        session_id=session_id,
        inference_call_id="comp-variant",
        repo=REPO if attributed else None,
        commit_sha="a" * 40 if attributed else None,
        file_path="math_utils.py" if attributed else None,
        similarity_score=1.0 if attributed else None,
        attribution_source=AttributionSource.GIT_NOTES if attributed else None,
        decisions=row_decisions,
        ci_outcomes=ci_outcomes or [],
        provenance=_provenance("3"),
        split="train",
        abandonment=abandonment,
    )


def test_attributed_completion_accepts_exactly_one_evidence_variant() -> None:
    attributed = _attributed_completion_variant(attributed=True)
    assert attributed.abandonment is None
    assert attributed.repo == REPO

    abandonment = _abandonment()
    abandoned = _attributed_completion_variant(abandonment=abandonment)
    assert abandoned.abandonment == abandonment
    assert abandoned.repo is None
    assert abandoned.commit_sha is None
    assert abandoned.file_path is None
    assert abandoned.similarity_score is None
    assert abandoned.attribution_source is None


def test_attributed_completion_rejects_both_or_neither_evidence_variant() -> None:
    with pytest.raises(ValueError, match="exactly one evidence variant"):
        _attributed_completion_variant()
    with pytest.raises(ValueError, match="exactly one evidence variant"):
        _attributed_completion_variant(attributed=True, abandonment=_abandonment())


def test_abandonment_evidence_requires_matching_identity_and_no_ci() -> None:
    with pytest.raises(ValueError, match="org_id and session_id"):
        _attributed_completion_variant(abandonment=_abandonment(org_id="other-org"))
    with pytest.raises(ValueError, match="org_id and session_id"):
        _attributed_completion_variant(
            abandonment=_abandonment(session_id="other-session")
        )
    with pytest.raises(ValueError, match="cannot carry CI outcomes"):
        _attributed_completion_variant(
            abandonment=_abandonment(),
            ci_outcomes=[
                CIOutcome(
                    org_id=ORG,
                    provider=CIProvider.GITHUB_ACTIONS,
                    run_id="run/variant",
                    repo=REPO,
                    commit_sha="a" * 40,
                    branch="main",
                    result=CIResult.PASSED,
                    run_url="run/variant",
                )
            ],
        )
    with pytest.raises(ValueError, match="joined explicit accepted decision"):
        _attributed_completion_variant(abandonment=_abandonment(), decisions=[])
    with pytest.raises(ValueError, match="joined explicit accepted decision"):
        _attributed_completion_variant(
            abandonment=_abandonment(explicit_accepted_decisions=0)
        )


def _abandoned_scenario(
    postgres_store_factory,
    tmp_path: Path,
    *,
    decisions: list[DeveloperDecision],
    completion_call_id: str = "call-1",
) -> tuple[FactStore, MirrorManager, InferenceCall]:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    _, store = postgres_store_factory()
    completion = _completion(
        "sess-abandoned",
        [message(role="user", content="write fib")],
        FIB,
        call_id=completion_call_id,
        captured_at=old,
    )
    store.store_inference_call(completion)
    store.store_decisions(decisions)
    store.store_decision(
        _decision(
            "sess-clock",
            "clock",
            accepted=False,
            occurred_at=old + timedelta(days=40),
        )
    )
    return store, MirrorManager(str(tmp_path / "mirrors")), completion


def _historical_abandonment(store: FactStore) -> AbandonmentResult:
    """A supplied legacy Derivation remains projectable by version-1 recipes."""
    decisions = [
        item
        for item in store.read_decisions(ORG)
        if item.session_id == "sess-abandoned"
    ]
    accepted = [item for item in decisions if item.accepted]
    return AbandonmentResult(
        sessions=[
            SessionAbandonment(
                org_id=ORG,
                session_id="sess-abandoned",
                accepted_decisions=len(accepted),
                explicit_accepted_decisions=sum(item.explicit for item in accepted),
                last_decision_at=max(item.occurred_at for item in decisions),
                as_of=datetime(2026, 2, 10, tzinfo=UTC),
                provenance=_provenance("4"),
            )
        ]
    )


def test_explicit_abandonment_emits_one_uncorrelated_attributed_completion(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    decision = _decision("sess-abandoned", "call-1", occurred_at=old)
    store, mirrors, completion = _abandoned_scenario(
        postgres_store_factory, tmp_path, decisions=[decision]
    )

    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG, abandonment=_historical_abandonment(store)
    )

    [row] = result.rows
    assert row.inference_call_id == completion.inference_call_id
    assert row.decisions == [decision]
    assert row.abandonment is not None
    assert row.abandonment.explicit_accepted_decisions == 1
    assert row.repo is None
    assert row.commit_sha is None
    assert row.file_path is None
    assert row.similarity_score is None
    assert row.attribution_source is None
    assert row.ci_outcomes == []
    assert row.provenance == _provenance("5")
    assert result.abandonment_skipped == {}


def test_implicit_only_abandonment_is_counted_without_emitting_a_row(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    store, mirrors, _ = _abandoned_scenario(
        postgres_store_factory,
        tmp_path,
        decisions=[
            _decision("sess-abandoned", "call-1", explicit=False, occurred_at=old)
        ],
    )

    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG, abandonment=_historical_abandonment(store)
    )

    assert result.rows == []
    assert len(result.abandonment.sessions) == 1
    assert result.abandonment_skipped == {"implicit_only_session": 1}


def test_unjoined_explicit_accept_is_counted_without_guessing_a_completion(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    store, mirrors, _ = _abandoned_scenario(
        postgres_store_factory,
        tmp_path,
        completion_call_id="different-call",
        decisions=[_decision("sess-abandoned", "call-1", occurred_at=old)],
    )

    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG, abandonment=_historical_abandonment(store)
    )

    assert result.rows == []
    assert result.abandonment_skipped == {"explicit_accept_unjoined": 1}


def test_subsumed_codex_redelivery_is_not_counted_as_unjoined(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    decisions = [
        _decision(
            "sess-abandoned",
            "call-1",
            file_path="",
            agent_harness=AgentHarness.CODEX,
            occurred_at=old,
        ),
        _decision(
            "sess-abandoned",
            "call-1",
            file_path="math_utils.py",
            agent_harness=AgentHarness.CODEX,
            occurred_at=old,
        ),
    ]
    store, mirrors, _ = _abandoned_scenario(
        postgres_store_factory, tmp_path, decisions=decisions
    )

    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG, abandonment=_historical_abandonment(store)
    )

    assert len(result.rows) == 1
    assert result.rows[0].decisions == [decisions[1]]
    assert result.abandonment_skipped == {}


def test_assembly_uses_one_fact_snapshot_during_concurrent_ingest(
    tmp_path: Path, postgres_store_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    original_decision = _decision("sess-abandoned", "call-1", occurred_at=old)
    store, mirrors, _ = _abandoned_scenario(
        postgres_store_factory, tmp_path, decisions=[original_decision]
    )
    writer = FactStore(store._engine)
    concurrent_decision = _decision(
        "sess-abandoned",
        "call-1",
        file_path="concurrent.py",
        occurred_at=old + timedelta(seconds=1),
    )
    original_attach = attributed_completions_module.attach_edit_retention

    def ingest_after_initial_read(*args: object, **kwargs: object) -> list:
        attached = original_attach(*args, **kwargs)
        writer.store_decision(concurrent_decision)
        return attached

    monkeypatch.setattr(
        attributed_completions_module,
        "attach_edit_retention",
        ingest_after_initial_read,
    )
    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG
    )

    assert result.rows == []
    [outcome] = result.abandonment.outcomes
    assert outcome.accepted_decisions == 1
    assert outcome.explicit_accepted_decisions == 1
    assert outcome.status == "attribution_unavailable"


def test_several_explicit_accepts_for_one_completion_emit_one_row(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    decisions = [
        _decision("sess-abandoned", "call-1", occurred_at=old),
        _decision(
            "sess-abandoned",
            "call-1",
            file_path="other.py",
            occurred_at=old + timedelta(seconds=1),
        ),
    ]
    store, mirrors, _ = _abandoned_scenario(
        postgres_store_factory, tmp_path, decisions=decisions
    )

    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG, abandonment=_historical_abandonment(store)
    )

    assert len(result.rows) == 1
    assert result.rows[0].decisions == decisions
    assert result.abandonment_skipped == {}


def test_partly_projectable_abandonment_counts_the_unjoined_accept(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    joined = _decision("sess-abandoned", "call-1", occurred_at=old)
    unjoined = _decision(
        "sess-abandoned", "call-2", occurred_at=old + timedelta(seconds=1)
    )
    store, mirrors, _ = _abandoned_scenario(
        postgres_store_factory, tmp_path, decisions=[joined, unjoined]
    )

    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG, abandonment=_historical_abandonment(store)
    )

    assert len(result.rows) == 1
    assert result.rows[0].decisions == [joined]
    assert result.abandonment_skipped == {"explicit_accept_unjoined": 1}


def test_abandonment_assembly_skips_attribution_without_pushes(
    tmp_path: Path, postgres_store_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    store, mirrors, _ = _abandoned_scenario(
        postgres_store_factory,
        tmp_path,
        decisions=[_decision("sess-abandoned", "call-1", occurred_at=old)],
    )
    original = attributed_completions_module.derive_attribution_result
    calls = 0

    def counted_attribution_walk(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        attributed_completions_module,
        "derive_attribution_result",
        counted_attribution_walk,
    )

    result = attributed_completions_module.assemble_attributed_completions_result(
        store, mirrors, ORG, abandonment=_historical_abandonment(store)
    )

    assert len(result.rows) == 1
    assert calls == 0


def test_shuffled_abandonment_facts_yield_identical_rows_and_counters(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    completions = [
        _completion(
            "sess-abandoned",
            [message(role="user", content=f"prompt {call_id}")],
            FIB,
            call_id=call_id,
            captured_at=old,
        )
        for call_id in ("call-1", "call-2")
    ]
    decisions = [
        _decision("sess-abandoned", call_id, occurred_at=old)
        for call_id in ("call-1", "call-2")
    ]
    clock = _decision(
        "sess-clock", "clock", accepted=False, occurred_at=old + timedelta(days=40)
    )

    _, forward = postgres_store_factory()
    for completion in completions:
        forward.store_inference_call(completion)
    forward.store_decisions([*decisions, clock])

    _, backward = postgres_store_factory()
    for completion in reversed(completions):
        backward.store_inference_call(completion)
    backward.store_decisions([clock, *reversed(decisions)])

    first = attributed_completions_module.assemble_attributed_completions_result(
        forward, MirrorManager(str(tmp_path / "forward-mirrors")), ORG
    )
    second = attributed_completions_module.assemble_attributed_completions_result(
        backward, MirrorManager(str(tmp_path / "backward-mirrors")), ORG
    )

    assert first == second


def test_abandonment_summary_covers_derivation_and_projection_counts() -> None:
    explicit = _abandonment(session_id="sess-explicit")
    implicit = SessionAbandonment(
        org_id=ORG,
        session_id="sess-implicit",
        accepted_decisions=2,
        explicit_accepted_decisions=0,
        last_decision_at=_recent() - timedelta(days=20),
        as_of=_recent(),
        provenance=_provenance("2"),
    )
    row = _attributed_completion_variant(
        abandonment=explicit, session_id="sess-explicit"
    )
    assembly = attributed_completions_module.AttributedCompletionAssemblyResult(
        rows=[row],
        abandonment=AbandonmentResult(
            sessions=[implicit, explicit],
            skipped=Counter({"within_grace_horizon": 2, "attribution_unavailable": 1}),
            as_of=_recent(),
            provenance=_provenance("2"),
        ),
        abandonment_skipped=Counter(
            {"explicit_accept_unjoined": 3, "implicit_only_session": 1}
        ),
    )

    summary = attributed_completions_module.build_abandonment_summary(assembly)

    assert summary.abandoned_sessions == 2
    assert summary.grade_eligible_sessions == 1
    assert summary.implicit_only_sessions == 1
    assert summary.negative_completions == 1
    assert summary.explicit_accepts_unjoined == 3
    assert summary.derivation_skipped == {
        "attribution_unavailable": 1,
        "within_grace_horizon": 2,
    }
    assert summary.provenance == _provenance("2")


def test_abandonment_summary_preserves_empty_provenance_and_is_deterministic() -> None:
    empty = attributed_completions_module.AttributedCompletionAssemblyResult(
        abandonment=AbandonmentResult(provenance=_provenance("2", 4))
    )
    empty_summary = attributed_completions_module.build_abandonment_summary(empty)
    assert empty_summary.abandoned_sessions == 0
    assert empty_summary.provenance == _provenance("2", 4)

    first = attributed_completions_module.AttributedCompletionAssemblyResult(
        abandonment=AbandonmentResult(
            skipped=Counter({"z": 2, "a": 1}), provenance=_provenance("2")
        ),
        abandonment_skipped=Counter({"explicit_accept_unjoined": 1}),
    )
    second = attributed_completions_module.AttributedCompletionAssemblyResult(
        abandonment=AbandonmentResult(
            skipped=Counter({"a": 1, "z": 2}), provenance=_provenance("2")
        ),
        abandonment_skipped=Counter({"explicit_accept_unjoined": 1}),
    )
    assert attributed_completions_module.build_abandonment_summary(
        first
    ) == attributed_completions_module.build_abandonment_summary(second)


def test_attribution_with_decision_and_ci_becomes_one_fully_populated_attributed_completion(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    session_id = "sess-1"
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    completion = _completion(
        session_id, [message(role="user", content="write fib")], FIB, call_id="call-1"
    )
    store.store_inference_call(completion)
    decision = _decision(session_id, "call-1")
    store.store_decision(decision)
    outcome = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run/1",
        repo=REPO,
        commit_sha=head,
        branch="main",
        result=CIResult.PASSED,
        run_url="run/1",
    )
    store.store_ci_outcome(outcome)

    attributed_completions = assemble_attributed_completions(store, mirrors, ORG)
    assert len(attributed_completions) == 1
    attributed_completion = attributed_completions[0]
    assert attributed_completion.org_id == ORG
    assert attributed_completion.session_id == session_id
    assert attributed_completion.inference_call_id == completion.inference_call_id
    assert attributed_completion.repo == REPO
    assert attributed_completion.commit_sha == head
    assert attributed_completion.file_path == "math_utils.py"
    assert attributed_completion.similarity_score > 0
    assert attributed_completion.attribution_source == AttributionSource.GIT_NOTES
    assert attributed_completion.decisions == [decision]
    assert attributed_completion.ci_outcomes == [outcome]
    assert attributed_completion.provenance.policy_version == "5"
    assert attributed_completion.split == "train"


def test_ambiguous_call_id_shared_by_two_completions_drops_decision(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    # Unique-or-drop at completion/org scope: two completions across the org
    # share "dup", so a decision keyed on it cannot bind to either — even
    # though only one of them (the noted one) actually attributes to a commit.
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-1"), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    attributed = _completion(
        "sess-1",
        [message(role="user", content="write fib")],
        FIB,
        call_id="dup",
        provider=GatewayProvider.LITELLM,
    )
    store.store_inference_call(attributed)
    # A second completion, unrelated session, sharing the same call_id (the
    # UNIQUE index is per-provider, so both persist).
    store.store_inference_call(
        _completion(
            "sess-2",
            [message(role="user", content="unrelated")],
            "unrelated output",
            call_id="dup",
            provider=GatewayProvider.PORTKEY,
        )
    )
    store.store_decision(_decision("sess-1", "dup"))

    result = assemble_attributed_completion_result(store, mirrors, ORG)
    attributed_completion = _only(
        result.attributed_completions, attributed.inference_call_id
    )
    assert attributed_completion.decisions == []
    assert result.skipped == {"ambiguous_decision_call_id": 1}


def test_ci_outcome_for_same_sha_in_a_different_repo_is_not_attached(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    store, mirrors, head = _stamped_scenario(postgres_store_factory, tmp_path)
    inference_call_id = store.read_inference_calls(ORG)[0].inference_call_id
    ours = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="ours/1",
        repo=REPO,
        commit_sha=head,
        branch="main",
        result=CIResult.PASSED,
        run_url="ours/1",
    )
    theirs = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="theirs/1",
        repo=OTHER_REPO,  # same sha, different (fork) repo
        commit_sha=head,
        branch="main",
        result=CIResult.FAILED,
        run_url="theirs/1",
    )
    store.store_ci_outcome(ours)
    store.store_ci_outcome(theirs)

    attributed_completions = assemble_attributed_completions(store, mirrors, ORG)
    attributed_completion = _only(attributed_completions, inference_call_id)
    assert attributed_completion.ci_outcomes == [ours]


def test_attribution_with_neither_decision_nor_ci_is_still_emitted(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    store, mirrors, head = _stamped_scenario(postgres_store_factory, tmp_path)
    inference_call_id = store.read_inference_calls(ORG)[0].inference_call_id

    attributed_completions = assemble_attributed_completions(store, mirrors, ORG)
    attributed_completion = _only(attributed_completions, inference_call_id)
    assert attributed_completion.decisions == []
    assert attributed_completion.ci_outcomes == []
    assert attributed_completion.commit_sha == head


def _filled_scenario(
    postgres_store_factory,
    tmp_path: Path,
    *,
    edit_retention_score: float | None,
) -> tuple[FactStore, MirrorManager, str]:
    """Attributed inference call + decision (call-1) ready for a survival fill.
    Returns ``(store, mirrors, inference_call_id)``."""
    session_id = "sess-1"
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    completion = _completion(
        session_id, [message(role="user", content="write fib")], FIB, call_id="call-1"
    )
    store.store_inference_call(completion)
    store.store_decision(
        _decision(
            session_id,
            "call-1",
            edit_retention_score=edit_retention_score,
        )
    )
    return store, mirrors, completion.inference_call_id


def test_matching_edit_observation_fills_edit_retention_score(
    tmp_path: Path, postgres_store_factory
) -> None:
    store, mirrors, inference_call_id = _filled_scenario(
        postgres_store_factory, tmp_path, edit_retention_score=None
    )
    # The applied snippet survives verbatim inside the session-end file.
    store.store_edit_observation(
        _edit_observation(
            "sess-1",
            "call-1",
            applied_text=FIB,
            observed_file_text=FIB + "print(fibonacci(5))\n",
        )
    )

    attributed_completion = _only(
        assemble_attributed_completions(store, mirrors, ORG), inference_call_id
    )
    assert [d.edit_retention_score for d in attributed_completion.decisions] == [1.0]


def test_vendor_supplied_edit_retention_score_stands(
    tmp_path: Path, postgres_store_factory
) -> None:
    store, mirrors, inference_call_id = _filled_scenario(
        postgres_store_factory, tmp_path, edit_retention_score=0.5
    )
    # A perfect-containment pair that would fill 1.0 — must not overwrite.
    store.store_edit_observation(
        _edit_observation(
            "sess-1",
            "call-1",
            applied_text=FIB,
            observed_file_text=FIB,
        )
    )

    attributed_completion = _only(
        assemble_attributed_completions(store, mirrors, ORG), inference_call_id
    )
    assert [d.edit_retention_score for d in attributed_completion.decisions] == [0.5]


def test_quarantined_decision_is_excluded_and_provenance_reflects_state(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    session_id = "sess-1"
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    completion = _completion(
        session_id, [message(role="user", content="write fib")], FIB, call_id="call-1"
    )
    store.store_inference_call(completion)
    decision = _decision(session_id, "call-1")
    store.store_decision(decision)

    before = _only(
        assemble_attributed_completions(store, mirrors, ORG),
        completion.inference_call_id,
    )
    assert before.decisions == [decision]
    assert before.provenance.quarantine_revision == 0

    store.quarantine_fact(
        ORG, FactTable.DEVELOPER_DECISIONS, decision.decision_id, reason="bad signal"
    )
    after = _only(
        assemble_attributed_completions(store, mirrors, ORG),
        completion.inference_call_id,
    )
    assert after.decisions == []
    assert after.provenance.quarantine_revision > 0


def test_rerunning_over_the_same_facts_is_byte_identical(
    tmp_path: Path, postgres_store_factory
) -> None:
    session_id = "sess-1"
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _completion(
            session_id, [message(role="user", content="write fib")], FIB, call_id="c1"
        )
    )
    store.store_decision(_decision(session_id, "c1"))
    store.store_edit_observation(
        _edit_observation(
            session_id,
            "c1",
            applied_text=FIB,
            observed_file_text=FIB,
        )
    )
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run/1",
            repo=REPO,
            commit_sha=head,
            branch="main",
            result=CIResult.PASSED,
            run_url="run/1",
        )
    )
    policy = AttributedCompletionPolicy()

    first = assemble_attributed_completions(store, mirrors, ORG, policy)
    second = assemble_attributed_completions(store, mirrors, ORG, policy)
    assert first == second
    assert len(first) == 1
    assert len(first[0].decisions) == 1
    assert first[0].decisions[0].edit_retention_score == 1.0
    assert len(first[0].ci_outcomes) == 1


def test_shuffled_ingest_order_yields_identical_attributed_completions(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    session_id = "sess-1"
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)

    c1 = _completion(
        session_id, [message(role="user", content="write fib")], FIB, call_id="c1"
    )
    c2 = _completion(
        session_id, [message(role="user", content="fib again")], FIB, call_id="c2"
    )
    d1, d2 = _decision(session_id, "c1"), _decision(session_id, "c2")
    o1 = _edit_observation(
        session_id,
        "c1",
        applied_text=FIB,
        observed_file_text=FIB,
    )
    o2 = _edit_observation(
        session_id,
        "c2",
        applied_text="def unrelated():\n",
        observed_file_text=FIB,
    )

    _, forward = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, forward, work, "0" * 40, head)
    for completion in (c1, c2):
        forward.store_inference_call(completion)
    for decision in (d1, d2):
        forward.store_decision(decision)
    for outcome in (o1, o2):
        forward.store_edit_observation(outcome)

    _, backward = postgres_store_factory()
    # Source Push identity is part of canonical Attribution evidence.
    backward.store_push(forward.read_pushes(ORG)[0])
    for outcome in (o2, o1):
        backward.store_edit_observation(outcome)
    for decision in (d2, d1):
        backward.store_decision(decision)
    for completion in (c2, c1):
        backward.store_inference_call(completion)

    policy = AttributedCompletionPolicy()
    assert assemble_attributed_completions(
        forward, mirrors, ORG, policy
    ) == assemble_attributed_completions(backward, mirrors, ORG, policy)


def test_attributed_completion_uses_canonical_default_split_primitive(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    store, mirrors, _head = _stamped_scenario(
        postgres_store_factory, tmp_path, session_id="alpha"
    )

    for row in assemble_attributed_completions(store, mirrors, ORG):
        assert row.split == split_of(row.session_id, 0.1)

    policy = AttributedCompletionPolicy(eval_fraction=0.5)
    attributed_completions = assemble_attributed_completions(
        store, mirrors, ORG, policy
    )
    for t in attributed_completions:
        assert t.split == split_of(t.session_id, 0.5)
    # Re-derivation reproduces the identical split assignment.
    again = assemble_attributed_completions(store, mirrors, ORG, policy)
    assert [t.split for t in again] == [t.split for t in attributed_completions]


def test_attributed_completion_policy_rejects_an_out_of_range_fraction() -> None:
    AttributedCompletionPolicy(eval_fraction=0.5)  # boundary is allowed
    with pytest.raises(ValueError):
        AttributedCompletionPolicy(eval_fraction=0.6)
    with pytest.raises(ValueError):
        AttributedCompletionPolicy(eval_fraction=-0.1)


def test_attributed_completion_policy_requires_one_attribution_definition() -> None:
    attribution = AttributionPolicy(
        jaccard=SimilarityPolicy(
            min_similarity=0.8,
            lookback_window_minutes=60,
        )
    )
    with pytest.raises(ValueError, match="attribution must match"):
        AttributedCompletionPolicy(attribution=attribution)

    policy = AttributedCompletionPolicy(
        attribution=attribution,
        abandonment=AbandonmentPolicy(attribution=attribution),
    )
    assert policy.policy_version == "5"


def test_no_attributions_yields_no_attributed_completions(
    tmp_path: Path, postgres_store_factory
) -> None:
    _, store = postgres_store_factory()
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    assert assemble_attributed_completions(store, mirrors, ORG) == []

    result = assemble_attributed_completion_result(store, mirrors, ORG)
    assert result.attributed_completions == []
    assert result.skipped == {}


@pytest.mark.parametrize("matched", [False, True])
def test_assembly_declines_cross_session_accept_and_is_ingest_deterministic(
    tmp_path: Path, postgres_store_factory, matched: bool
) -> None:
    from sediment_export import resolve_confidence, SFTPolicy, project_sft

    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-1"), head)
    _, first_store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, first_store, work, "0" * 40, head)
    call = _completion("sess-1", [message("user", "write fib")], FIB, call_id="unique")
    other = _completion("sess-2", [message("user", "unrelated")], "unrelated")
    decisions = [_decision("sess-2", "unique")]
    if matched:
        decisions.append(_decision("sess-1", "unique"))
    facts = [call, other, *decisions]
    _, shuffled_store = postgres_store_factory()
    shuffled_store.store_push(first_store.read_pushes(ORG)[0])
    for store, population in ((first_store, facts), (shuffled_store, reversed(facts))):
        for fact in population:
            if isinstance(fact, InferenceCall):
                store.store_inference_call(fact)
            else:
                store.store_decision(fact)
    result = assemble_attributed_completion_result(first_store, mirrors, ORG)
    row = _only(result.attributed_completions, call.inference_call_id)
    assert row.decisions == (decisions[1:] if matched else [])
    assert result.skipped == {"decision_session_mismatch": 1}
    assert row.provenance.policy_version == "5"
    assert assemble_attributed_completion_result(first_store, mirrors, ORG) == result
    assert assemble_attributed_completion_result(shuffled_store, mirrors, ORG) == result
    assert (resolve_confidence(row) is not None) is matched
    training = project_sft([row], {call.inference_call_id: call}, SFTPolicy())
    assert bool(training.rows) is matched


@pytest.mark.parametrize(
    "case",
    [
        "matching",
        "late",
        "other_org",
        "other_repo",
        "other_commit",
        "other_session",
        "quarantined",
    ],
)
def test_canonical_assembly_carries_only_eligible_captured_observation_facts(
    tmp_path, postgres_store, case
):
    from sediment_core import SessionCommitObservation, FactTable
    from sediment_derive import Attribution

    boundary = datetime(2026, 9, 6, tzinfo=UTC)
    observation = SessionCommitObservation(
        observation_id="captured",
        org_id="other" if case == "other_org" else ORG,
        repo="other/repo" if case == "other_repo" else REPO,
        commit_sha=("b" if case == "other_commit" else "a") * 40,
        session_id="other" if case == "other_session" else "candidate",
        source_push_id="push",
        captured_at=boundary + timedelta(microseconds=1 if case == "late" else 0),
    )
    postgres_store.store_session_commit_observation(observation)
    if case == "quarantined":
        postgres_store.quarantine_fact(
            ORG, FactTable.SESSION_COMMIT_OBSERVATIONS, "captured", reason="test"
        )
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha="a" * 40,
        session_id="candidate",
        inference_call_id="call",
        file_path="file.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.JACCARD,
        provenance=_provenance("1"),
    )
    result = attributed_completions_module.assemble_attributed_completions_result(
        postgres_store,
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        attributions=[attribution],
        completions=[],
        decisions=[],
        edit_observations=[],
        ci_outcomes=[],
        abandonment=AbandonmentResult(),
        as_of=boundary,
    )
    [row] = result.rows
    assert row.attribution_source == AttributionSource.JACCARD
    assert row.session_commit_observations == (
        (observation,) if case == "matching" else ()
    )


@pytest.mark.parametrize("owner", ["merge_retention", "rollout", "assembly"])
def test_public_observation_defaults_keep_both_fold_instants(
    postgres_store, tmp_path, owner
):
    from zoneinfo import ZoneInfo

    from sediment_core import PullRequestMerge, SessionCommitObservation
    from sediment_derive import derive_merge_retention_result, derive_rollout_result

    zone = ZoneInfo("Europe/London")
    early = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=0)
    late = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=1)
    work = _work_repo(tmp_path)
    (work / "README").write_text("base\n")
    base = _commit(work, "base")
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note("session"), head)
    push = Push(
        push_id="push",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(_make_remote(tmp_path, work)),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=head,
        captured_at=early.astimezone(UTC),
    )
    mirrors = MirrorManager(tmp_path / "mirrors")
    mirrors.ensure(push)
    postgres_store.store_push(push)
    postgres_store.store_inference_call(
        _completion(
            "session",
            [message("user", "write fibonacci")],
            FIB,
            captured_at=early.astimezone(UTC),
        )
    )
    postgres_store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=1,
            head_repo=REPO,
            head_ref="feature",
            head_sha=head,
            base_ref="main",
            base_sha=base,
            merge_commit_sha=head,
            merged_at=late,
            captured_at=late,
        )
    )
    observations = [
        SessionCommitObservation(
            observation_id=identifier,
            org_id=ORG,
            repo=REPO,
            commit_sha=sha,
            session_id="session",
            source_push_id="push",
            captured_at=when,
        )
        for identifier, sha, when in (("early", base, early), ("late", head, late))
    ]
    for fact in observations:
        postgres_store.store_session_commit_observation(fact)

    def derive(facts, **kwargs):
        kwargs["session_commit_observations"] = facts
        if owner == "merge_retention":
            result = derive_merge_retention_result(
                postgres_store, mirrors, ORG, **kwargs
            )
            return result.rows, result.attributed_candidates, dict(result.skipped)
        if owner == "rollout":
            result = derive_rollout_result(postgres_store, mirrors, ORG, **kwargs)
            [row] = result.rollouts
        else:
            result = assemble_attributed_completion_result(
                postgres_store, mirrors, ORG, **kwargs
            )
            [row] = result.attributed_completions
        return tuple(item.observation_id for item in row.session_commit_observations)

    stored = derive(None)
    if owner == "merge_retention":
        assert len(stored[0]) == stored[1] == 1
        assert stored[2] == {}
    else:
        assert stored == ("late",)
    for facts in (observations, list(reversed(observations))):
        assert derive(facts, as_of=late.astimezone(UTC)) == stored
        assert derive(facts) == stored
    # The repeated local wall time is an earlier instant when fold is zero.
    early_result = derive(observations, as_of=early)
    if owner == "merge_retention":
        assert early_result == ([], 0, {"session_commit_unobserved": 1})
    else:
        assert early_result == ()


@pytest.mark.parametrize("abandoned", [False, True])
def test_assembly_preserves_shared_repeated_hour_decision_order(
    tmp_path, postgres_store, abandoned
):
    from itertools import permutations
    from zoneinfo import ZoneInfo

    from sediment_derive import Attribution

    early = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0)
    late = early.replace(fold=1)
    call = inference_call(
        inference_call_id="inference-fold",
        org_id=ORG,
        session_id="sess-abandoned",
        model_call_id="call-fold",
    )
    decisions = [
        _decision(call.session_id, "call-fold", occurred_at=early).model_copy(
            update={"decision_id": "z-earlier"}
        ),
        _decision(call.session_id, "call-fold", occurred_at=late).model_copy(
            update={"decision_id": "a-later"}
        ),
    ]
    postgres_store.store_inference_call(call)
    for decision in decisions:
        postgres_store.store_decision(decision)
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha="a" * 40,
        file_path="math.py",
        inference_call_id=call.inference_call_id,
        session_id=call.session_id,
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=_provenance("2"),
    )
    abandonment = AbandonmentResult()
    if abandoned:
        abandonment.sessions.append(_abandonment(explicit_accepted_decisions=2))
    for order in permutations(decisions):
        result = attributed_completions_module.assemble_attributed_completions_result(
            postgres_store,
            MirrorManager(str(tmp_path / "mirrors")),
            ORG,
            attributions=[] if abandoned else [attribution],
            completions=[call],
            decisions=list(order),
            edit_observations=[],
            ci_outcomes=[],
            abandonment=abandonment,
            session_commit_observations=[],
            as_of=late.astimezone(UTC),
        )
        assert result.abandonment_skipped == {}
        [row] = result.rows
        assert [decision.decision_id for decision in row.decisions] == [
            "z-earlier",
            "a-later",
        ]
        assert [decision.occurred_at.astimezone(UTC) for decision in row.decisions] == [
            early.astimezone(UTC),
            late.astimezone(UTC),
        ]
        assert [decision.occurred_at.fold for decision in decisions] == [0, 1]
