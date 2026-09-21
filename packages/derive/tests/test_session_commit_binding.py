# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure observation binding retains qualified source scope and UTC ordering."""

from datetime import UTC, datetime, timedelta, timezone
from itertools import permutations

import pytest

from sediment_core import SessionCommitObservation
from sediment_derive.session_commit import bind_session_commits


def test_binding_is_deterministic_inclusive_and_utc_ordered():
    boundary = datetime(2026, 9, 6, 12, tzinfo=UTC)
    template = SessionCommitObservation(
        observation_id="b",
        org_id="acme",
        session_id="session",
        repo="acme/repo",
        commit_sha="a" * 40,
        source_push_id="push-b",
        captured_at=boundary.astimezone(timezone(timedelta(hours=3))),
    )
    earlier_id = template.model_copy(
        update={
            "observation_id": "a",
            "source_push_id": "push-a",
            "captured_at": boundary,
        }
    )
    late = template.model_copy(
        update={
            "observation_id": "late",
            "captured_at": boundary + timedelta(microseconds=1),
        }
    )
    foreign = template.model_copy(
        update={"observation_id": "foreign", "org_id": "other"}
    )
    expected = {("acme/repo", "a" * 40, "session"): (earlier_id, template)}
    for facts in permutations((template, earlier_id, late, foreign)):
        assert bind_session_commits(facts, "acme", as_of=boundary) == expected
    assert bind_session_commits([], "acme", as_of=boundary) == {}


def test_binding_rejects_naive_boundary_even_with_no_sources():
    with pytest.raises(ValueError, match="aware datetime"):
        bind_session_commits([], "acme", as_of=datetime(2026, 9, 6))
