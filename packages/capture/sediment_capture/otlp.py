# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Translate OTLP/JSON *log* exports from coding agents into ``DeveloperDecision``
facts. Committed sanitized fixtures cover Claude Code 2.1.200, Copilot Chat,
and Codex CLI 0.142.5 wire payloads.

Pure payload→fact translation — no I/O, no storage, no attribution at ingest
(ADR 0001). ``org_id`` arrives as a parameter: the payload's own org
attributes are untrusted and never read for tenancy; binding the tenant to
the ingest credential is the routes' job.

The generic plumbing here (``_as_list``, ``_scalar``, ``_attrs``,
``_iter_records``, ``_occurred_at``, ``_body``) is source-agnostic. Per-agent
knowledge lives in *translator* functions listed in ``_TRANSLATORS``;
``parse_otlp_decisions`` concatenates them. ``parse_otlp_logs`` calls all four
Fact parsers and returns their Facts with record and container counts.

A translator is a pure function ``(payload, *, org_id) -> list[DeveloperDecision]``:

  - No I/O; unit-tested against committed fixtures.
  - Takes the **whole batch payload** (decision/result pairs correlate by call
    id within a batch), not a single record.
  - Self-filters by event name — it silently skips events it doesn't
    recognize. There is no central dispatch; each translator owns its match.
  - Best-effort per record: one malformed record never sinks the batch.
  - Accepts the private ``_translated_records`` set and marks its object-only
    record position after emitting a Fact. Several emitted Facts still mark
    one position. Supporting tool-result records emit no Facts themselves.

Record counts partition every supplied ``logRecords`` list entry: translated
(one or more Facts), untranslated (an object emitting none), or malformed
(not an object). The envelope walker counts non-list repeated fields and
non-object resource/scope entries separately as malformed containers, without
guessing how many records they hide. Missing repeated fields mean empty lists.

Two fields are mandatory:

  - ``occurred_at`` maps from the record's ``timeUnixNano`` (Codex:
    ``observedTimeUnixNano`` fallback) and is part of the dedup key — a
    record with no parseable event time is skipped with a warning, never
    backfilled from ingest time (that would break redelivery collapse).
  - ``session_id`` comes from OTel ``session.id`` / ``conversation.id``.
    Placeholder session ids are banned (ADR 0002): a record without one is a
    capture bug to surface, so it is skipped with a warning, not stored as
    "unknown".

Native Cursor supplies no execution timestamp. Its adapter stamps hook receipt
time in ``timeUnixNano``; capture preserves that time without inventing an earlier
execution time. For implicit successful ``Write`` records with a validated call
id, ``decision_id`` is ``cursor-write-v1:`` followed by the SHA-256 hex digest of
compact ASCII JSON encoding ``(org_id, agent_harness, session_id, call_id,
file_path, accepted, explicit, interaction_mode, "Write")`` after Fact validation.
The existing PostgreSQL primary key preserves the first receipt, independent of
later receipt times or user metadata. Other shim records keep their existing
identity behavior. Historical random-ID Facts stay immutable: a later receipt
can coexist once across the upgrade boundary; subsequent receipts collapse.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    RejectedEdit,
    RetryLinkage,
    InteractionMode,
    normalize_commit_sha,
)

logger = logging.getLogger("sediment.capture.otlp")

# OTLP attribute keys (record scope unless noted).
_FILE = "copilot_chat.file.relative_path"
_COMMIT = "github.copilot.git.commit_sha"
_SESSION = "session.id"  # Copilot: resource scope; Claude Code: record scope
_USER = "user.id"  # resource scope (from OTEL_RESOURCE_ATTRIBUTES, if set)


def _as_list(value: Any) -> list[Any]:
    """OTLP input is untrusted — coerce a missing/non-list field to []."""
    return value if isinstance(value, list) else []


def _scalar(value: Any) -> Any:
    """Unwrap an OTLP AnyValue to a Python scalar; keep raw for arrays/kvlists."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:  # OTLP/JSON encodes int64 as a string
        try:
            return int(value["intValue"])
        except (TypeError, ValueError, OverflowError):
            return value["intValue"]
    return value


def _attrs(items: Any) -> dict[str, Any]:
    """Flatten an OTLP attribute list to {key: scalar}, tolerating junk.

    Skips any item that isn't a ``{"key": <str>, "value": ...}`` dict so one
    malformed attribute can't raise out of the per-record handling — a
    non-string (possibly unhashable) crafted key must be dropped, not raise
    TypeError at insertion.
    """
    out: dict[str, Any] = {}
    for item in _as_list(items):
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = _scalar(item.get("value", {}))
    return out


def _occurred_at(time_unix_nano: Any) -> datetime | None:
    """Map an OTLP timestamp to event time; None when absent or unparseable.

    Zero is proto3's "unset" (Codex serializes it as the string "0"), so
    non-positive values are unset — never the 1970 epoch, which would corrupt
    the dedup key occurred_at is part of. OverflowError/OSError from a
    crafted out-of-range value must skip the record, not sink the batch.
    """
    if isinstance(time_unix_nano, bool):  # bool is an int; True → epoch+1ns
        return None
    try:
        nanos = int(time_unix_nano)
    except (TypeError, ValueError, OverflowError):
        return None
    if nanos <= 0:
        return None
    try:
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _id_or_none(value: Any) -> str | None:
    """A nullable join id: non-string or blank degrades to None — absent,
    never a padded id that raises NonEmptyId at the schema and sinks the whole
    record. For nullable id fields (call_id, user_id); a required id
    (EditObservation.call_id, the dedup key) keeps the record-level skip."""
    value = _str_or_none(value)
    if value is None:
        return None
    return value.strip() or None


def _count_or_none(value: Any) -> int | None:
    """A nullable non-negative line count.

    Absent is the common case and means "no window covered this call" — not
    zero. Junk from an untrusted sender (a string, a float, a negative, or a
    value past int64) degrades to absent too rather than raising at the
    schema and sinking a record whose text pair is perfectly good. The upper
    bound mirrors the model's for exactly that reason: without it the oversized
    value reaches ``EditObservation`` and drops the whole record.
    ``bool`` is an ``int`` in Python, so it is excluded explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > 2**63 - 1:
        return None
    return value


def _str_or_none(value: Any) -> str | None:
    """Non-empty str or None. OTLP attr values are untrusted: an intValue
    where a string belongs must degrade (fallback / None / skip-with-trail),
    never raise at fact construction."""
    return value if isinstance(value, str) and value else None


def _required(value: Any, field: str, source: str, org_id: str) -> Any | None:
    """Gate a mandatory identity field: a missing/empty/junk value means the
    record is skipped (with a trail) — never papered over with a placeholder
    (ADR 0002) or backfilled (occurred_at is part of the dedup key)."""
    if value:
        return value
    logger.warning(
        "otlp_record_missing_%s", field, extra={"org_id": org_id, "source": source}
    )
    return None


def _iter_records(
    payload: dict[str, Any],
    *,
    counts: Counter[str] | None = None,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Walk the OTLP envelope, yielding (resource_attrs, log_record) pairs.

    Tolerates untrusted input: non-list levels become empty, non-dict entries
    are skipped. Isolates the structural walk from the record→fact mapping.
    """
    counts = counts if counts is not None else Counter()

    def items(container: dict[str, Any], key: str) -> list[Any]:
        value = container.get(key, [])
        if not isinstance(value, list):
            counts["malformed_containers"] += 1
            return []
        return value

    for resource_logs in items(payload, "resourceLogs"):
        if not isinstance(resource_logs, dict):
            counts["malformed_containers"] += 1
            continue
        resource_obj = resource_logs.get("resource")
        resource = _attrs(
            resource_obj.get("attributes") if isinstance(resource_obj, dict) else None
        )
        for scope_logs in items(resource_logs, "scopeLogs"):
            if not isinstance(scope_logs, dict):
                counts["malformed_containers"] += 1
                continue
            for record in items(scope_logs, "logRecords"):
                if isinstance(record, dict):
                    yield resource, record
                else:
                    counts["records_malformed"] += 1


def _body(record: dict[str, Any]) -> str:
    """The record body as a string; the discriminator for every wire that
    namespaces its event name there rather than in an attribute."""
    value = _scalar(record.get("body"))
    return value if isinstance(value, str) else ""


def _sediment_identity(
    resource: dict[str, Any], record: dict[str, Any], attrs: dict[str, Any], org_id: str
) -> tuple[Any, Any, Any, Any]:
    """The identity every sediment-wire record must carry:
    ``(occurred_at, session_id, call_id, file_path)``. Each is gated by
    ``_required``, so a missing one comes back None with a trail and the
    caller skips the record — never a placeholder (ADR 0002)."""
    return (
        _required(_occurred_at(record.get("timeUnixNano")), "time", "sediment", org_id),
        _required(
            _str_or_none(attrs.get(_SESSION)) or _str_or_none(resource.get(_SESSION)),
            "session",
            "sediment",
            org_id,
        ),
        _required(
            _str_or_none(attrs.get("tool_use_id")), "call_id", "sediment", org_id
        ),
        _required(
            _str_or_none(attrs.get("file_path")), "file_path", "sediment", org_id
        ),
    )


def _outcome(
    event_name: str, attrs: dict[str, Any]
) -> tuple[bool, bool, InteractionMode] | None:
    """Return (accepted, explicit, interaction mode) for a Copilot decision."""
    if event_name == "copilot_chat.edit.feedback":
        outcome = attrs.get("outcome")
        if outcome not in ("accepted", "rejected"):
            return None  # e.g. "saved" — not an accept/reject decision
        interaction_mode = (
            InteractionMode.INLINE
            if attrs.get("edit_surface") == "inline_chat"
            else InteractionMode.AGENT
        )
        return outcome == "accepted", True, interaction_mode
    if event_name == "copilot_chat.inline.done":
        accepted = attrs.get("accepted")
        if not isinstance(accepted, bool):  # untrusted: require a real bool
            return None
        return accepted, True, InteractionMode.INLINE
    if event_name == "copilot_chat.edit.survival":
        # Implicit-accept backstop for agent (apply_patch) edits only; inline
        # survival would double-count with copilot_chat.inline.done.
        if attrs.get("edit_source") != "apply_patch":
            return None
        rate = attrs.get("survival_rate_no_revert")
        if not isinstance(rate, (int, float)) or isinstance(rate, bool):
            return None  # malformed vendor retention rate — don't guess
        # Flattened to a bool here; the graded survival_rate_four_gram value
        # is captured separately onto DeveloperDecision.edit_retention_score by
        # _edit_retention_fields — this boolean is not derived from it. An explicit
        # decision and an implicit retention-inferred one for the same edit are
        # separate facts by design: weighing them and consuming the retention
        # score as continuous preference strength is derivation work, not capture.
        return rate > 0, False, InteractionMode.AGENT
    return None


def _edit_retention_fields(
    attrs: dict[str, Any],
) -> tuple[float | None, int | None]:
    """Extract Copilot's graded edit-retention signal from a
    ``copilot_chat.edit.survival`` record's attributes.

    ``survival_rate_four_gram`` is the n-gram overlap between the AI output
    and the developer's subsequent edit, in [0, 1] — the only
    vendor-supplied *graded* edit-retention signal across all three agents.
    ``_outcome`` computes the ``accepted = rate > 0`` boolean from the separate
    ``survival_rate_no_revert`` field. ``time_delay_ms`` is the bucket the rate was measured at —
    0/5s/30s/2m/5m per the Copilot client source; only the bucket-0 record
    is wire-captured so far (the committed fixture), and whether the later
    buckets carry distinct ``timeUnixNano`` values is unverified, which is
    why the bucket sits in the store's natural dedup key defensively. A
    missing/malformed rate degrades to ``(None, None)`` rather than
    guessing; a present rate with a missing/malformed window keeps the rate
    and drops only the window.

    Malformed includes hostile numerics, not just wrong types: ``json.loads``
    accepts ``NaN``/``Infinity`` literals, ``int(nan)``/``int(inf)`` raise,
    and a finite-but-huge window overflows PostgreSQL BIGINT at bind
    time — any of which would 500 the whole batch instead of degrading one
    field. So the rate must be finite and in [0, 1] (its documented domain,
    enforced on the model too) and the window finite and in [0, 2**63).
    """
    rate = attrs.get("survival_rate_four_gram")
    # Integers are finite without float conversion, which can itself overflow.
    if (
        not isinstance(rate, (int, float))
        or isinstance(rate, bool)
        or (isinstance(rate, float) and not math.isfinite(rate))
        or not 0.0 <= rate <= 1.0
    ):
        return None, None
    window = attrs.get("time_delay_ms")
    if (
        not isinstance(window, (int, float))
        or isinstance(window, bool)
        or (isinstance(window, float) and not math.isfinite(window))
        or not 0 <= window < 2**63
    ):
        window = None
    else:
        window = int(window)
    return float(rate), window


def _copilot_decisions(
    payload: dict[str, Any],
    *,
    org_id: str,
    _translated_records: set[int] | None = None,
) -> list[DeveloperDecision]:
    """Translate GitHub Copilot Chat OTLP logs into DeveloperDecisions.

    Copilot exports the accept/reject decision as OTLP **logs** (not metrics),
    discriminated by an ``event.name`` attribute. Three events carry a usable
    decision:

      - ``copilot_chat.edit.feedback`` — explicit accept/reject (``outcome``)
      - ``copilot_chat.inline.done`` — explicit inline accept/reject (``accepted``)
      - ``copilot_chat.edit.survival`` — implicit accept (``survival_rate_no_revert``);
        also the only event carrying the graded ``edit_retention_score``/
        ``observation_delay_ms`` fields (from ``survival_rate_four_gram`` /
        ``time_delay_ms``)

    All other event names (``gen_ai.*``, ``tool.call``, ``agent.turn``, …) are
    ignored. Best-effort per record — one bad record never sinks the batch.
    """
    decisions: list[DeveloperDecision] = []
    for position, (resource, record) in enumerate(_iter_records(payload)):
        attrs = _attrs(record.get("attributes"))
        event_name = str(attrs.get("event.name", ""))
        mapped = _outcome(event_name, attrs)
        if mapped is None:
            continue
        accepted, explicit, interaction_mode = mapped
        edit_retention_score, observation_delay_ms = (
            _edit_retention_fields(attrs)
            if event_name == "copilot_chat.edit.survival"
            else (None, None)
        )

        # Require a real file path (wire-verified: all three events carry
        # one): skip rather than store junk (file_path="") that would also
        # weaken the dedup key, which includes file_path.
        file_path = _required(
            _str_or_none(attrs.get(_FILE)), "file_path", "copilot", org_id
        )
        occurred = _required(
            _occurred_at(record.get("timeUnixNano")), "time", "copilot", org_id
        )
        session_id = _required(
            _str_or_none(resource.get(_SESSION)), "session", "copilot", org_id
        )
        if file_path is None or occurred is None or session_id is None:
            continue

        # A junk sha degrades to absent rather than sinking the decision:
        # commit_sha is nullable and DeveloperDecision validates it — absent,
        # never guessed.
        commit_sha = _str_or_none(attrs.get(_COMMIT))
        if commit_sha is not None:
            try:
                commit_sha = normalize_commit_sha(commit_sha)
            except ValueError:
                logger.warning(
                    "copilot_invalid_commit_sha",
                    extra={
                        "org_id": org_id,
                        "call_id": _str_or_none(attrs.get("request_id")),
                    },
                )
                commit_sha = None

        try:
            decisions.append(
                DeveloperDecision(
                    org_id=org_id,
                    session_id=session_id,
                    user_id=_id_or_none(resource.get(_USER)),
                    agent_harness=AgentHarness.COPILOT,
                    file_path=file_path,
                    accepted=accepted,
                    explicit=explicit,
                    interaction_mode=interaction_mode,
                    commit_sha=commit_sha,
                    call_id=_id_or_none(attrs.get("request_id")),
                    edit_retention_score=edit_retention_score,
                    observation_delay_ms=observation_delay_ms,
                    occurred_at=occurred,
                    raw=record,
                )
            )
            if _translated_records is not None:
                _translated_records.add(position)
        except Exception:  # malformed record — skip, don't sink the batch
            logger.warning(
                "otlp_record_invalid",
                extra={
                    "org_id": org_id,
                    "source": "copilot",
                    "call_id": _str_or_none(attrs.get("request_id")),
                },
            )
    return decisions


# Claude Code wires. The namespaced event name lives in the record BODY
# ("claude_code.tool_decision"); the event.name attribute is the bare
# "tool_decision", which another agent could also use — so translators
# discriminate on the body.
_CC_DECISION = "claude_code.tool_decision"
_CC_RESULT = "claude_code.tool_result"
# MultiEdit no longer exists as a tool in Claude Code 2.x (wire-verified) but is
# kept for older clients; NotebookEdit's input uses notebook_path, not file_path.
_CC_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# decision "source" values where a human decided at the permission prompt.
# config (allowedTools / acceptEdits / bypassPermissions) and hook are automatic.
_CC_EXPLICIT_SOURCES = frozenset(
    {"user_permanent", "user_temporary", "user_abort", "user_reject"}
)
# The automatic complement — the two sets are the closed vocabulary. A value
# in neither maps to implicit (fail-safe: never a fabricated human gesture)
# but logs, so new wire vocabulary — auto mode's classifier verdicts,
# defaulting 2026-08-14 — is visible on day one. Compared raw, not
# casefolded: every wire-verified value is lowercase snake_case, and the
# Codex casefold exists only to absorb that wire's Capitalization quirk.
_CC_IMPLICIT_SOURCES = frozenset({"config", "hook"})


def _cc_file_path(result_attrs: dict[str, Any]) -> str:
    """Recover file_path from a tool_result's serialized ``tool_input``.

    Requires OTEL_LOG_TOOL_DETAILS=1 on the client. Long attribute values are
    truncated in-place ("…[N chars]") but the JSON stays valid and file_path
    survives (wire-verified); a path we can't parse degrades to "".
    """
    tool_input = result_attrs.get("tool_input")
    if not isinstance(tool_input, str):
        return ""
    try:
        parsed = json.loads(tool_input)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    path = parsed.get("file_path") or parsed.get("notebook_path")
    return path if isinstance(path, str) else ""


def _claude_code_decisions(
    payload: dict[str, Any],
    *,
    org_id: str,
    _translated_records: set[int] | None = None,
) -> list[DeveloperDecision]:
    """Translate Claude Code OTLP logs (``claude_code.tool_decision``).

    ``tool_decision`` carries ground-truth accept/reject per tool call but no
    file path; the path is joined from the ``tool_result`` sharing the same
    ``tool_use_id`` **within the batch**. Two wire-verified gaps degrade to
    ``file_path=""`` rather than dropping the decision (rejects are the
    scarce, high-value half of the reward):

      - a rejected tool call never emits a ``tool_result`` at all;
      - a decision and its result can straddle an export-batch boundary.

    ``call_id`` (= ``tool_use_id``, unique per call) keeps the dedup key
    precise despite the empty path.
    """
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    decisions: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for position, (resource, record) in enumerate(_iter_records(payload)):
        body = _body(record)
        if body == _CC_RESULT:
            attrs = _attrs(record.get("attributes"))
            tool_use_id = attrs.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                results[tool_use_id] = (record, attrs)
        elif body == _CC_DECISION:
            decisions.append(
                (position, resource, record, _attrs(record.get("attributes")))
            )

    out: list[DeveloperDecision] = []
    for position, resource, record, attrs in decisions:
        tool_name = attrs.get("tool_name")
        if not isinstance(tool_name, str) or tool_name not in _CC_EDIT_TOOLS:
            logger.info(
                "otlp_record_unsupported_tool",
                extra={
                    "org_id": org_id,
                    "source": "claude-code",
                    "record_position": position,
                    "reason": "unsupported_discriminator"
                    if isinstance(tool_name, str)
                    else "malformed_discriminator",
                },
            )
            continue
        tool_use_id = _str_or_none(attrs.get("tool_use_id"))
        decision = attrs.get("decision")
        if not isinstance(decision, str) or decision not in ("accept", "reject"):
            logger.warning(
                "otlp_record_unknown_decision",
                extra={
                    "org_id": org_id,
                    "source": "claude-code",
                    "call_id": tool_use_id,
                    "decision": decision,
                    "record_position": position,
                    "reason": "unsupported_discriminator"
                    if isinstance(decision, str)
                    else "malformed_discriminator",
                },
            )
            continue
        occurred = _required(
            _occurred_at(record.get("timeUnixNano")), "time", "claude-code", org_id
        )
        # session.id/user.id are record-scope on this wire (the record user.id
        # is an anonymous per-install hash; the resource user.id comes from
        # OTEL_RESOURCE_ATTRIBUTES and is the reliable identity when set).
        session_id = _required(
            _str_or_none(attrs.get(_SESSION)) or _str_or_none(resource.get(_SESSION)),
            "session",
            "claude-code",
            org_id,
        )
        if occurred is None or session_id is None:
            continue
        result = results.get(tool_use_id) if tool_use_id else None
        decision_source = attrs.get("source")
        explicit = (
            isinstance(decision_source, str) and decision_source in _CC_EXPLICIT_SOURCES
        )
        if not isinstance(decision_source, str) or (
            not explicit and decision_source not in _CC_IMPLICIT_SOURCES
        ):
            logger.warning(
                "otlp_record_unknown_source",
                extra={
                    "org_id": org_id,
                    "source": "claude-code",
                    "call_id": tool_use_id,
                    "decision_source": decision_source,
                    "record_position": position,
                    "reason": "unsupported_discriminator"
                    if isinstance(decision_source, str)
                    else "malformed_discriminator",
                },
            )
        try:
            out.append(
                DeveloperDecision(
                    org_id=org_id,
                    session_id=session_id,
                    user_id=_id_or_none(resource.get(_USER))
                    or _id_or_none(attrs.get(_USER)),
                    agent_harness=AgentHarness.CLAUDE_CODE,
                    file_path=_cc_file_path(result[1]) if result else "",
                    accepted=decision == "accept",
                    explicit=explicit,
                    interaction_mode=InteractionMode.AGENT,
                    call_id=_id_or_none(tool_use_id),
                    occurred_at=occurred,
                    raw={"decision": record, "result": result[0] if result else None},
                )
            )
            if _translated_records is not None:
                _translated_records.add(position)
        except ValidationError:
            # Source values are type-gated above; only Fact validation remains.
            logger.warning(
                "otlp_record_invalid",
                extra={
                    "org_id": org_id,
                    "source": "claude-code",
                    "call_id": tool_use_id,
                    "record_position": position,
                    "reason": "invalid_fact",
                },
            )
    return out


# Codex CLI wires (captured live against Codex 0.142.5). Unlike Claude Code,
# the namespaced name is a record ATTRIBUTE ("codex.tool_decision"), not the
# body; decision/result pair by ``call_id``
# within a batch. Two wire-format details the translator must handle (both
# would otherwise corrupt or drop every Codex decision):
#   - ``source`` is Capitalized on the wire ("Config"/"User"), so compare
#     case-folded;
#   - ``timeUnixNano`` is "0" — the real time is in ``observedTimeUnixNano`` —
#     and occurred_at must never collapse to the 1970 epoch (it is part of the
#     dedup key), so we fall back to the observed time.
_CX_DECISION = "codex.tool_decision"
_CX_RESULT = "codex.tool_result"
_CX_CONV = "conversation.id"
_CX_PATCH_TOOL = "apply_patch"
_CX_SHELL_TOOL = "exec_command"
# The terminal verdicts the apply_patch path emits (codex-rs ReviewDecision
# to_opaque_string). Non-terminal variants — "timed_out" (auto-review
# timeout), execpolicy/MCP/network amendments — are not routine for edit
# tools, so they skip with otlp_record_unknown_decision, not a quiet-list.
_CX_ACCEPT = frozenset({"approved", "approved_for_session"})
_CX_REJECT = frozenset({"denied", "abort"})
# Per-developer identity Codex puts on every record. We derive user_id from the
# resource scope, never these — and the client doc promises they are not stored,
# so they must not survive into the persisted ``raw`` payload either.
_CX_PII = frozenset({"user.email", "user.account_id"})


def _cx_scrub(attrs: Any) -> Any:
    """Drop per-developer PII from a flattened attrs dict for the ``raw`` field.

    ``raw`` is opaque audit provenance; it must honour the same privacy promise
    as the extracted fields. Passes ``None`` through so a missing
    ``tool_result`` stays ``None``.
    """
    if not isinstance(attrs, dict):
        return attrs
    return {k: v for k, v in attrs.items() if k not in _CX_PII}


def _files_from_v4a(arguments: Any) -> list[str]:
    """Extract file paths from an apply_patch V4A diff (``tool_result.arguments``).

    Paths follow the ``*** Add/Update/Delete File: <path>`` markers, matched
    at column 0 only: V4A body lines are prefixed (space for context, +/- for
    edits), so a stripped match would fabricate phantom paths out of patched
    content that merely quotes a marker. Order is preserved and duplicates
    collapsed; a non-string or path-less diff yields ``[]`` (the caller then
    emits one path-less decision).
    """
    if not isinstance(arguments, str):
        return []
    markers = ("*** Update File: ", "*** Add File: ", "*** Delete File: ")
    paths: list[str] = []
    for line in arguments.splitlines():
        for marker in markers:
            if line.startswith(marker):
                path = line[len(marker) :].strip()
                if path and path not in paths:
                    paths.append(path)
                break
    return paths


def _v4a_from_shell_arguments(arguments: Any) -> str | None:
    """Return a V4A diff from an ``exec_command`` apply_patch invocation.

    OpenAI-compatible providers can expose Codex's patch operation through the
    shell tool. Match only a command whose first nonblank line invokes
    ``apply_patch`` with a heredoc. A command that mentions or prints patch text
    is not an edit decision.
    """
    if not isinstance(arguments, str):
        return None
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    command = parsed.get("cmd")
    if not isinstance(command, str):
        return None
    first_line = command.lstrip().partition("\n")[0]
    if not first_line.startswith("apply_patch <<"):
        return None
    return command


def _codex_decisions(
    payload: dict[str, Any],
    *,
    org_id: str,
    _translated_records: set[int] | None = None,
) -> list[DeveloperDecision]:
    """Translate Codex CLI OTLP logs (``codex.tool_decision``).

    ``tool_decision`` carries the verdict per tool call but no file path. The
    path comes from the V4A diff in the matching ``tool_result``. Codex can
    carry that diff in its native ``apply_patch`` tool or in a shell-wrapped
    ``apply_patch`` sent through ``exec_command``. One patch can touch several
    files → one decision per file. ``source=user`` (reviewed approval) is an
    explicit accept, ``source=config`` (auto-approve) an implicit one.

    The supported Codex interactive patch-approval UI offers **no plain
    deny** — its only reject option ("No, and tell Codex what to do
    differently", esc) interrupts the turn before the approval resolves, so an
    interactive reject emits no ``tool_decision``. The
    ``denied``/``abort`` emit path does exist upstream (codex-rs
    ``tools/orchestrator.rs`` emits whatever ``ReviewDecision`` resolves,
    lowercased) and is reachable via permission-request hooks / policy / the
    automated reviewer — so the reject branch below is kept, degrading to one
    ``file_path=""`` decision (the reject convention shared with Claude Code).
    Non-patch tools (``exec_command`` reads) are ignored. Best-effort per
    record.
    """
    results: dict[str, dict[str, Any]] = {}
    decisions: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for position, (resource, record) in enumerate(_iter_records(payload)):
        attrs = _attrs(record.get("attributes"))
        name = attrs.get("event.name")
        if name == _CX_RESULT:
            call_id = attrs.get("call_id")
            if isinstance(call_id, str) and call_id:
                results[call_id] = attrs
        elif name == _CX_DECISION:
            decisions.append((position, resource, record, attrs))

    out: list[DeveloperDecision] = []
    for position, resource, record, attrs in decisions:
        tool_name = attrs.get("tool_name")
        if not isinstance(tool_name, str) or tool_name not in {
            _CX_PATCH_TOOL,
            _CX_SHELL_TOOL,
        }:
            logger.info(
                "otlp_record_unsupported_tool",
                extra={
                    "org_id": org_id,
                    "source": "codex",
                    "record_position": position,
                    "reason": "unsupported_discriminator"
                    if isinstance(tool_name, str)
                    else "malformed_discriminator",
                },
            )
            continue
        decision = attrs.get("decision")
        if isinstance(decision, str) and decision in _CX_ACCEPT:
            accepted = True
        elif isinstance(decision, str) and decision in _CX_REJECT:
            accepted = False
        else:
            logger.warning(
                "otlp_record_unknown_decision",
                extra={
                    "org_id": org_id,
                    "source": "codex",
                    "call_id": _str_or_none(attrs.get("call_id")),
                    "decision": decision,
                    "record_position": position,
                    "reason": "unsupported_discriminator"
                    if isinstance(decision, str)
                    else "malformed_discriminator",
                },
            )
            continue  # unknown verdict — don't guess
        source = attrs.get("source")
        explicit = isinstance(source, str) and source.casefold() == "user"
        call_id = _str_or_none(attrs.get("call_id"))
        result = results.get(call_id) if call_id else None
        patch = result.get("arguments") if result else None
        if tool_name == _CX_SHELL_TOOL:
            patch = _v4a_from_shell_arguments(patch)
            if patch is None:
                continue
        files = _files_from_v4a(patch)
        # Codex sends timeUnixNano="0" (unset — _occurred_at maps it to None);
        # the real event time is in observedTimeUnixNano.
        occurred = _occurred_at(record.get("timeUnixNano")) or _occurred_at(
            record.get("observedTimeUnixNano")
        )
        occurred = _required(occurred, "time", "codex", org_id)
        session_id = _required(
            _str_or_none(attrs.get(_CX_CONV)) or _str_or_none(resource.get(_CX_CONV)),
            "session",
            "codex",
            org_id,
        )
        if occurred is None or session_id is None:
            continue
        for file_path in files or [""]:  # one decision per patched file
            try:
                out.append(
                    DeveloperDecision(
                        org_id=org_id,
                        session_id=session_id,
                        user_id=_id_or_none(resource.get(_USER)),
                        agent_harness=AgentHarness.CODEX,
                        file_path=file_path,
                        accepted=accepted,
                        explicit=explicit,
                        interaction_mode=InteractionMode.AGENT,
                        call_id=_id_or_none(call_id),
                        occurred_at=occurred,
                        # Store scrubbed flattened attrs (not the raw record):
                        # per-developer identity (user.email/account_id) must
                        # not be persisted.
                        raw={
                            "decision": _cx_scrub(attrs),
                            "result": _cx_scrub(result),
                        },
                    )
                )
                if _translated_records is not None:
                    _translated_records.add(position)
            except ValidationError:
                # Source values are type-gated above; only Fact validation remains.
                logger.warning(
                    "otlp_record_invalid",
                    extra={
                        "org_id": org_id,
                        "source": "codex",
                        "call_id": call_id,
                        "record_position": position,
                        "reason": "invalid_fact",
                    },
                )
    return out


# The harness-neutral decision wire (docs/agents/capture-clients.md): shims
# emit Sediment's own event shape instead of impersonating a vendor-native
# vocabulary, so this one translator serves every shim. Agent identity rides
# the mandatory ``agent`` attribute, never the event name.
_SD_DECISION = "sediment.tool_decision"


def _agent_harness(
    value: Any, *, org_id: str, call_id: str | None
) -> AgentHarness | None:
    """Map the ``agent`` attribute to a registered ``AgentHarness``.

    Missing or unregistered values skip the record at the trust boundary:
    schema is the source of truth, so an unregistered agent is a capture bug
    to surface — never a fact persisted under a placeholder agent harness.
    """
    agent = _required(_str_or_none(value), "agent", "sediment", org_id)
    if agent is None:
        return None
    try:
        return AgentHarness(agent)
    except ValueError:
        logger.warning(
            "otlp_record_unknown_agent",
            extra={"org_id": org_id, "source": "sediment", "call_id": call_id},
        )
        return None


def _sediment_decisions(
    payload: dict[str, Any],
    *,
    org_id: str,
    _translated_records: set[int] | None = None,
) -> list[DeveloperDecision]:
    """Translate shim-emitted ``sediment.tool_decision`` records.

    One record carries everything — no decision/result join, because the
    shim observes the edit directly. ``decision`` is ``accept``/``reject``;
    ``explicit`` must be a real bool (the shim knows the harness's
    permission semantics; a shim for a harness with no human approval
    gesture always emits ``false``). Emitting edit-tool executions only is
    the shim's contract obligation; the translator does not re-filter tool
    names. A reject may carry ``file_path=""`` (the shared reject
    convention — the shim never saw an applied edit).
    """
    out: list[DeveloperDecision] = []
    for position, (resource, record) in enumerate(_iter_records(payload)):
        if _body(record) != _SD_DECISION:
            continue
        attrs = _attrs(record.get("attributes"))
        call_id = _str_or_none(attrs.get("tool_use_id"))
        agent_harness = _agent_harness(
            attrs.get("agent"), org_id=org_id, call_id=call_id
        )
        occurred = _required(
            _occurred_at(record.get("timeUnixNano")), "time", "sediment", org_id
        )
        session_id = _required(
            _str_or_none(attrs.get(_SESSION)) or _str_or_none(resource.get(_SESSION)),
            "session",
            "sediment",
            org_id,
        )
        call_id = _required(call_id, "call_id", "sediment", org_id)
        decision = attrs.get("decision")
        if decision not in ("accept", "reject"):
            logger.warning(
                "otlp_record_missing_decision",
                extra={"org_id": org_id, "source": "sediment", "call_id": call_id},
            )
            continue
        explicit = attrs.get("explicit")
        if not isinstance(explicit, bool):  # untrusted: require a real bool
            logger.warning(
                "otlp_record_missing_explicit",
                extra={"org_id": org_id, "source": "sediment", "call_id": call_id},
            )
            continue
        if (
            agent_harness is None
            or occurred is None
            or session_id is None
            or call_id is None
        ):
            continue
        file_path = _str_or_none(attrs.get("file_path")) or ""
        try:
            fact = DeveloperDecision(
                org_id=org_id,
                session_id=session_id,
                user_id=_id_or_none(resource.get(_USER))
                or _id_or_none(attrs.get(_USER)),
                agent_harness=agent_harness,
                file_path=file_path,
                accepted=decision == "accept",
                explicit=explicit,
                interaction_mode=InteractionMode.AGENT,
                call_id=_id_or_none(call_id),
                occurred_at=occurred,
                raw={"decision": attrs},
            )
            if (
                fact.agent_harness is AgentHarness.CURSOR
                and attrs.get("tool_name") == "Write"
                and fact.accepted
                and not fact.explicit
                and fact.call_id is not None
            ):
                # Native Write IDs identify executions, while timeUnixNano is
                # hook receipt time. PostgreSQL's primary key collapses repeats;
                # the first receipt stays immutable. Hash only validated keys.
                identity = (
                    fact.org_id,
                    fact.agent_harness.value,
                    fact.session_id,
                    fact.call_id,
                    fact.file_path,
                    fact.accepted,
                    fact.explicit,
                    fact.interaction_mode.value,
                    "Write",
                )
                encoded = json.dumps(identity, separators=(",", ":")).encode("ascii")
                fact = fact.model_copy(
                    update={
                        "decision_id": "cursor-write-v1:"
                        + hashlib.sha256(encoded).hexdigest()
                    }
                )
            out.append(fact)
            if _translated_records is not None:
                _translated_records.add(position)
        except Exception:  # malformed record — skip, don't sink the batch
            logger.warning(
                "otlp_record_invalid",
                extra={"org_id": org_id, "source": "sediment", "call_id": call_id},
            )
    return out


_TRANSLATORS = [
    _copilot_decisions,
    _claude_code_decisions,
    _codex_decisions,
    _sediment_decisions,
]


def parse_otlp_decisions(
    payload: dict[str, Any],
    *,
    org_id: str,
    _translated_records: set[int] | None = None,
) -> list[DeveloperDecision]:
    """Map an OTLP/JSON ExportLogsServiceRequest into DeveloperDecision facts.

    Concatenates every translator's output; each self-filters by event name.
    """
    return [
        d
        for translate in _TRANSLATORS
        for d in translate(
            payload, org_id=org_id, _translated_records=_translated_records
        )
    ]


# Sediment's own transcript-extractor wire (ADR 0007): the SessionEnd
# client (cli/sediment_cli/transcript.py) emits one record per applied edit,
# named in the record BODY like Claude Code's events. The mandatory ``agent``
# attribute selects the harness parser that produced the observation.
_EDIT_OBSERVATION = "sediment.edit_observation"
# Mirror of the client's per-side cap (cli/sediment_cli/transcript.py
# MAX_TEXT_BYTES). The client already refuses to ship oversized pairs, so a
# bigger one arriving here is a buggy or hostile sender — this is the first
# fact type embedding client-supplied text blobs, and the cap must hold at
# the trust boundary, not only in the well-behaved client.
_MAX_TEXT_BYTES = 256 * 1024


def parse_otlp_edit_observations(
    payload: dict[str, Any],
    *,
    org_id: str,
    _translated_records: set[int] | None = None,
) -> list[EditObservation]:
    """Map ``sediment.edit_observation`` records into EditObservation facts.

    Same posture as the decision translators: pure, best-effort per record,
    mandatory identity gated by ``_required`` (session, tool_use_id,
    file_path, event time) — a record missing any of them is skipped with a
    trail, never stored with placeholders. ``applied_text`` and
    ``observed_file_text`` must both be strings ("" is legal on either side:
    an empty Write, a deleted file).

    The external-delta counts are optional and independently degradable: a
    record whose counts are junk still stores its text pair, with the counts
    absent.
    """
    out: list[EditObservation] = []
    for position, (resource, record) in enumerate(_iter_records(payload)):
        if _body(record) != _EDIT_OBSERVATION:
            continue
        attrs = _attrs(record.get("attributes"))
        occurred, session_id, call_id, file_path = _sediment_identity(
            resource, record, attrs, org_id
        )
        applied_text = attrs.get("applied_text")
        observed_file_text = attrs.get("observed_file_text")
        if not isinstance(applied_text, str) or not isinstance(observed_file_text, str):
            logger.warning(
                "otlp_record_missing_pair", extra={"org_id": org_id, "call_id": call_id}
            )
            continue
        agent_harness = _agent_harness(
            attrs.get("agent"), org_id=org_id, call_id=call_id
        )
        if agent_harness is None:
            continue
        if (
            len(applied_text.encode("utf-8", "replace")) > _MAX_TEXT_BYTES
            or len(observed_file_text.encode("utf-8", "replace")) > _MAX_TEXT_BYTES
        ):
            logger.warning(
                "otlp_record_oversized_pair",
                extra={"org_id": org_id, "call_id": call_id},
            )
            continue
        if None in (occurred, session_id, call_id, file_path):
            continue
        try:
            out.append(
                EditObservation(
                    org_id=org_id,
                    session_id=session_id,
                    user_id=_id_or_none(resource.get(_USER))
                    or _id_or_none(attrs.get(_USER)),
                    agent_harness=agent_harness,
                    file_path=file_path,
                    call_id=call_id,
                    applied_text=applied_text,
                    observed_file_text=observed_file_text,
                    external_lines_added=_count_or_none(
                        attrs.get("external_lines_added")
                    ),
                    external_lines_removed=_count_or_none(
                        attrs.get("external_lines_removed")
                    ),
                    occurred_at=occurred,
                    # The pair already lives in dedicated columns; raw keeps
                    # only the light provenance, not a second copy of the text.
                    raw={"tool_name": attrs.get("tool_name")},
                )
            )
            if _translated_records is not None:
                _translated_records.add(position)
        except Exception:  # malformed record — skip, don't sink the batch
            logger.warning(
                "otlp_record_invalid",
                extra={"org_id": org_id, "source": "sediment", "call_id": call_id},
            )
    return out


# The refused-edit wire (ADR 0007): the same SessionEnd client emits
# these alongside edit-observation records in one batch, so this translator
# self-filters by body exactly like the pair translator above.
_REJECTED_EDIT = "sediment.rejected_edit"


def parse_otlp_rejected_edits(
    payload: dict[str, Any],
    *,
    org_id: str,
    _translated_records: set[int] | None = None,
) -> list[RejectedEdit]:
    """Map ``sediment.rejected_edit`` log records into RejectedEdit facts.

    Same posture as ``parse_otlp_edit_observations``: pure, best-effort per
    record, mandatory identity gated by ``_required`` (session, tool_use_id,
    file_path, event time), and the client's per-side cap re-enforced here
    because a bigger payload arriving means a buggy or hostile sender.

    ``proposed`` must be a string; ``""`` is legal (an empty Write the
    developer refused is still a refusal). Whether the call was a refusal
    rather than a tool failure is decided client-side, where the transcript's
    reject marker lives — the server trusts the event name, the same way it
    trusts ``sediment.tool_decision``'s ``decision`` attribute.
    """
    out: list[RejectedEdit] = []
    for position, (resource, record) in enumerate(_iter_records(payload)):
        if _body(record) != _REJECTED_EDIT:
            continue
        attrs = _attrs(record.get("attributes"))
        occurred, session_id, call_id, file_path = _sediment_identity(
            resource, record, attrs, org_id
        )
        proposed = attrs.get("proposed")
        if not isinstance(proposed, str):
            logger.warning(
                "otlp_record_missing_proposed",
                extra={"org_id": org_id, "call_id": call_id},
            )
            continue
        agent_harness = _agent_harness(
            attrs.get("agent"), org_id=org_id, call_id=call_id
        )
        if agent_harness is None:
            continue
        if len(proposed.encode("utf-8", "replace")) > _MAX_TEXT_BYTES:
            logger.warning(
                "otlp_record_oversized_proposed",
                extra={"org_id": org_id, "call_id": call_id},
            )
            continue
        if None in (occurred, session_id, call_id, file_path):
            continue
        try:
            out.append(
                RejectedEdit(
                    org_id=org_id,
                    session_id=session_id,
                    user_id=_id_or_none(resource.get(_USER))
                    or _id_or_none(attrs.get(_USER)),
                    agent_harness=agent_harness,
                    file_path=file_path,
                    call_id=call_id,
                    proposed=proposed,
                    occurred_at=occurred,
                    # The text lives in its own column; raw keeps only the
                    # light provenance, not a second copy.
                    raw={"tool_name": attrs.get("tool_name")},
                )
            )
            if _translated_records is not None:
                _translated_records.add(position)
        except Exception:  # malformed record — skip, don't sink the batch
            logger.warning(
                "otlp_record_invalid",
                extra={"org_id": org_id, "source": "sediment", "call_id": call_id},
            )
    return out


_RETRY_LINKAGE = "sediment.retry_linkage"
_EDIT_TOOLS_FOR_RETRY = frozenset({"Edit", "Write"})


class RetryLinkageSkipReason(StrEnum):
    """Closed reasons that a retry-linkage record doesn't become a fact."""

    MALFORMED_ATTRIBUTES = "malformed_attributes"
    MISSING_SESSION = "missing_session"
    MISSING_AGENT_HARNESS = "missing_agent_harness"
    INVALID_AGENT_HARNESS = "invalid_agent_harness"
    MISSING_FILE_PATH = "missing_file_path"
    MISSING_TOOL_NAME = "missing_tool_name"
    INVALID_TOOL_NAME = "invalid_tool_name"
    MISSING_REJECTED_CALL_ID = "missing_rejected_call_id"
    MISSING_ACCEPTED_CALL_ID = "missing_accepted_call_id"
    MISSING_EVENT_TIME = "missing_event_time"
    INVALID_FACT = "invalid_fact"


def _skip_retry_linkage(
    reason: RetryLinkageSkipReason,
    counts: Counter[RetryLinkageSkipReason],
    *,
    org_id: str,
    call_id: str | None = None,
) -> None:
    counts[reason] += 1
    logger.warning(
        "otlp_retry_linkage_skipped",
        extra={"org_id": org_id, "reason": reason.value, "call_id": call_id},
    )


def parse_otlp_retry_linkages(
    payload: dict[str, Any],
    *,
    org_id: str,
    skip_counts: Counter[RetryLinkageSkipReason] | None = None,
    _translated_records: set[int] | None = None,
) -> list[RetryLinkage]:
    """Map ``sediment.retry_linkage`` records into immutable facts.

    Each matched record validates independently. Missing identity and event
    time never receive defaults. The caller can pass a Counter to publish the
    closed skip vocabulary without changing the successful-fact return shape.
    """
    counts: Counter[RetryLinkageSkipReason] = (
        skip_counts if skip_counts is not None else Counter()
    )
    out: list[RetryLinkage] = []
    for position, (resource, record) in enumerate(_iter_records(payload)):
        if _body(record) != _RETRY_LINKAGE:
            continue
        if not isinstance(record.get("attributes"), list):
            _skip_retry_linkage(
                RetryLinkageSkipReason.MALFORMED_ATTRIBUTES,
                counts,
                org_id=org_id,
            )
            continue
        attrs = _attrs(record["attributes"])
        rejected_call_id = _id_or_none(attrs.get("rejected_call_id"))
        accepted_call_id = _id_or_none(attrs.get("accepted_call_id"))
        session_id = _id_or_none(attrs.get(_SESSION)) or _id_or_none(
            resource.get(_SESSION)
        )
        file_path = _str_or_none(attrs.get("file_path"))
        tool_name = _str_or_none(attrs.get("tool_name"))
        occurred_at = _occurred_at(record.get("timeUnixNano"))
        agent_value = _str_or_none(attrs.get("agent"))

        required = (
            (session_id, RetryLinkageSkipReason.MISSING_SESSION),
            (agent_value, RetryLinkageSkipReason.MISSING_AGENT_HARNESS),
            (file_path, RetryLinkageSkipReason.MISSING_FILE_PATH),
            (tool_name, RetryLinkageSkipReason.MISSING_TOOL_NAME),
            (rejected_call_id, RetryLinkageSkipReason.MISSING_REJECTED_CALL_ID),
            (accepted_call_id, RetryLinkageSkipReason.MISSING_ACCEPTED_CALL_ID),
            (occurred_at, RetryLinkageSkipReason.MISSING_EVENT_TIME),
        )
        missing = next((reason for value, reason in required if value is None), None)
        if missing is not None:
            _skip_retry_linkage(
                missing, counts, org_id=org_id, call_id=rejected_call_id
            )
            continue
        try:
            agent_harness = AgentHarness(agent_value)
        except ValueError:
            _skip_retry_linkage(
                RetryLinkageSkipReason.INVALID_AGENT_HARNESS,
                counts,
                org_id=org_id,
                call_id=rejected_call_id,
            )
            continue
        if tool_name not in _EDIT_TOOLS_FOR_RETRY:
            _skip_retry_linkage(
                RetryLinkageSkipReason.INVALID_TOOL_NAME,
                counts,
                org_id=org_id,
                call_id=rejected_call_id,
            )
            continue
        try:
            out.append(
                RetryLinkage(
                    org_id=org_id,
                    session_id=session_id,
                    user_id=_id_or_none(resource.get(_USER))
                    or _id_or_none(attrs.get(_USER)),
                    agent_harness=agent_harness,
                    file_path=file_path,
                    tool_name=tool_name,
                    rejected_call_id=rejected_call_id,
                    accepted_call_id=accepted_call_id,
                    occurred_at=occurred_at,
                )
            )
            if _translated_records is not None:
                _translated_records.add(position)
        except Exception:
            _skip_retry_linkage(
                RetryLinkageSkipReason.INVALID_FACT,
                counts,
                org_id=org_id,
                call_id=rejected_call_id,
            )
    return out


@dataclass(frozen=True)
class OTLPCaptureResult:
    """Parsed Facts and independent record/container counts for one OTLP batch."""

    decisions: list[DeveloperDecision]
    edit_observations: list[EditObservation]
    rejected_edits: list[RejectedEdit]
    retry_linkages: list[RetryLinkage]
    retry_linkage_skips: Counter[RetryLinkageSkipReason]
    records_received: int
    records_translated: int
    records_untranslated: int
    records_malformed: int
    malformed_containers: int


def parse_otlp_logs(payload: dict[str, Any], *, org_id: str) -> OTLPCaptureResult:
    """Reuse the canonical batch translators and account for every record entry.

    A successful translator marks the position of the record that emits its
    Fact. A supporting tool-result record emits none of its own and therefore
    remains untranslated. Several Facts or translators can mark one position.
    Positions follow the existing object-only walk, preserving diagnostic order;
    malformed entries and non-enumerable containers are counted separately.
    """
    counts: Counter[str] = Counter()
    objects = sum(1 for _ in _iter_records(payload, counts=counts))
    translated: set[int] = set()
    decisions = parse_otlp_decisions(
        payload, org_id=org_id, _translated_records=translated
    )
    observations = parse_otlp_edit_observations(
        payload, org_id=org_id, _translated_records=translated
    )
    rejected = parse_otlp_rejected_edits(
        payload, org_id=org_id, _translated_records=translated
    )
    retry_skips: Counter[RetryLinkageSkipReason] = Counter()
    retries = parse_otlp_retry_linkages(
        payload,
        org_id=org_id,
        skip_counts=retry_skips,
        _translated_records=translated,
    )
    return OTLPCaptureResult(
        decisions=decisions,
        edit_observations=observations,
        rejected_edits=rejected,
        retry_linkages=retries,
        retry_linkage_skips=retry_skips,
        records_received=objects + counts["records_malformed"],
        records_translated=len(translated),
        records_untranslated=objects - len(translated),
        records_malformed=counts["records_malformed"],
        malformed_containers=counts["malformed_containers"],
    )
