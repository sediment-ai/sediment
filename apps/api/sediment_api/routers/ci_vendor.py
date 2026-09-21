# SPDX-License-Identifier: AGPL-3.0-or-later
"""
POST /ingest/ci — vendor-neutral CI ingest. The GitHub webhook door lives
at /ingest/github/ci, so the plain name belongs to the door any CI can
call.

One authenticated bearer POST any CI pipeline can ``curl`` to store a
``CIOutcome`` fact. The caller normalizes its own CI system into a
``CIProvider`` value and the known ``CIResult`` vocabulary; the server
validates at the trust boundary and stores — no per-vendor parsing, no
new fact shape.

Dedup: the store's ``uq_ci_run`` UNIQUE index on provider run id and attempt
collapses redeliveries; ``stored: false`` means success, not an error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sediment_core import (
    BranchName,
    CIOutcome,
    CIReason,
    CIProvider,
    CIResult,
    CommitSha,
    FactStore,
    ForgeProvider,
    ForgeHost,
    ProviderRepositoryId,
    NonEmptyId,
    NonEmptyContent,
    RepoSlug,
    WorkflowName,
    WorkflowPath,
)

from ..config import settings
from ..deps import get_store, verify_ingest_token

router = APIRouter(tags=["ci-vendor"])


class VendorCIRequest(BaseModel):
    """The vendor-neutral CI outcome payload — callers normalize their own
    CI system into this shape. ``extra=\"forbid\"`` so a caller naming
    an org or fabricating fields is rejected at the boundary.

    The identity fields carry the shared core definitions on
    the envelope, so malformed input 422s at the door rather than
    surfacing as a ValidationError-turned-500 inside the handler:
    ``CommitSha`` (40- and 64-char object names — a SHA-256 repo can use
    this door), ``RepoSlug`` (required — a vendor fact without a repo has
    no CI-attach join), ``BranchName`` (``refs/heads/x`` normalizes to
    ``x``, one recovery lineage bucket per pipeline), and a required
    non-empty ``run_id``. ``run_url`` is optional location metadata."""

    model_config = ConfigDict(extra="forbid")

    provider: CIProvider = Field(
        description=(
            "The normalized CI system. The sender declares it from integration "
            "configuration; Sediment does not infer it from a URL"
        )
    )
    run_id: NonEmptyId = Field(
        description=(
            "The provider-issued pipeline-run id, unique within the deployment "
            "organization and provider namespace"
        )
    )
    run_attempt: int | None = Field(default=None, ge=1, le=2**63 - 1, strict=True)
    repo: RepoSlug
    repository_provider: ForgeProvider | None = None
    repository_host: ForgeHost | None = None
    repository_id: ProviderRepositoryId | None = None
    commit_sha: CommitSha
    branch: BranchName
    # Workflow names preserve descriptive content; paths identify definitions.
    workflow_name: WorkflowName = ""
    workflow_id: NonEmptyId | None = None
    workflow_path: WorkflowPath | None = None
    result: CIResult
    run_url: NonEmptyContent | None = None
    provider_result: NonEmptyContent | None = None
    error_type: NonEmptyContent | None = None
    reason: CIReason | None = None
    source_event_type: NonEmptyContent | None = None
    source_spec_version: NonEmptyContent | None = None
    source_event_id: NonEmptyId | None = None

    @model_validator(mode="after")
    def _complete_repository_identity(self):
        present = tuple(
            value is not None
            for value in (
                self.repository_provider,
                self.repository_host,
                self.repository_id,
            )
        )
        if any(present) and not all(present):
            raise ValueError("repository identity must be wholly present or absent")
        return self

    @field_validator("repo")
    @classmethod
    def _repo_required(cls, v: str) -> str:
        # RepoSlug admits "" as the webhook parsers' absent sentinel; a
        # vendor caller controls its own pipeline and must name the repo.
        if not v:
            raise ValueError("repo is required (owner/repo)")
        return v


@router.post("/ci")
def ingest_ci_vendor(
    body: VendorCIRequest,
    _: None = Depends(verify_ingest_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    outcome = CIOutcome(
        org_id=settings.org_id,
        provider=body.provider,
        run_id=body.run_id,
        run_attempt=body.run_attempt,
        repo=body.repo,
        repository_provider=body.repository_provider,
        repository_host=body.repository_host,
        repository_id=body.repository_id,
        commit_sha=body.commit_sha,
        branch=body.branch,
        result=body.result,
        workflow_name=body.workflow_name,
        workflow_id=body.workflow_id,
        workflow_path=body.workflow_path or None,
        run_url=body.run_url,
        provider_result=body.provider_result,
        error_type=body.error_type,
        reason=body.reason,
        source_event_type=body.source_event_type,
        source_spec_version=body.source_spec_version,
        source_event_id=body.source_event_id,
    )
    receipt = store.store_ci_outcome_receipt(outcome)
    return {"fact_id": receipt.fact_id, "stored": receipt.stored}
