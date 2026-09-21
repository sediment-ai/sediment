# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Attribution-share derivation and decline-alert tests.

``derive_attribution_share`` tests build real repos via subprocess git and a
real ``FactStore`` (AGENTS.md: no git mocks). ``check_attribution_share_alerts``
is a pure comparison over already-derived ``RepoAttributionShare`` rows, so
its tests construct those rows directly — no git or facts involved, the same
style ``test_precision_harness.py`` uses for ``PrecisionRecallResult`` math.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gitfixtures import commit_all, make_remote, make_work_repo, run_git
from sediment_core import (
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)
from sediment_core.store import FactStore
from sediment_derive import (
    AttributionShareAlertKind,
    AttributionSharePolicy,
    MirrorManager,
    Provenance,
    RepoAttributionShare,
    check_attribution_share_alerts,
    derive_attribution_share,
)
from sediment_derive.precision_harness import _wilson_score_interval

ORG = "acme-corp"
REPO = "acme-corp/backend-service"

MATH = "def total(values):\n    return sum(values) if values else 0\n"
CART = "class ShoppingCart:\n    def add_item(self, item, quantity):\n        self.items[item] = quantity\n"
LOOKUP = "def find_item(catalog, key):\n    return catalog.get(key)\n"
UNRELATED_PROSE = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do"


def _note(*session_ids: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": s,
                    "stamped_at": "2026-07-13T00:00:00+00:00",
                }
                for s in session_ids
            ],
        }
    )


def _mirror_and_store(
    tmp_path: Path,
    store: FactStore,
    work: Path,
    before: str,
    after: str,
) -> tuple[FactStore, MirrorManager, Push]:
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=before,
        after_sha=after,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    store.store_push(push)
    return store, mirrors, push


def _inference_call(
    session_id: str, text: str, *, observed_at: datetime
) -> InferenceCall:
    return InferenceCall(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="write it")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=text)])
        ],
        input_tokens=10,
        output_tokens=20,
        duration_ms=50,
        observed_at=observed_at,
    )


def test_grain_is_commit_not_file_notes_only_full_share(
    tmp_path: Path, postgres_store
) -> None:
    # A two-file commit noted to one session, both files matched by that
    # session's completions, must count as ONE agent-plausible commit with
    # ONE notes attribution -- not two (outcome_report.py::CIGrain.COMMIT's
    # reasoning: a commit touching k files is one trial, not k).
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(MATH)
    (work / "cart.py").write_text(CART)
    head = commit_all(work, "add math and cart")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-1"), head)
    store, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    captured = push.captured_at - timedelta(minutes=5)
    store.store_inference_call(_inference_call("sess-1", MATH, observed_at=captured))
    store.store_inference_call(_inference_call("sess-1", CART, observed_at=captured))

    result = derive_attribution_share(store, mirrors, ORG)
    assert len(result) == 1
    row = result[0]
    assert row.org_id == ORG
    assert row.repo == REPO
    assert row.agent_plausible_commits == 1
    assert row.git_notes_attributed == 1
    assert row.jaccard_attributed == 0
    assert row.unattributed == 0
    assert row.git_notes_share == 1.0
    assert row.git_notes_share_ci == _wilson_score_interval(1, 1)
    assert row.provenance == Provenance(policy_version="2", quarantine_revision=0)

    assert derive_attribution_share(store, mirrors, ORG) == result


def test_supplied_note_snapshot_controls_attribution_and_plausibility(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(MATH)
    head = commit_all(work, "add math")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("later"), head)
    store, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(
        _inference_call(
            "recorded",
            MATH,
            observed_at=push.captured_at - timedelta(minutes=90),
        )
    )

    result = derive_attribution_share(
        store,
        mirrors,
        ORG,
        note_sessions_by_commit={(REPO, head): frozenset({"recorded"})},
    )

    assert len(result) == 1
    assert result[0].agent_plausible_commits == 1
    assert result[0].git_notes_attributed == 1


def test_unnoted_jaccard_match_classified_jaccard(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(MATH)
    head = commit_all(work, "add math")  # no note
    store, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(
        _inference_call(
            "sess-1", MATH, observed_at=push.captured_at - timedelta(minutes=5)
        )
    )

    result = derive_attribution_share(store, mirrors, ORG)
    assert len(result) == 1
    row = result[0]
    assert row.agent_plausible_commits == 1
    assert row.jaccard_attributed == 1
    assert row.git_notes_attributed == 0
    assert row.unattributed == 0


def test_agent_plausible_commit_without_any_match_is_unattributed(
    tmp_path: Path,
    postgres_store,
) -> None:
    # The commit is agent-plausible (a completion falls inside the org-wide
    # window) but nothing attributes on any file -- the literal outage
    # signature derive_attributions itself never surfaces (it emits rows only
    # for matches).
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(MATH)
    head = commit_all(work, "add math")  # no note
    store, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(
        _inference_call(
            "sess-unrelated",
            UNRELATED_PROSE,
            observed_at=push.captured_at - timedelta(minutes=5),
        )
    )

    result = derive_attribution_share(store, mirrors, ORG)
    assert len(result) == 1
    row = result[0]
    assert row.agent_plausible_commits == 1
    assert row.git_notes_attributed == 0
    assert row.jaccard_attributed == 0
    assert row.unattributed == 1
    assert row.git_notes_share == 0.0


def test_zero_agent_activity_repo_excluded_entirely(
    tmp_path: Path, postgres_store
) -> None:
    # No completion falls inside ANY window the policy computes (org-wide or
    # notes) -- presumed human-only. The repo must not appear in the result
    # at all, so it can never trigger a zero-share alert downstream.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(MATH)
    head = commit_all(work, "add math")
    store, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(
        _inference_call(
            "sess-far", MATH, observed_at=push.captured_at - timedelta(days=10)
        )
    )

    assert derive_attribution_share(store, mirrors, ORG) == []


def test_determinism_shuffled_insertion_order(
    tmp_path: Path, postgres_store, postgres_store_factory
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(MATH)
    head = commit_all(work, "add math")
    store_a, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    captured = push.captured_at - timedelta(minutes=5)
    first = _inference_call("sess-1", MATH, observed_at=captured).model_copy(
        update={"inference_call_id": "aaa-first"}
    )
    second = _inference_call(
        "sess-2", UNRELATED_PROSE, observed_at=captured
    ).model_copy(update={"inference_call_id": "zzz-second"})
    store_a.store_inference_call(first)
    store_a.store_inference_call(second)

    _, store_b = postgres_store_factory()
    store_b.store_push(push)
    store_b.store_inference_call(second)
    store_b.store_inference_call(first)

    result_a = derive_attribution_share(store_a, mirrors, ORG)
    result_b = derive_attribution_share(store_b, mirrors, ORG)
    assert result_a == result_b


def test_read_attribution_candidates_called_once_across_repos(
    tmp_path: Path, postgres_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # derive_attribution_share builds the candidate set once
    # and threads it into each repo's derive_attributions call. Reading and
    # tokenizing the org's inference calls must not scale with repo count.
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    store = postgres_store
    for name, text in (("one", MATH), ("two", CART), ("three", LOOKUP)):
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        work = make_work_repo(repo_dir)
        (work / "code.py").write_text(text)
        head = commit_all(work, f"add {name}")
        remote = make_remote(repo_dir, work)
        push = Push(
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=f"{ORG}/{name}",
            clone_url=str(remote),
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha=head,
        )
        mirrors.ensure(push)
        store.store_push(push)
        store.store_inference_call(
            _inference_call(
                f"sess-{name}",
                text,
                observed_at=push.captured_at - timedelta(minutes=5),
            )
        )

    calls = 0
    # The public Derivation reads every repository inside one shared snapshot.
    from sediment_core.store import _FactSnapshot

    original = _FactSnapshot.read_attribution_candidates

    def counting(*args: object, **kwargs: object) -> list[object]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(_FactSnapshot, "read_attribution_candidates", counting)

    result = derive_attribution_share(store, mirrors, ORG)

    assert calls == 1
    assert {row.repo for row in result} == {
        f"{ORG}/{name}" for name in ("one", "two", "three")
    }


def test_window_days_excludes_pushes_outside_the_report_window(
    tmp_path: Path, postgres_store
) -> None:
    # Two pushes far apart: with a one-day report window, only the newer
    # push's commit is agent-plausible for the returned row -- the window is
    # anchored on the latest push in scope, never wall-clock now.
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    base = commit_all(work, "root")
    (work / "old.py").write_text(MATH)
    old_head = commit_all(work, "old change")
    old_push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url="unused",
        ref="refs/heads/main",
        before_sha=base,
        after_sha=old_head,
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    (work / "new.py").write_text(CART)
    new_head = commit_all(work, "new change")
    remote = make_remote(tmp_path, work)
    new_push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=old_head,
        after_sha=new_head,
        captured_at=datetime(2026, 1, 20, tzinfo=UTC),
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(new_push)
    store = postgres_store
    store.store_push(old_push)
    store.store_push(new_push)
    store.store_inference_call(
        _inference_call(
            "sess-old", MATH, observed_at=old_push.captured_at - timedelta(minutes=5)
        )
    )
    store.store_inference_call(
        _inference_call(
            "sess-new", CART, observed_at=new_push.captured_at - timedelta(minutes=5)
        )
    )

    policy = AttributionSharePolicy(window_days=1, min_cases_for_decline_verdict=1)
    result = derive_attribution_share(store, mirrors, ORG, policy)
    assert len(result) == 1
    row = result[0]
    assert row.window_end == new_push.captured_at
    assert row.window_start == new_push.captured_at - timedelta(days=1)
    assert row.agent_plausible_commits == 1
    assert row.jaccard_attributed == 1


def _row(
    *,
    agent_plausible_commits: int,
    git_notes_attributed: int,
    jaccard_attributed: int,
    git_notes_share: float,
    git_notes_share_ci: tuple[float, float],
    repo: str = REPO,
) -> RepoAttributionShare:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    return RepoAttributionShare(
        org_id=ORG,
        repo=repo,
        window_start=now - timedelta(days=7),
        window_end=now,
        agent_plausible_commits=agent_plausible_commits,
        git_notes_attributed=git_notes_attributed,
        jaccard_attributed=jaccard_attributed,
        unattributed=agent_plausible_commits
        - git_notes_attributed
        - jaccard_attributed,
        git_notes_share=git_notes_share,
        git_notes_share_ci=git_notes_share_ci,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )


def test_notes_only_repo_full_share_both_windows_no_alert() -> None:
    current = [
        _row(
            agent_plausible_commits=5,
            git_notes_attributed=5,
            jaccard_attributed=0,
            git_notes_share=1.0,
            git_notes_share_ci=(0.6, 1.0),
        )
    ]
    baseline = [
        _row(
            agent_plausible_commits=5,
            git_notes_attributed=5,
            jaccard_attributed=0,
            git_notes_share=1.0,
            git_notes_share_ci=(0.6, 1.0),
        )
    ]
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=1)

    assert check_attribution_share_alerts(current, baseline, policy) == []


def test_zero_share_nonzero_activity_fires_without_sample_gate() -> None:
    # A single agent-plausible commit is enough -- this rule has no
    # min_cases_for_decline_verdict gate, unlike sustained_decline.
    current = [
        _row(
            agent_plausible_commits=1,
            git_notes_attributed=0,
            jaccard_attributed=0,
            git_notes_share=0.0,
            git_notes_share_ci=(0.0, 0.0),
        )
    ]
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=10_000)

    alerts = check_attribution_share_alerts(current, [], policy)
    assert len(alerts) == 1
    assert alerts[0].kind == AttributionShareAlertKind.ZERO_SHARE_NONZERO_ACTIVITY
    assert alerts[0].baseline is None


def test_zero_notes_share_with_jaccard_activity_fires_without_sample_gate() -> None:
    # The client-side stamper can stop while jaccard keeps attributing commits.
    # This notes-dark state must alert without a baseline or sample-size gate.
    current = [
        _row(
            agent_plausible_commits=1,
            git_notes_attributed=0,
            jaccard_attributed=1,
            git_notes_share=0.0,
            git_notes_share_ci=(0.0, 0.0),
        )
    ]
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=10_000)

    alerts = check_attribution_share_alerts(current, [], policy)
    assert len(alerts) == 1
    assert alerts[0].kind == AttributionShareAlertKind.ZERO_SHARE_NONZERO_ACTIVITY
    assert alerts[0].baseline is None


def test_sustained_decline_gated_by_overlapping_wilson_ci() -> None:
    # A noisy one-off dip must not fire: the CIs still overlap.
    current = [
        _row(
            agent_plausible_commits=3,
            git_notes_attributed=1,
            jaccard_attributed=2,
            git_notes_share=0.333,
            git_notes_share_ci=(0.06, 0.79),
        )
    ]
    baseline = [
        _row(
            agent_plausible_commits=3,
            git_notes_attributed=2,
            jaccard_attributed=1,
            git_notes_share=0.667,
            git_notes_share_ci=(0.21, 0.94),
        )
    ]
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=1)

    assert check_attribution_share_alerts(current, baseline, policy) == []


def test_sustained_decline_fires_once_ci_gap_opens() -> None:
    current = [
        _row(
            agent_plausible_commits=10,
            git_notes_attributed=1,
            jaccard_attributed=9,
            git_notes_share=0.1,
            git_notes_share_ci=(0.02, 0.4),
        )
    ]
    baseline = [
        _row(
            agent_plausible_commits=10,
            git_notes_attributed=9,
            jaccard_attributed=1,
            git_notes_share=0.9,
            git_notes_share_ci=(0.6, 0.98),
        )
    ]
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=1)

    alerts = check_attribution_share_alerts(current, baseline, policy)
    assert len(alerts) == 1
    assert alerts[0].kind == AttributionShareAlertKind.SUSTAINED_DECLINE
    assert alerts[0].baseline is baseline[0]
    assert alerts[0].current is current[0]


def test_sustained_decline_gated_by_min_cases_despite_ci_gap() -> None:
    # Same non-overlapping CIs as the fires-once-gap-opens case, but neither
    # window clears min_cases_for_decline_verdict: no verdict is trusted yet.
    current = [
        _row(
            agent_plausible_commits=10,
            git_notes_attributed=1,
            jaccard_attributed=9,
            git_notes_share=0.1,
            git_notes_share_ci=(0.02, 0.4),
        )
    ]
    baseline = [
        _row(
            agent_plausible_commits=10,
            git_notes_attributed=9,
            jaccard_attributed=1,
            git_notes_share=0.9,
            git_notes_share_ci=(0.6, 0.98),
        )
    ]
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=11)

    assert check_attribution_share_alerts(current, baseline, policy) == []


def test_no_baseline_row_skips_decline_check() -> None:
    current = [
        _row(
            agent_plausible_commits=4,
            git_notes_attributed=1,
            jaccard_attributed=3,
            git_notes_share=0.25,
            git_notes_share_ci=(0.05, 0.7),
        )
    ]
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=1)

    assert check_attribution_share_alerts(current, [], policy) == []


def test_zero_activity_repo_never_reaches_alert_check() -> None:
    # derive_attribution_share already excludes a zero-activity repo from its
    # output (see test_zero_agent_activity_repo_excluded_entirely); an empty
    # current list must therefore never synthesize an alert.
    policy = AttributionSharePolicy(min_cases_for_decline_verdict=1)
    assert check_attribution_share_alerts([], [], policy) == []
