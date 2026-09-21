# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sediment fact models — the canonical shapes for everything persisted.

Facts are immutable records of things that happened (ADR 0001). Idempotency
is NOT modeled here: dedup is the fact store's job, enforced by UNIQUE
indexes (ADR 0003). Do not define fact shapes anywhere else.
"""

from __future__ import annotations

import uuid
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from .org import normalize_org_id


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


def _scalar_identity(value: str) -> str:
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("identity must not contain NUL or surrogate code points")
    return value


# Publish the same restriction that the Python validator enforces. Empty
# sentinels and whitespace rules belong to each identity's own normalizer.
ScalarIdentity = Annotated[
    str,
    AfterValidator(_scalar_identity),
    WithJsonSchema({"type": "string", "pattern": r"^[^\u0000\uD800-\uDFFF]*$"}),
]


def _non_empty_id(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must be a non-empty id (ADR 0002)")
    return v


# Placeholder/empty session and user ids are banned (ADR 0002). Enforced at
# the schema so every construction site fails loudly — not only the paths
# that happen to route through the HTTP envelope's own min_length check.
# Ids are also stripped here: they are join keys, and a padded and an
# unpadded spelling of one id must not fragment into two sessions that never
# join. Whitespace padding is transport noise, not identity; the wire-faithful
# original stays in `raw`.
NonEmptyId = Annotated[ScalarIdentity, AfterValidator(_non_empty_id)]


def _forge_host(value: str) -> str:
    value = value.lower()
    if len(value) > 253 or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
        value,
    ):
        raise ValueError("forge host must be an ASCII DNS hostname without a port")
    return value


def _provider_repository_id(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("repository ID must be canonical positive decimal text")
    return value


ForgeHost = Annotated[ScalarIdentity, AfterValidator(_forge_host)]
ProviderRepositoryId = Annotated[
    ScalarIdentity, AfterValidator(_provider_repository_id)
]

# Descriptive values preserve exceptional content with the same logical trim
# and nonempty rules that these fields used before ADR 0015.
NonEmptyContent = Annotated[str, AfterValidator(_non_empty_id)]


CI_REASON_MAX_LENGTH = 4096


def _ci_reason(value: str) -> str:
    # Pydantic's native string max_length first requires UTF-8 encoding.
    # Logical content length must also accept preserved surrogate code points.
    if len(value) > CI_REASON_MAX_LENGTH:
        raise ValueError(
            f"CI reason must have at most {CI_REASON_MAX_LENGTH} characters"
        )
    return value


CIReason = Annotated[
    str,
    AfterValidator(_ci_reason),
    WithJsonSchema({"type": "string", "maxLength": CI_REASON_MAX_LENGTH}),
]


def normalize_commit_sha(v: str) -> str:
    """Full-length commit sha, stripped and lowercased; raises ValueError
    otherwise. Public so the fail-soft capture parsers apply the same
    definition and skip-and-log instead of raising."""
    v = v.strip().lower()
    if len(v) not in (40, 64) or any(c not in "0123456789abcdef" for c in v):
        raise ValueError("must be a full-length commit sha (40 or 64 hex chars)")
    return v


# The rule the types below implement: a field that is a join key, a
# dedup-index component, or the tenancy key gets a validated type, never a
# bare str.

# Tenancy key. Every boundary routes through normalize_org_id (org.py), so
# two spellings of one tenant cannot partition into two invisible tenants —
# a fact stored under "  ACME " would never appear in any read, derivation,
# export, or count for "acme".
OrgId = Annotated[ScalarIdentity, AfterValidator(normalize_org_id)]


def normalize_repo_slug(v: str) -> str:
    """``owner/repo``, stripped and lowercased, or ``""``; raises
    ValueError otherwise. Public so the fail-soft webhook parsers can
    degrade to the absent sentinel instead of raising."""
    v = v.strip().lower()
    if not v:
        # "" is the documented absent sentinel (push_missing_repo /
        # workflow_run_missing_repo store-anyway trails) — facts first.
        return ""
    _scalar_identity(v)
    owner, sep, name = v.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError('must be "owner/repo"')
    return v


# repo is the CI-attach join key ((repo, commit_sha) in attachment.py), a
# uq_pushes_natural component, and a mirror directory name. Lowercased
# because GitHub treats owner/repo case-insensitively — two case spellings
# of one repo must not fragment the join.
RepoSlug = Annotated[ScalarIdentity, AfterValidator(normalize_repo_slug)]

RequiredRepoSlug = Annotated[RepoSlug, AfterValidator(_non_empty_id)]


def _branch_name(v: str) -> str:
    # GitHub sends "main"; a vendor normalizing its own CI may send
    # "refs/heads/main". Those must land in one recovery lineage bucket
    # (recovery.py keys on branch), so the fully-qualified spelling is
    # normalized to the short one. Case is preserved — branch names are
    # case-sensitive. The loop makes this idempotent: the store re-validates
    # rows on read, so a validator must be a fixed point or a stored value
    # silently differs from its read-back. A branch literally named
    # "refs/heads/main" conflates with "main" — inherent: the wire form
    # cannot carry that distinction.
    v = v.strip()
    while v.startswith("refs/heads/"):
        # Re-strip after each removal: "refs/heads/ main" must reach the
        # same fixed point as " main", or stored and read-back diverge.
        v = v.removeprefix("refs/heads/").strip()
    return v


BranchName = Annotated[ScalarIdentity, AfterValidator(_branch_name)]


def _model_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("model must be non-empty when present")
    return v


# model is the grouping key for every model-comparison surface: DPO pair
# buckets (org, model, prompt), A/B arms, significance, the per-model
# reports. Stripped and non-empty so padding cannot split an arm;
# provider-prefix spelling ("deepseek/x" vs "x") stays distinct: the gateway
# stamps whatever LiteLLM's SLO carries, and normalizing prefixes would
# fabricate identity across genuinely different routes.
ModelName = Annotated[ScalarIdentity, AfterValidator(_model_name)]


def _stripped(v: str) -> str:
    return v.strip()


# Workflow names are descriptive. A path identifies a workflow definition;
# both preserve the existing stripped-empty sentinel where accepted.
WorkflowName = Annotated[str, AfterValidator(_stripped)]
WorkflowPath = Annotated[ScalarIdentity, AfterValidator(_stripped)]


# A fact's join key is never an abbreviation: attribution, rollouts,
# recovery, reward, and RLVR export all key on commit_sha, and `abc1234`
# would never join `abc1234def…`. 40 = SHA-1, 64 = SHA-256 object names —
# both full-length forms every forge and CI system sends in practice.
# Normalized (stripped, lowercased) because dedup lives in the database:
# uq_ci_run and uq_pushes_natural key on these columns and PostgreSQL compares
# TEXT byte-exact, so two spellings of one commit must not occupy two index
# entries. notes.py keeps its wider _HEX_OBJECT read guard — validating a
# value on its way to a git command line is a different job from validating
# a fact.
CommitSha = Annotated[str, AfterValidator(normalize_commit_sha)]


class GatewayProvider(StrEnum):
    LITELLM = "litellm"
    # Envelope-valid but unregistered in capture's ADAPTERS: the ingest
    # route 400s these until an adapter lands. UNKNOWN included — it is
    # part of the ingest contract (a clean 400, not a 422 schema error).
    PORTKEY = "portkey"
    HELICONE = "helicone"
    UNKNOWN = "unknown"


class CIProvider(StrEnum):
    # Grows as integrations exercise the vendor-neutral ingest: the caller
    # declares its normalized CI system, and the bearer token authenticates
    # that caller rather than attesting the provider assertion. OTHER is the
    # catch-all for systems not yet enumerated — never per-vendor parsing
    # server-side.
    GITHUB_ACTIONS = "github_actions"
    JENKINS = "jenkins"
    GITLAB_CI = "gitlab_ci"
    CIRCLECI = "circleci"
    BUILDKITE = "buildkite"
    OTHER = "other"


class CIResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class ForgeProvider(StrEnum):
    GITHUB = "github"


class AgentHarness(StrEnum):
    CLAUDE_CODE = "claude-code"
    COPILOT = "copilot"
    CODEX = "codex"
    CURSOR = "cursor"
    PI = "pi"


class InteractionMode(StrEnum):
    AGENT = "agent"
    INLINE = "inline"


class FactTable(StrEnum):
    """The quarantinable fact tables. Sessions are upserted metadata, not
    quarantinable facts, so they are deliberately absent."""

    INFERENCE_CALLS = "inference_calls"
    DEVELOPER_DECISIONS = "developer_decisions"
    CI_OUTCOMES = "ci_outcomes"
    PUSHES = "pushes"
    PULL_REQUEST_MERGES = "pull_request_merges"
    PULL_REQUEST_REVISIONS = "pull_request_revisions"
    EDIT_OBSERVATIONS = "edit_observations"
    REJECTED_EDITS = "rejected_edits"
    RETRY_LINKAGES = "retry_linkages"
    SESSION_COMMIT_OBSERVATIONS = "session_commit_observations"
    REPOSITORY_RENAMES = "repository_renames"


class QuarantineAction(StrEnum):
    QUARANTINE = "quarantine"
    RELEASE = "release"


class TextPart(BaseModel):
    """Plain text in a structured inference message."""

    type: Literal["text"] = "text"
    content: str


class ReasoningPart(BaseModel):
    """Readable model reasoning in a structured inference message."""

    type: Literal["reasoning"] = "reasoning"
    content: str


class ToolCallPart(BaseModel):
    """A model-requested tool invocation.

    The shape follows the OpenTelemetry Generative AI message schema. The
    tool-call id is distinct from the provider's model-call id.
    """

    type: Literal["tool_call"] = "tool_call"
    id: NonEmptyId
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallResponsePart(BaseModel):
    """A result supplied for an earlier tool call."""

    type: Literal["tool_call_response"] = "tool_call_response"
    id: NonEmptyId
    result: Any


InferenceMessagePart = Annotated[
    TextPart | ReasoningPart | ToolCallPart | ToolCallResponsePart,
    Field(discriminator="type"),
]


class InferenceMessage(BaseModel):
    """One ordered message in an inference request or response."""

    role: str
    parts: list[InferenceMessagePart]
    finish_reason: str | None = None


class InferenceCall(BaseModel):
    """Normalized model request and response fact.

    The canonical schema preserves the provider's ordered message parts.
    """

    schema_version: Literal[1] = 1
    inference_call_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    session_id: NonEmptyId
    user_id: NonEmptyId | None = None
    gateway_provider: GatewayProvider
    model_provider: ScalarIdentity | None = None
    model: ModelName | None = None
    input_messages: list[InferenceMessage]
    output_messages: list[InferenceMessage]
    input_tokens: int | None = Field(default=None, ge=0, le=2**63 - 1)
    output_tokens: int | None = Field(default=None, ge=0, le=2**63 - 1)
    duration_ms: int | None = Field(default=None, ge=0, le=2**63 - 1)
    model_call_id: NonEmptyId | None = None
    observed_at: AwareDatetime = Field(default_factory=_now)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _require_schema_version_one(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


UNKNOWN_USER_ID = "unknown"
"""Legacy missing-user literal retained for compatibility with immutable Facts.

Capture translators leave ``user_id`` unset (``None``) when a client supplies
no user resource attribute. Stored ``DeveloperDecision`` rows can still carry
this literal, so Derivations treat it identically to ``None`` (ADR 0001).
Naming it here avoids hardcoding the string in consumers."""


class DeveloperDecision(BaseModel):
    """A developer's accept/reject of an AI-generated change. Session-scoped.

    ``occurred_at`` is required client-observed time, mapped from the OTLP
    record's timeUnixNano. No backfilling from server ingest time. Most decision
    dedup keys include it; native Cursor Write identity uses a deterministic
    ``decision_id`` while ``occurred_at`` preserves hook receipt time.
    """

    decision_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    session_id: NonEmptyId
    user_id: NonEmptyId | None = None
    agent_harness: AgentHarness
    file_path: ScalarIdentity  # "" when the client emits no path (e.g. rejects)
    accepted: bool
    explicit: bool  # True = real human gesture; False = auto-applied/inferred
    interaction_mode: InteractionMode
    commit_sha: CommitSha | None = None
    # Per-call id joining back to the completion — NonEmptyId when present,
    # matching EditObservation.call_id (both read the same wire attribute; a
    # padded value must not strip on one fact and not the other, or the
    # edit-retention join silently never fires).
    call_id: NonEmptyId | None = None
    # Graded preference strength, currently Copilot-only: the four-gram
    # overlap between applied edit text and a later file observation, in
    # [0, 1] — enforced at the schema (nan/inf fail the bounds too), since
    # this value feeds reward policy as preference strength. Nullable —
    # every other harness (and older Copilot events without this data)
    # leaves it None; `accepted` remains the boolean flattening.
    edit_retention_score: float | None = Field(default=None, ge=0.0, le=1.0)
    # The delay bucket (ms) the vendor rate was measured at (Copilot buckets:
    # 0/5s/30s/2m/5m). The Copilot translator only sets it alongside a
    # edit_retention_score (the model does not enforce that pairing).
    observation_delay_ms: int | None = Field(default=None, ge=0, le=2**63 - 1)
    # AwareDatetime: occurred_at is a dedup-index component and _iso would
    # reinterpret a naive value in the host's local zone — a naive and an
    # aware spelling of one instant must not fail to collapse (store.py
    # already rejects naive read bounds for the same reason).
    occurred_at: AwareDatetime
    captured_at: AwareDatetime = Field(default_factory=_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class EditObservation(BaseModel):
    """The applied text and later observed file text for one AI edit, observed at
    session end by the client-side transcript extractor (ADR 0007).
    Session-scoped.

    The pair is the fact; the edit retention score is a derivation over it (the
    scorer seam in ``sediment_derive.survival``) —
    never computed client-side, so the metric stays a re-derivable policy
    choice. ``call_id`` is the tool-call id (Claude Code ``tool_use_id``)
    joining this pair to its ``DeveloperDecision``. ``observed_file_text`` is
    the file's content at session end; ``""`` means the file was gone.
    """

    observation_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    session_id: NonEmptyId
    user_id: NonEmptyId | None = None
    agent_harness: AgentHarness
    file_path: (
        ScalarIdentity  # non-empty in practice: the translator skips path-less records
    )
    call_id: NonEmptyId
    applied_text: str  # the edit tool's new_string / Write content, as applied
    observed_file_text: str  # file content at session end ("" = file deleted)
    # Lines changed by something other than the agent between this edit and
    # the next observation of the file. "External", not "human": a
    # formatter, a linter, a file watcher, or a rebase moves lines too, and
    # calling that a human correction at capture time would freeze a
    # judgment into a fact. Whether it graded as a correction is a policy
    # question for the derivation. Both are None when no window covered the
    # call — absent, never a zero that reads as "nobody touched it".
    # Bounded 0..int64 like CIOutcome.pr_number: an unbounded count
    # from a buggy or hostile sender passes Pydantic and then raises
    # OverflowError at INSERT, turning one bad attribute into a 500 on an
    # authenticated ingest route. strict=True for the same reason it is there
    # — a float or a bool must not coerce into a line count.
    external_lines_added: int | None = Field(
        default=None, ge=0, le=2**63 - 1, strict=True
    )
    external_lines_removed: int | None = Field(
        default=None, ge=0, le=2**63 - 1, strict=True
    )
    occurred_at: AwareDatetime  # the edit's transcript timestamp (dedup key is
    # (org, agent_harness, session, call_id) — see uq_edit_observations_call)
    captured_at: AwareDatetime = Field(default_factory=_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class RejectedEdit(BaseModel):
    """The text a developer refused, for one rejected AI edit (ADR 0007).
    Session-scoped.

    A DPO pair needs the rejected sample's text, and for a non-gateway
    harness it exists nowhere else: the ``DeveloperDecision`` for a reject
    carries ``file_path=""`` and no content, and the gateway stores an
    empty completion for exactly the tool-call turns proposed edits live
    in. The client recovers the proposal from the transcript.

    Refusals only. A call the *tool* failed on is not captured: nobody
    judged that text, so it carries no preference signal. ``call_id`` (the
    tool-call id) joins this to the rejecting ``DeveloperDecision``, which
    is what makes it usable as a rejected side.

    There is deliberately no ``observed_file_text``: a refused edit never
    reached the file, so there is no file state to observe.
    """

    rejection_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    session_id: NonEmptyId
    user_id: NonEmptyId | None = None
    agent_harness: AgentHarness
    file_path: (
        ScalarIdentity  # non-empty in practice: the translator skips path-less records
    )
    call_id: NonEmptyId
    proposed: str  # the denied new_string / Write content, as the model wrote it
    occurred_at: AwareDatetime  # the call's transcript timestamp (dedup key is
    # (org, agent_harness, session, call_id) — see uq_rejected_edits_call)
    captured_at: AwareDatetime = Field(default_factory=_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class RetryLinkage(BaseModel):
    """One rejected edit call and the accepted retry that followed it.

    The SessionEnd extractor emits this session-scoped fact only when a
    developer correction separates same-tool calls on the same file. The fact
    carries identifiers and audit metadata, never the correction or attempt
    text. Training-objective eligibility remains a derivation decision.
    """

    retry_linkage_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    session_id: NonEmptyId
    user_id: NonEmptyId | None = None
    agent_harness: AgentHarness
    file_path: ScalarIdentity
    tool_name: Literal["Edit", "Write"]
    rejected_call_id: NonEmptyId
    accepted_call_id: NonEmptyId
    occurred_at: AwareDatetime
    captured_at: AwareDatetime = Field(default_factory=_now)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("file_path")
    @classmethod
    def _require_file_path(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("retry linkage file path must be non-empty")
        return value

    @model_validator(mode="after")
    def _require_distinct_calls(self) -> RetryLinkage:
        if self.rejected_call_id == self.accepted_call_id:
            raise ValueError("rejected and accepted call ids must differ")
        return self


class _RepositoryFact(BaseModel):
    """Captured repository qualification, with explicit legacy absence."""

    schema_version: Literal[1, 2] = 2
    repository_provider: ForgeProvider | None = None
    repository_host: ForgeHost | None = None
    repository_id: ProviderRepositoryId | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _require_repository_schema_version(cls, value: Any) -> int:
        if type(value) is not int or value not in (1, 2):
            raise ValueError("schema_version must be the integer 1 or 2")
        return value

    @model_validator(mode="after")
    def _require_repository_components(self):
        for prefix in ("repository", "head_repository"):
            values = tuple(
                getattr(self, f"{prefix}_{field}", None)
                for field in ("provider", "host", "id")
            )
            present = tuple(value is not None for value in values)
            if any(present) and not all(present):
                raise ValueError("repository identity must be wholly present or absent")
            if any(present) and self.schema_version == 1:
                raise ValueError(
                    "legacy schema version 1 cannot carry repository identity"
                )
            provider = getattr(self, "provider", None)
            if values[0] is not None and isinstance(provider, ForgeProvider):
                if values[0] != provider:
                    raise ValueError(
                        "repository provider must match the forge provider"
                    )
        return self


class CIOutcome(_RepositoryFact):
    """One provider-observed terminal CI pipeline run attempt.

    Repo-scoped, not session-scoped. The fact preserves provider evidence;
    derivations decide whether that evidence can label training data.
    """

    outcome_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    provider: CIProvider
    # Provider-issued pipeline-run identity. Every capture door must supply it;
    # URL is only a location and retries can reuse it. The value must be unique
    # within the deployment organization and normalized provider namespace.
    run_id: NonEmptyId
    # None means the provider didn't report an attempt. The database uses 0
    # only inside the expression index so absence remains visible on the fact.
    run_attempt: int | None = Field(default=None, ge=1, le=2**63 - 1, strict=True)
    repo: RepoSlug  # forge-relative "owner/repo"; "" = absent sentinel
    commit_sha: CommitSha
    branch: BranchName
    result: CIResult
    workflow_name: WorkflowName = ""  # workflow_run.name, e.g. "CI"
    workflow_id: NonEmptyId | None = None
    # workflow_run.path: the check's YAML inside the repo — the mirror holds
    # that file at every commit, so definition + repo state are in-perimeter.
    # Stripped; empty degrades to None at the parsers (absent, never
    # guessed) — "" would fragment the lineage key vs a genuine None.
    workflow_path: WorkflowPath | None = None
    # Provider location for the run. It is display and navigation metadata,
    # never identity or a dedup component.
    run_url: NonEmptyContent | None = None
    # Exact provider terminal value before normalization. Unknown values stay
    # here even when ``result`` becomes UNKNOWN.
    provider_result: NonEmptyContent | None = None
    # Structured provider evidence only. Capture never derives either value
    # from free-form CI logs.
    error_type: NonEmptyContent | None = None
    reason: CIReason | None = None
    # Source contract provenance. Absence stays None; translators don't
    # invent a specification version or delivery id.
    source_event_type: NonEmptyContent | None = None
    source_spec_version: NonEmptyContent | None = None
    source_event_id: NonEmptyId | None = None
    # Mirrors _extract_pr_number's guard (1..int64) so a future third
    # door cannot overflow PostgreSQL BIGINT at INSERT.
    pr_number: int | None = Field(default=None, ge=1, le=2**63 - 1, strict=True)
    captured_at: AwareDatetime = Field(default_factory=_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class Push(_RepositoryFact):
    """A forge push receipt: derivation trigger and audit fact. Git truth
    lives in the mirror, not here."""

    push_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    provider: ForgeProvider
    repo: RepoSlug  # "" = absent sentinel (push_missing_repo trail)
    clone_url: str
    # NonEmptyId: uq_pushes_natural component — the door gates the
    # refs/heads/ shape; the schema pins non-empty like every dedup key.
    ref: NonEmptyId  # e.g. "refs/heads/main"
    # Push carries no `raw`, so normalizing case here discards the original
    # spelling — theoretical, GitHub always sends lowercase.
    before_sha: CommitSha
    after_sha: CommitSha
    forced: bool = False
    captured_at: AwareDatetime = Field(default_factory=_now)


class SessionCommitObservation(_RepositoryFact):
    """The first time Sediment observes a Git-note Session-to-commit edge."""

    observation_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    repo: RepoSlug
    commit_sha: CommitSha
    session_id: NonEmptyId
    source_push_id: NonEmptyId
    captured_at: AwareDatetime = Field(default_factory=_now)

    @field_validator("repo")
    @classmethod
    def _require_repo(cls, value: str) -> str:
        if not value:
            raise ValueError("Session-to-commit observation repo must be non-empty")
        return value


class PullRequestMerge(_RepositoryFact):
    """Immutable pull request merge boundary captured from a forge."""

    merge_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    provider: ForgeProvider
    repo: RepoSlug
    pr_number: int = Field(ge=1, le=2**63 - 1, strict=True)
    head_repo: RepoSlug
    head_repository_provider: ForgeProvider | None = None
    head_repository_host: ForgeHost | None = None
    head_repository_id: ProviderRepositoryId | None = None
    head_ref: BranchName
    head_sha: CommitSha
    base_ref: BranchName
    base_sha: CommitSha
    merge_commit_sha: CommitSha
    merged_at: AwareDatetime
    source_event_id: NonEmptyId | None = None
    captured_at: AwareDatetime = Field(default_factory=_now)

    @field_validator("repo", "head_repo", "head_ref", "base_ref")
    @classmethod
    def _require_merge_identity(cls, value: str) -> str:
        if not value:
            raise ValueError("pull request merge identity must be non-empty")
        return value


class PullRequestRevision(_RepositoryFact):
    """Immutable pull request head revision observed at a forge boundary."""

    revision_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    provider: ForgeProvider
    repo: RepoSlug
    pr_number: int = Field(ge=1, le=2**63 - 1, strict=True)
    head_repo: RepoSlug
    head_repository_provider: ForgeProvider | None = None
    head_repository_host: ForgeHost | None = None
    head_repository_id: ProviderRepositoryId | None = None
    head_ref: BranchName
    head_sha: CommitSha
    base_ref: BranchName
    base_sha: CommitSha
    previous_head_sha: CommitSha | None = None
    source_event_id: NonEmptyId | None = None
    captured_at: AwareDatetime = Field(default_factory=_now)

    @field_validator("repo", "head_repo", "head_ref", "base_ref")
    @classmethod
    def _require_revision_identity(cls, value: str) -> str:
        if not value:
            raise ValueError("pull request revision identity must be non-empty")
        return value


class RepositoryRename(BaseModel):
    """A provider-observed name change for one identified repository."""

    schema_version: Literal[1] = 1
    rename_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    repository_provider: ForgeProvider
    repository_host: ForgeHost
    repository_id: ProviderRepositoryId
    old_repo: RequiredRepoSlug
    new_repo: RequiredRepoSlug
    source_event_id: NonEmptyId | None = None
    occurred_at: AwareDatetime | None = None
    captured_at: AwareDatetime = Field(default_factory=_now)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _require_schema_version_one(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def _require_distinct_names(self):
        if not self.old_repo or not self.new_repo or self.old_repo == self.new_repo:
            raise ValueError("repository rename requires distinct nonempty names")
        return self


class QuarantineRecord(BaseModel):
    """One row of the append-only quarantine log (ADR 0001): a fact about a
    fact's exclusion from derivations. A fact is quarantined iff its most
    recent record (by append order) has ``action='quarantine'``.
    ``recorded_at`` is audit metadata, not the ordering key — it is stamped
    at append time and a wall-clock step must not flip latest-wins. Facts
    are never deleted; they are quarantined.
    """

    quarantine_id: NonEmptyId = Field(default_factory=_uuid)
    org_id: OrgId
    fact_table: FactTable
    # NonEmptyId: ghost ids are documented-legal (see quarantine_fact),
    # but an empty/whitespace id is a typo that quarantines nothing.
    fact_id: NonEmptyId
    action: QuarantineAction
    reason: str  # free text, required — audit trail
    recorded_at: AwareDatetime = Field(default_factory=_now)

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        # An exclusion without a recorded reason is how audit trails die.
        if not v.strip():
            raise ValueError("reason is required")
        return v
