# SPDX-License-Identifier: AGPL-3.0-or-later
"""
GitHub forge webhook parsing: push payloads into Push facts, workflow_run
payloads into CIOutcome facts, plus X-Hub-Signature-256 verification.

Pure payload→fact translation — no I/O, no storage, no attribution at
ingest (ADR 0001). ``org_id`` arrives as a parameter: binding the tenant to
the webhook credential is the ingest routes' job. Pull request payloads capture
only the
immutable merge boundary; correlation remains a Derivation.
https://docs.github.com/en/webhooks/webhook-events-and-payloads
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter, ValidationError

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    ForgeProvider,
    ForgeHost,
    ProviderRepositoryId,
    RepositoryRename,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    normalize_commit_sha,
    normalize_repo_slug,
)

logger = logging.getLogger("sediment.capture.github")

# GitHub's terminal vocabulary maps without collapsing non-verdict states.
# Any value outside the documented mappings remains UNKNOWN and survives
# verbatim on provider_result.
_RESULT_MAP = {
    "success": CIResult.PASSED,
    "failure": CIResult.FAILED,
    "cancelled": CIResult.CANCELLED,
    "timed_out": CIResult.TIMED_OUT,
    "skipped": CIResult.SKIPPED,
    "neutral": CIResult.NEUTRAL,
}

# PostgreSQL BIGINT is a signed 64-bit integer. Bound it here so a crafted
# payload degrades instead of failing at INSERT.
_INT64_MAX = 2**63 - 1


class PullRequestRevisionSkipReason(StrEnum):
    """Closed reasons for declining a pull request revision payload."""

    UNSUPPORTED_ACTION = "unsupported_pull_request_action"
    INVALID_BOUNDARY = "invalid_pull_request_revision_boundary"
    SYNCHRONIZE_HEAD_MISMATCH = "synchronize_head_mismatch"
    HEAD_REPOSITORY_DELETED = "head_repository_deleted"


class RepositoryCaptureSkipReason(StrEnum):
    IDENTITY_ABSENT = "repository_identity_absent"
    IDENTITY_INVALID = "repository_identity_invalid"
    MALFORMED_DISCRIMINATOR = "malformed_discriminator"
    UNSUPPORTED_DISCRIMINATOR = "unsupported_discriminator"
    INVALID_RENAME_BOUNDARY = "invalid_repository_rename_boundary"
    NAME_UNCHANGED = "repository_name_unchanged"


_REPOSITORY_ID = TypeAdapter(ProviderRepositoryId)
_FORGE_HOST = TypeAdapter(ForgeHost)


def _repository_identity_fields(repository, *, org_id, github_host, role="repo"):
    """Capture one role; names and URLs never supply absent provider identity."""
    value = repository.get("id")
    reason = RepositoryCaptureSkipReason.IDENTITY_ABSENT
    if value is not None:
        try:
            # Repository IDs have a different contract from workflow/run IDs.
            if type(value) is int:
                value = str(value)
            identity = _REPOSITORY_ID.validate_python(value)
            host = _FORGE_HOST.validate_python(github_host)
        except (ValueError, ValidationError):
            reason = RepositoryCaptureSkipReason.IDENTITY_INVALID
        else:
            prefix = "head_repository" if role == "head_repo" else "repository"
            return {
                f"{prefix}_provider": ForgeProvider.GITHUB,
                f"{prefix}_host": host,
                f"{prefix}_id": identity,
            }, None
    logger.warning(
        "repository_identity_declined",
        extra={
            "org_id": org_id,
            "source": "github",
            "role": role,
            "reason": reason.value,
            "count": 1,
        },
    )
    return {}, reason


def sign_payload(payload_bytes: bytes | bytearray, secret: str) -> str:
    """Compute the ``X-Hub-Signature-256`` header value for a webhook body.

    The single source of truth for the signing scheme; ``verify_signature``
    and any caller that needs to *produce* a signature (e.g. the e2e driver)
    share it so the two can never drift.

    Accepts ``bytearray`` as well as ``bytes``: the API's capped body reader
    returns one to avoid a copy at the size ceiling, and ``hmac`` hashes any
    bytes-like to the same digest.
    """
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    payload_bytes: bytes | bytearray, signature_header: str, secret: str
) -> bool:
    """Validate an X-Hub-Signature-256 header.

    Compares as bytes: ``hmac.compare_digest`` raises ``TypeError`` on
    non-ASCII ``str`` arguments, so a malformed (e.g. non-ASCII)
    attacker-controlled header would otherwise crash the caller with a 500
    instead of being rejected as an invalid signature (401). The comparison
    stays constant-time.
    """
    expected = sign_payload(payload_bytes, secret)
    return hmac.compare_digest(expected.encode(), (signature_header or "").encode())


def _coerce_str(value: Any) -> str:
    """Best-effort string for a webhook field. Real forges send strings; this
    keeps a crafted, signature-valid non-string (e.g. ``"after": 123``) from
    raising a pydantic ValidationError at fact construction — a malformed
    payload must skip or degrade, never raise."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _str_or_none(value: Any) -> str | None:
    """None-default sibling of ``_coerce_str`` for ``str | None`` fields:
    a non-string drops to None (absent), never to a fabricated string."""
    return value if isinstance(value, str) else None


def _path_or_none(value: Any) -> str | None:
    """Stripped non-empty string or None — workflow_path's absent form is
    None, never ''."""
    value = _str_or_none(value)
    if value is None:
        return None
    return value.strip() or None


def _github_id_or_none(value: Any) -> str | None:
    """A positive GitHub integer id, or a non-empty string spelling."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= _INT64_MAX else None


def _head_repository_deleted(head: dict[str, Any]) -> bool:
    """GitHub sets ``head.repo: null`` once a pull request's source fork no
    longer exists. The head ref, SHA, and label can still be present. No head
    repository name survives to satisfy the boundary's required
    ``head_repo``. Distinguishing this case from other malformed-payload
    causes keeps the evidence loss countable instead of folded into an
    unrelated reason."""
    return head.get("repo") is None


def _repo_slug_or_absent(value: Any) -> str:
    """``owner/repo`` lowercased, or ``""`` — the absent sentinel — when a
    crafted payload's full_name has no usable shape. The missing-repo
    warnings downstream are the trail; a junk repo must degrade, never raise
    at fact construction."""
    try:
        return normalize_repo_slug(_coerce_str(value))
    except ValueError:
        return ""


def parse_push(
    payload: dict[str, Any], *, org_id: str, github_host: str = "github.com"
) -> Push | None:
    """Parse a GitHub push webhook payload into a Push fact.

    Returns None when there is nothing to correlate: a non-branch ref (a tag
    push re-points at commits already seen on a branch), a branch deletion,
    or a falsy ``after``.
    """
    ref = _coerce_str(payload.get("ref"))
    if not ref.startswith("refs/heads/"):
        return None
    if not payload.get("after"):  # None / "" / 0 / False → nothing to correlate
        return None
    after = _coerce_str(payload.get("after"))
    # Branch deletion: the documented `deleted` flag (exact bool — a crafted
    # string like "false" must not skip a real push) plus the all-zeros sha
    # backstop, hash-size agnostic (40 for SHA-1, 64 for SHA-256).
    if payload.get("deleted") is True or after.strip("0") == "":
        return None

    # A malformed sha must skip-and-warn, never raise: both shas are
    # uq_pushes_natural components and Push validates them, so a crafted
    # signature-valid payload would otherwise 500 at construction.
    # You cannot 422 a webhook into correctness.
    try:
        before = normalize_commit_sha(_coerce_str(payload.get("before")))
        after = normalize_commit_sha(after)
    except ValueError:
        logger.warning("push_invalid_sha", extra={"org_id": org_id, "ref": ref})
        return None

    repository = payload.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    repo = _repo_slug_or_absent(repository.get("full_name"))
    if not repo:
        # Stored anyway (facts first; quarantine covers bad facts) but the
        # mirror cannot fetch a repo-less push — leave a trail.
        logger.warning("push_missing_repo", extra={"org_id": org_id, "ref": ref})
    try:
        return Push(
            org_id=org_id,
            **_repository_identity_fields(
                repository, org_id=org_id, github_host=github_host
            )[0],
            provider=ForgeProvider.GITHUB,
            repo=repo,
            clone_url=_coerce_str(repository.get("clone_url")),
            ref=ref,
            before_sha=before,
            after_sha=after,
            forced=payload.get("forced") is True,
        )
    except ValidationError:
        logger.warning(
            "push_invalid_fact",
            extra={
                "org_id": org_id,
                "source": "github",
                "record_position": 0,
                "reason": "invalid_fact",
            },
        )
        return None


def parse_pull_request_merge(
    payload: dict[str, Any],
    *,
    org_id: str,
    source_event_id: str | None = None,
    github_host: str = "github.com",
) -> PullRequestMerge | None:
    """Parse a merged GitHub pull request into its immutable merge boundary."""
    action = payload.get("action")
    if not isinstance(action, str) or action != "closed":
        logger.info(
            "github_action_declined",
            extra={
                "org_id": org_id,
                "source": "github",
                "record_position": 0,
                "reason": "unsupported_discriminator"
                if isinstance(action, str)
                else "malformed_discriminator",
            },
        )
        return None
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict) or pull_request.get("merged") is not True:
        return None

    repository = payload.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    head = pull_request.get("head")
    head = head if isinstance(head, dict) else {}
    head_repository = head.get("repo")
    head_repository = head_repository if isinstance(head_repository, dict) else {}
    base = pull_request.get("base")
    base = base if isinstance(base, dict) else {}

    repo = _repo_slug_or_absent(_str_or_none(repository.get("full_name")))
    head_repo = _repo_slug_or_absent(_str_or_none(head_repository.get("full_name")))
    pr_number = _positive_int_or_none(pull_request.get("number"))
    head_ref = _path_or_none(head.get("ref"))
    base_ref = _path_or_none(base.get("ref"))
    if _head_repository_deleted(head):
        logger.warning(
            "pull_request_merge_head_repository_deleted",
            extra={"org_id": org_id, "repo": repo, "pr_number": pr_number},
        )
        return None
    try:
        head_sha_value = _str_or_none(head.get("sha"))
        base_sha_value = _str_or_none(base.get("sha"))
        merge_commit_sha_value = _str_or_none(pull_request.get("merge_commit_sha"))
        if (
            head_sha_value is None
            or base_sha_value is None
            or merge_commit_sha_value is None
        ):
            raise ValueError("non-string commit sha")
        head_sha = normalize_commit_sha(head_sha_value)
        base_sha = normalize_commit_sha(base_sha_value)
        merge_commit_sha = normalize_commit_sha(merge_commit_sha_value)
        merged_at_value = _path_or_none(pull_request.get("merged_at"))
        if merged_at_value is None:
            raise ValueError("missing merged_at")
        merged_at = datetime.fromisoformat(merged_at_value.replace("Z", "+00:00"))
        if merged_at.tzinfo is None or merged_at.utcoffset() is None:
            raise ValueError("naive merged_at")
        if (
            not repo
            or not head_repo
            or pr_number is None
            or not head_ref
            or not base_ref
        ):
            raise ValueError("incomplete merge boundary")
        return PullRequestMerge(
            org_id=org_id,
            **_repository_identity_fields(
                repository, org_id=org_id, github_host=github_host
            )[0],
            provider=ForgeProvider.GITHUB,
            repo=repo,
            pr_number=pr_number,
            head_repo=head_repo,
            **_repository_identity_fields(
                head_repository,
                org_id=org_id,
                github_host=github_host,
                role="head_repo",
            )[0],
            head_ref=head_ref,
            head_sha=head_sha,
            base_ref=base_ref,
            base_sha=base_sha,
            merge_commit_sha=merge_commit_sha,
            merged_at=merged_at,
            source_event_id=_path_or_none(source_event_id),
        )
    except (ValueError, ValidationError):
        logger.warning(
            "pull_request_merge_invalid_boundary",
            extra={"org_id": org_id, "repo": repo, "pr_number": pr_number},
        )
        return None


def parse_pull_request_revision(
    payload: dict[str, Any],
    *,
    org_id: str,
    source_event_id: str | None = None,
    github_host: str = "github.com",
) -> tuple[PullRequestRevision | None, PullRequestRevisionSkipReason | None]:
    """Parse an observed GitHub pull request head into an immutable Fact."""
    action = payload.get("action")
    if not isinstance(action, str) or action not in {"opened", "synchronize"}:
        logger.info(
            "github_action_declined",
            extra={
                "org_id": org_id,
                "source": "github",
                "record_position": 0,
                "reason": "unsupported_discriminator"
                if isinstance(action, str)
                else "malformed_discriminator",
            },
        )
        return None, PullRequestRevisionSkipReason.UNSUPPORTED_ACTION

    pull_request = payload.get("pull_request")
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    repository = payload.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    head = pull_request.get("head")
    head = head if isinstance(head, dict) else {}
    head_repository = head.get("repo")
    head_repository = head_repository if isinstance(head_repository, dict) else {}
    base = pull_request.get("base")
    base = base if isinstance(base, dict) else {}

    repo = _repo_slug_or_absent(_str_or_none(repository.get("full_name")))
    head_repo = _repo_slug_or_absent(_str_or_none(head_repository.get("full_name")))
    pr_number = _positive_int_or_none(pull_request.get("number"))
    head_ref = _path_or_none(head.get("ref"))
    base_ref = _path_or_none(base.get("ref"))
    if _head_repository_deleted(head):
        logger.warning(
            "pull_request_revision_head_repository_deleted",
            extra={"org_id": org_id, "repo": repo, "pr_number": pr_number},
        )
        return None, PullRequestRevisionSkipReason.HEAD_REPOSITORY_DELETED
    try:
        head_sha_value = _str_or_none(head.get("sha"))
        base_sha_value = _str_or_none(base.get("sha"))
        if head_sha_value is None or base_sha_value is None:
            raise ValueError("non-string commit sha")
        head_sha = normalize_commit_sha(head_sha_value)
        base_sha = normalize_commit_sha(base_sha_value)
        previous_head_sha = None
        if action == "synchronize":
            after_value = _str_or_none(payload.get("after"))
            before_value = _str_or_none(payload.get("before"))
            if after_value is None or before_value is None:
                raise ValueError("missing synchronize head chain")
            after_sha = normalize_commit_sha(after_value)
            previous_head_sha = normalize_commit_sha(before_value)
            if after_sha != head_sha:
                logger.warning(
                    "pull_request_revision_synchronize_head_mismatch",
                    extra={"org_id": org_id, "repo": repo, "pr_number": pr_number},
                )
                return (
                    None,
                    PullRequestRevisionSkipReason.SYNCHRONIZE_HEAD_MISMATCH,
                )
        if (
            not repo
            or not head_repo
            or pr_number is None
            or not head_ref
            or not base_ref
        ):
            raise ValueError("incomplete revision boundary")
        return (
            PullRequestRevision(
                org_id=org_id,
                **_repository_identity_fields(
                    repository, org_id=org_id, github_host=github_host
                )[0],
                provider=ForgeProvider.GITHUB,
                repo=repo,
                pr_number=pr_number,
                head_repo=head_repo,
                **_repository_identity_fields(
                    head_repository,
                    org_id=org_id,
                    github_host=github_host,
                    role="head_repo",
                )[0],
                head_ref=head_ref,
                head_sha=head_sha,
                base_ref=base_ref,
                base_sha=base_sha,
                previous_head_sha=previous_head_sha,
                source_event_id=_path_or_none(source_event_id),
            ),
            None,
        )
    except (ValueError, ValidationError):
        logger.warning(
            "pull_request_revision_invalid_boundary",
            extra={"org_id": org_id, "repo": repo, "pr_number": pr_number},
        )
        return None, PullRequestRevisionSkipReason.INVALID_BOUNDARY


def parse_workflow_run(
    payload: dict[str, Any],
    *,
    org_id: str,
    source_event_id: str | None = None,
    github_host: str = "github.com",
) -> CIOutcome | None:
    """Parse a GitHub workflow_run webhook payload into a CIOutcome fact.

    Returns None unless the run is completed and carries a head SHA. Check
    identity and the full ``workflow_run`` object are always captured:
    ``workflow_run.name`` → ``workflow_name`` ("" when absent),
    ``workflow_run.path`` → ``workflow_path`` (None when absent), the object
    itself → ``raw``. Fail-soft: missing keys become defaults, never a
    rejected fact.
    """
    if payload.get("action") != "completed":
        return None

    run = payload.get("workflow_run")
    run = run if isinstance(run, dict) else {}
    repository = payload.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    repo = _repo_slug_or_absent(repository.get("full_name"))
    raw_run_id = run.get("id")
    run_id = _github_id_or_none(raw_run_id)
    if run_id is None:
        logger.warning("workflow_run_invalid_run_id", extra={"org_id": org_id})
        return None

    # Widened from "empty" to "not a valid CommitSha": a junk head_sha used
    # to store a CIOutcome that could never join a commit — the failure
    # surfaced months later as a coverage gap, not an error.
    try:
        head_sha = normalize_commit_sha(_coerce_str(run.get("head_sha")))
    except ValueError:
        logger.warning(
            "workflow_run_invalid_sha",
            extra={"org_id": org_id, "repo": repo, "run_id": raw_run_id},
        )
        return None
    if not repo:
        # Stored anyway (facts first; quarantine covers bad facts) but the
        # mirror can never resolve workflow_path without a repo — leave a trail.
        logger.warning(
            "workflow_run_missing_repo", extra={"org_id": org_id, "run_id": run_id}
        )

    conclusion = _path_or_none(run.get("conclusion"))
    html_url = _str_or_none(run.get("html_url")) or ""
    run_url = html_url.strip() or None

    try:
        return CIOutcome(
            org_id=org_id,
            provider=CIProvider.GITHUB_ACTIONS,
            **_repository_identity_fields(
                repository, org_id=org_id, github_host=github_host
            )[0],
            run_id=run_id,
            run_attempt=_positive_int_or_none(run.get("run_attempt")),
            repo=repo,
            commit_sha=head_sha,
            branch=_coerce_str(run.get("head_branch")),
            result=_RESULT_MAP.get(conclusion or "", CIResult.UNKNOWN),
            workflow_name=_coerce_str(run.get("name")),
            workflow_id=_github_id_or_none(run.get("workflow_id")),
            # Whitespace-only degrades to absent: the model strips, and a
            # stored "" is a different spelling of None that the comment on
            # CIOutcome.workflow_path promises the parsers never produce.
            workflow_path=_path_or_none(run.get("path")),
            run_url=run_url,
            provider_result=conclusion,
            source_event_type="github.workflow_run.completed",
            source_event_id=_path_or_none(source_event_id),
            pr_number=_extract_pr_number(run),
            raw=run,
        )
    except ValidationError:
        logger.warning(
            "workflow_run_invalid_fact",
            extra={
                "org_id": org_id,
                "source": "github",
                "record_position": 0,
                "reason": "invalid_fact",
            },
        )
        return None


def parse_repository_rename(
    payload: dict[str, Any],
    *,
    org_id: str,
    source_event_id: str | None = None,
    github_host: str = "github.com",
) -> tuple[RepositoryRename | None, RepositoryCaptureSkipReason | None]:
    """Capture a named transition, without guessing when GitHub performed it."""
    action = payload.get("action")
    if not isinstance(action, str) or action != "renamed":
        reason = (
            RepositoryCaptureSkipReason.UNSUPPORTED_DISCRIMINATOR
            if isinstance(action, str)
            else RepositoryCaptureSkipReason.MALFORMED_DISCRIMINATOR
        )
    else:
        repository = payload.get("repository")
        repository = repository if isinstance(repository, dict) else {}
        identity, reason = _repository_identity_fields(
            repository, org_id=org_id, github_host=github_host
        )
        if reason is not None:
            return None, reason
        try:
            old_name = payload["changes"]["repository"]["name"]["from"]
            full_name = repository["full_name"]
            if not isinstance(old_name, str) or not isinstance(full_name, str):
                raise ValueError
            new_repo = normalize_repo_slug(full_name)
            old_repo = normalize_repo_slug(f"{new_repo.split('/')[0]}/{old_name}")
            if not old_repo or not new_repo:
                raise ValueError
            if old_repo == new_repo:
                reason = RepositoryCaptureSkipReason.NAME_UNCHANGED
            else:
                return RepositoryRename(
                    org_id=org_id,
                    **identity,
                    old_repo=old_repo,
                    new_repo=new_repo,
                    source_event_id=_path_or_none(source_event_id),
                ), None
        except (KeyError, TypeError, ValueError, ValidationError):
            reason = RepositoryCaptureSkipReason.INVALID_RENAME_BOUNDARY
    logger.warning(
        "repository_rename_declined",
        extra={
            "org_id": org_id,
            "source": "github",
            "record_position": 0,
            "reason": reason.value,
            "count": 1,
        },
    )
    return None, reason


def _extract_pr_number(run: dict[str, Any]) -> int | None:
    prs = run.get("pull_requests")
    if not isinstance(prs, list) or not prs or not isinstance(prs[0], dict):
        return None
    number = prs[0].get("number")
    # bool is an int subclass (True would become pull request number 1); out-of-range ints
    # overflow PostgreSQL BIGINT at INSERT. Both degrade to None.
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    if not 0 < number <= _INT64_MAX:
        return None
    return number
