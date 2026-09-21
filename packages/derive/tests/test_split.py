# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for the deterministic train/eval holdout split.

The split is a pure function of ``session_id`` — no fixtures to mock, no RNG to
seed. These lock the properties the split's value depends on: determinism
across calls and orderings, the disabled-by-default behaviour, uniform
distribution, and boundary behavior.
"""

from __future__ import annotations

from hashlib import sha256

from sediment_derive import (
    is_eval,
    session_eval_score,
    split_of,
)

# A deterministically generated corpus — f-string ids, no RNG, so the suite
# reproduces byte-for-byte on every run and machine.
CORPUS = [f"session-{i:04d}" for i in range(1000)]


# ── determinism ───────────────────────────────────────────────────────────


def test_score_matches_the_documented_formula() -> None:
    # The score is exactly "top 8 bytes of sha256 as a big-endian int, scaled".
    # Recomputing the formula independently pins it against an accidental edit.
    for sid in ("s1", "acme/session-xyz", "", "unicode-☃-session"):
        expected = int.from_bytes(sha256(sid.encode()).digest()[:8], "big") / 2**64
        assert session_eval_score(sid) == expected
        assert 0.0 <= session_eval_score(sid) < 1.0


def test_is_eval_is_stable_across_repeated_calls() -> None:
    # Same session, same fraction → same verdict, every call. No hidden state.
    for sid in CORPUS[:50]:
        first = is_eval(sid, 0.3)
        assert all(is_eval(sid, 0.3) is first for _ in range(5))


def test_split_is_independent_of_evaluation_order() -> None:
    # Computing the corpus's splits forwards, backwards, or via a set yields the
    # identical per-session mapping — the split never depends on call order.
    forward = {sid: is_eval(sid, 0.2) for sid in CORPUS}
    backward = {sid: is_eval(sid, 0.2) for sid in reversed(CORPUS)}
    shuffled = {sid: is_eval(sid, 0.2) for sid in sorted(CORPUS, key=sha256_key)}
    assert forward == backward == shuffled


def sha256_key(sid: str) -> str:
    return sha256(sid.encode()).hexdigest()


def test_holdouts_are_nested_raising_the_fraction_only_adds_sessions() -> None:
    # Because the verdict is score < fraction, a session eval at f is eval at
    # every larger fraction: raising the knob only grows the eval set, never
    # reshuffles it. This is what makes the split safe to tune.
    for sid in CORPUS[:100]:
        if is_eval(sid, 0.1):
            assert is_eval(sid, 0.2)
            assert is_eval(sid, 0.5)


# ── disabled by default ───────────────────────────────────────────────────


def test_fraction_zero_puts_every_session_in_train() -> None:
    # Default 0.0 disables the split: no session is ever eval, so unconfigured
    # deployments stay byte-for-byte unchanged.
    assert not any(is_eval(sid, 0.0) for sid in CORPUS)
    assert all(split_of(sid, 0.0) == "train" for sid in CORPUS)


def test_split_of_returns_the_matching_label() -> None:
    for sid in CORPUS[:100]:
        assert split_of(sid, 0.3) == ("eval" if is_eval(sid, 0.3) else "train")


# ── distribution sanity ───────────────────────────────────────────────────


def test_eval_share_tracks_the_fraction() -> None:
    # sha256 spreads ids uniformly, so ~10% of 1000 ids land in eval at 0.1.
    # The band is wide (5–15%) so the test is not flaky, yet a broken hash
    # (e.g. all-train, or a biased slice) still fails it.
    eval_count = sum(is_eval(sid, 0.1) for sid in CORPUS)
    assert 50 <= eval_count <= 150, eval_count
