# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Deterministic train/eval holdout split (ADR 0004).

The split is a **pure function of the session id** — no RNG, no seed, no
wall-clock. The same session lands in the same split on every derivation,
forever, so a re-run (or a run over the same facts ingested in a different
order) reproduces an identical holdout. Determinism is the whole point: there
is deliberately *no* seed knob, because a seed would make the split
irreproducible across deployments and time.

**Why the session is the split unit.** Rows from one session are near-duplicates
(shared message prefixes, same task), so a per-row split would leak train-side
context into the eval set. Keying on ``session_id`` keeps a whole session on one
side of the fence. The score is the top 8 bytes of ``sha256(session_id)`` read
as a big-endian integer, scaled into ``[0, 1)``; a session is eval when its score
falls below ``fraction``. sha256 spreads session ids uniformly, so the eval share
converges on ``fraction`` without any distribution assumptions about the ids.

**Seam note for attributed-completion assembly (do not build here).** When
attributed-completion assembly lands, it must stamp its ``split`` via *these same
primitives* — never a second hash — so a completion carries the identical split
whether it surfaces as a rollout or an attributed-completion projection. A plain
attributed completion stamps ``split_of(inference_call.session_id, fraction)``.
A multi-session artifact (a DPO
pair setting one session's completion against another's) is eval when ANY
constituent session is eval — eval wins, so a train-side completion can never
leak into the eval set through a cross-session pairing; build that helper at
the attributed-completion layer when it lands. The canonical derivation policy
supplies the fraction.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal

# A completion/rollout/attributed completion is assigned exactly one of these.
Split = Literal["train", "eval"]

# The two labels, named so producers stamp them by symbol rather than a bare
# string literal that a typo could silently corrupt.
TRAIN: Split = "train"
EVAL: Split = "eval"


def session_eval_score(session_id: str) -> float:
    """The session's position in ``[0, 1)`` — top 8 bytes of its sha256 as a
    big-endian integer, scaled. A session is eval when this is ``< fraction``.

    Exposed for tests and diagnostics; producers should call :func:`is_eval`
    or :func:`split_of` rather than compare scores themselves."""
    return int.from_bytes(sha256(session_id.encode()).digest()[:8], "big") / 2**64


def is_eval(session_id: str, fraction: float) -> bool:
    """Whether ``session_id`` falls in the eval holdout at ``fraction``.

    The split primitive: pure, deterministic, no seed. ``fraction`` is the
    eval share in ``[0, 1)``; ``0.0`` puts every session in train. Range
    validation lives on the canonical derivation policy, so this stays a bare
    mathematical predicate."""
    return session_eval_score(session_id) < fraction


def split_of(session_id: str, fraction: float) -> Split:
    """The single-session stamp: ``"eval"`` when :func:`is_eval`, else
    ``"train"``. Used by the rollout derivation and by future single-completion
    attributed completions."""
    return EVAL if is_eval(session_id, fraction) else TRAIN
