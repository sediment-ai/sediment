# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable identities for canonical serialized training-row contracts."""

from __future__ import annotations

from typing import Literal

SCHEMA_ID_BASE = "https://sediment.so/schemas"
CANONICAL_SCHEMA_VERSION: Literal[1] = 1
AGENT_HARNESS_SCHEMA_VERSION: Literal[2] = 2


def canonical_schema_id(family: str, slug: str, version: int = 1) -> str:
    """Return the stable URI for one canonical schema contract."""
    return f"{SCHEMA_ID_BASE}/{family}/{slug}/v{version}.json"


DPO_PAIR_SCHEMA_VERSION: Literal[4] = 4
SFT_SAMPLE_SCHEMA_VERSION: Literal[3] = 3
DIFF_SFT_SAMPLE_SCHEMA_VERSION: Literal[3] = 3
RECOVERY_ROW_SCHEMA_VERSION: Literal[3] = 3
SEDIMENT_TASK_ROW_SCHEMA_VERSION: Literal[3] = 3
SEDIMENT_ROLLOUT_ROW_SCHEMA_VERSION: Literal[4] = 4
SWE_BENCH_TASK_ROW_SCHEMA_VERSION: Literal[3] = 3
NEMO_GYM_ROLLOUT_ROW_SCHEMA_VERSION: Literal[4] = 4

DPO_PAIR_SCHEMA_ID = canonical_schema_id(
    "training-rows", "dpo-pair", DPO_PAIR_SCHEMA_VERSION
)
SFT_SAMPLE_SCHEMA_ID = canonical_schema_id(
    "training-rows", "sft-sample", SFT_SAMPLE_SCHEMA_VERSION
)
DIFF_SFT_SAMPLE_SCHEMA_ID = canonical_schema_id(
    "training-rows", "diff-sft-sample", DIFF_SFT_SAMPLE_SCHEMA_VERSION
)
RECOVERY_ROW_SCHEMA_ID = canonical_schema_id(
    "training-rows", "recovery-row", RECOVERY_ROW_SCHEMA_VERSION
)
SEDIMENT_TASK_ROW_SCHEMA_ID = canonical_schema_id(
    "training-rows", "sediment-rlvr-task", SEDIMENT_TASK_ROW_SCHEMA_VERSION
)
SEDIMENT_ROLLOUT_ROW_SCHEMA_ID = canonical_schema_id(
    "training-rows", "sediment-rlvr-rollout", SEDIMENT_ROLLOUT_ROW_SCHEMA_VERSION
)
SWE_BENCH_TASK_ROW_SCHEMA_ID = canonical_schema_id(
    "training-rows", "swe-bench-task", SWE_BENCH_TASK_ROW_SCHEMA_VERSION
)
NEMO_GYM_ROLLOUT_ROW_SCHEMA_ID = canonical_schema_id(
    "training-rows", "nemo-gym-rollout", NEMO_GYM_ROLLOUT_ROW_SCHEMA_VERSION
)
