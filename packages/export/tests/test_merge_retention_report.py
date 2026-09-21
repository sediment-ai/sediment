# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merge-retention diagnostic aggregation."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    InteractionMode,
    PullRequestMerge,
    Push,
    TextPart,
    SessionCommitObservation,
)
from sediment_derive import (
    Attribution,
    AttributionSource,
    MergeRetention,
    MergeRetentionResult,
    MirrorManager,
    Provenance,
    derive_merge_retention_result,
)
from sediment_export import (
    build_merge_retention_report,
    generate_merge_retention_report,
    generate_merge_retention_report_result,
    merge_retention_to_export_rows,
    write_jsonl,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
FIB = (
    "def fibonacci(n: int) -> int:\n"
    "    if n <= 1:\n"
    "        return n\n"
    "    return fibonacci(n - 1) + fibonacci(n - 2)\n"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _commit(work: Path, message: str) -> str:
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD").strip()


def _row(call_id: str, head: float, merged: float, pr_number: int) -> MergeRetention:
    provenance = Provenance(policy_version="1", quarantine_revision=3)
    return MergeRetention(
        org_id="acme",
        repo="acme/backend",
        pr_number=pr_number,
        merge_id=f"merge-{pr_number}",
        inference_call_id=call_id,
        session_id=f"session-{call_id}",
        source_commit_sha=call_id[0] * 40,
        source_file_path=f"{call_id}.py",
        head_commit_sha="d" * 40,
        head_file_path=f"{call_id}.py",
        merge_commit_sha="e" * 40,
        merge_file_path=f"{call_id}.py",
        head_retention_score=head,
        merge_retention_score=merged,
        attribution_source=AttributionSource.GIT_NOTES,
        attribution_similarity_score=1.0,
        provenance=provenance,
    )


def test_build_merge_retention_report_reconstructs_source_counts_and_thresholds() -> (
    None
):
    provenance = Provenance(policy_version="1", quarantine_revision=3)
    result = MergeRetentionResult(
        provenance=provenance,
        rows=[
            _row("a", 1.0, 1.0, 41),
            _row("b", 0.9, 0.8, 41),
            _row("c", 0.5, 0.2, 42),
        ],
        skipped=Counter({"binary_file": 1}),
        membership=Counter({"attribution_without_merge": 2}),
        attributed_candidates=7,
        joined_candidates=5,
        joined_pull_requests=2,
    )

    result.session_commit_observations = tuple(
        SessionCommitObservation(
            observation_id=f"observation/{row.inference_call_id}",
            org_id=row.org_id,
            repo=row.repo,
            commit_sha=row.source_commit_sha,
            session_id=row.session_id,
            source_push_id="push",
            captured_at=T0,
        )
        for row in result.rows
    )
    report = build_merge_retention_report(
        "acme",
        result,
        explicit_accepted_inference_call_ids={"a", "c"},
        attribution_skipped=Counter({"no_match": 4}),
        decision_attachment_skipped=Counter({"ambiguous_decision_call_id": 2}),
        attribution_provenance=Provenance(policy_version="2", quarantine_revision=3),
    )

    assert report.attributed_file_candidates == 3
    assert report.candidates_joined_to_merge == 3
    assert report.scored_rows == 3
    assert report.joined_pull_requests == 2
    assert report.scored_pull_requests == 2
    assert report.membership == {}
    assert report.scoring_skips == {"binary_file": 1}
    assert report.decision_attachment_skips == {"ambiguous_decision_call_id": 2}
    assert report.head.distribution.mean == pytest.approx(0.8)
    assert report.head.distribution.median == 0.9
    assert report.head.distribution.p10 == pytest.approx(0.58)
    assert report.head.distribution.p90 == pytest.approx(0.98)
    assert [item.below for item in report.head.thresholds] == [1, 1, 2]
    assert [item.total for item in report.head.thresholds] == [3, 3, 3]
    assert [item.below for item in report.merge.thresholds] == [1, 2, 2]
    assert [item.below for item in report.explicit_accept_head_thresholds] == [
        1,
        1,
        1,
    ]
    assert all(item.total == 2 for item in report.explicit_accept_head_thresholds)
    assert report.attribution_provenance.policy_version == "2"
    assert report.merge_retention_provenance == provenance


def test_merge_retention_rows_write_canonical_schema_in_derivation_order(
    tmp_path: Path,
) -> None:
    rows = [_row("b", 0.9, 0.8, 42), _row("a", 1.0, 1.0, 41)]
    destination = tmp_path / "merge-retention.jsonl"

    write_result = write_jsonl(
        merge_retention_to_export_rows(rows),
        destination,
        split_enabled=False,
    )

    objects = [json.loads(line) for line in destination.read_text().splitlines()]
    schema_path = (
        Path(__file__).parents[3] / "schemas/derived-artifacts/merge-retention/v3.json"
    )
    validator = Draft202012Validator(json.loads(schema_path.read_text()))
    assert [item["inference_call_id"] for item in objects] == ["b", "a"]
    assert write_result.written == {str(destination): 2}
    assert all(not list(validator.iter_errors(item)) for item in objects)


def test_empty_merge_retention_rows_leave_existing_destination_untouched(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "merge-retention.jsonl"
    destination.write_text("earlier artifact\n")

    result = write_jsonl(
        merge_retention_to_export_rows([]),
        destination,
        split_enabled=False,
    )

    assert destination.read_text() == "earlier artifact\n"
    assert result.skipped_empty == [str(destination)]


def test_generate_merge_retention_report_joins_explicit_acceptance(
    tmp_path: Path, postgres_store
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    (work / "README.md").write_text("# service\n")
    base = _commit(work, "root")
    (work / "math_utils.py").write_text(FIB)
    source = _commit(work, "add fibonacci")
    (work / "README.md").write_text("# reviewed service\n")
    head = _commit(work, "address review")
    (work / "README.md").write_text("# merged service\n")
    merged = _commit(work, "merge boundary")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")

    source_push = Push(
        push_id="push-source",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=source,
        captured_at=T0,
    )
    merged_push = Push(
        push_id="push-merged",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=head,
        after_sha=merged,
        captured_at=T0 + timedelta(minutes=2),
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(merged_push)
    postgres_store.store_push(source_push)
    postgres_store.store_push(merged_push)
    postgres_store.store_session_commit_observation(
        SessionCommitObservation(
            observation_id="observed-source",
            org_id=ORG,
            repo=REPO,
            commit_sha=source_push.after_sha,
            session_id="session-1",
            source_push_id=source_push.push_id,
            captured_at=T0,
        )
    )
    postgres_store.store_inference_call(
        InferenceCall(
            inference_call_id="inference-1",
            org_id=ORG,
            session_id="session-1",
            user_id="developer-1",
            gateway_provider=GatewayProvider.LITELLM,
            model="model-1",
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content="Add fib")])
            ],
            output_messages=[
                InferenceMessage(role="assistant", parts=[TextPart(content=FIB)])
            ],
            model_call_id="call-1",
            observed_at=T0 - timedelta(minutes=1),
        )
    )
    postgres_store.store_decision(
        DeveloperDecision(
            decision_id="decision-1",
            org_id=ORG,
            session_id="session-1",
            user_id="developer-1",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="math_utils.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="call-1",
            occurred_at=T0,
            captured_at=T0,
        )
    )
    postgres_store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge-41",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature/query",
            head_sha=head,
            base_ref="main",
            base_sha=base,
            merge_commit_sha=merged,
            merged_at=T0 + timedelta(minutes=3),
            captured_at=T0 + timedelta(minutes=3),
        )
    )

    generated = generate_merge_retention_report_result(postgres_store, mirrors, ORG)
    report = generated.report

    assert report.attributed_file_candidates == 1
    assert report.candidates_joined_to_merge == 1
    assert report.scored_rows == 1
    assert report.joined_pull_requests == 1
    assert report.scored_pull_requests == 1
    assert report.explicit_accept_rows == 1
    assert report.decision_attachment_skips == {}
    assert report.head.distribution.mean == 1.0
    assert report.merge.distribution.mean == 1.0
    assert len(generated.rows) == 1
    assert generated.rows[0].inference_call_id == "inference-1"

    attributions = [
        Attribution(
            org_id=ORG,
            repo=REPO,
            commit_sha=source,
            file_path="math_utils.py",
            inference_call_id=f"inference-{suffix}",
            session_id=f"session-{suffix}",
            similarity_score=1.0,
            attribution_source=AttributionSource.GIT_NOTES,
            provenance=Provenance(policy_version="1", quarantine_revision=0),
        )
        for suffix in ("z", "a")
    ]
    for attribution in attributions:
        postgres_store.store_session_commit_observation(
            SessionCommitObservation(
                observation_id=f"observed/{attribution.session_id}",
                org_id=ORG,
                repo=REPO,
                commit_sha=source,
                session_id=attribution.session_id,
                source_push_id=source_push.push_id,
                captured_at=T0,
            )
        )
    ordered = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=attributions,
    )
    reversed_input = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=list(reversed(attributions)),
    )
    ordered_path = tmp_path / "ordered.jsonl"
    reversed_path = tmp_path / "reversed.jsonl"
    write_jsonl(
        merge_retention_to_export_rows(ordered.rows),
        ordered_path,
        split_enabled=False,
    )
    write_jsonl(
        merge_retention_to_export_rows(reversed_input.rows),
        reversed_path,
        split_enabled=False,
    )

    assert ordered_path.read_bytes() == reversed_path.read_bytes()

    compatible_report = generate_merge_retention_report(postgres_store, mirrors, ORG)
    assert compatible_report == report

    postgres_store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="wrong-session",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="math_utils.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="call-1",
            occurred_at=T0,
        )
    )
    mismatched = generate_merge_retention_report(postgres_store, mirrors, ORG)
    assert mismatched.explicit_accept_rows == 1
    assert mismatched.decision_attachment_skips == {"decision_session_mismatch": 1}

    postgres_store.store_inference_call(
        InferenceCall(
            inference_call_id="inference-2",
            org_id=ORG,
            session_id="session-2",
            user_id="developer-2",
            gateway_provider=GatewayProvider.PORTKEY,
            model="model-2",
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content="Unrelated")])
            ],
            output_messages=[
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="Unrelated output")]
                )
            ],
            model_call_id="call-1",
            observed_at=T0,
        )
    )

    ambiguous_report = generate_merge_retention_report(postgres_store, mirrors, ORG)

    assert ambiguous_report.explicit_accept_rows == 0
    assert ambiguous_report.decision_attachment_skips == {
        "ambiguous_decision_call_id": 2
    }


def test_direct_merge_report_declines_rows_without_observed_session_sources():
    provenance = Provenance("2", 0)
    result = MergeRetentionResult(provenance=provenance, rows=[_row("a", 1.0, 1.0, 1)])
    report = build_merge_retention_report(
        "acme",
        result,
        explicit_accepted_inference_call_ids={"a"},
        attribution_skipped=Counter(),
        decision_attachment_skipped=Counter(),
        attribution_provenance=provenance,
    )
    assert report.scored_rows == 0
    assert report.scoring_skips["session_commit_unobserved"] == 1
