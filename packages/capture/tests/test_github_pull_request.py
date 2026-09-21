# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub pull_request merge-boundary translation."""

from datetime import UTC, datetime
import json
from pathlib import Path

from sediment_capture import (
    PullRequestRevisionSkipReason,
    parse_pull_request_merge,
    parse_pull_request_revision,
)

FIXTURE = Path(__file__).parent / "fixtures" / "github_pull_request.json"


def _payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text())


def test_parse_pull_request_merge_preserves_the_merge_boundary() -> None:
    merge = parse_pull_request_merge(
        _payload(), org_id="acme-corp", source_event_id="delivery-1"
    )

    assert merge is not None
    assert merge.repo == "acme-corp/backend-service"
    assert merge.pr_number == 41
    assert merge.head_repo == "acme-corp/backend-service"
    assert merge.head_ref == "feature/query"
    assert merge.head_sha == "a" * 40
    assert merge.base_ref == "main"
    assert merge.base_sha == "b" * 40
    assert merge.merge_commit_sha == "c" * 40
    assert merge.merged_at == datetime(2026, 9, 3, 12, 34, 56, tzinfo=UTC)
    assert merge.source_event_id == "delivery-1"


def test_parse_pull_request_merge_skips_non_merge_events() -> None:
    for action, merged in (("opened", False), ("closed", False)):
        payload = _payload()
        payload["action"] = action
        pull_request = payload["pull_request"]
        assert isinstance(pull_request, dict)
        pull_request["merged"] = merged
        assert parse_pull_request_merge(payload, org_id="acme-corp") is None


def test_parse_pull_request_merge_skips_incomplete_join_boundaries() -> None:
    for path in (
        ("pull_request", "merge_commit_sha"),
        ("pull_request", "merged_at"),
        ("pull_request", "head", "sha"),
        ("pull_request", "base", "sha"),
    ):
        payload = _payload()
        node = payload
        for key in path[:-1]:
            child = node[key]
            assert isinstance(child, dict)
            node = child
        node.pop(path[-1])
        assert parse_pull_request_merge(payload, org_id="acme-corp") is None


def test_parse_pull_request_merge_skips_non_string_repository_identities() -> None:
    payload = _payload()
    repository = payload["repository"]
    assert isinstance(repository, dict)
    repository["full_name"] = ["acme-corp/backend-service"]
    assert parse_pull_request_merge(payload, org_id="acme-corp") is None

    payload = _payload()
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    head = pull_request["head"]
    assert isinstance(head, dict)
    head_repository = head["repo"]
    assert isinstance(head_repository, dict)
    head_repository["full_name"] = ["acme-corp/backend-service"]
    assert parse_pull_request_merge(payload, org_id="acme-corp") is None


def test_parse_pull_request_merge_declines_deleted_source_fork(caplog) -> None:
    import logging

    payload = _payload()
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    head = pull_request["head"]
    assert isinstance(head, dict)
    head["repo"] = None
    with caplog.at_level(logging.WARNING):
        assert parse_pull_request_merge(payload, org_id="acme-corp") is None
    [record] = [
        item
        for item in caplog.records
        if item.message == "pull_request_merge_head_repository_deleted"
    ]
    assert (record.org_id, record.repo, record.pr_number) == (
        "acme-corp",
        "acme-corp/backend-service",
        41,
    )


def test_parse_pull_request_revision_declines_deleted_source_fork(caplog) -> None:
    import logging

    payload = _payload()
    payload["action"] = "opened"
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    head = pull_request["head"]
    assert isinstance(head, dict)
    head["repo"] = None
    with caplog.at_level(logging.WARNING):
        revision, reason = parse_pull_request_revision(payload, org_id="acme-corp")
    assert revision is None
    assert reason is PullRequestRevisionSkipReason.HEAD_REPOSITORY_DELETED
    [record] = [
        item
        for item in caplog.records
        if item.message == "pull_request_revision_head_repository_deleted"
    ]
    assert (record.org_id, record.repo, record.pr_number) == (
        "acme-corp",
        "acme-corp/backend-service",
        41,
    )


def test_parse_pull_request_merge_skips_non_string_commit_shas() -> None:
    numeric_sha = int("1" * 40)
    for path in (
        ("pull_request", "head", "sha"),
        ("pull_request", "base", "sha"),
        ("pull_request", "merge_commit_sha"),
    ):
        payload = _payload()
        node = payload
        for key in path[:-1]:
            child = node[key]
            assert isinstance(child, dict)
            node = child
        node[path[-1]] = numeric_sha
        assert parse_pull_request_merge(payload, org_id="acme-corp") is None


def test_parse_opened_pull_request_revision_preserves_observed_head() -> None:
    payload = _payload()
    payload["action"] = "opened"

    revision, reason = parse_pull_request_revision(
        payload, org_id="ACME-CORP", source_event_id="delivery-opened"
    )

    assert reason is None
    assert revision is not None
    assert revision.org_id == "acme-corp"
    assert revision.repo == "acme-corp/backend-service"
    assert revision.pr_number == 41
    assert revision.head_repo == "acme-corp/backend-service"
    assert revision.head_ref == "feature/query"
    assert revision.head_sha == "a" * 40
    assert revision.base_ref == "main"
    assert revision.base_sha == "b" * 40
    assert revision.previous_head_sha is None
    assert revision.source_event_id == "delivery-opened"


def test_parse_synchronize_pull_request_revision_preserves_head_chain() -> None:
    payload = _payload()
    payload["action"] = "synchronize"
    payload["before"] = "d" * 40
    payload["after"] = "a" * 40

    revision, reason = parse_pull_request_revision(
        payload, org_id="acme-corp", source_event_id="delivery-sync"
    )

    assert reason is None
    assert revision is not None
    assert revision.head_sha == "a" * 40
    assert revision.previous_head_sha == "d" * 40
    assert revision.source_event_id == "delivery-sync"


def test_parse_synchronize_pull_request_revision_rejects_head_mismatch() -> None:
    payload = _payload()
    payload["action"] = "synchronize"
    payload["before"] = "d" * 40
    payload["after"] = "e" * 40

    revision, reason = parse_pull_request_revision(payload, org_id="acme-corp")

    assert revision is None
    assert reason is PullRequestRevisionSkipReason.SYNCHRONIZE_HEAD_MISMATCH


def test_parse_pull_request_revision_returns_closed_skip_reasons() -> None:
    payload = _payload()
    payload["action"] = "edited"
    assert parse_pull_request_revision(payload, org_id="acme-corp") == (
        None,
        PullRequestRevisionSkipReason.UNSUPPORTED_ACTION,
    )

    payload["action"] = "opened"
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    head = pull_request["head"]
    assert isinstance(head, dict)
    head["sha"] = "not-a-sha"
    assert parse_pull_request_revision(payload, org_id="acme-corp") == (
        None,
        PullRequestRevisionSkipReason.INVALID_BOUNDARY,
    )


def test_merge_declined_action_logs_discriminator_reason(caplog) -> None:
    import logging

    for action, reason in [
        (None, "malformed_discriminator"),
        ([], "malformed_discriminator"),
        ({}, "malformed_discriminator"),
        ("opened", "unsupported_discriminator"),
    ]:
        caplog.clear()
        payload = _payload()
        payload["action"] = action
        with caplog.at_level(logging.INFO):
            assert parse_pull_request_merge(payload, org_id="acme-corp") is None
        [record] = [
            item for item in caplog.records if item.message == "github_action_declined"
        ]
        assert (
            record.org_id,
            record.source,
            record.record_position,
            record.reason,
        ) == ("acme-corp", "github", 0, reason)
        assert payload["action"] is action
