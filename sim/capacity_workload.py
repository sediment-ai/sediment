# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded synthetic gateway traffic with complete per-Session histories.

Profiles describe synthetic populations and text bytes, not lines of code or
customer capacity. Each call constructs one envelope from a frozen wire capture.
The generator retains no corpus, and each envelope contains all earlier turns in
its Session. The cumulative text budget counts repeated inputs and outputs;
database storage and serialization add overhead beyond those text bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

_MIB = 1024 * 1024
_PROFILE_BYTES = 64 * 1024
_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "packages/capture/tests/fixtures/litellm_standard_logging_object.json"
)
_LIMITS = {
    "schema_version": (1, 1),
    "developers": (1, 1000),
    "history_weeks": (1, 26),
    "sessions_per_week": (1, 100000),
    "calls_per_session": (1, 1000),
    "history_bytes": (1, _MIB),
    "output_bytes": (1, _MIB),
    "live_interval_ms": (1, 60000),
    "max_live_calls": (1, 100000),
    "job_timeout_seconds": (1, 3600),
    "max_process_rss_mib": (1, 65536),
    "max_workspace_mib": (1, 1048576),
}


@dataclass(frozen=True)
class CapacityProfile:
    """Versioned dimensions and budgets for one synthetic rehearsal.

    ``sessions_per_week`` is the total across all developers. ``history_bytes``
    specifies each repeated ASCII user message, and ``output_bytes`` specifies
    each ASCII assistant response. Every later call repeats both prior bodies.
    ``history_weeks`` groups the historical population; it does not simulate
    elapsed ingestion time or repository activity over those weeks.
    """

    schema_version: int
    developers: int
    history_weeks: int
    sessions_per_week: int
    calls_per_session: int
    history_bytes: int
    output_bytes: int
    live_interval_ms: int
    max_live_calls: int
    job_timeout_seconds: int
    max_process_rss_mib: int
    max_workspace_mib: int

    def __post_init__(self) -> None:
        for name, (minimum, maximum) in _LIMITS.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"{name} must be an integer from {minimum} to {maximum}"
                )
        if self.total_calls > 100000:
            raise ValueError("total_calls must not exceed 100000")
        if self.developers > self.total_sessions:
            raise ValueError("developers must not exceed total_sessions")
        turns = self.calls_per_session
        repeated_bytes = (
            turns * (turns + 1) // 2 * (self.history_bytes + self.output_bytes)
        )
        if repeated_bytes > 256 * _MIB:
            raise ValueError(
                "cumulative input and output text per Session exceeds 256 MiB"
            )

    @property
    def total_sessions(self) -> int:
        return self.history_weeks * self.sessions_per_week

    @property
    def total_calls(self) -> int:
        return self.total_sessions * self.calls_per_session


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("capacity profile contains a duplicate field")
        result[key] = value
    return result


def load_profile(path: str | Path) -> CapacityProfile:
    """Read a small strict JSON profile before creating external resources."""
    with Path(path).open("rb") as source:
        content = source.read(_PROFILE_BYTES + 1)
    if len(content) > _PROFILE_BYTES:
        raise ValueError("capacity profile exceeds 64 KiB")
    try:
        values = json.loads(content, object_pairs_hook=_unique_fields)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise ValueError("capacity profile must contain valid JSON") from None
    if not isinstance(values, dict):
        raise ValueError("capacity profile must be a JSON object")
    expected = {field.name for field in fields(CapacityProfile)}
    if values.keys() - expected:
        raise ValueError("capacity profile contains unknown fields")
    if expected - values.keys():
        raise ValueError("capacity profile is missing required fields")
    return CapacityProfile(**values)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be an aware datetime")
    return value.astimezone(UTC)


def _index(profile: CapacityProfile, index: int, live: bool) -> None:
    if type(live) is not bool:
        raise ValueError("live must be a boolean")
    maximum = profile.max_live_calls if live else profile.total_calls
    if type(index) is not int or not 0 <= index < maximum:
        raise ValueError(f"index must be an integer from 0 to {maximum - 1}")


def historical_observed_at(
    profile: CapacityProfile, index: int, anchor: datetime
) -> datetime:
    """Spread calls evenly within ``[anchor - history_weeks, anchor)``.

    Each week contains ``sessions_per_week * calls_per_session`` observations.
    These injected timestamps provide deterministic cohort selection, not an
    accelerated simulation of capture, git, or CI activity through time.
    """
    _index(profile, index, False)
    anchor = _aware(anchor, "anchor")
    duration = timedelta(weeks=profile.history_weeks)
    return anchor - duration + duration * index / profile.total_calls


def _body(label: str, size: int) -> str:
    return (label * ((size + len(label) - 1) // len(label)))[:size]


def gateway_envelope(
    profile: CapacityProfile,
    index: int,
    *,
    observed_at: datetime,
    live: bool = False,
) -> dict:
    """Build one gateway envelope; replay uses the same capture and natural IDs."""
    _index(profile, index, live)
    observed_at = _aware(observed_at, "observed_at")
    namespace = "capacity/live" if live else "capacity/history"
    session_index, turn = divmod(index, profile.calls_per_session)
    session_id = f"{namespace}/session-{session_index:06d}"
    call_id = f"{namespace}/call-{index:06d}"
    user_id = f"capacity/developer-{session_index % profile.developers:04d}"
    user = _body("synthetic capacity user message ", profile.history_bytes)
    messages = []
    for previous in range(turn):
        previous_id = session_index * profile.calls_per_session + previous
        messages.extend(
            [
                {"role": "user", "content": user},
                {
                    "role": "assistant",
                    "content": _body(
                        f"{namespace}/call-{previous_id:06d} synthetic assistant ",
                        profile.output_bytes,
                    ),
                },
            ]
        )
    messages.append({"role": "user", "content": user})
    payload = json.loads(_FIXTURE.read_bytes())
    payload["id"] = payload["response"]["id"] = f"chatcmpl-{call_id}"
    payload["litellm_call_id"] = call_id
    payload["trace_id"] = session_id
    payload["end_user"] = user_id
    payload["messages"] = messages
    payload["response"]["choices"][0]["message"]["content"] = _body(
        f"{call_id} synthetic assistant ", profile.output_bytes
    )
    payload["startTime"] = observed_at.timestamp()
    payload["endTime"] = payload["startTime"] + payload["response_time"]
    payload["completionStartTime"] = payload["endTime"]
    payload["response"]["created"] = int(payload["startTime"])
    metadata = payload["metadata"]
    metadata["user_api_key_user_id"] = user_id
    metadata["user_api_key_end_user_id"] = user_id
    metadata["requester_metadata"] = {"session_id": session_id, "user_id": user_id}
    # Body sizes do not establish token usage or cost. Preserve their absence
    # instead of reporting the frozen fixture's unrelated measurements.
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "response_cost",
        "cost_breakdown",
        "saved_cache_cost",
    ):
        payload.pop(key, None)
    payload["response"].pop("usage", None)
    metadata.pop("usage_object", None)
    payload["hidden_params"].pop("usage_object", None)
    payload["hidden_params"].pop("response_cost", None)
    return {
        "provider": "litellm",
        "session_id": session_id,
        "user_id": user_id,
        "capture": {
            "id": str(uuid5(NAMESPACE_URL, call_id)),
            "observed_at": observed_at.isoformat(),
        },
        "payload": payload,
    }
