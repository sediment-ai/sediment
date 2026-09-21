# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical derived-bundle construction over real facts and git mirrors."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sediment_capture import LiteLLMAdapter
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    ForgeProvider,
    InferenceCall,
    Push,
    ReasoningPart,
    ToolCallPart,
)
from sediment_core.store import FactStore
from sediment_derive import MirrorManager, render_scoring_text
from export_factories import inference_call, message
from sediment_export import (
    DerivationScope,
    build_derived_bundle,
    read_derived_bundle,
    write_derived_bundle,
)
from sediment_export import derived_bundle as derived_bundle_module

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
FIB = "def fibonacci(n):\n    return n if n <= 1 else fibonacci(n - 1)\n"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _repository(
    tmp_path: Path,
    session_id: str,
    *,
    source_text: str = FIB,
    include_base: bool = False,
) -> tuple[Path, str]:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    if include_base:
        (work / "README").write_text("base\n")
        _git(work, "add", "README")
        _git(work, "commit", "-q", "-m", "base")
    (work / "math_utils.py").write_text(source_text, encoding="utf-8")
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = T0.isoformat()
    env["GIT_COMMITTER_DATE"] = T0.isoformat()
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "add fibonacci", env=env)
    head = _git(work, "rev-parse", "HEAD")
    note = json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": session_id,
                    "stamped_at": T0.isoformat(),
                }
            ],
        }
    )
    _git(work, "notes", "--ref=sediment", "add", "-m", note, head, env=env)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    return remote, head


def _inference_call(
    session_id: str,
    user_id: str,
    *,
    inference_call_id: str,
    captured_at: datetime,
    text: str = FIB,
) -> InferenceCall:
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id=session_id,
        user_id=user_id,
        model="claude-sonnet-5",
        input_messages=[message("user", "write fibonacci")],
        output=text,
        model_call_id=f"call-{inference_call_id}",
        observed_at=captured_at,
    )


def _scenario(
    tmp_path: Path,
    store: FactStore,
    inference_calls: list[InferenceCall],
    *,
    source_text: str = FIB,
) -> tuple[FactStore, MirrorManager]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote, head = _repository(
        tmp_path, inference_calls[0].session_id, source_text=source_text
    )
    push = Push(
        push_id="push-1",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        captured_at=T0 + timedelta(minutes=10),
    )
    for call in inference_calls:
        store.store_inference_call(call)
    store.store_push(push)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    return store, mirrors


def test_complete_bundle_defaults_to_every_user_in_the_organization(
    tmp_path: Path,
    postgres_store,
) -> None:
    completion = _inference_call(
        "session-1", "alice", inference_call_id="completion-1", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [completion])

    bundle = build_derived_bundle(store, mirrors, ORG)

    assert bundle.scope.users is None
    assert [row.inference_call_id for row in bundle.attributed_completions] == [
        completion.inference_call_id
    ]
    assert [row.session_id for row in bundle.rollouts] == [completion.session_id]
    assert bundle.inference_calls == (completion,)
    assert bundle.policy.attribution.policy_version == "3"
    assert bundle.excluded == {}
    assert bundle.as_of == T0 + timedelta(minutes=10)
    assert (
        bundle.mirror_revisions[REPO]["refs/heads/main"]
        == bundle.attributed_completions[0].commit_sha
    )
    assert bundle.attributed_completions[0].provenance.policy_version == "5"
    assert (
        bundle.attributed_completions[0].provenance.policy_digest
        == bundle.policy.digest
    )
    assert bundle.rollouts[0].provenance.policy_version == "4"
    assert bundle.rollouts[0].provenance.policy_digest == bundle.policy.digest


def test_mirror_revisions_and_rollout_share_locked_disk_truth_after_rename(
    tmp_path: Path, postgres_store, monkeypatch
) -> None:
    completion = _inference_call(
        "session-1", "alice", inference_call_id="completion-1", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [completion])
    renamed = "acme-corp/backend-renamed"
    assert mirrors.rename(ORG, REPO, renamed) is True

    original = derived_bundle_module.derive_rollout_result
    received_repos: list[tuple[str, ...] | None] = []
    original_snapshot = mirrors.read_repository_snapshot
    locked_repos: list[tuple[str, ...]] = []

    @contextmanager
    def read_observed_snapshot(repositories):
        locked_repos.append(tuple(sorted(key.repo for key in repositories)))
        with original_snapshot(repositories) as snapshot:
            yield snapshot

    def derive_with_observed_repos(*args, mirror_repositories=None, **kwargs):
        received_repos.append(tuple(key.repo for key in mirror_repositories))
        return original(*args, mirror_repositories=mirror_repositories, **kwargs)

    monkeypatch.setattr(
        derived_bundle_module, "derive_rollout_result", derive_with_observed_repos
    )
    monkeypatch.setattr(mirrors, "read_repository_snapshot", read_observed_snapshot)

    bundle = build_derived_bundle(store, mirrors, ORG)

    # The mirror snapshot lock set and the manifest's
    # mirror_revisions enumerate disk truth. The push-derived set would still
    # hold the pre-rename name, which opens to nothing after the directory
    # move; disk truth keeps the renamed mirror locked and reported.
    assert set(bundle.mirror_revisions) == {renamed}
    assert locked_repos == [tuple(sorted((REPO, renamed)))]
    assert received_repos == [(renamed,)]
    assert (
        bundle.mirror_revisions[renamed]["refs/heads/main"]
        == store.read_pushes(ORG)[0].after_sha
    )


def test_time_scope_filters_after_full_derivation_and_keeps_whole_rollout(
    tmp_path: Path,
    postgres_store,
) -> None:
    matching = _inference_call(
        "session-1", "alice", inference_call_id="matching", captured_at=T0, text=FIB
    )
    in_window = _inference_call(
        "session-1",
        "alice",
        inference_call_id="in-window",
        captured_at=T0 + timedelta(hours=2),
        text="unrelated response",
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [matching, in_window])
    scope = DerivationScope(
        since=T0 + timedelta(hours=1), until=T0 + timedelta(hours=3)
    )

    bundle = build_derived_bundle(store, mirrors, ORG, scope=scope)

    assert bundle.attributed_completions == ()
    assert len(bundle.rollouts) == 1
    assert {
        turn.inference_call_id
        for segment in bundle.rollouts[0].segments
        for turn in segment
    } == {"matching", "in-window"}
    assert bundle.rollouts[0].commits
    assert {completion.inference_call_id for completion in bundle.inference_calls} == {
        "matching",
        "in-window",
    }
    assert bundle.excluded == {"attributed_completion_time_scope": 1}


def test_user_scope_excludes_and_counts_a_mixed_user_rollout(
    tmp_path: Path, postgres_store
) -> None:
    alice = _inference_call(
        "session-1", "alice", inference_call_id="alice", captured_at=T0, text=FIB
    )
    bob = _inference_call(
        "session-1",
        "bob",
        inference_call_id="bob",
        captured_at=T0 + timedelta(minutes=1),
        text="unrelated response",
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [alice, bob])

    bundle = build_derived_bundle(
        store, mirrors, ORG, scope=DerivationScope(users=("alice",))
    )

    assert [row.inference_call_id for row in bundle.attributed_completions] == ["alice"]
    assert bundle.rollouts == ()
    assert bundle.inference_calls == (alice,)
    assert {item.inference_call_id for item in bundle.inference_call_identities} == {
        "alice",
        "bob",
    }
    assert bundle.excluded == {"mixed_user_rollout": 1}


def test_bundle_identity_read_declines_overflow_before_partial_derivation(
    tmp_path, postgres_store, monkeypatch
):
    import pytest
    from sediment_core import OperationalReportLimitExceeded

    call = _inference_call(
        "session-1", "alice", inference_call_id="call", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [call])
    store.store_inference_call(
        call.model_copy(
            update={
                "inference_call_id": "outside",
                "model_call_id": "outside",
                "session_id": "other-session",
            }
        )
    )
    monkeypatch.setattr(derived_bundle_module, "_IDENTITY_LIMIT", 1)
    with pytest.raises(OperationalReportLimitExceeded, match="identity"):
        build_derived_bundle(
            store, mirrors, ORG, scope=DerivationScope(users=("alice",))
        )


def test_scoped_bundle_retains_complete_alias_evidence_and_quarantine(
    tmp_path, postgres_store
):
    from sediment_core import FactTable, InferenceMessage, TextPart
    from test_derived_bundle_io import _decision

    calls = []
    for identity, user, at in [
        ("alice", "alice", T0),
        ("bob", "bob", T0 - timedelta(days=30)),
    ]:
        call = _inference_call(
            "session-1", user, inference_call_id=identity, captured_at=at
        )
        calls.append(
            call.model_copy(
                update={
                    "output_messages": [
                        InferenceMessage(
                            role="assistant",
                            parts=[
                                TextPart(content=FIB),
                                ToolCallPart(id="shared", name="Edit", arguments={}),
                            ],
                        )
                    ]
                }
            )
        )
    store, mirrors = _scenario(tmp_path, postgres_store, calls)
    store.store_decision(_decision(call_id="shared"))
    scope = DerivationScope(users=("alice",))
    bundle = build_derived_bundle(store, mirrors, ORG, scope=scope)
    assert {item.inference_call_id for item in bundle.inference_call_identities} == {
        "alice",
        "bob",
    }
    assert {call.inference_call_id for call in bundle.inference_calls} == {"alice"}
    assert bundle.rollouts == ()
    assert bundle.attributed_completions[0].decisions == []
    store.quarantine_fact(ORG, FactTable.INFERENCE_CALLS, "bob", reason="control")
    restored = build_derived_bundle(store, mirrors, ORG, scope=scope)
    assert {item.inference_call_id for item in restored.inference_call_identities} == {
        "alice"
    }
    assert [
        decision.call_id for decision in restored.attributed_completions[0].decisions
    ] == ["shared"]


def test_bundle_is_deterministic_for_reversed_fact_insertion_order(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    inference_calls = [
        _inference_call(
            "session-1", "alice", inference_call_id="first", captured_at=T0, text=FIB
        ),
        _inference_call(
            "session-1",
            "alice",
            inference_call_id="second",
            captured_at=T0 + timedelta(minutes=1),
            text="unrelated response",
        ),
    ]
    _, forward_store = postgres_store_factory()
    forward_store, forward_mirrors = _scenario(
        tmp_path / "forward", forward_store, inference_calls
    )
    _, reverse_store = postgres_store_factory()
    reverse_store, reverse_mirrors = _scenario(
        tmp_path / "reverse", reverse_store, list(reversed(inference_calls))
    )

    assert build_derived_bundle(
        forward_store, forward_mirrors, ORG
    ) == build_derived_bundle(reverse_store, reverse_mirrors, ORG)


def test_inference_call_round_trips_through_bundle(
    tmp_path: Path, postgres_store
) -> None:
    fixture = (
        Path(__file__).parents[2] / "capture/tests/fixtures/litellm_slo_tool_call.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["messages"][0]["content"] = [
        {"type": "thinking", "thinking": "Inspect the existing implementation."},
        {"type": "text", "text": "edit the file"},
    ]
    payload["response"]["choices"][0]["message"]["reasoning_content"] = (
        "Use the smallest edit."
    )
    translated = LiteLLMAdapter().normalize(
        payload,
        session_id="session-1",
        user_id="alice",
        org_id=ORG,
    )
    call = translated.model_copy(update={"observed_at": T0})
    source_text = (
        'FILE_PATH = "/home/dev/project/app/math_utils.py"\n'
        'OLD_STRING = "a"\n'
        'NEW_STRING = "b"\n'
    )
    store, mirrors = _scenario(
        tmp_path, postgres_store, [call], source_text=source_text
    )

    assert store.read_inference_calls(ORG) == [call]
    assert render_scoring_text(call) == ("/home/dev/project/app/math_utils.py\nb\na")

    bundle = build_derived_bundle(store, mirrors, ORG)
    destination = tmp_path / "bundle"
    write_derived_bundle(bundle, destination)

    assert bundle.inference_calls == (call,)
    assert bundle.attributed_completions[0].inference_call_id == call.inference_call_id
    turn = bundle.rollouts[0].segments[0][0]
    assert turn.new_messages == call.input_messages
    assert call.input_messages[0].parts[0] == ReasoningPart(
        content="Inspect the existing implementation."
    )
    assert call.output_messages[0].parts[0] == ReasoningPart(
        content="Use the smallest edit."
    )
    tool_call = call.output_messages[0].parts[1]
    assert isinstance(tool_call, ToolCallPart)
    assert turn.tool_calls == (tool_call,)
    assert read_derived_bundle(destination) == bundle


def test_rich_ci_fact_round_trips_through_canonical_bundle_v4(
    tmp_path: Path, postgres_store
) -> None:
    call = _inference_call(
        "session-1", "alice", inference_call_id="completion-1", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [call])
    head = store.read_pushes(ORG)[0].after_sha
    outcome = CIOutcome(
        outcome_id="outcome-1",
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run-9",
        run_attempt=2,
        repo=REPO,
        commit_sha=head,
        branch="main",
        result=CIResult.ERROR,
        workflow_name="CI",
        workflow_id="workflow-9",
        workflow_path=".github/workflows/ci.yml",
        run_url="https://github.com/acme/backend/actions/runs/9",
        provider_result="startup_failure",
        error_type="runner_lost",
        reason="runner stopped responding",
        source_event_type="github.workflow_run.completed",
        source_spec_version="2022-11-28",
        source_event_id="delivery-9",
        captured_at=T0 + timedelta(minutes=11),
        raw={"conclusion": "startup_failure"},
    )
    store.store_ci_outcome(outcome)

    bundle = build_derived_bundle(store, mirrors, ORG)
    destination = tmp_path / "bundle"
    write_derived_bundle(bundle, destination)
    manifest = json.loads((destination / "manifest.json").read_text())

    assert bundle.attributed_completions[0].ci_outcomes == [outcome]
    assert bundle.rollouts[0].terminal_outcomes == [outcome]
    assert manifest["bundle_schema_version"] == 4
    assert manifest["implementation_versions"]["attribution"] == "3"
    assert read_derived_bundle(destination) == bundle


def test_rollout_fragmentation_and_owner_version_reach_bundle(
    tmp_path: Path, postgres_store
) -> None:
    first = _inference_call(
        "session-1", "alice", inference_call_id="first", captured_at=T0
    )
    second = _inference_call(
        "session-1",
        "alice",
        inference_call_id="second",
        captured_at=T0 + timedelta(minutes=1),
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [first, second])
    bundle = build_derived_bundle(store, mirrors, ORG)
    assert bundle.fragmented == {"prior_output_not_replayed": 1}
    assert bundle.rollouts[0].provenance.policy_version == "4"
    assert "prior_output_not_replayed" not in bundle.skipped


def test_direct_rlvr_export_preserves_fragmentation_diagnostics(
    tmp_path: Path, postgres_store
) -> None:
    from sediment_export import export_rlvr

    first = _inference_call(
        "session-1", "alice", inference_call_id="first", captured_at=T0
    )
    second = _inference_call(
        "session-1",
        "alice",
        inference_call_id="second",
        captured_at=T0 + timedelta(minutes=1),
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [first, second])
    summary = export_rlvr(store, mirrors, ORG, tmp_path / "rlvr", target="nemo-gym")
    assert summary["fragmented"] == {"prior_output_not_replayed": 1}


def test_bundle_boundary_includes_carried_session_observation_capture(
    tmp_path, postgres_store
):
    from sediment_core import SessionCommitObservation

    call = _inference_call("session", "user", inference_call_id="call", captured_at=T0)
    store, mirrors = _scenario(tmp_path, postgres_store, [call])
    push = store.read_pushes(ORG)[0]
    fact = SessionCommitObservation(
        observation_id="late-observation",
        org_id=ORG,
        session_id="session",
        repo=REPO,
        commit_sha=push.after_sha,
        source_push_id=push.push_id,
        captured_at=T0 + timedelta(days=20),
    )
    store.store_session_commit_observation(fact)
    bundle = build_derived_bundle(store, mirrors, ORG)
    assert bundle.as_of == fact.captured_at
    assert bundle.attributed_completions[0].session_commit_observations == (fact,)
    assert bundle.rollouts[0].session_commit_observations == (fact,)
    destination = tmp_path / "observation-bundle"
    write_derived_bundle(bundle, destination)
    assert read_derived_bundle(destination) == bundle


def test_builder_sets_observation_boundary_before_artifact_derivation(
    tmp_path: Path, postgres_store, monkeypatch
) -> None:
    from sediment_core import SessionCommitObservation

    call = _inference_call(
        "session-1", "alice", inference_call_id="completion-1", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [call])
    push = store.read_pushes(ORG)[0]
    observation = SessionCommitObservation(
        observation_id="observation-boundary",
        org_id=ORG,
        repo=REPO,
        commit_sha=push.after_sha,
        session_id="session-1",
        source_push_id=push.push_id,
        captured_at=T0 + timedelta(days=5),
    )
    store.store_session_commit_observation(observation)
    observed = []
    for name in ("assemble_attributed_completion_result", "derive_rollout_result"):
        original = getattr(derived_bundle_module, name)

        def inspect_boundary(*args, _original=original, **kwargs):
            observed.append(
                (kwargs.get("as_of"), kwargs.get("session_commit_observations"))
            )
            return _original(*args, **kwargs)

        monkeypatch.setattr(derived_bundle_module, name, inspect_boundary)
    bundle = build_derived_bundle(store, mirrors, ORG)
    assert observed == [(observation.captured_at, [observation])] * 2
    assert bundle.as_of == observation.captured_at


def test_bundle_preserves_attributed_retention_fill_and_captured_rollout_decision(
    tmp_path: Path, postgres_store
) -> None:
    from sediment_core import (
        AgentHarness,
        DeveloperDecision,
        EditObservation,
        InteractionMode,
    )

    call = _inference_call(
        "session-1", "alice", inference_call_id="completion-1", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [call])
    decision = DeveloperDecision(
        decision_id="retention-decision",
        org_id=ORG,
        session_id="session-1",
        user_id="alice",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="math_utils.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id=call.model_call_id,
        occurred_at=T0,
        captured_at=T0,
    )
    observation = EditObservation(
        observation_id="retention-observation",
        org_id=ORG,
        session_id="session-1",
        user_id="alice",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="math_utils.py",
        call_id=call.model_call_id,
        applied_text=FIB,
        observed_file_text=FIB,
        occurred_at=T0,
        captured_at=T0,
    )
    store.store_decision(decision)
    store.store_edit_observation(observation)
    bundle = build_derived_bundle(store, mirrors, ORG)
    assert bundle.attributed_completions[0].decisions[0].edit_retention_score == 1.0
    assert bundle.rollouts[0].segments[0][0].decisions[0].edit_retention_score is None
    destination = tmp_path / "retention-bundle"
    write_derived_bundle(bundle, destination)
    assert read_derived_bundle(destination) == bundle
    assert store.read_decisions(ORG)[0].edit_retention_score is None


def _identified_scenario(tmp_path, postgres_store):
    from sediment_core import RepositoryRename, SessionCommitObservation
    from sediment_derive import read_repository_context

    remote, head = _repository(tmp_path, "session-1", include_base=True)
    identity = dict(
        repository_provider="github", repository_host="github.com", repository_id="101"
    )
    push = Push(
        push_id="identified-push",
        org_id=ORG,
        provider="github",
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        captured_at=T0,
        **identity,
    )
    outcome = CIOutcome(
        org_id=ORG,
        outcome_id="identified-ci",
        provider="github_actions",
        repo="acme-corp/renamed",
        commit_sha=head,
        branch="main",
        run_id="identified-run",
        result="passed",
        captured_at=T0 + timedelta(minutes=1),
        **identity,
    )
    observation = SessionCommitObservation(
        org_id=ORG,
        observation_id="identified-observation",
        repo=REPO,
        commit_sha=head,
        session_id="session-1",
        source_push_id=push.push_id,
        captured_at=T0,
        **identity,
    )
    rename = RepositoryRename(
        org_id=ORG,
        old_repo=REPO,
        new_repo=outcome.repo,
        captured_at=T0 + timedelta(minutes=2),
        **identity,
    )
    call = _inference_call(
        "session-1", "alice", inference_call_id="identified-call", captured_at=T0
    )
    for writer, fact in [
        ("store_push", push),
        ("store_ci_outcome", outcome),
        ("store_session_commit_observation", observation),
        ("store_repository_rename", rename),
        ("store_inference_call", call),
    ]:
        getattr(postgres_store, writer)(fact)
    with postgres_store.read_snapshot() as snapshot:
        context = read_repository_context(snapshot, ORG)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push, repository_context=context)
    bundle = build_derived_bundle(postgres_store, mirrors, ORG)
    return bundle, mirrors, push, outcome, observation, rename


def test_identified_bundle_retains_complete_population_and_exact_sources(
    tmp_path, postgres_store
):
    from sediment_derive.repository_identity import repository_identity_of

    bundle, mirrors, push, outcome, observation, rename = _identified_scenario(
        tmp_path, postgres_store
    )
    assert bundle.as_of == rename.captured_at
    assert len(bundle.repository_identities) == 3
    assert bundle.repository_renames == (rename,)
    [row] = bundle.attributed_completions
    assert row.repository_identity == repository_identity_of(push)
    assert row.source_push_id == push.push_id
    assert row.ci_outcomes == [outcome]
    assert row.session_commit_observations == (observation,)
    assert bundle.rollouts[0].commits[0].repository_identity == row.repository_identity
    destination = write_derived_bundle(bundle, tmp_path / "bundle")
    assert read_derived_bundle(destination) == bundle
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["bundle_schema_version"] == 4
    assert manifest["repository_population"] == "organization-through-as-of-v1"
    assert manifest["implementation_versions"]["repository_identity"] == "1"
    assert set(manifest["files"]) == {
        "attributed_completions",
        "rollouts",
        "inference_calls",
        "inference_call_identities",
        "repository_identities",
        "repository_renames",
    }
    scoped = build_derived_bundle(
        postgres_store, mirrors, ORG, scope=DerivationScope(users=("nobody",))
    )
    assert not scoped.attributed_completions and not scoped.rollouts
    assert scoped.repository_identities == bundle.repository_identities
    assert scoped.repository_renames == bundle.repository_renames


def test_rename_only_bundle_has_repository_capture_boundary(tmp_path, postgres_store):
    from sediment_core import RepositoryRename

    rename = RepositoryRename(
        org_id=ORG,
        repository_provider="github",
        repository_host="github.com",
        repository_id="101",
        old_repo=REPO,
        new_repo="acme-corp/renamed",
        captured_at=T0,
    )
    postgres_store.store_repository_rename(rename)
    bundle = build_derived_bundle(
        postgres_store, MirrorManager(str(tmp_path / "mirrors")), ORG
    )
    assert bundle.as_of == T0
    assert bundle.repository_renames == (rename,)
    assert (
        read_derived_bundle(write_derived_bundle(bundle, tmp_path / "rename-only"))
        == bundle
    )


@pytest.mark.parametrize("target", ["sediment", "swe-bench", "nemo-gym"])
def test_all_rlvr_targets_keep_identified_verifier_and_observation_sources(
    tmp_path, postgres_store, target
):
    from sediment_export.rlvr import export_rlvr_from_bundle

    bundle, mirrors, push, outcome, observation, _ = _identified_scenario(
        tmp_path, postgres_store
    )
    destination = tmp_path / target
    result = export_rlvr_from_bundle(
        bundle, mirrors if target != "nemo-gym" else None, destination, target=target
    )
    assert result["task_rows" if target == "swe-bench" else "rollout_rows"] == 1
    rows = [
        json.loads(line)
        for path in destination.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert rows
    for row in rows:
        metadata = row.get("metadata", row)
        assert metadata["repository_identity"] == {
            "provider": "github",
            "host": "github.com",
            "repository_id": "101",
        }
        assert metadata["session_commit_observation_ids"] == [
            observation.observation_id
        ]
        assert metadata["ci_resolution"]["source_outcome_ids"] == [outcome.outcome_id]


def test_diff_sft_opens_identified_mirror_and_preserves_source_ids(
    tmp_path, postgres_store
):
    from sediment_export.derived_bundle import validate_derived_bundle
    from sediment_export.diff_sft import project_diff_sft
    from sediment_export.sft import SFTPolicy

    bundle, mirrors, _, outcome, observation, _ = _identified_scenario(
        tmp_path, postgres_store
    )
    context = validate_derived_bundle(bundle)
    result = project_diff_sft(
        bundle.attributed_completions,
        {call.inference_call_id: call for call in bundle.inference_calls},
        mirrors,
        SFTPolicy(recipe_id="sft_verified"),
        repository_context=context,
    )
    [row] = result.rows
    assert (
        row.metadata.repository_identity
        == bundle.attributed_completions[0].repository_identity
    )
    assert row.metadata.source_ids.session_commit_observation_ids == (
        observation.observation_id,
    )
    assert row.metadata.source_ids.ci_outcome_ids == [outcome.outcome_id]


def test_context_builder_bounds_full_fact_lifetime_and_preserves_output(
    tmp_path: Path, postgres_store, monkeypatch
) -> None:
    import tracemalloc
    from dataclasses import replace
    from sediment_core.store import _FactSnapshot

    calls = [
        _inference_call(
            f"session-{index}",
            "alice",
            inference_call_id=f"call-{index:02}",
            captured_at=T0 + timedelta(seconds=index),
        ).model_copy(update={"raw": {"payload": str(index) + "x" * (1024 * 1024)}})
        for index in range(12)
    ]
    store, mirrors = _scenario(tmp_path / "scenario", postgres_store, calls)
    baseline = build_derived_bundle(store, mirrors, ORG)
    expected_path = write_derived_bundle(baseline, tmp_path / "expected")
    del baseline, calls
    staging = tmp_path / "staging"
    staging.mkdir()
    builder = getattr(derived_bundle_module, "build_derived_bundle_context", None)
    assert callable(builder), "canonical construction needs a bounded context"

    def forbid_population(*args, **kwargs):
        pytest.fail("canonical construction materialized the full Fact population")

    monkeypatch.setattr(_FactSnapshot, "read_inference_calls_by_ids", forbid_population)
    tracemalloc.start()
    try:
        with builder(store, mirrors, ORG, temporary_parent=staging) as bundle:
            assert len(bundle.inference_calls) == 12
            # Metadata replacement does not materialize the payload sequences.
            assert replace(bundle, skipped=dict(bundle.skipped)) == bundle
            actual_path = write_derived_bundle(bundle, tmp_path / "actual")
            _, peak = tracemalloc.get_traced_memory()
            assert peak < 10 * 1024 * 1024
            assert list(staging.iterdir())
        assert not list(staging.iterdir())
        with pytest.raises(ValueError, match="closed"):
            bundle.inference_calls[0]
    finally:
        tracemalloc.stop()
    for expected in expected_path.iterdir():
        assert expected.read_bytes() == (actual_path / expected.name).read_bytes()


def test_builder_materialization_budget_refuses_before_payload_tuple(
    tmp_path: Path, postgres_store
) -> None:
    call = _inference_call(
        "session-1", "alice", inference_call_id="call-1", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path, postgres_store, [call])
    with pytest.raises(derived_bundle_module.BundleCapacityError, match="materializ"):
        build_derived_bundle(
            store,
            mirrors,
            ORG,
            limits=derived_bundle_module.BundleLimits(max_materialized_bytes=1),
        )


def test_context_builder_capacity_failure_cleans_private_stage(
    tmp_path: Path, postgres_store
) -> None:
    call = _inference_call(
        "session-1", "alice", inference_call_id="call-1", captured_at=T0
    )
    store, mirrors = _scenario(tmp_path / "scenario", postgres_store, [call])
    staging = tmp_path / "staging"
    staging.mkdir()
    builder = getattr(derived_bundle_module, "build_derived_bundle_context", None)
    assert callable(builder), "canonical construction needs a bounded context"
    with pytest.raises(derived_bundle_module.BundleCapacityError):
        with builder(
            store,
            mirrors,
            ORG,
            temporary_parent=staging,
            limits=derived_bundle_module.BundleLimits(max_staging_bytes=1),
        ):
            pytest.fail("oversized construction must not expose a partial bundle")
    assert not list(staging.iterdir())
