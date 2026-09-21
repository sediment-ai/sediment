# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sediment capture: translators that turn provider payloads into facts."""

from .gateway import ADAPTERS, LiteLLMAdapter
from .github import (
    PullRequestRevisionSkipReason,
    RepositoryCaptureSkipReason,
    parse_repository_rename,
    parse_pull_request_merge,
    parse_pull_request_revision,
    parse_push,
    parse_workflow_run,
    sign_payload,
    verify_signature,
)
from .otlp import (
    OTLPCaptureResult,
    parse_otlp_logs,
    parse_otlp_decisions,
    parse_otlp_edit_observations,
    parse_otlp_rejected_edits,
    parse_otlp_retry_linkages,
    RetryLinkageSkipReason,
)
from .session_identity import SessionIdentity, resolve_identity

__all__ = [
    "ADAPTERS",
    "LiteLLMAdapter",
    "SessionIdentity",
    "PullRequestRevisionSkipReason",
    "RepositoryCaptureSkipReason",
    "parse_repository_rename",
    "OTLPCaptureResult",
    "parse_otlp_logs",
    "parse_otlp_decisions",
    "parse_otlp_edit_observations",
    "parse_otlp_rejected_edits",
    "parse_otlp_retry_linkages",
    "RetryLinkageSkipReason",
    "parse_push",
    "parse_pull_request_merge",
    "parse_pull_request_revision",
    "parse_workflow_run",
    "resolve_identity",
    "sign_payload",
    "verify_signature",
]
