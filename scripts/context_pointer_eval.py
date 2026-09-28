# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 3: does a pointer list recover the tokens a minimal context loses?

Phase 2 found that J2 cut delivered context from 15 KB to 0.9 KB but saved only
17% of tokens: the agent re-read files and re-ran commands it no longer had.
Phase 3 lengthens the source Sessions (about ten file reads over five turns)
and compares FULL, J2 (unchanged), and J2P: J2 plus a pointer list of the paths
read and commands run, with no content. It reuses the Phase 2 driver and runner
unchanged in operation; commands and flags are the same.

The targets below were fixed before any development or held-out run.
"""

from __future__ import annotations

from pathlib import Path

import bounded_evidence_selection as selector
import bounded_selection_eval as run
import context_minimal_eval as phase2

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "tests/fixtures/context_pointer"
ARMS = ("FULL", "J2", "J2P")
SETS = {
    "heldout": {"families": ("invoice-subtotal", "username-check"), "repetitions": 2},
    "development": {"families": ("threshold-alerts",), "repetitions": 1},
}
# Long histories exceed the Phase 1 limits of 32 parts and 32 KB. Reads are
# chunked at the API's 32 references per request.
CATALOG_PART_LIMIT = 96
CATALOG_BYTES_LIMIT = 131_072
EVIDENCE_CALL_LIMIT = 12
# Five inspection turns plus notes need more than the 12-call continuation limit.
SOURCE_MODEL_CALL_LIMIT = 40
TARGETS = {
    "validity": "every slot recorded; no instrument failure; at least 10 of 12 "
    "tasks have all three arms measured with complete usage",
    "rule_delivery": "J2P delivers the rule in every measured missing and "
    "correction run",
    "quality": "over complete triples, J2P both-check passes >= FULL's - 1",
    "tokens": "over complete triples, J2P combined tokens <= 0.60 x FULL's",
    "supported": "all four targets hold",
    "secondary": "J2P versus J2 tokens isolates the pointer list",
}


def configure() -> None:
    """Point the Phase 2 driver at Phase 3 inputs, then apply it to the runner."""
    phase2.FIXTURES, phase2.ARMS, phase2.SETS = FIXTURES, ARMS, SETS
    phase2.CANDIDATE, phase2.SECONDARY = "J2P", "J2"
    phase2.EXPERIMENT, phase2.TARGETS = "context-pointer-phase-3", TARGETS
    phase2.PROTOCOL_VERSION = 6
    phase2.configure()
    run.SOURCE_MODEL_CALL_LIMIT = SOURCE_MODEL_CALL_LIMIT
    base = selector.base
    base.CATALOG_PART_LIMIT = CATALOG_PART_LIMIT
    base.CATALOG_BYTES_LIMIT = CATALOG_BYTES_LIMIT
    base.EVIDENCE_CALL_LIMIT = EVIDENCE_CALL_LIMIT
    if not getattr(base._read, "chunked", False):
        base._read = selector.read_chunked(base._read)
        base._read.chunked = True
    selector.FULL_CONTEXT_BYTES = CATALOG_BYTES_LIMIT + 1024
    identity = run.protocol_identity

    def protocol_identity(config: dict, task_set: str, transport_label: str) -> dict:
        value = identity(config, task_set, transport_label)
        value.update(
            phase3_driver_sha256=run.legacy.digest(Path(__file__).read_bytes()),
            limits={
                "catalog_parts": CATALOG_PART_LIMIT,
                "catalog_bytes": CATALOG_BYTES_LIMIT,
                "evidence_calls": EVIDENCE_CALL_LIMIT,
                "full_context_bytes": selector.FULL_CONTEXT_BYTES,
                "read_chunk": 32,
            },
        )
        return value

    run.protocol_identity = protocol_identity


if __name__ == "__main__":
    configure()
    raise SystemExit(run.main())
