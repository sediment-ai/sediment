# SPDX-License-Identifier: AGPL-3.0-or-later
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bounded_evidence_selection as selector  # noqa: E402
import bounded_selection_eval as run  # noqa: E402
import context_minimal_eval as phase2  # noqa: E402
import context_pointer_eval as phase3  # noqa: E402

RUNNER = (
    "FIXTURES",
    "EVALUATION",
    "ARMS",
    "SETS",
    "run_order",
    "summarize",
    "GATE_UPSTREAM_RETRY",
    "PI_IDLE_TIMEOUT_MS",
    "EXCLUDE_UPSTREAM_UNAVAILABLE",
    "CONTEXT_WINDOW",
    "SOURCE_MODEL_CALL_LIMIT",
    "protocol_identity",
)
PHASE2 = (
    "FIXTURES",
    "ARMS",
    "SETS",
    "CANDIDATE",
    "SECONDARY",
    "EXPERIMENT",
    "TARGETS",
    "PROTOCOL_VERSION",
)
BASE = ("CATALOG_PART_LIMIT", "CATALOG_BYTES_LIMIT", "EVIDENCE_CALL_LIMIT", "_read")


def test_configure_binds_phase_3_arms_limits_and_identity(monkeypatch, tmp_path):
    for module, names in ((run, RUNNER), (phase2, PHASE2), (selector.base, BASE)):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(selector, "FULL_CONTEXT_BYTES", selector.FULL_CONTEXT_BYTES)
    monkeypatch.setattr(selector, "J2_POLICY_VERSION", selector.J2_POLICY_VERSION)
    phase3.configure()
    order = run.run_order()
    assert len(order) == 36
    assert Counter(s["arm"] for s in order) == dict.fromkeys(phase3.ARMS, 12)
    assert {s["family"] for s in order} == set(phase3.SETS["heldout"]["families"])
    assert selector.base.CATALOG_PART_LIMIT == 96 and selector.base._read.chunked
    runtime = tmp_path / "runtime.json"
    runtime.write_text("{}")
    config = {
        "model": "m",
        "agent_image": "a",
        "gate_image": "g",
        "gateway_url": "u",
        "api_url": "u",
        "operator_api_url": "u",
        "runtime_identity_path": runtime,
    }
    identity = run.protocol_identity(config, "heldout", "direct")
    assert identity["experiment"] == "context-pointer-phase-3"
    assert identity["protocol_version"] == 7
    assert identity["policy"]["j2"]["policy_version"] == 3
    assert "relevant >= 0.80" in identity["policy"]["j2"]["qualify"]
    assert identity["arms"] == list(phase3.ARMS)
    assert identity["limits"]["catalog_bytes"] == 131_072
    assert identity["generation"]["source_model_calls"] == 40
    assert identity["generation"]["coding_model_calls"] == 12
    assert "evaluation/verify.py" in identity["fixture_hashes"]
    assert run.summarize([], "heldout")["candidate"] == "J2P"
