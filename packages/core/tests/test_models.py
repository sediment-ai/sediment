# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fact models: schema-level identity invariants (ADR 0002 — placeholder or
empty session/user ids fail at construction, at every construction site)."""

from __future__ import annotations

import types
import typing
from datetime import UTC, datetime
from enum import StrEnum

import pytest
import sediment_core
import sediment_core.models as fact_models
from pydantic import AfterValidator, AwareDatetime, BaseModel, ValidationError
from pydantic.fields import FieldInfo
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
    PullRequestMerge,
    PullRequestRevision,
    RetryLinkage,
    SessionCommitObservation,
    RepositoryRename,
    QuarantineAction,
    QuarantineRecord,
    ReasoningPart,
    ToolCallPart,
    ToolCallResponsePart,
    TextPart,
)

T0 = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def test_session_commit_observation_v1_preserves_git_note_edge() -> None:
    observation = SessionCommitObservation(
        schema_version=1,
        observation_id="observation-1",
        org_id="acme",
        repo="Acme/Service",
        commit_sha="A" * 40,
        session_id=" session-1 ",
        source_push_id="push-1",
        captured_at=T0,
    )

    assert observation.schema_version == 1
    assert observation.repo == "acme/service"
    assert observation.commit_sha == "a" * 40
    assert observation.session_id == "session-1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 3),
        ("repo", ""),
        ("commit_sha", "not-a-sha"),
        ("session_id", " "),
        ("source_push_id", " "),
        ("captured_at", datetime(2026, 7, 10, 12, 0, 0)),
    ],
)
def test_session_commit_observation_rejects_invalid_contract(
    field: str, value: object
) -> None:
    payload = {
        "org_id": "acme",
        "repo": "acme/service",
        "commit_sha": "a" * 40,
        "session_id": "session-1",
        "source_push_id": "push-1",
        "captured_at": T0,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        SessionCommitObservation(**payload)


def _inference_call(**over) -> InferenceCall:
    base = dict(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        gateway_provider=GatewayProvider.LITELLM,
        model="gpt-4o",
        input_messages=[],
        output_messages=[],
        input_tokens=0,
        output_tokens=0,
        duration_ms=0,
    )
    base.update(over)
    return InferenceCall(**base)


def _decision(**over) -> DeveloperDecision:
    base = dict(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app/math.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        occurred_at=T0,
    )
    base.update(over)
    return DeveloperDecision(**base)


@pytest.mark.parametrize("field", ["session_id", "user_id"])
@pytest.mark.parametrize("value", ["", "   "])
def test_inference_call_rejects_empty_identity(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _inference_call(**{field: value})


def test_inference_call_v1_preserves_ordered_structured_parts() -> None:
    call = InferenceCall(
        inference_call_id="inference-1",
        org_id="acme",
        session_id="sess-1",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[
            InferenceMessage(
                role="user",
                parts=[TextPart(content="Update app.py")],
            )
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    TextPart(content="I will apply the change."),
                    ToolCallPart(
                        id="tool-1",
                        name="apply_patch",
                        arguments={"patch": "*** Begin Patch"},
                    ),
                ],
            ),
            InferenceMessage(
                role="tool",
                parts=[
                    ToolCallResponsePart(
                        id="tool-1",
                        result={"changed": ["app.py"]},
                    )
                ],
            ),
        ],
        model_call_id="model-call-1",
        observed_at=T0,
    )

    assert [part.type for part in call.output_messages[0].parts] == [
        "text",
        "tool_call",
    ]
    assert call.output_messages[0].parts[1].arguments == {"patch": "*** Begin Patch"}
    assert call.output_messages[1].parts[0].result == {"changed": ["app.py"]}

    payload = call.model_dump(mode="json", exclude_none=True)
    assert payload["schema_version"] == 1
    assert payload["gateway_provider"] == "litellm"
    assert "model_provider" not in payload
    assert "model" not in payload
    assert "user_id" not in payload
    assert "input_tokens" not in payload
    assert "output_tokens" not in payload
    assert "duration_ms" not in payload
    assert (
        not {
            "provider",
            "messages",
            "completion",
            "prompt_tokens",
            "completion_tokens",
            "latency_ms",
            "call_id",
        }
        & payload.keys()
    )


def test_inference_call_v1_preserves_readable_reasoning_parts() -> None:
    message = InferenceMessage.model_validate(
        {
            "role": "assistant",
            "parts": [{"type": "reasoning", "content": "Inspect the callers."}],
        }
    )
    call = _inference_call(output_messages=[message])

    assert message.parts[0].__class__.__name__ == "ReasoningPart"
    assert message.model_dump(mode="json") == {
        "role": "assistant",
        "parts": [{"type": "reasoning", "content": "Inspect the callers."}],
        "finish_reason": None,
    }
    assert call.schema_version == 1
    assert sediment_core.ReasoningPart is fact_models.ReasoningPart


def test_inference_call_rejects_boolean_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        _inference_call(schema_version=True)


@pytest.mark.parametrize("legacy_name", ["Completion", "Message", "ToolCall"])
def test_legacy_model_call_shapes_are_removed(legacy_name: str) -> None:
    assert not hasattr(fact_models, legacy_name)


def test_inference_calls_are_the_only_model_call_fact_table() -> None:
    assert "completions" not in FactTable


@pytest.mark.parametrize("field", ["session_id", "user_id"])
@pytest.mark.parametrize("value", ["", "   "])
def test_decision_rejects_empty_identity(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _decision(**{field: value})


@pytest.mark.parametrize("value", ["sess-1\n", " sess-1 ", "\tsess-1"])
def test_padded_identity_normalizes_to_stripped(value: str) -> None:
    # Ids are join keys — a padded and an unpadded spelling of one id
    # must land as one session, at every construction site.
    assert _inference_call(session_id=value).session_id == "sess-1"
    assert _decision(session_id=value).session_id == "sess-1"
    assert _inference_call(user_id=value.replace("sess", "dev")).user_id == "dev-1"


def test_decision_empty_file_path_stays_allowed() -> None:
    # Deliberate: rejects carry no path; "" is the documented degrade.
    assert _decision(file_path="").file_path == ""


def test_retry_linkage_requires_distinct_non_empty_call_ids() -> None:
    base = dict(
        org_id="acme",
        session_id="sess-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app.py",
        tool_name="Edit",
        rejected_call_id="toolu-rejected",
        accepted_call_id="toolu-accepted",
        occurred_at=T0,
    )
    linkage = RetryLinkage(**base)
    assert linkage.rejected_call_id == "toolu-rejected"
    assert linkage.accepted_call_id == "toolu-accepted"
    with pytest.raises(ValidationError):
        RetryLinkage(**{**base, "tool_name": "Bash"})
    with pytest.raises(ValidationError, match="file path must be non-empty"):
        RetryLinkage(**{**base, "file_path": "   "})
    for field in ("rejected_call_id", "accepted_call_id"):
        with pytest.raises(ValidationError):
            RetryLinkage(**{**base, field: "   "})
    with pytest.raises(ValidationError, match="must differ"):
        RetryLinkage(**{**base, "accepted_call_id": "toolu-rejected"})


def test_decision_edit_retention_fields_default_to_none() -> None:
    # Nullable graded fields — every non-Copilot construction site (and
    # every existing fixture/test) is unaffected without passing them.
    d = _decision()
    assert d.edit_retention_score is None
    assert d.observation_delay_ms is None


def test_decision_edit_retention_fields_accept_real_values() -> None:
    # Copilot's edit.survival translator populates these from
    # survival_rate_four_gram / time_delay_ms.
    d = _decision(edit_retention_score=0.73, observation_delay_ms=30_000)
    assert d.edit_retention_score == 0.73
    assert d.observation_delay_ms == 30_000


def test_decision_edit_retention_score_zero_is_not_none() -> None:
    # 0.0 is a present, meaningful value distinct from no retention data.
    d = _decision(edit_retention_score=0.0, observation_delay_ms=0)
    assert d.edit_retention_score == 0.0
    assert d.observation_delay_ms == 0


# ── CommitSha: the join key gets one definition, at the schema ───────────


def _ci_outcome(**over) -> CIOutcome:
    base = dict(
        org_id="acme",
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run-1",
        repo="acme/app",
        commit_sha="a1" * 20,
        branch="main",
        result=CIResult.PASSED,
    )
    base.update(over)
    return CIOutcome(**base)


def test_ci_result_preserves_verdict_and_non_verdict_states() -> None:
    assert {result.value for result in CIResult} == {
        "passed",
        "failed",
        "error",
        "timed_out",
        "cancelled",
        "skipped",
        "neutral",
        "unknown",
    }


def test_ci_outcome_keeps_legacy_version_and_defaults_to_identity_contract() -> None:
    assert _ci_outcome().schema_version == 2
    assert _ci_outcome(schema_version=1).model_dump(mode="json")["schema_version"] == 1
    for value in (3, True, "1"):
        with pytest.raises(ValidationError, match="schema_version"):
            _ci_outcome(schema_version=value)


def test_ci_outcome_requires_provider_run_identity() -> None:
    with pytest.raises(ValidationError):
        _ci_outcome(run_id=None)
    with pytest.raises(ValidationError):
        _ci_outcome(run_id="   ")


def test_ci_outcome_attempt_is_positive_when_present() -> None:
    assert _ci_outcome(run_attempt=None).run_attempt is None
    assert _ci_outcome(run_attempt=2).run_attempt == 2
    for value in (0, -1, True, 2**63):
        with pytest.raises(ValidationError):
            _ci_outcome(run_attempt=value)


def test_ci_outcome_preserves_provider_and_source_provenance() -> None:
    outcome = _ci_outcome(
        run_attempt=3,
        workflow_id="workflow-9",
        provider_result="startup_failure",
        error_type="runner_lost",
        reason="runner stopped responding",
        source_event_type="dev.cdevents.pipelinerun.finished.0.2.0",
        source_spec_version="0.5.0",
        source_event_id="delivery-4",
        run_url=None,
    )

    assert outcome.run_id == "run-1"
    assert outcome.run_attempt == 3
    assert outcome.workflow_id == "workflow-9"
    assert outcome.provider_result == "startup_failure"
    assert outcome.error_type == "runner_lost"
    assert outcome.reason == "runner stopped responding"
    assert outcome.source_event_type == "dev.cdevents.pipelinerun.finished.0.2.0"
    assert outcome.source_spec_version == "0.5.0"
    assert outcome.source_event_id == "delivery-4"
    assert outcome.run_url is None


def _push(**over) -> Push:
    base = dict(
        org_id="acme",
        provider=ForgeProvider.GITHUB,
        repo="acme/app",
        clone_url="https://github.com/acme/app.git",
        ref="refs/heads/main",
        before_sha="a1" * 20,
        after_sha="b2" * 20,
    )
    base.update(over)
    return Push(**base)


def test_pull_request_merge_preserves_validated_merge_boundary() -> None:
    assert hasattr(fact_models, "PullRequestMerge")
    merge = fact_models.PullRequestMerge(
        merge_id="merge-1",
        org_id="ACME",
        provider=ForgeProvider.GITHUB,
        repo="Acme/App",
        pr_number=17,
        head_repo="Acme/App",
        head_ref="refs/heads/feature/query",
        head_sha="A1" * 20,
        base_ref="refs/heads/main",
        base_sha="B2" * 20,
        merge_commit_sha="C3" * 20,
        merged_at=T0,
        source_event_id="delivery-1",
    )

    assert merge.org_id == "acme"
    assert merge.repo == "acme/app"
    assert merge.head_repo == "acme/app"
    assert merge.head_ref == "feature/query"
    assert merge.base_ref == "main"
    assert merge.head_sha == "a1" * 20
    assert merge.base_sha == "b2" * 20
    assert merge.merge_commit_sha == "c3" * 20
    assert merge.pr_number == 17
    assert merge.source_event_id == "delivery-1"


def test_pull_request_revision_preserves_validated_observed_boundary() -> None:
    revision = PullRequestRevision(
        revision_id="revision-1",
        org_id="ACME",
        provider=ForgeProvider.GITHUB,
        repo="Acme/App",
        pr_number=17,
        head_repo="Acme/App",
        head_ref="refs/heads/feature/query",
        head_sha="A1" * 20,
        base_ref="refs/heads/main",
        base_sha="B2" * 20,
        previous_head_sha="C3" * 20,
        source_event_id="delivery-1",
        captured_at=T0,
    )

    assert revision.org_id == "acme"
    assert revision.repo == "acme/app"
    assert revision.head_repo == "acme/app"
    assert revision.head_ref == "feature/query"
    assert revision.base_ref == "main"
    assert revision.head_sha == "a1" * 20
    assert revision.base_sha == "b2" * 20
    assert revision.previous_head_sha == "c3" * 20
    assert revision.pr_number == 17
    assert revision.source_event_id == "delivery-1"


@pytest.mark.parametrize("field", ["repo", "head_repo", "head_ref", "base_ref"])
def test_pull_request_revision_rejects_absent_required_identity(field: str) -> None:
    values = {
        "org_id": "acme",
        "provider": ForgeProvider.GITHUB,
        "repo": "acme/app",
        "pr_number": 17,
        "head_repo": "acme/app",
        "head_ref": "feature/query",
        "head_sha": "a1" * 20,
        "base_ref": "main",
        "base_sha": "b2" * 20,
        "previous_head_sha": None,
        "captured_at": T0,
    }
    values[field] = ""

    with pytest.raises(ValidationError):
        PullRequestRevision(**values)


@pytest.mark.parametrize("field", ["repo", "head_repo", "head_ref", "base_ref"])
def test_pull_request_merge_rejects_absent_required_identity(field: str) -> None:
    values = {
        "org_id": "acme",
        "provider": ForgeProvider.GITHUB,
        "repo": "acme/app",
        "pr_number": 17,
        "head_repo": "acme/app",
        "head_ref": "feature/query",
        "head_sha": "a1" * 20,
        "base_ref": "main",
        "base_sha": "b2" * 20,
        "merge_commit_sha": "c3" * 20,
        "merged_at": T0,
    }
    values[field] = ""

    with pytest.raises(ValidationError):
        PullRequestMerge(**values)


_BAD_SHAS = [
    "",
    "   ",
    "not-a-sha",
    "abc1234",  # abbreviated: a join key is never an abbreviation
    "a" * 39,
    "a" * 41,
    "a" * 63,
    "a" * 65,
    "g" * 40,  # non-hex
    "a" * 39 + "G",
]


@pytest.mark.parametrize("value", _BAD_SHAS)
def test_ci_outcome_rejects_malformed_sha(value: str) -> None:
    with pytest.raises(ValidationError):
        _ci_outcome(commit_sha=value)


@pytest.mark.parametrize("field", ["before_sha", "after_sha"])
@pytest.mark.parametrize("value", _BAD_SHAS)
def test_push_rejects_malformed_sha(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _push(**{field: value})


@pytest.mark.parametrize("value", _BAD_SHAS)
def test_decision_rejects_malformed_sha_when_present(value: str) -> None:
    with pytest.raises(ValidationError):
        _decision(commit_sha=value)


def test_decision_commit_sha_stays_nullable() -> None:
    # None means "no join key" and must keep working (ADR 0003 posture).
    assert _decision().commit_sha is None
    assert _decision(commit_sha=None).commit_sha is None


def test_commit_sha_accepts_both_full_lengths() -> None:
    # 40 = SHA-1, 64 = SHA-256 object names: a SHA-256 repo is not locked
    # out of any door.
    assert _ci_outcome(commit_sha="a" * 40).commit_sha == "a" * 40
    assert _ci_outcome(commit_sha="f" * 64).commit_sha == "f" * 64


def test_commit_sha_normalizes_case_and_whitespace() -> None:
    # Two spellings of one commit must not occupy two dedup-index entries:
    # PostgreSQL compares TEXT byte-exact under uq_ci_run / uq_pushes_natural.
    assert _ci_outcome(commit_sha="  " + "AB12" * 10 + " ").commit_sha == "ab12" * 10
    p = _push(before_sha="CD34" * 10, after_sha="\tEF56" * 1 + "ef56" * 9 + "\n")
    assert p.before_sha == "cd34" * 10
    assert p.after_sha == "ef56" * 10


# ── identity-bearing fields get validated types, not bare str ────────────


def test_org_id_normalizes_case() -> None:
    # Two spellings of one tenant must not partition into two invisible
    # tenants — normalize_org_id was always the requirement (org.py).
    assert _inference_call(org_id="ACME").org_id == "acme"
    assert _push(org_id="ACME").org_id == "acme"


@pytest.mark.parametrize("value", ["  ACME ", " acme", "", "☃org"])
def test_org_id_rejects_padded_empty_or_non_ascii(value: str) -> None:
    # Padded input is rejected, not stripped — pinned in test_org.py.
    for factory in (_inference_call, _decision, _ci_outcome, _push):
        with pytest.raises(ValidationError):
            factory(org_id=value)


def test_model_call_id_is_non_empty_when_present() -> None:
    with pytest.raises(ValidationError):
        _inference_call(model_call_id="   ")
    assert _inference_call(model_call_id=None).model_call_id is None
    assert _inference_call(model_call_id=" x ").model_call_id == "x"


def test_decision_call_id_is_non_empty_when_present() -> None:
    with pytest.raises(ValidationError):
        _decision(call_id="   ")
    assert _decision(call_id=None).call_id is None
    assert _decision(call_id=" x ").call_id == "x"


def test_run_url_rejects_empty_accepts_none() -> None:
    # "" is not NULL: it would participate in the partial uq_ci_run index
    # and every retried delivery would insert a duplicate.
    with pytest.raises(ValidationError):
        _ci_outcome(run_url="")
    with pytest.raises(ValidationError):
        _ci_outcome(run_url="   ")
    assert _ci_outcome(run_url=None).run_url is None


def test_repo_slug_normalizes_and_keeps_absent_sentinel() -> None:
    # GitHub treats owner/repo case-insensitively; the join must too.
    assert _ci_outcome(repo="Acme/App").repo == "acme/app"
    assert _push(repo="Acme/App").repo == "acme/app"
    # "" is the documented absent sentinel (push_missing_repo trail).
    assert _ci_outcome(repo="").repo == ""


@pytest.mark.parametrize("value", ["noslash", "a/b/c", "/repo", "owner/", " / "])
def test_repo_slug_rejects_malformed(value: str) -> None:
    for factory in (_ci_outcome, _push):
        with pytest.raises(ValidationError):
            factory(repo=value)


def test_branch_normalizes_fully_qualified_form() -> None:
    # GitHub sends "main"; a vendor may send "refs/heads/main" — both must
    # land in one recovery lineage bucket. Case is preserved: branch names
    # are case-sensitive.
    assert _ci_outcome(branch="refs/heads/main").branch == "main"
    assert _ci_outcome(branch="main").branch == "main"
    assert _ci_outcome(branch="refs/heads/Feature-X").branch == "Feature-X"


def test_naive_datetimes_rejected() -> None:
    # occurred_at is a dedup-index component; _iso would reinterpret a
    # naive value in the host's local zone, so a naive and an aware
    # spelling of one instant would fail to collapse.
    naive = datetime(2026, 1, 1, 12, 0)
    with pytest.raises(ValidationError):
        _decision(occurred_at=naive)
    with pytest.raises(ValidationError):
        _inference_call(observed_at=naive)
    with pytest.raises(ValidationError):
        _push(captured_at=naive)


def test_negative_numerics_rejected() -> None:
    # Same rationale as survival_rate's bounds: these feed operator
    # reports (decision_latency min/max/mean).
    for field in ("input_tokens", "output_tokens", "duration_ms"):
        with pytest.raises(ValidationError):
            _inference_call(**{field: -5})


def test_branch_name_validator_is_idempotent() -> None:
    # The store re-validates rows on read: every normalizing
    # validator must be a fixed point, or a stored value silently differs
    # from its own read-back. Whitespace between prefixes included — the
    # re-strip inside the loop is what makes these true fixed points.
    for hostile in (
        "refs/heads/refs/heads/main",
        "refs/heads/ main",
        "refs/heads/\trefs/heads/x",
    ):
        once = _ci_outcome(branch=hostile).branch
        assert _ci_outcome(branch=once).branch == once, hostile
    assert _ci_outcome(branch="refs/heads/refs/heads/main").branch == "main"


# ── the last bare load-bearing fields ────────────────────────────────────


def test_model_name_strips_and_rejects_empty() -> None:
    # model is the grouping key for every model-comparison surface: padding
    # must not split an A/B arm; empty is never a model (adapters default
    # to "unknown").
    assert _inference_call(model=" gpt-4o ").model == "gpt-4o"
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            _inference_call(model=bad)


def test_workflow_lineage_fields_strip() -> None:
    # Two thirds of the recovery lineage key: whitespace must not split one
    # red→green stream into two lineages. Case is preserved (display names).
    o = _ci_outcome(workflow_name=" CI ", workflow_path=" .github/workflows/ci.yml ")
    assert o.workflow_name == "CI"
    assert o.workflow_path == ".github/workflows/ci.yml"
    assert _ci_outcome(workflow_name="").workflow_name == ""  # shared bucket


def test_push_ref_rejects_empty() -> None:
    # uq_pushes_natural component — the last dedup-index component that was
    # a bare str.
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            _push(ref=bad)
    assert _push(ref=" refs/heads/main ").ref == "refs/heads/main"


def test_pr_number_mirrors_the_parser_bounds() -> None:
    # github.py's _extract_pr_number guards 1..int64; the schema now holds
    # the same bound so a future third door cannot overflow at INSERT.
    assert _ci_outcome(pr_number=1).pr_number == 1
    for bad in (0, -1, 2**63):
        with pytest.raises(ValidationError):
            _ci_outcome(pr_number=bad)
    assert _ci_outcome(pr_number=None).pr_number is None


def test_quarantine_fact_id_rejects_blank() -> None:
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            QuarantineRecord(
                org_id="acme",
                fact_table=FactTable.PUSHES,
                fact_id=bad,
                action=QuarantineAction.QUARANTINE,
                reason="typo",
            )


def test_primary_key_ids_reject_blank_when_supplied() -> None:
    # uuid defaults pass trivially; a caller supplying an explicit blank id
    # fails loudly instead of storing an unjoinable row.
    with pytest.raises(ValidationError):
        _inference_call(inference_call_id="   ")
    with pytest.raises(ValidationError):
        _push(push_id="")


def test_pr_number_rejects_bools() -> None:
    # Pydantic lax mode coerces True → 1, but the parser this bound mirrors
    # rejects bools precisely so `true` cannot become PR 1 — strict mode
    # keeps schema and parser telling the same story.
    for bad in (True, False):
        with pytest.raises(ValidationError):
            _ci_outcome(pr_number=bad)


# ── rule-5 self-enforcement: every field passes the validated-type gate ──


def _is_validated_annotation(annotation: typing.Any, field_info: FieldInfo) -> bool:
    """Return True when *annotation* is a recognized validated type."""
    origin = typing.get_origin(annotation)

    # Annotated types with AfterValidator (NonEmptyId, OrgId, ModelName, …)
    if origin is typing.Annotated:
        _, *metadata = typing.get_args(annotation)
        for m in metadata:
            if isinstance(m, AfterValidator):
                return True
        return False

    # Optional[X | None] — recurse into the non-None arm.
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _is_validated_annotation(non_none[0], field_info)
        return False

    if origin is typing.Literal:
        return True

    # StrEnum subclasses
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return True

    # bool
    if annotation is bool:
        return True

    # Bounded numeric — the Field(ge=…) / Field(le=…) constraint is
    # represented as Ge / Le objects in field_info.metadata.
    if annotation in (int, float):
        for m in field_info.metadata:
            if hasattr(m, "ge") and m.ge is not None:
                return True
            if hasattr(m, "le") and m.le is not None:
                return True
        return False

    # list[BaseModel subclass] — list[InferenceMessage]
    if origin is list:
        args = typing.get_args(annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return True
        if args and typing.get_origin(args[0]) is typing.Annotated:
            part_union = typing.get_args(args[0])[0]
            part_args = typing.get_args(part_union)
            if part_args and all(
                isinstance(part, type) and issubclass(part, BaseModel)
                for part in part_args
            ):
                return True
        return False

    # dict (including dict[str, Any])
    if origin is dict or annotation is dict:
        return True

    # AwareDatetime
    if annotation is AwareDatetime:
        return True

    return False


WAIVED: dict[tuple[str, str], str] = {
    # InferenceCall
    # Supporting models
    ("InferenceMessage", "role"): "message role, not a join or dedup key",
    ("InferenceMessage", "finish_reason"): "provider output, not a join key",
    ("ReasoningPart", "content"): "content, byte-compared only within one session",
    ("TextPart", "content"): "content, byte-compared only within one session",
    ("ToolCallPart", "name"): "tool name, not a join or dedup key",
    ("ToolCallResponsePart", "result"): "provider-supplied structured content",
    # EditObservation
    ("EditObservation", "applied_text"): (
        "content — edit tool's new_string as applied"
    ),
    ("EditObservation", "observed_file_text"): (
        'content — file content at session end; "" = file deleted'
    ),
    # Push
    ("Push", "clone_url"): "→ MirrorPolicy; not a join/dedup key",
    # QuarantineRecord
    ("QuarantineRecord", "reason"): (
        "free text, required — has field_validator; audit trail"
    ),
}


FACT_MODELS: tuple[type[BaseModel], ...] = (
    InferenceCall,
    DeveloperDecision,
    EditObservation,
    RetryLinkage,
    CIOutcome,
    Push,
    PullRequestMerge,
    PullRequestRevision,
    QuarantineRecord,
    RepositoryRename,
)

SUPPORTING_MODELS: tuple[type[BaseModel], ...] = (
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)

ALL_MODELS = FACT_MODELS + SUPPORTING_MODELS


def _check_model(model_cls: type[BaseModel]) -> None:
    """Assert every field on *model_cls* passes the validated-type rule."""
    model_name = model_cls.__name__
    hints = typing.get_type_hints(model_cls, include_extras=True)

    for field_name, annotation in hints.items():
        field_info = model_cls.model_fields[field_name]
        waiver_key = (model_name, field_name)

        if _is_validated_annotation(annotation, field_info):
            continue

        waiver_rationale = WAIVED.get(waiver_key)
        if waiver_rationale is not None:
            continue

        msg = (
            f"{waiver_key[0]}.{waiver_key[1]}: bare type "
            f"({annotation!r}) — AGENTS.md rule 5: a field that is a join "
            f"key, a dedup-index component, or the tenancy key must use a "
            f"validated type, never a bare str or naive datetime.\n"
            f"Escape hatches:\n"
            f"  1. Use a validated type (NonEmptyId, OrgId, ModelName, "
            f"CommitSha, RepoSlug, BranchName, WorkflowName, AwareDatetime, "
            f"a StrEnum subclass, bool, a Field-bounded numeric, "
            f"list[BaseModel], dict, or X|None of these).\n"
            f'  2. Add a WAIVED entry: WAIVED[{waiver_key}] = "<rationale>" '
            f"— the waiver becomes a reviewable diff line."
        )
        raise AssertionError(msg)


def test_all_fact_models_pass_validated_type_rule() -> None:
    for model_cls in FACT_MODELS:
        _check_model(model_cls)


def test_supporting_models_pass_validated_type_rule() -> None:
    for model_cls in SUPPORTING_MODELS:
        _check_model(model_cls)


def test_every_waiver_entry_matches_a_real_model_and_field() -> None:
    """Stale waiver entries are dead code — fail loudly."""
    for (model_name, field_name), rationale in WAIVED.items():
        match = [m for m in ALL_MODELS if m.__name__ == model_name]
        assert match, (
            f"WAIVED[{model_name!r}, {field_name!r}]: {rationale} — "
            f"no model named {model_name!r} exists"
        )
        model_cls = match[0]
        assert field_name in model_cls.model_fields, (
            f"WAIVED[{model_name!r}, {field_name!r}]: {rationale} — "
            f"{model_name!r} has no field named {field_name!r}"
        )
        # The field must actually be bare — if it now carries a validated
        # type, the waiver is stale and should be removed.
        hints = typing.get_type_hints(model_cls, include_extras=True)
        annotation = hints[field_name]
        field_info = model_cls.model_fields[field_name]
        assert not _is_validated_annotation(annotation, field_info), (
            f"WAIVED[{model_name!r}, {field_name!r}]: {rationale} — "
            f"this field is now validated; remove the stale waiver entry"
        )


def test_edit_observation_external_counts_reject_negative() -> None:
    # A negative count is a quietly wrong number in every report built on it
    # (same rationale as InferenceCall's token counts).
    base = dict(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        agent_harness="claude-code",
        file_path="app.py",
        call_id="toolu-1",
        applied_text="a",
        observed_file_text="b",
        occurred_at=datetime(2026, 7, 24, tzinfo=UTC),
    )
    assert EditObservation(**base, external_lines_added=0).external_lines_added == 0
    with pytest.raises(ValidationError):
        EditObservation(**base, external_lines_added=-1)
    with pytest.raises(ValidationError):
        EditObservation(**base, external_lines_removed=-1)
    # Bounded at int64: an unbounded count passes Pydantic and then
    # raises OverflowError at INSERT — a 500 on an authenticated route.
    assert EditObservation(**base, external_lines_added=2**63 - 1)
    with pytest.raises(ValidationError):
        EditObservation(**base, external_lines_added=2**63)
    with pytest.raises(ValidationError):
        EditObservation(**base, external_lines_removed=10**30)
    # strict: a float or a bool is not a line count.
    with pytest.raises(ValidationError):
        EditObservation(**base, external_lines_added=1.5)
    with pytest.raises(ValidationError):
        EditObservation(**base, external_lines_added=True)
