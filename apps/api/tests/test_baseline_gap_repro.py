# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end regression tests for the attribution-share CLI baseline window.

``_baseline_rows`` must bound the trailing baseline to the
``baseline_window_days`` immediately preceding each repo's current
``window_start`` so a cadence gap before the current window cannot re-anchor
the baseline at a stale pre-current push -- the CLI module docstring's
"immediately preceding it" contract. The derivation function
``derive_attribution_share`` anchors every row's ``window_end`` at the latest
push in the slice it is handed, so the bound must be enforced by the caller
(the same contiguous-bounds discipline ``derive_model_report_attribution_share``
already follows).

These tests build real git fixtures and a real PostgreSQL ``FactStore``
(AGENTS.md: no git mocks) and exercise the full CLI code path:
``derive_attribution_share`` -> ``_baseline_rows`` ->
``check_attribution_share_alerts``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Reuse derive's git fixture helpers (the documented cross-package fixture
# pattern, matching ``cli/tests/conftest.py``). apps/api/tests/ is one level
# deeper than cli/tests/, so parents[3] is the repo root.
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "derive" / "tests"))

from gitfixtures import commit_all, make_remote, make_work_repo, run_git  # noqa: E402
from sediment_api.reports.attribution_share_report import (  # noqa: E402
    _baseline_rows,
    main,
)
from sediment_core import (  # noqa: E402
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)
from sediment_derive import (  # noqa: E402
    AttributionShareAlertKind,
    AttributionSharePolicy,
    MirrorManager,
    check_attribution_share_alerts,
    derive_attribution_share,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"

# Identical content for every fixture commit. A single matching completion in
# a push's window attributes every file in that push (notes for the noted
# commit, jaccard for the rest), so the share is set by the noted/unnoted
# split, not by content shape.
HELPERS = "def total(values):\n    return sum(values) if values else 0\n"

WINDOW_DAYS = 7
BASELINE_WINDOW_DAYS = 28


def _note(session_id: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": session_id,
                    "stamped_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        }
    )


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


def _append_push_commits(
    work: Path,
    before_sha: str,
    *,
    n: int,
    prefix: str,
    noted_indices: set[int],
    note_session: str,
) -> str:
    """Append ``n`` commits (each adding a distinct file with identical
    content), stamp a sediment note referencing ``note_session`` on the commits
    whose 0-based index is in ``noted_indices``. Returns the new HEAD sha."""
    head = before_sha
    for i in range(n):
        (work / f"{prefix}_{i}.py").write_text(HELPERS)
        head = commit_all(work, f"{prefix} commit {i}")
        if i in noted_indices:
            run_git(
                work, "notes", "--ref=sediment", "add", "-m", _note(note_session), head
            )
    return head


def _build_scenario(
    tmp_path: Path,
    store,
    *,
    pushes: list[dict],
) -> tuple[MirrorManager, list[Push]]:
    """Build a single-repo git fixture with several pushes and a real
    ``FactStore``.

    ``pushes`` is an ordered list of specifications ``{"n", "prefix", "noted_indices",
    "note_session", "captured_at"}``. A root commit seeds the repo (so the first
    push's ``before_sha`` is a real commit, not all-zeros -- otherwise
    ``list_push_commits`` degrades to head-only and would report one commit
    instead of ``n``). One inference call per push (matching that push's note
    session, content ``HELPERS``, observed five minutes before the push) is
    stored -- it satisfies the git-notes window for noted commits and the
    jaccard window for unnoted commits of the same push, and is far enough from
    every other push that no cross-push window contamination occurs.
    """
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    root = commit_all(work, "root")

    heads: list[str] = []
    for spec in pushes:
        head = _append_push_commits(
            work,
            root if not heads else heads[-1],
            n=spec["n"],
            prefix=spec["prefix"],
            noted_indices=spec["noted_indices"],
            note_session=spec["note_session"],
        )
        heads.append(head)

    # One bare remote holding the full history (commits + the sediment notes
    # ref stamped above); every push points at it so a single mirror clone
    # carries every push's commits and notes.
    remote = make_remote(tmp_path, work)

    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    push_facts: list[Push] = []
    for spec, after in zip(pushes, heads, strict=True):
        before = root if len(push_facts) == 0 else push_facts[-1].after_sha
        push = Push(
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            clone_url=str(remote),
            ref="refs/heads/main",
            before_sha=before,
            after_sha=after,
            captured_at=spec["captured_at"],
        )
        mirrors.ensure(push)
        store.store_push(push)
        store.store_inference_call(
            _inference_call(
                spec["note_session"],
                HELPERS,
                observed_at=spec["captured_at"] - timedelta(minutes=5),
            )
        )
        push_facts.append(push)
    return mirrors, push_facts


def _policy(*, min_cases: int) -> AttributionSharePolicy:
    return AttributionSharePolicy(
        window_days=WINDOW_DAYS,
        baseline_window_days=BASELINE_WINDOW_DAYS,
        min_cases_for_decline_verdict=min_cases,
    )


def test_sparse_push_gap_yields_no_baseline_and_no_decline(
    tmp_path, postgres_store
) -> None:
    """The repro: a high-notes-share active period, a pause >=
    ``baseline_window_days``, then a lower-share burst. The contiguous baseline
    window immediately preceding the current window contains no pushes, so the
    baseline is empty and ``SUSTAINED_DECLINE`` must not fire -- regardless of
    the stale high-share history that the unbounded selection used to reach for.
    """
    active_at = datetime(2026, 1, 1, tzinfo=UTC)
    # current.window_end = burst.captured_at; window_start = burst - 7d.
    burst_at = datetime(2026, 2, 12, tzinfo=UTC)
    mirrors, _ = _build_scenario(
        tmp_path,
        postgres_store,
        pushes=[
            {
                "n": 10,
                "prefix": "active",
                "noted_indices": set(range(10)),  # all noted -> share 1.0
                "note_session": "active-sess",
                "captured_at": active_at,
            },
            {
                "n": 10,
                "prefix": "burst",
                "noted_indices": {0},  # 1 noted + 9 jaccard -> share 0.1
                "note_session": "burst-sess",
                "captured_at": burst_at,
            },
        ],
    )
    policy = _policy(min_cases=10)

    current = derive_attribution_share(postgres_store, mirrors, ORG, policy)
    assert len(current) == 1
    current_row = current[0]
    assert current_row.repo == REPO
    # Window anchored on the burst push (ADR 0001 -- never wall-clock).
    assert current_row.window_end == burst_at
    assert current_row.window_start == burst_at - timedelta(days=WINDOW_DAYS)
    assert current_row.agent_plausible_commits == 10
    assert current_row.git_notes_attributed == 1
    assert current_row.jaccard_attributed == 9
    assert current_row.git_notes_share == 0.1

    # The contiguous baseline window [window_start - 28d, window_start)
    # genuinely contains no pushes -- it is the pause -- so _baseline_rows
    # returns nothing, not a stale re-anchored row.
    baseline_start = current_row.window_start - timedelta(days=BASELINE_WINDOW_DAYS)
    in_contiguous_window = [
        push
        for push in postgres_store.read_pushes(ORG)
        if push.repo == REPO
        and baseline_start <= push.captured_at < current_row.window_start
    ]
    assert in_contiguous_window == []

    baseline = _baseline_rows(postgres_store, mirrors, ORG, policy, current)
    assert baseline == []

    alerts = check_attribution_share_alerts(current, baseline, policy)
    assert alerts == []


def test_baseline_bounds_are_contiguous_and_decline_still_fires(
    tmp_path, postgres_store
) -> None:
    """No regression: when pushes are dense enough that the contiguous baseline
    window does contain the high-share period, ``_baseline_rows`` returns a
    baseline row whose reported bounds are exactly
    ``[current.window_start - baseline_window_days, current.window_start)`` and
    ``SUSTAINED_DECLINE`` still fires for a real material drop.
    """
    # 8-day gap (>= the 7-day git-notes lookback so windows don't contaminate,
    # but small enough that the Feb 4 push falls inside the [Jan 8, Feb 5)
    # baseline window of the Feb 12 current window).
    active_at = datetime(2026, 2, 4, tzinfo=UTC)
    burst_at = datetime(2026, 2, 12, tzinfo=UTC)
    mirrors, _ = _build_scenario(
        tmp_path,
        postgres_store,
        pushes=[
            {
                "n": 10,
                "prefix": "active",
                "noted_indices": set(range(10)),  # share 1.0
                "note_session": "active-sess",
                "captured_at": active_at,
            },
            {
                "n": 10,
                "prefix": "burst",
                "noted_indices": {0},  # share 0.1
                "note_session": "burst-sess",
                "captured_at": burst_at,
            },
        ],
    )
    policy = _policy(min_cases=10)

    current = derive_attribution_share(postgres_store, mirrors, ORG, policy)
    assert len(current) == 1
    current_row = current[0]
    assert current_row.window_end == burst_at
    assert current_row.window_start == burst_at - timedelta(days=WINDOW_DAYS)
    expected_baseline_start = current_row.window_start - timedelta(
        days=BASELINE_WINDOW_DAYS
    )

    baseline = _baseline_rows(postgres_store, mirrors, ORG, policy, current)
    assert len(baseline) == 1
    baseline_row = baseline[0]
    assert baseline_row.repo == REPO
    # Contiguous bounds, not the derive function's latest-push anchor (which
    # would be Feb 4, not the current window_start of Feb 5).
    assert baseline_row.window_end == current_row.window_start
    assert baseline_row.window_start == expected_baseline_start
    assert baseline_row.agent_plausible_commits == 10
    assert baseline_row.git_notes_attributed == 10
    assert baseline_row.git_notes_share == 1.0

    alerts = check_attribution_share_alerts(current, baseline, policy)
    assert len(alerts) == 1
    assert alerts[0].kind == AttributionShareAlertKind.SUSTAINED_DECLINE
    assert alerts[0].current is current_row
    assert alerts[0].baseline is baseline_row


def test_baseline_selection_excludes_pushes_outside_contiguous_range(
    tmp_path, postgres_store
) -> None:
    """The baseline lower bound is enforced: a push older than
    ``current.window_start - baseline_window_days`` is excluded from the
    baseline even though it precedes the current window. The unbounded
    "all pushes before current" selection would have included it (re-anchoring
    the baseline at its captured_at and double-counting its commits); the
    bounded selection reports only the in-range push's commits and the
    contiguous bound ends at ``current.window_start``.
    """
    stale_at = datetime(2026, 1, 1, tzinfo=UTC)  # before baseline_start
    active_at = datetime(2026, 3, 1, tzinfo=UTC)  # inside baseline window
    burst_at = datetime(2026, 3, 15, tzinfo=UTC)  # current window
    mirrors, _ = _build_scenario(
        tmp_path,
        postgres_store,
        pushes=[
            {
                "n": 4,
                "prefix": "stale",
                "noted_indices": set(range(4)),
                "note_session": "stale-sess",
                "captured_at": stale_at,
            },
            {
                "n": 4,
                "prefix": "active",
                "noted_indices": set(range(4)),
                "note_session": "active-sess",
                "captured_at": active_at,
            },
            {
                "n": 4,
                "prefix": "burst",
                "noted_indices": {0},
                "note_session": "burst-sess",
                "captured_at": burst_at,
            },
        ],
    )
    policy = _policy(min_cases=1)

    current = derive_attribution_share(postgres_store, mirrors, ORG, policy)
    assert len(current) == 1
    current_row = current[0]
    assert current_row.window_end == burst_at
    assert current_row.window_start == burst_at - timedelta(days=WINDOW_DAYS)
    expected_baseline_start = current_row.window_start - timedelta(
        days=BASELINE_WINDOW_DAYS
    )

    # Mar 1 sits inside [Jan 8, Mar 8); Jan 1 does not. The stale push must not
    # contribute commits to the baseline, and the bound must end at the
    # current window_start (not the derive anchor of Mar 1).
    assert expected_baseline_start <= active_at < current_row.window_start
    assert stale_at < expected_baseline_start

    baseline = _baseline_rows(postgres_store, mirrors, ORG, policy, current)
    assert len(baseline) == 1
    baseline_row = baseline[0]
    assert baseline_row.window_end == current_row.window_start
    assert baseline_row.window_start == expected_baseline_start
    # Only the in-range push's 4 commits -- the stale Jan 1 push is excluded
    # by the lower bound (the buggy selection would have counted 8).
    assert baseline_row.agent_plausible_commits == 4
    assert baseline_row.git_notes_attributed == 4


def test_main_json_sparse_gap_emits_no_alerts(
    tmp_path, postgres_store, postgres_database_url
) -> None:
    """The fix flows through the operator CLI end-to-end. ``--target-margin 1.0``
    lowers ``min_cases_for_decline_verdict`` to 1 so the baseline-contiguity fix
    is the sole cause of the empty alerts (under the stale-baseline behavior
    this same invocation would emit a ``sustained_decline`` alert against the
    Jan 1 history)."""
    active_at = datetime(2026, 1, 1, tzinfo=UTC)
    burst_at = datetime(2026, 2, 12, tzinfo=UTC)
    mirrors, _ = _build_scenario(
        tmp_path,
        postgres_store,
        pushes=[
            {
                "n": 10,
                "prefix": "active",
                "noted_indices": set(range(10)),
                "note_session": "active-sess",
                "captured_at": active_at,
            },
            {
                "n": 10,
                "prefix": "burst",
                "noted_indices": {0},
                "note_session": "burst-sess",
                "captured_at": burst_at,
            },
        ],
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(
            [
                "--org",
                ORG,
                "--target-margin",
                "1.0",
                "--window-days",
                str(WINDOW_DAYS),
                "--baseline-window-days",
                str(BASELINE_WINDOW_DAYS),
                "--json",
                "--database-url",
                postgres_database_url,
                # Reuse the fixture's already-populated mirror so the derivation
                # can read the repo's commits and notes (default ./mirrors is CWD
                # and has nothing).
                "--mirror-path",
                str(mirrors.base),
            ]
        )
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["baseline"] == []
    assert payload["alerts"] == []
    assert len(payload["current"]) == 1
    current = payload["current"][0]
    assert (
        current["window_start"] == (burst_at - timedelta(days=WINDOW_DAYS)).isoformat()
    )
    assert current["window_end"] == burst_at.isoformat()
