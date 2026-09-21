# SPDX-License-Identifier: AGPL-3.0-or-later
"""OTLP log-record → DeveloperDecision translation.

Driven by committed sanitized wire captures from Copilot, Claude Code,
and Codex. HTTP receiver tests live with the ingest app.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from sediment_capture import (
    parse_otlp_decisions,
    parse_otlp_edit_observations,
    parse_otlp_rejected_edits,
    parse_otlp_retry_linkages,
    RetryLinkageSkipReason,
)
from sediment_core import AgentHarness, InteractionMode

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"
ORG = "acme-corp"


def _fixture(agent: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / agent / name).read_text())


def _translate(payload: dict[str, Any]) -> list:
    return parse_otlp_decisions(payload, org_id=ORG)


def _capture(payload):
    import sediment_capture

    assert callable(getattr(sediment_capture, "parse_otlp_logs", None))
    return sediment_capture.parse_otlp_logs(payload, org_id=ORG)


def _fact_values(facts):
    return [
        fact.model_dump(
            exclude={
                "decision_id",
                "observation_id",
                "rejection_id",
                "retry_linkage_id",
                "captured_at",
            }
        )
        for fact in facts
    ]


@pytest.mark.parametrize(
    "payload,containers,received,malformed",
    [
        ({}, 0, 0, 0),
        ({"resourceLogs": []}, 0, 0, 0),
        ({"resourceLogs": None}, 1, 0, 0),
        ({"resourceLogs": {}}, 1, 0, 0),
        ({"resourceLogs": [None, [], {}]}, 2, 0, 0),
        ({"resourceLogs": [{"scopeLogs": None}]}, 1, 0, 0),
        ({"resourceLogs": [{"scopeLogs": [None, {}, []]}]}, 2, 0, 0),
        ({"resourceLogs": [{"scopeLogs": [{"logRecords": None}]}]}, 1, 0, 0),
        ({"resourceLogs": [{"scopeLogs": [{"logRecords": {}}]}]}, 1, 0, 0),
        (
            {"resourceLogs": [{"scopeLogs": [{"logRecords": [None, [], 1, {}]}]}]},
            0,
            4,
            3,
        ),
    ],
)
def test_capture_counts_only_enumerable_records(
    payload, containers, received, malformed
):
    from copy import deepcopy

    before = deepcopy(payload)
    result = _capture(payload)
    assert result.records_received == received
    assert result.records_malformed == malformed
    assert result.records_translated == 0
    assert result.records_untranslated == received - malformed
    assert result.malformed_containers == containers
    assert (
        result.decisions
        == result.edit_observations
        == result.rejected_edits
        == result.retry_linkages
        == []
    )
    assert payload == before


def test_capture_counts_duplicate_positions_once_and_preserves_every_parser():
    record = _sd_record()
    retry_declined = _retry_linkage_record(accepted_call_id=None)
    payload = _eo_batch(
        record,
        record,
        _eo_record(),
        _re_record(),
        _retry_linkage_record(),
        retry_declined,
        {"body": []},
        None,
    )
    result = _capture(payload)
    assert (
        result.records_received,
        result.records_translated,
        result.records_untranslated,
        result.records_malformed,
    ) == (8, 5, 2, 1)
    assert result.retry_linkage_skips == {
        RetryLinkageSkipReason.MISSING_ACCEPTED_CALL_ID: 1
    }
    for field, parser in (
        ("decisions", parse_otlp_decisions),
        ("edit_observations", parse_otlp_edit_observations),
        ("rejected_edits", parse_otlp_rejected_edits),
        ("retry_linkages", parse_otlp_retry_linkages),
    ):
        assert _fact_values(getattr(result, field)) == _fact_values(
            parser(payload, org_id=ORG)
        )


@pytest.mark.parametrize("reverse_resources", [False, True])
def test_capture_preserves_all_native_sources_context_order_and_diagnostics(
    reverse_resources, caplog
):
    from copy import deepcopy

    resources = [
        resource
        for path in sorted(FIXTURES.rglob("*.json"))
        for resource in json.loads(path.read_text())["resourceLogs"]
    ]
    if reverse_resources:
        resources.reverse()
    # Keep result context across all scopes and resources. Malformed entries
    # do not renumber the object-only positions reported by existing parsers.
    resources.insert(0, {"scopeLogs": [{"logRecords": [None, {"body": []}]}]})
    resources.extend([None, {"scopeLogs": [None, {"logRecords": None}]}])
    payload = {"resourceLogs": resources}
    before = deepcopy(payload)
    caplog.set_level("INFO", logger="sediment.capture.otlp")
    legacy = _translate(payload)
    legacy_logs = [
        (r.message, getattr(r, "record_position", None)) for r in caplog.records
    ]
    caplog.clear()
    result = _capture(payload)
    assert _fact_values(result.decisions) == _fact_values(legacy)
    assert {fact.agent_harness for fact in result.decisions} >= {
        AgentHarness.CLAUDE_CODE,
        AgentHarness.CODEX,
        AgentHarness.COPILOT,
    }
    assert [
        (r.message, getattr(r, "record_position", None)) for r in caplog.records
    ] == legacy_logs
    assert result.records_translated > 0
    assert result.records_untranslated > 0
    assert result.records_malformed == 1
    assert result.malformed_containers == 3
    assert (
        result.records_received
        == result.records_translated
        + result.records_untranslated
        + result.records_malformed
    )
    assert payload == before


def test_capture_counts_one_record_once_when_two_translators_emit():
    # Translators self-filter independently; retain that behavior for records
    # carrying both a body discriminator and a different event attribute.
    record = _sd_record()
    record["attributes"].extend(
        _otlp_attrs(
            {
                "event.name": "copilot_chat.inline.done",
                "accepted": True,
                "request_id": "copilot-call",
                "copilot_chat.file.relative_path": "a.py",
            }
        )
    )
    payload = _eo_batch(record)
    payload["resourceLogs"][0]["resource"] = {
        "attributes": _otlp_attrs({"session.id": "sess-1"})
    }
    result = _capture(payload)
    assert len(result.decisions) == 2
    assert result.records_received == result.records_translated == 1
    assert result.records_untranslated == result.records_malformed == 0


def test_capture_counts_codex_partial_fanout_as_one_translated_record(caplog):
    patch = "*** Begin Patch\n*** Add File: invalid\x00.py\n+x\n*** Add File: valid.py\n+y\n*** End Patch"
    decision = _cx_record(
        **{
            "event.name": "codex.tool_decision",
            "call_id": "patch",
            "tool_name": "apply_patch",
            "decision": "approved",
            "source": "user",
        }
    )
    result = _cx_record(
        **{
            "event.name": "codex.tool_result",
            "call_id": "patch",
            "arguments": patch,
        }
    )
    capture = _capture(_cx_batch(None, {}, decision, result))
    assert [fact.file_path for fact in capture.decisions] == ["valid.py"]
    assert (
        capture.records_received,
        capture.records_translated,
        capture.records_untranslated,
        capture.records_malformed,
    ) == (4, 1, 2, 1)
    [invalid] = [r for r in caplog.records if r.message == "otlp_record_invalid"]
    assert invalid.record_position == 1


def _otlp_attrs(mapping: dict[str, object]) -> list[dict[str, Any]]:
    """OTLP AnyValue-encode a flat mapping (strings and bools cover the tests)."""
    return [
        {
            "key": k,
            "value": {"boolValue": v}
            if isinstance(v, bool)
            else {"stringValue": str(v)},
        }
        for k, v in mapping.items()
    ]


def _cx_record(**attrs: object) -> dict[str, Any]:
    """A Codex log record: identity on the record, timeUnixNano='0' like the
    wire. conversation.id defaults on so synthetic batches carry the (now
    mandatory) session identity unless a test removes it."""
    attrs.setdefault("conversation.id", "conv-1")
    return {
        "timeUnixNano": "0",
        "observedTimeUnixNano": "1783542925478460000",
        "attributes": _otlp_attrs(attrs),
    }


def _cx_batch(*records: dict, resource: dict | None = None) -> dict[str, Any]:
    """A Codex OTLP/JSON batch with resource identity from OTEL_RESOURCE_ATTRIBUTES."""
    res_attrs = resource or {"user.id": "developer-1", "org.id": "resource-org"}
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _otlp_attrs(res_attrs)},
                "scopeLogs": [{"logRecords": list(records)}],
            }
        ]
    }


def _logs(resource: dict[str, object] | None = None, **attrs: object) -> dict[str, Any]:
    """A one-record OTLP/JSON logs payload (Copilot-shaped: session on the
    resource) with the given record attributes."""
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": _otlp_attrs(resource or {"session.id": "sess-1"})
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1782578510649000000",
                                "attributes": _otlp_attrs(attrs),
                            }
                        ]
                    }
                ],
            }
        ]
    }


def _typed_value(value: object) -> dict[str, Any]:
    """OTLP AnyValue-encode a value preserving its numeric/bool type — unlike
    ``_otlp_attrs``, which stringifies everything but bools. Needed for
    survival_rate_four_gram/time_delay_ms, which are wire-numeric (the
    fixture carries them as intValue) and must round-trip as such."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _logs_typed(
    resource: dict[str, object] | None = None, **attrs: object
) -> dict[str, Any]:
    """Like ``_logs``, but record attribute values keep their Python type
    (int/float/bool/str) instead of being stringified."""
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": _otlp_attrs(resource or {"session.id": "sess-1"})
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1782578510649000000",
                                "attributes": [
                                    {"key": k, "value": _typed_value(v)}
                                    for k, v in attrs.items()
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_translate_edit_feedback_reject() -> None:
    [d] = _translate(_fixture("copilot", "edit_feedback_reject_agent.json"))
    assert d.agent_harness is AgentHarness.COPILOT
    assert d.org_id == ORG  # the parameter — payload org attrs are not tenancy
    assert d.accepted is False
    assert d.explicit is True
    assert d.interaction_mode is InteractionMode.AGENT
    assert d.file_path == "example.md"
    assert d.session_id == "00000000-0000-0000-0000-000000000000"  # resource
    assert d.commit_sha == "0" * 40
    assert d.call_id == "11111111-1111-1111-1111-111111111111"
    assert d.occurred_at.year >= 2026  # mapped from timeUnixNano, not ingest


def test_translate_inline_done_accept() -> None:
    decisions = _translate(_fixture("copilot", "inline_done_accept.json"))
    done = [d for d in decisions if d.interaction_mode is InteractionMode.INLINE]
    assert len(done) == 1
    assert done[0].accepted is True
    assert done[0].explicit is True
    assert done[0].call_id is None  # inline.done carries no call_id


def test_translate_edit_survival_implicit_accept() -> None:
    decisions = _translate(_fixture("copilot", "edit_survival.json"))
    surv = [d for d in decisions if not d.explicit]
    assert surv
    assert all(d.accepted is True for d in surv)  # no_revert > 0
    assert all(d.interaction_mode is InteractionMode.AGENT for d in surv)


def test_translate_edit_survival_populates_graded_fields() -> None:
    # The graded survival_rate_four_gram / time_delay_ms fields ride
    # alongside the existing accepted=rate>0 boolean, not replacing it.
    [d] = _translate(_fixture("copilot", "edit_survival.json"))
    assert d.accepted is True
    assert d.edit_retention_score == 1.0  # fixture's survival_rate_four_gram=1
    assert d.observation_delay_ms == 0  # fixture's time_delay_ms=0
    assert d.call_id == "11111111-1111-1111-1111-111111111111"  # request_id
    assert d.commit_sha == "0" * 40


def test_translate_edit_survival_zero_rate_is_not_none() -> None:
    # rate=0 is a present, meaningful value (implicit reject) — must be
    # stored as 0.0, never coerced to None (which means "no survival data").
    payload = _logs_typed(
        **{
            "event.name": "copilot_chat.edit.survival",
            "edit_source": "apply_patch",
            "survival_rate_no_revert": 0,
            "survival_rate_four_gram": 0,
            "time_delay_ms": 5000,
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    [d] = _translate(payload)
    assert d.accepted is False
    assert d.edit_retention_score == 0.0
    assert d.observation_delay_ms == 5000


def test_translate_non_survival_events_leave_graded_fields_none() -> None:
    # Events other than copilot_chat.edit.survival never populate the graded
    # fields, even if a same-named attribute were somehow present.
    [d] = _translate(_fixture("copilot", "edit_feedback_reject_agent.json"))
    assert d.edit_retention_score is None
    assert d.observation_delay_ms is None


def test_translate_survival_malformed_four_gram_rate_skipped() -> None:
    # A non-numeric survival_rate_four_gram must degrade both graded fields
    # to None rather than raising or guessing; the flattened accepted
    # boolean (from survival_rate_no_revert) is unaffected.
    payload = _logs_typed(
        **{
            "event.name": "copilot_chat.edit.survival",
            "edit_source": "apply_patch",
            "survival_rate_no_revert": 1,
            "survival_rate_four_gram": "not-a-number",
            "time_delay_ms": 5000,
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    [d] = _translate(payload)
    assert d.accepted is True
    assert d.edit_retention_score is None
    assert d.observation_delay_ms is None


def test_translate_survival_hostile_rate_degrades_both_fields() -> None:
    # json.loads accepts NaN/Infinity literals, and [0, 1] is the rate's
    # documented domain: every hostile rate must degrade to (None, None) —
    # never raise out of the translator (one bad record must not sink the
    # batch) and never reach the model/bind as a nonsense preference.
    for rate in [float("nan"), float("inf"), float("-inf"), 1.5, -0.1, 10**400]:
        payload = _logs_typed(
            **{
                "event.name": "copilot_chat.edit.survival",
                "edit_source": "apply_patch",
                "survival_rate_no_revert": 1,
                "survival_rate_four_gram": rate,
                "time_delay_ms": 5000,
                "copilot_chat.file.relative_path": "a.py",
            }
        )
        [d] = _translate(payload)
        assert d.accepted is True
        assert d.edit_retention_score is None
        assert d.observation_delay_ms is None


def test_translate_survival_hostile_window_keeps_rate_drops_window() -> None:
    # A malformed window drops only the window (documented behavior) — and
    # "malformed" includes non-finite floats and ints that would overflow
    # PostgreSQL BIGINT at bind time (a 500 after the response path
    # already committed earlier decisions in the batch).
    for window in [float("nan"), float("inf"), -1, 2**63, 10**25, 10**400]:
        payload = _logs_typed(
            **{
                "event.name": "copilot_chat.edit.survival",
                "edit_source": "apply_patch",
                "survival_rate_no_revert": 1,
                "survival_rate_four_gram": 1,
                "time_delay_ms": window,
                "copilot_chat.file.relative_path": "a.py",
            }
        )
        [d] = _translate(payload)
        assert d.edit_retention_score == 1.0
        assert d.observation_delay_ms is None


def test_translate_ignores_unknown_events() -> None:
    payload = _logs(**{"event.name": "gen_ai.tool.call"})
    assert _translate(payload) == []


def test_translate_tolerates_malformed_structure() -> None:
    # Junk at every level (non-dict entries, missing keys, non-list attributes)
    # must not raise — bad parts are skipped, good decisions still extracted.
    good = _fixture("copilot", "edit_feedback_reject_agent.json")["resourceLogs"][0]
    payload = {
        "resourceLogs": [
            "not-a-dict",
            {"resource": "nope", "scopeLogs": "nope"},
            {
                "scopeLogs": [
                    "junk",
                    {
                        "logRecords": [
                            "junk",
                            {"attributes": "not-a-list"},
                            # unhashable crafted key must not raise TypeError
                            {"attributes": [{"key": ["not", "a", "str"]}]},
                        ]
                    },
                ]
            },
            good,  # one valid record mixed in
        ]
    }
    decisions = _translate(payload)
    assert len(decisions) == 1
    assert decisions[0].file_path == "example.md"


def test_translate_skips_record_without_file_path() -> None:
    # No file path → skipped (junk file_path="" would weaken the dedup key).
    payload = _logs(**{"event.name": "copilot_chat.inline.done", "accepted": True})
    assert _translate(payload) == []


def test_translate_skips_saved_outcome() -> None:
    # "saved" is neither accept nor reject — must not be stored as a reject.
    payload = _logs(
        **{
            "event.name": "copilot_chat.edit.feedback",
            "outcome": "saved",
            "edit_surface": "agent",
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    assert _translate(payload) == []


def test_translate_skips_inline_survival() -> None:
    # Survival is the implicit backstop for agent (apply_patch) edits only;
    # inline_chat survival would double-count with inline.done.
    payload = _logs(
        **{
            "event.name": "copilot_chat.edit.survival",
            "edit_source": "inline_chat",
            "survival_rate_no_revert": "1",
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    assert _translate(payload) == []


def test_translate_inline_done_non_bool_accepted_skipped() -> None:
    # A stringy "false" must not be coerced to True — require a real bool.
    payload = _logs(
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": "false",
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    assert _translate(payload) == []


def test_translate_survival_non_numeric_rate_skipped() -> None:
    # A non-numeric survival rate must be skipped, not raise TypeError.
    payload = _logs(
        **{
            "event.name": "copilot_chat.edit.survival",
            "edit_source": "apply_patch",
            "survival_rate_no_revert": "not-a-number",
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    assert _translate(payload) == []


def test_translate_missing_time_skipped() -> None:
    # occurred_at is required and maps from timeUnixNano only — a record with
    # no event time is skipped, never backfilled from ingest time.
    payload = _logs(
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    del payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["timeUnixNano"]
    assert _translate(payload) == []


def test_translate_missing_session_skipped() -> None:
    # Placeholder session ids are banned (ADR 0002): no session.id → skipped,
    # not stored as "unknown".
    payload = _logs(
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    payload["resourceLogs"][0]["resource"]["attributes"] = []
    assert _translate(payload) == []


def test_translate_zero_time_skipped() -> None:
    # timeUnixNano="0" is proto3 "unset" — must be skipped, never stored as
    # the 1970 epoch (occurred_at is part of the dedup key).
    payload = _logs(
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["timeUnixNano"] = "0"
    assert _translate(payload) == []
    # bool is an int subclass: a crafted `true` must not become epoch+1ns.
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["timeUnixNano"] = True
    assert _translate(payload) == []


def test_translate_overflow_time_does_not_sink_batch() -> None:
    # A crafted out-of-range timeUnixNano raises OverflowError inside
    # fromtimestamp — that record is skipped; the rest of the batch survives.
    bad = _logs(
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    bad_resource = bad["resourceLogs"][0]
    bad_resource["scopeLogs"][0]["logRecords"][0]["timeUnixNano"] = "9" * 30
    good = _fixture("copilot", "edit_feedback_reject_agent.json")["resourceLogs"][0]
    decisions = _translate({"resourceLogs": [bad_resource, good]})
    assert len(decisions) == 1
    assert decisions[0].file_path == "example.md"


def test_translate_infinite_time_does_not_sink_batch() -> None:
    # json.loads decodes 1e999 to float('inf'); int(float('inf')) raises
    # OverflowError in _occurred_at's int(...) coercion. That must skip the
    # record, not escape and 500 the whole batch.
    bad = _logs(
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    bad["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["timeUnixNano"] = float(
        "inf"
    )
    good = _fixture("copilot", "edit_feedback_reject_agent.json")["resourceLogs"][0]
    decisions = _translate({"resourceLogs": [bad["resourceLogs"][0], good]})
    assert len(decisions) == 1
    assert decisions[0].file_path == "example.md"


def test_translate_out_of_range_intvalue_does_not_sink_batch() -> None:
    # json.loads decodes 1e999 to float('inf'); _scalar's int(float('inf'))
    # raises OverflowError, which must degrade the attribute, not escape and
    # 500 the whole batch.
    payload = _logs(
        resource={"session.id": "sess-1"},
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        },
    )
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["attributes"].append({"key": "junk", "value": {"intValue": float("inf")}})
    [d] = _translate(payload)
    assert d.accepted is True


def test_translate_non_string_user_id_degrades_to_absent() -> None:
    # user.id as intValue (junk type) must degrade to None, not drop the
    # decision via a ValidationError or invent a shared identity.
    payload = _logs(
        resource={"session.id": "sess-1"},
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        },
    )
    payload["resourceLogs"][0]["resource"]["attributes"].append(
        {"key": "user.id", "value": {"intValue": "12345"}}
    )
    [d] = _translate(payload)
    assert d.user_id is None
    assert d.accepted is True


def test_whitespace_user_id_degrades_to_absent() -> None:
    # A whitespace-only user.id must degrade to None (the field is optional),
    # never reach NonEmptyId and sink the whole decision record.
    payload = _logs(
        resource={"session.id": "sess-1", "user.id": "   "},
        **{
            "event.name": "copilot_chat.inline.done",
            "accepted": True,
            "copilot_chat.file.relative_path": "a.py",
        },
    )
    [d] = _translate(payload)
    assert d.user_id is None
    assert d.accepted is True


def test_cc_accept_config_auto_applied() -> None:
    # allowedTools/acceptEdits auto-apply: accepted but NOT an explicit human
    # decision. file_path joined from tool_result via tool_use_id.
    [d] = _translate(_fixture("claude_code", "tool_decision_accept_config.json"))
    assert d.agent_harness is AgentHarness.CLAUDE_CODE
    assert d.interaction_mode is InteractionMode.AGENT
    assert d.org_id == ORG
    assert d.accepted is True
    assert d.explicit is False  # source=config
    assert d.file_path == "/home/dev/project/hello.txt"
    assert d.session_id == "11111111-1111-1111-1111-111111111111"
    assert d.user_id == "spike-dev"  # resource user.id beats the install hash
    assert d.call_id == "toolu_0000000000000001"
    assert d.commit_sha is None
    assert d.occurred_at.year >= 2026
    assert d.raw["decision"] is not None and d.raw["result"] is not None


def test_cc_whitespace_user_id_degrades_to_absent() -> None:
    # Both user.id sources whitespace-only: the decision survives with an
    # absent user_id, never dropped by NonEmptyId stripping to "".
    payload = _fixture("claude_code", "tool_decision_accept_config.json")
    for attr in payload["resourceLogs"][0]["resource"]["attributes"]:
        if attr["key"] == "user.id":
            attr["value"]["stringValue"] = "   "
    for record in payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        for attr in record["attributes"]:
            if attr["key"] == "user.id":
                attr["value"]["stringValue"] = "   "
    [d] = _translate(payload)
    assert d.accepted is True
    assert d.user_id is None


def test_cc_accept_user_explicit() -> None:
    [d] = _translate(_fixture("claude_code", "tool_decision_accept_user.json"))
    assert d.accepted is True
    assert d.explicit is True  # source=user_temporary
    assert d.file_path == "/home/dev/project/approved.txt"


def test_cc_reject_has_no_file_path() -> None:
    # A rejected tool call emits NO tool_result (wire-verified), so the reject
    # is stored call-level with file_path="" — never dropped, never fanned out.
    [d] = _translate(_fixture("claude_code", "tool_decision_reject_user.json"))
    assert d.accepted is False
    assert d.explicit is True  # source=user_reject
    assert d.file_path == ""
    assert d.call_id == "toolu_0000000000000001"  # keeps the dedup key precise
    assert d.raw["result"] is None


def test_cc_unknown_source_is_implicit_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A source outside both vocabularies (auto mode may add one) stays
    # fail-safe implicit — never a fabricated human gesture — but leaves a
    # trail so the new wire value is noticed, not silently absorbed.
    payload = _fixture("claude_code", "tool_decision_accept_config.json")
    for record in payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        if record["body"]["stringValue"] == "claude_code.tool_decision":
            for attr in record["attributes"]:
                if attr["key"] == "source":
                    attr["value"] = {"stringValue": "auto"}
    with caplog.at_level(logging.WARNING):
        [d] = _translate(payload)
    assert d.accepted is True
    assert d.explicit is False
    [warning] = [r for r in caplog.records if r.message == "otlp_record_unknown_source"]
    assert warning.decision_source == "auto"


def test_cc_known_sources_do_not_log_unknown_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        for name in (
            "tool_decision_accept_config.json",
            "tool_decision_accept_user.json",
            "tool_decision_reject_user.json",
        ):
            _translate(_fixture("claude_code", name))
    assert not [r for r in caplog.records if r.message == "otlp_record_unknown_source"]


def test_cc_unknown_decision_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An unrecognized decision verdict (auto mode may introduce one)
    # cannot map to the accepted bool — the record is skipped, never guessed —
    # but leaves a trail so the new wire value is noticed, not silently lost.
    payload = _fixture("claude_code", "tool_decision_accept_config.json")
    for record in payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        if record["body"]["stringValue"] == "claude_code.tool_decision":
            for attr in record["attributes"]:
                if attr["key"] == "decision":
                    attr["value"] = {"stringValue": "ask"}
    with caplog.at_level(logging.WARNING):
        assert _translate(payload) == []
    [warning] = [
        r for r in caplog.records if r.message == "otlp_record_unknown_decision"
    ]
    assert warning.decision == "ask"


def test_cc_edit_tool_filter() -> None:
    # Fixture carries a Read tool_result (orphan) plus an Edit decision+result:
    # only the Edit becomes a decision; Read never does.
    [d] = _translate(_fixture("claude_code", "tool_decision_acceptedits_edit.json"))
    assert d.raw["decision"] is not None
    assert d.file_path == "/home/dev/project/target.txt"
    assert d.accepted is True
    assert d.explicit is False  # acceptEdits auto-apply → source=config


def test_cc_truncated_tool_input_still_yields_path() -> None:
    # OTEL_LOG_TOOL_DETAILS truncates long values in-place ("…[N chars]") but
    # the serialized tool_input stays valid JSON and file_path survives.
    [d] = _translate(_fixture("claude_code", "tool_result_truncated_input.json"))
    assert d.file_path == "/home/dev/project/big.txt"
    assert d.accepted is True


def test_cc_decision_without_in_batch_result() -> None:
    # Decision and result can straddle an export batch (wire-verified): the
    # accept is still stored, with file_path degraded to "" — never guessed.
    payload = _fixture("claude_code", "tool_decision_accept_config.json")
    records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"] = [
        r for r in records if r["body"]["stringValue"] != "claude_code.tool_result"
    ]
    [d] = _translate(payload)
    assert d.accepted is True
    assert d.file_path == ""


def test_cc_missing_session_skipped() -> None:
    # session.id is record-scope on this wire; with it stripped (and none on
    # the resource) the decision is a capture bug — skipped, no placeholder.
    payload = _fixture("claude_code", "tool_decision_reject_user.json")
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["attributes"] = [a for a in record["attributes"] if a["key"] != "session.id"]
    assert _translate(payload) == []


def test_cc_missing_time_skipped() -> None:
    payload = _fixture("claude_code", "tool_decision_reject_user.json")
    del payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["timeUnixNano"]
    assert _translate(payload) == []


def test_cc_ignores_bare_tool_decision_event_name() -> None:
    # The namespaced discriminator is the record BODY; a record with only the
    # bare event.name attribute (a future non-Claude agent) must not match.
    payload = _fixture("claude_code", "tool_decision_reject_user.json")
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["body"] = {"stringValue": "codex.tool_decision"}
    assert _translate(payload) == []


# A V4A apply_patch diff touching two files (the format Codex puts in
# tool_result.arguments; verified single-file against the real capture).
_V4A_TWO_FILES = (
    "*** Begin Patch\n"
    "*** Update File: calc.py\n"
    "@@\n def add(a, b):\n     return a + b\n"
    "+\n+def multiply(a, b):\n+    return a * b\n"
    "*** Add File: util.py\n+def noop():\n+    return None\n"
    "*** End Patch\n"
)


def test_cx_accept_real_capture() -> None:
    # Sanitized live capture (Codex 0.142.5, full-auto): apply_patch auto-approved
    # by policy → accepted, but NOT an explicit human review (source=Config).
    [d] = _translate(_fixture("codex", "accept_apply_patch.json"))
    assert d.agent_harness is AgentHarness.CODEX
    assert d.interaction_mode is InteractionMode.AGENT
    assert d.accepted is True
    assert d.explicit is False  # source=Config (capitalized on the wire)
    assert d.file_path == "calc.py"
    assert d.session_id == "01900000-0000-7000-8000-000000000001"  # conversation.id
    assert d.user_id == "developer-1"  # resource user.id (OTEL_RESOURCE_ATTRIBUTES)
    assert d.org_id == ORG  # the parameter, NOT the resource org.id attribute
    assert d.call_id is not None and d.call_id.startswith("call_")
    assert d.commit_sha is None
    # timeUnixNano is "0" on the wire; occurred_at must come from
    # observedTimeUnixNano, NOT collapse to the 1970 epoch.
    assert d.occurred_at.year >= 2026


def test_cx_whitespace_user_id_degrades_to_absent() -> None:
    payload = _fixture("codex", "accept_apply_patch.json")
    for attr in payload["resourceLogs"][0]["resource"]["attributes"]:
        if attr["key"] == "user.id":
            attr["value"]["stringValue"] = "   "
    [d] = _translate(payload)
    assert d.accepted is True
    assert d.user_id is None


def test_cx_raw_does_not_persist_developer_pii() -> None:
    # Codex privacy boundary: user.email / user.account_id are
    # not stored. The fixture carries both on every record; the serialized
    # decision (raw included) must contain neither.
    [d] = _translate(_fixture("codex", "accept_apply_patch.json"))
    dumped = d.model_dump_json()
    assert "dev@example.com" not in dumped
    assert "user.email" not in dumped
    assert "user.account_id" not in dumped
    # ...while the useful audit fields survive in raw.
    assert d.raw["decision"]["tool_name"] == "apply_patch"


def test_cx_explicit_when_source_user() -> None:
    # Sanitized live capture: a prompt-reviewed approval (developer was shown the
    # diff and approved it) arrives as decision=approved, source=User → explicit
    # human accept. (Wire-capitalized "User"; the translator compares case-folded.)
    [d] = _translate(_fixture("codex", "accept_user_explicit.json"))
    assert d.agent_harness is AgentHarness.CODEX
    assert d.accepted is True
    assert d.explicit is True  # source=User, unlike the source=Config auto-apply
    assert d.file_path == "calc.py"


def test_cx_unknown_decision_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A verdict outside the closed accept/reject vocabulary (upstream
    # ReviewDecision has non-terminal variants like "timed_out" that reach the
    # wire in auto-review mode) cannot map to the accepted bool — the
    # record is skipped, never guessed — but leaves a trail instead of
    # vanishing silently.
    payload = _fixture("codex", "accept_apply_patch.json")
    for record in payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        attrs = record["attributes"]
        name = next(
            a["value"]["stringValue"] for a in attrs if a["key"] == "event.name"
        )
        if name != "codex.tool_decision":
            continue
        for attr in attrs:
            if attr["key"] == "decision":
                attr["value"] = {"stringValue": "timed_out"}
    with caplog.at_level(logging.WARNING):
        assert _translate(payload) == []
    [warning] = [
        r for r in caplog.records if r.message == "otlp_record_unknown_decision"
    ]
    assert warning.decision == "timed_out"


def test_cx_one_decision_per_patched_file() -> None:
    # One apply_patch touching N files → N decisions, one per file.
    batch = _cx_batch(
        _cx_record(
            **{
                "event.name": "codex.tool_decision",
                "tool_name": "apply_patch",
                "call_id": "call_multi",
                "decision": "approved",
                "source": "Config",
            }
        ),
        _cx_record(
            **{
                "event.name": "codex.tool_result",
                "tool_name": "apply_patch",
                "call_id": "call_multi",
                "arguments": _V4A_TWO_FILES,
            }
        ),
    )
    decisions = _translate(batch)
    assert {d.file_path for d in decisions} == {"calc.py", "util.py"}
    assert all(d.accepted and d.call_id == "call_multi" for d in decisions)


def test_cx_shell_wrapped_apply_patch_is_an_edit_decision() -> None:
    # Some OpenAI-compatible providers expose apply_patch through exec_command.
    # Codex still emits its native decision/result pair with the patch in the
    # result arguments. Preserve the edit decision and its call-id join.
    command = (
        "apply_patch <<'PATCH'\n"
        "*** Begin Patch\n"
        "*** Update File: calc.py\n"
        "@@\n"
        "-    return a - b\n"
        "+    return a + b\n"
        "*** End Patch\n"
        "PATCH"
    )
    batch = _cx_batch(
        _cx_record(
            **{
                "event.name": "codex.tool_decision",
                "tool_name": "exec_command",
                "call_id": "call_shell_patch",
                "decision": "approved",
                "source": "User",
            }
        ),
        _cx_record(
            **{
                "event.name": "codex.tool_result",
                "tool_name": "exec_command",
                "call_id": "call_shell_patch",
                "arguments": json.dumps({"cmd": command}),
            }
        ),
    )

    [decision] = _translate(batch)
    assert decision.accepted is True
    assert decision.explicit is True
    assert decision.file_path == "calc.py"
    assert decision.call_id == "call_shell_patch"


def test_cx_v4a_context_line_is_not_a_file_marker() -> None:
    # V4A body lines are prefixed (space for context, +/- for edits); a
    # context line quoting a marker must not fabricate a phantom file
    # decision — markers only count at column 0.
    diff = (
        "*** Begin Patch\n"
        "*** Update File: calc.py\n"
        "@@\n"
        " *** Update File: phantom.py\n"  # context line inside the patched file
        "+real added line\n"
        "*** End Patch\n"
    )
    batch = _cx_batch(
        _cx_record(
            **{
                "event.name": "codex.tool_decision",
                "tool_name": "apply_patch",
                "call_id": "call_ctx",
                "decision": "approved",
                "source": "Config",
            }
        ),
        _cx_record(
            **{
                "event.name": "codex.tool_result",
                "tool_name": "apply_patch",
                "call_id": "call_ctx",
                "arguments": diff,
            }
        ),
    )
    decisions = _translate(batch)
    assert {d.file_path for d in decisions} == {"calc.py"}


def test_cx_reject_degrades_to_empty_path() -> None:
    # The supported interactive patch-approval UI
    # has no plain deny (its only reject option interrupts the turn), so an
    # interactive reject emits NO tool_decision — this branch does not fire for
    # interactive use. The denied/abort emit path exists upstream (permission-
    # request hooks / policy / automated reviewer resolve to
    # ReviewDecision::Denied|Abort, lowercased on the wire), so the branch is
    # kept: a denied decision with no diff is stored call-level with
    # file_path="" (not dropped) — the reject convention shared with Claude
    # Code. The payload here is synthetic because the interactive wire never
    # produces one.
    batch = _cx_batch(
        _cx_record(
            **{
                "event.name": "codex.tool_decision",
                "tool_name": "apply_patch",
                "call_id": "call_deny",
                "decision": "denied",
                "source": "User",
                "conversation.id": "conv-2",
            }
        )
    )
    [d] = _translate(batch)
    assert d.accepted is False
    assert d.explicit is True  # source=User
    assert d.file_path == ""
    assert d.call_id == "call_deny"  # keeps the dedup key precise
    assert d.session_id == "conv-2"
    # _cx_batch's resource carries org.id="resource-org": tenancy must be the
    # parameter, never the payload attribute (the real-capture fixture can't
    # prove this — its org.id happens to equal ORG).
    assert d.org_id == ORG


def test_cx_ignores_non_patch_tools() -> None:
    # exec_command (a shell read Codex auto-approves) is not an edit — no decision.
    batch = _cx_batch(
        _cx_record(
            **{
                "event.name": "codex.tool_decision",
                "tool_name": "exec_command",
                "call_id": "call_read",
                "decision": "approved",
                "source": "Config",
            }
        ),
        _cx_record(
            **{
                "event.name": "codex.tool_result",
                "tool_name": "exec_command",
                "call_id": "call_read",
                "arguments": '{"cmd":"sed -n 1,120p calc.py"}',
            }
        ),
    )
    assert _translate(batch) == []


def test_cx_decision_without_in_batch_result() -> None:
    # If a decision's result lands in another export batch, the accept is still
    # stored with file_path degraded to "" (mirrors the Claude Code path).
    batch = _cx_batch(
        _cx_record(
            **{
                "event.name": "codex.tool_decision",
                "tool_name": "apply_patch",
                "call_id": "call_lonely",
                "decision": "approved",
                "source": "Config",
            }
        )
    )
    [d] = _translate(batch)
    assert d.accepted is True
    assert d.file_path == ""


def test_cx_zero_time_without_observed_skipped() -> None:
    # timeUnixNano="0" with no observedTimeUnixNano fallback: no real event
    # time exists — the record is skipped, never stamped with ingest time or
    # the 1970 epoch.
    record = _cx_record(
        **{
            "event.name": "codex.tool_decision",
            "tool_name": "apply_patch",
            "call_id": "call_no_time",
            "decision": "approved",
            "source": "Config",
        }
    )
    del record["observedTimeUnixNano"]
    assert _translate(_cx_batch(record)) == []
    # Same when the fallback itself is "0": still no real event time.
    record["observedTimeUnixNano"] = "0"
    assert _translate(_cx_batch(record)) == []


def test_cx_missing_conversation_id_skipped() -> None:
    # No conversation.id on record or resource → no real session id → skipped
    # (ADR 0002), not stored under a placeholder.
    record = _cx_record(
        **{
            "event.name": "codex.tool_decision",
            "tool_name": "apply_patch",
            "call_id": "call_no_conv",
            "decision": "approved",
            "source": "Config",
        }
    )
    record["attributes"] = [
        a for a in record["attributes"] if a["key"] != "conversation.id"
    ]
    assert _translate(_cx_batch(record)) == []


def test_store_roundtrip_and_redelivery_collapse(postgres_store) -> None:
    # A translated decision persists; re-translating the same batch (an
    # at-least-once redelivery) collapses on uq_decisions_natural — the whole
    # point of mapping occurred_at from the record, not ingest time.
    store = postgres_store
    [first] = _translate(_fixture("claude_code", "tool_decision_accept_user.json"))
    [redelivered] = _translate(
        _fixture("claude_code", "tool_decision_accept_user.json")
    )
    assert store.store_decision(first) is True
    assert store.store_decision(redelivered) is False
    [read] = store.read_decisions(ORG)
    assert read.file_path == first.file_path
    assert read.occurred_at == first.occurred_at
    assert read.session_id == first.session_id


def test_store_empty_path_reject_collapses_via_call_id(postgres_store) -> None:
    # The call-level reject (file_path="") still dedups: call_id
    # (= tool_use_id) is in the natural key.
    store = postgres_store
    [first] = _translate(_fixture("claude_code", "tool_decision_reject_user.json"))
    [redelivered] = _translate(
        _fixture("claude_code", "tool_decision_reject_user.json")
    )
    assert store.store_decision(first) is True
    assert store.store_decision(redelivered) is False


# The sediment.edit_observation wire (ADR 0007) is Sediment's own:
# cli/sediment_cli/transcript.py emits it, and the cross-pin between that
# client's payload builder and this translator lives in
# scripts/tests/test_transcript.py. These tests pin the translator's gating
# alone.


def _eo_record(**over: object) -> dict[str, Any]:
    attrs: dict[str, object] = {
        "session.id": "sess-1",
        "tool_use_id": "toolu-eo-1",
        "tool_name": "Edit",
        "file_path": "/repo/app/math.py",
        "applied_text": "def add(a, b):\n    return a + b",
        "observed_file_text": "def add(a, b):\n    return int(a) + int(b)",
        "agent": "claude-code",
    }
    attrs.update(over)
    return {
        "body": {"stringValue": "sediment.edit_observation"},
        "timeUnixNano": "1782578510649000000",
        "attributes": _otlp_attrs({k: v for k, v in attrs.items() if v is not None}),
    }


def _eo_batch(*records: dict, resource: dict | None = None) -> dict[str, Any]:
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _otlp_attrs(resource or {})},
                "scopeLogs": [{"logRecords": list(records)}],
            }
        ]
    }


def test_edit_observation_accepts_canonical_wire_contract() -> None:
    observations = parse_otlp_edit_observations(_eo_batch(_eo_record()), org_id=ORG)

    assert len(observations) == 1
    assert observations[0].agent_harness is AgentHarness.CLAUDE_CODE
    assert observations[0].applied_text.endswith("return a + b")
    assert observations[0].observed_file_text.endswith("int(a) + int(b)")


def test_edit_observation_rejects_missing_agent() -> None:
    assert (
        parse_otlp_edit_observations(_eo_batch(_eo_record(agent=None)), org_id=ORG)
        == []
    )


def test_edit_observation_happy_path() -> None:
    [o] = parse_otlp_edit_observations(_eo_batch(_eo_record()), org_id=ORG)
    assert o.org_id == ORG
    assert o.agent_harness is AgentHarness.CLAUDE_CODE
    assert o.session_id == "sess-1"
    assert o.call_id == "toolu-eo-1"
    assert o.file_path == "/repo/app/math.py"
    assert o.applied_text.startswith("def add")
    assert o.observed_file_text.endswith("int(a) + int(b)")
    assert o.user_id is None
    assert o.occurred_at.year >= 2026  # mapped from timeUnixNano, not ingest
    assert o.raw == {"tool_name": "Edit"}  # no second copy of the pair


def test_edit_observation_user_id_from_resource() -> None:
    [o] = parse_otlp_edit_observations(
        _eo_batch(_eo_record(), resource={"user.id": "developer-1"}), org_id=ORG
    )
    assert o.user_id == "developer-1"


def test_edit_observation_whitespace_user_id_degrades_to_absent() -> None:
    record = _eo_record(**{"user.id": "   "})
    [o] = parse_otlp_edit_observations(
        _eo_batch(record, resource={"user.id": "   "}), org_id=ORG
    )
    assert o.user_id is None


def test_edit_observation_empty_observed_file_text_is_legal() -> None:
    # "" = the file was gone at session end (edit fully discarded).
    [o] = parse_otlp_edit_observations(
        _eo_batch(_eo_record(observed_file_text="")), org_id=ORG
    )
    assert o.observed_file_text == ""


def test_edit_observation_missing_identity_skipped() -> None:
    for field in (
        "session.id",
        "tool_use_id",
        "file_path",
        "applied_text",
        "observed_file_text",
    ):
        payload = _eo_batch(_eo_record(**{field: None}))
        assert parse_otlp_edit_observations(payload, org_id=ORG) == [], field


def test_edit_observation_missing_time_skipped() -> None:
    record = _eo_record()
    del record["timeUnixNano"]
    assert parse_otlp_edit_observations(_eo_batch(record), org_id=ORG) == []


def test_edit_observation_oversized_pair_skipped() -> None:
    # The client caps each side at 256 KiB before shipping; the translator
    # re-enforces it at the trust boundary — an oversized pair here is a
    # buggy or hostile sender, not a fact to store.
    from sediment_capture.otlp import _MAX_TEXT_BYTES

    big = "x" * (_MAX_TEXT_BYTES + 1)
    assert (
        parse_otlp_edit_observations(
            _eo_batch(_eo_record(observed_file_text=big)), org_id=ORG
        )
        == []
    )
    assert (
        parse_otlp_edit_observations(
            _eo_batch(_eo_record(applied_text=big)), org_id=ORG
        )
        == []
    )
    # At the cap exactly is still legal.
    [o] = parse_otlp_edit_observations(
        _eo_batch(_eo_record(observed_file_text="x" * _MAX_TEXT_BYTES)), org_id=ORG
    )
    assert len(o.observed_file_text) == _MAX_TEXT_BYTES


def test_edit_observation_records_invisible_to_decision_translators() -> None:
    # Cross-filter both ways: the decision parser ignores our event, and the
    # edit-observation parser ignores a real decision fixture.
    assert _translate(_eo_batch(_eo_record())) == []
    decision_payload = _fixture("claude_code", "tool_decision_accept_user.json")
    assert parse_otlp_edit_observations(decision_payload, org_id=ORG) == []


def test_edit_observation_store_refire_collapses(postgres_store) -> None:
    store = postgres_store
    [first] = parse_otlp_edit_observations(_eo_batch(_eo_record()), org_id=ORG)
    [refire] = parse_otlp_edit_observations(
        _eo_batch(_eo_record(observed_file_text="different at second SessionEnd")),
        org_id=ORG,
    )
    assert store.store_edit_observation(first) is True
    assert store.store_edit_observation(refire) is False  # first write wins
    assert (
        store.read_edit_observations(ORG)[0].observed_file_text
        == first.observed_file_text
    )


# The shim wire (harness-neutral contract): one record carries everything, no
# decision/result join — the shim observes the edit directly. Agent identity
# rides the mandatory ``agent`` attribute, mapped to AgentHarness at the
# trust boundary.


def _sd_record(**over: object) -> dict[str, Any]:
    attrs: dict[str, object] = {
        "agent": "pi",
        "session.id": "sess-1",
        "tool_use_id": "call-1",
        "tool_name": "write",
        "decision": "accept",
        "explicit": False,
        "file_path": "/repo/app/math.py",
    }
    attrs.update(over)
    return {
        "body": {"stringValue": "sediment.tool_decision"},
        "timeUnixNano": "1782578510649000000",
        "attributes": _otlp_attrs({k: v for k, v in attrs.items() if v is not None}),
    }


def _sd_batch(*records: dict, resource: dict | None = None) -> dict[str, Any]:
    return _eo_batch(*records, resource=resource)


def test_sediment_decision_happy_path() -> None:
    [d] = _translate(_sd_batch(_sd_record()))
    assert d.org_id == ORG
    assert d.agent_harness is AgentHarness.PI
    assert d.interaction_mode is InteractionMode.AGENT
    assert d.session_id == "sess-1"
    assert d.call_id == "call-1"
    assert d.file_path == "/repo/app/math.py"
    assert d.accepted is True
    assert d.explicit is False  # a real bool off the wire, not a default
    assert d.user_id is None
    assert d.occurred_at.year >= 2026  # mapped from timeUnixNano, not ingest
    assert d.raw["decision"]["tool_name"] == "write"


def test_sediment_decision_whitespace_user_id_degrades_to_absent() -> None:
    # resource user.id and record user.id both whitespace-only: the decision
    # survives with an absent user_id.
    record = _sd_record(**{"user.id": "   "})
    [d] = _translate(_sd_batch(record, resource={"user.id": "   "}))
    assert d.accepted is True
    assert d.user_id is None


def test_sediment_decision_preserves_cursor_harness_identity() -> None:
    [decision] = _translate(_sd_batch(_sd_record(agent="cursor")))

    assert decision.agent_harness is AgentHarness.CURSOR


def test_translated_cursor_decision_round_trips_through_fact_store(
    postgres_store,
) -> None:
    [decision] = _translate(_sd_batch(_sd_record(agent="cursor")))

    assert postgres_store.store_decision(decision) is True
    [stored] = postgres_store.read_decisions(ORG)
    assert stored == decision


# Native Cursor 3.18.25 Write identity from the pilot rehearsal. The newline
# belongs to the opaque call id; it isn't a delimiter or a second tool call.
_CURSOR_SESSION = "d9072e22-f54c-4b80-9169-b221af897579"
_CURSOR_CALL = (
    "call-37629b8f-1f28-4764-918c-a781c85d5e9f-0\n"
    "fc_f6910d75-7b37-988d-b4b1-c8f6b9e6cd2b_0"
)
_CURSOR_OTHER_CALL = (
    "call-e20bdac6-f603-4a17-b62a-733673a68629-1\n"
    "fc_7c058196-fc5e-9904-870b-96793f3ed18c_0"
)


def _cursor_write_record(receipt: int = 0, **over: object) -> dict[str, Any]:
    # Cursor omits file_path and source event time. The adapter supplies each
    # hook receipt's time. These synthetic times make redelivery deterministic.
    attrs = {
        "agent": "cursor",
        "session.id": _CURSOR_SESSION,
        "tool_use_id": _CURSOR_CALL,
        "tool_name": "Write",
        "file_path": None,
        **over,
    }
    record = _sd_record(**attrs)
    record["timeUnixNano"] = str(1782578510649000000 + receipt * 1_000_000_000)
    return record


def test_cursor_write_redelivery_preserves_the_first_receipt(postgres_store) -> None:
    [first] = _translate(_sd_batch(_cursor_write_record()))
    [repeated] = _translate(_sd_batch(_cursor_write_record(1)))

    assert first.call_id == _CURSOR_CALL
    assert first.file_path == ""
    assert first.decision_id == repeated.decision_id
    assert first.decision_id.startswith("cursor-write-v1:")
    assert (repeated.occurred_at - first.occurred_at).total_seconds() == 1
    assert postgres_store.store_decisions([first, repeated]) == [True, False]
    assert postgres_store.read_decisions(ORG) == [first]


@pytest.mark.parametrize(
    "changed",
    [
        {"org_id": "other-org"},
        {"session.id": "other-session"},
        {"tool_use_id": _CURSOR_OTHER_CALL},
        {"file_path": "/repo/other.py"},
        {"decision": "reject"},
        {"explicit": True},
        {"agent": "pi"},
    ],
)
def test_cursor_write_identity_preserves_distinct_decisions(
    postgres_store, changed
) -> None:
    changed = dict(changed)
    org_id = changed.pop("org_id", ORG)
    [first] = _translate(_sd_batch(_cursor_write_record()))
    [other] = parse_otlp_decisions(
        _sd_batch(_cursor_write_record(**changed)), org_id=org_id
    )

    assert first.decision_id != other.decision_id
    assert postgres_store.store_decisions([first, other]) == [True, True]


def test_cursor_write_identity_uses_validated_keys_not_delivery_metadata(
    postgres_store,
) -> None:
    [first] = _translate(_sd_batch(_cursor_write_record(), resource={"user.id": "u1"}))
    [repeated] = parse_otlp_decisions(
        _sd_batch(
            _cursor_write_record(
                1,
                **{
                    "session.id": f"  {_CURSOR_SESSION}\n",
                    "tool_use_id": f"\t{_CURSOR_CALL}  ",
                },
            ),
            resource={"user.id": "u2", "org.id": "untrusted-org"},
        ),
        org_id="ACME-CORP",
    )

    assert first.decision_id == repeated.decision_id
    assert repeated.org_id == ORG
    assert repeated.user_id == "u2"
    assert postgres_store.store_decisions([first, repeated]) == [True, False]
    assert postgres_store.read_decisions(ORG) == [first]


def test_cursor_write_identity_does_not_join_ambiguous_delimiters() -> None:
    [first] = _translate(
        _sd_batch(_cursor_write_record(**{"session.id": "a:b", "tool_use_id": "c"}))
    )
    [other] = _translate(
        _sd_batch(_cursor_write_record(**{"session.id": "a", "tool_use_id": "b:c"}))
    )
    assert first.decision_id != other.decision_id


@pytest.mark.parametrize(
    "changed",
    [
        {"tool_name": "Edit"},
        {"tool_name": None},
        {"decision": "reject"},
        {"explicit": True},
        {"agent": "pi"},
        {"tool_use_id": "   "},
    ],
)
def test_non_native_cursor_write_records_keep_existing_identity_behavior(
    changed,
) -> None:
    [first] = _translate(_sd_batch(_cursor_write_record(**changed)))
    [repeated] = _translate(_sd_batch(_cursor_write_record(1, **changed)))
    assert first.decision_id != repeated.decision_id
    assert not first.decision_id.startswith("cursor-write-v1:")


def test_cursor_write_replay_preserves_historical_random_id_facts(
    postgres_store,
) -> None:
    [first_receipt] = _translate(_sd_batch(_cursor_write_record()))
    [second_receipt] = _translate(_sd_batch(_cursor_write_record(1)))
    historical = [
        first_receipt.model_copy(update={"decision_id": "historical-receipt-1"}),
        second_receipt.model_copy(update={"decision_id": "historical-receipt-2"}),
    ]
    assert postgres_store.store_decisions(historical) == [True, True]
    assert postgres_store.store_decision(first_receipt) is False  # old natural key

    [after_upgrade] = _translate(_sd_batch(_cursor_write_record(2)))
    [repeated] = _translate(_sd_batch(_cursor_write_record(3)))
    assert postgres_store.store_decisions([after_upgrade, repeated]) == [True, False]
    assert postgres_store.read_decisions(ORG) == [*historical, after_upgrade]


def test_cursor_write_concurrent_replay_is_database_enforced(postgres_store) -> None:
    decisions = [
        _translate(_sd_batch(_cursor_write_record(receipt)))[0] for receipt in range(6)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(postgres_store.store_decision, decisions))
    assert sum(receipts) == 1
    [stored] = postgres_store.read_decisions(ORG)
    assert stored in decisions


def test_cursor_write_missing_time_or_invalid_identity_does_not_get_a_fact() -> None:
    for field in ("session.id", "tool_use_id", "file_path"):
        assert _translate(_sd_batch(_cursor_write_record(**{field: "bad\0id"}))) == []
    record = _cursor_write_record()
    del record["timeUnixNano"]
    assert _translate(_sd_batch(record)) == []


def test_sediment_decision_reject() -> None:
    # Rejects follow the shared convention: file_path may be "" (the shim
    # never saw an applied edit).
    [d] = _translate(
        _sd_batch(_sd_record(decision="reject", explicit=True, file_path=""))
    )
    assert d.accepted is False
    assert d.explicit is True
    assert d.file_path == ""


def test_sediment_decision_session_from_resource_scope() -> None:
    record = _sd_record(**{"session.id": None})
    [d] = _translate(_sd_batch(record, resource={"session.id": "sess-res"}))
    assert d.session_id == "sess-res"


def test_sediment_decision_missing_identity_skipped() -> None:
    for field in ("agent", "session.id", "tool_use_id", "decision", "explicit"):
        payload = _sd_batch(_sd_record(**{field: None}))
        assert _translate(payload) == [], field


def test_sediment_decision_missing_time_skipped() -> None:
    record = _sd_record()
    del record["timeUnixNano"]
    assert _translate(_sd_batch(record)) == []


def test_sediment_decision_unknown_agent_skipped() -> None:
    # Schema is the source of truth: an unregistered agent is a capture bug
    # to surface, never a fact persisted under a placeholder source.
    assert _translate(_sd_batch(_sd_record(agent="bogus-agent"))) == []


def test_sediment_decision_bad_decision_value_skipped() -> None:
    assert _translate(_sd_batch(_sd_record(decision="maybe"))) == []


def test_sediment_decision_explicit_must_be_bool() -> None:
    # Untrusted input: a stringified "true" is not a permission semantics
    # observation — skip, don't coerce.
    assert _translate(_sd_batch(_sd_record(explicit="true"))) == []


def test_sediment_decision_ignores_other_bodies() -> None:
    record = _sd_record()
    record["body"] = {"stringValue": "pi.tool_decision"}  # vendor-native ≠ contract
    assert _translate(_sd_batch(record)) == []


def test_sediment_decision_malformed_record_does_not_sink_batch() -> None:
    good = _sd_record(tool_use_id="call-good")
    bad = _sd_record(tool_use_id="call-bad")
    bad["attributes"] = {"not": "a list"}
    [d] = _translate(_sd_batch(good, bad))
    assert d.call_id == "call-good"


def test_sediment_decision_shuffle_deterministic() -> None:
    records = [_sd_record(tool_use_id=f"call-{i}") for i in range(5)]
    forward = _translate(_sd_batch(*records))
    shuffled = _translate(_sd_batch(*reversed(records)))
    # decision_id/captured_at are generated per construction; content is what
    # must be ingest-order-independent.
    generated = {"decision_id", "captured_at"}
    assert [d.model_dump(exclude=generated) for d in forward] == [
        d.model_dump(exclude=generated) for d in reversed(shuffled)
    ]


def test_sediment_decision_conformance_fixture() -> None:
    # The golden contract payload (fixtures/otlp/sediment/tool_decision.json):
    # a shim is conformant when its output matches this shape. Any change to
    # the accepted shape is a contract change — update the contract doc and
    # every shim in the same PR.
    decisions = _translate(_fixture("sediment", "tool_decision.json"))
    assert len(decisions) == 3
    by_call = {d.call_id: d for d in decisions}
    accept = by_call["call-write-1"]
    assert accept.agent_harness is AgentHarness.PI
    assert accept.accepted is True
    assert accept.explicit is False
    assert accept.file_path == "/repo/app/main.py"
    assert accept.user_id == "developer-1"
    edit = by_call["call-edit-1"]
    assert edit.accepted is True
    assert edit.file_path == "/repo/app/util.py"
    reject = by_call["call-edit-2"]
    assert reject.accepted is False
    assert reject.explicit is True
    assert reject.file_path == ""


def test_edit_observation_agent_attribute_maps_harness() -> None:
    [o] = parse_otlp_edit_observations(_eo_batch(_eo_record(agent="pi")), org_id=ORG)
    assert o.agent_harness is AgentHarness.PI


def test_edit_observation_rejects_legacy_wire_contract() -> None:
    old_event = _eo_record()
    old_event["body"] = {"stringValue": "sediment.edit_outcome"}
    assert parse_otlp_edit_observations(_eo_batch(old_event), org_id=ORG) == []

    old_fields = _eo_record(
        applied_text=None,
        observed_file_text=None,
        original="applied",
        final="observed",
    )
    assert parse_otlp_edit_observations(_eo_batch(old_fields), org_id=ORG) == []


def test_edit_observation_unknown_agent_skipped() -> None:
    assert (
        parse_otlp_edit_observations(_eo_batch(_eo_record(agent="bogus")), org_id=ORG)
        == []
    )


def test_whitespace_request_id_degrades_to_keyless_decision() -> None:
    # A padded/blank request_id must not sink the whole decision — call_id is
    # nullable, so the id degrades to None (absent) and the human decision fact
    # survives. Same treatment on every nullable call_id site;
    # EditObservation.call_id stays required (dedup key) and keeps the
    # record-level skip.
    payload = _logs_typed(
        **{
            "event.name": "copilot_chat.edit.feedback",
            "outcome": "accepted",
            "edit_surface": "agent",
            "request_id": "   ",
            "copilot_chat.file.relative_path": "a.py",
        }
    )
    [d] = _translate(payload)
    assert d.accepted is True
    assert d.call_id is None


def _eo_record_with_counts(added: object, removed: object) -> dict[str, Any]:
    """An edit-observation record carrying counts as OTLP intValue (the client's
    encoding — OTLP/JSON writes int64 as a string inside intValue)."""
    record = _eo_record()
    record["attributes"] = list(record["attributes"]) + [
        {"key": "external_lines_added", "value": {"intValue": added}},
        {"key": "external_lines_removed", "value": {"intValue": removed}},
    ]
    return record


def test_edit_observation_external_counts_parsed() -> None:
    [o] = parse_otlp_edit_observations(
        _eo_batch(_eo_record_with_counts("4", "2")), org_id=ORG
    )
    assert o.external_lines_added == 4
    assert o.external_lines_removed == 2


def test_edit_observation_external_counts_absent_stays_none() -> None:
    # The common case: no snapshot covered the call. Absent, not zero.
    [o] = parse_otlp_edit_observations(_eo_batch(_eo_record()), org_id=ORG)
    assert o.external_lines_added is None
    assert o.external_lines_removed is None


def test_edit_observation_external_zero_is_kept() -> None:
    # 0 is a real observation — nothing else touched the file.
    [o] = parse_otlp_edit_observations(
        _eo_batch(_eo_record_with_counts("0", "0")), org_id=ORG
    )
    assert o.external_lines_added == 0
    assert o.external_lines_removed == 0


def test_edit_observation_junk_counts_degrade_but_keep_the_pair() -> None:
    # An untrusted sender must not be able to drop a good text pair by
    # corrupting an optional attribute.
    for junk in ("-1", "not-a-number", "1.5", str(2**63), str(10**30)):
        [o] = parse_otlp_edit_observations(
            _eo_batch(_eo_record_with_counts(junk, "1")), org_id=ORG
        )
        assert o.external_lines_added is None, junk
        assert o.applied_text.startswith("def add"), junk


def test_edit_observation_string_valued_counts_degrade() -> None:
    # stringValue where intValue belongs: absent, never a coerced number.
    record = _eo_record()
    record["attributes"] = list(record["attributes"]) + [
        {"key": "external_lines_added", "value": {"stringValue": "4"}}
    ]
    [o] = parse_otlp_edit_observations(_eo_batch(record), org_id=ORG)
    assert o.external_lines_added is None


def _re_record(**over: object) -> dict[str, Any]:
    attrs: dict[str, object] = {
        "session.id": "sess-1",
        "tool_use_id": "toolu-re-1",
        "tool_name": "Edit",
        "file_path": "/repo/app/math.py",
        "proposed": "def worse(a, b):\n    return a - b",
        "agent": "claude-code",
    }
    attrs.update(over)
    return {
        "body": {"stringValue": "sediment.rejected_edit"},
        "timeUnixNano": "1782578510649000000",
        "attributes": _otlp_attrs({k: v for k, v in attrs.items() if v is not None}),
    }


def test_rejected_edit_happy_path() -> None:
    [r] = parse_otlp_rejected_edits(_eo_batch(_re_record()), org_id=ORG)
    assert r.org_id == ORG
    assert r.agent_harness is AgentHarness.CLAUDE_CODE
    assert r.session_id == "sess-1"
    assert r.call_id == "toolu-re-1"
    assert r.file_path == "/repo/app/math.py"
    assert r.proposed.startswith("def worse")
    assert r.occurred_at.year >= 2026
    assert r.raw == {"tool_name": "Edit"}


def test_rejected_edit_whitespace_user_id_degrades_to_absent() -> None:
    record = _re_record(**{"user.id": "   "})
    [r] = parse_otlp_rejected_edits(
        _eo_batch(record, resource={"user.id": "   "}), org_id=ORG
    )
    assert r.user_id is None


def test_rejected_edit_empty_proposed_is_legal() -> None:
    [r] = parse_otlp_rejected_edits(_eo_batch(_re_record(proposed="")), org_id=ORG)
    assert r.proposed == ""


def test_rejected_edit_missing_identity_skipped() -> None:
    for field in ("session.id", "tool_use_id", "file_path", "proposed"):
        payload = _eo_batch(_re_record(**{field: None}))
        assert parse_otlp_rejected_edits(payload, org_id=ORG) == [], field


def test_rejected_edit_unregistered_agent_skipped() -> None:
    # No pre-contract claude-code fallback here: this record type is new, so
    # every emitter carries `agent`. An unknown one is skipped, never stored
    # under a guessed source.
    payload = _eo_batch(_re_record(agent="some-new-harness"))
    assert parse_otlp_rejected_edits(payload, org_id=ORG) == []
    assert (
        parse_otlp_rejected_edits(_eo_batch(_re_record(agent=None)), org_id=ORG) == []
    )


def test_rejected_edit_oversized_proposed_skipped() -> None:
    payload = _eo_batch(_re_record(proposed="x" * (256 * 1024 + 1)))
    assert parse_otlp_rejected_edits(payload, org_id=ORG) == []


def test_rejected_edit_and_edit_observation_self_filter_in_one_batch() -> None:
    # The client ships both record types in a single POST; each translator
    # must see only its own.
    batch = _eo_batch(_eo_record(), _re_record())
    [observation] = parse_otlp_edit_observations(batch, org_id=ORG)
    [rejected] = parse_otlp_rejected_edits(batch, org_id=ORG)
    assert observation.call_id == "toolu-eo-1"
    assert rejected.call_id == "toolu-re-1"
    # And a decision translator must not claim either of them.
    assert parse_otlp_decisions(batch, org_id=ORG) == []


def _retry_linkage_record(**over: object) -> dict[str, Any]:
    attrs: dict[str, object] = {
        "session.id": "sess-retry",
        "agent": "claude-code",
        "file_path": "/repo/app.py",
        "tool_name": "Edit",
        "rejected_call_id": "toolu-rejected",
        "accepted_call_id": "toolu-accepted",
    }
    attrs.update(over)
    return {
        "body": {"stringValue": "sediment.retry_linkage"},
        "timeUnixNano": "1782578510649000000",
        "attributes": _otlp_attrs({k: v for k, v in attrs.items() if v is not None}),
    }


def test_retry_linkage_happy_path_and_shuffle_determinism() -> None:
    first = _retry_linkage_record(
        rejected_call_id="toolu-r1", accepted_call_id="toolu-a1"
    )
    second = _retry_linkage_record(
        rejected_call_id="toolu-r2", accepted_call_id="toolu-a2"
    )
    forward = parse_otlp_retry_linkages(_eo_batch(first, second), org_id=ORG)
    reverse = parse_otlp_retry_linkages(_eo_batch(second, first), org_id=ORG)
    generated = {"retry_linkage_id", "captured_at"}
    assert [fact.model_dump(exclude=generated) for fact in forward] == [
        fact.model_dump(exclude=generated) for fact in reversed(reverse)
    ]
    assert forward[0].tool_name == "Edit"
    assert forward[0].raw == {}


def test_retry_linkage_missing_identifiers_count_closed_skip_reasons() -> None:
    fields = {
        "session.id": RetryLinkageSkipReason.MISSING_SESSION,
        "agent": RetryLinkageSkipReason.MISSING_AGENT_HARNESS,
        "file_path": RetryLinkageSkipReason.MISSING_FILE_PATH,
        "tool_name": RetryLinkageSkipReason.MISSING_TOOL_NAME,
        "rejected_call_id": RetryLinkageSkipReason.MISSING_REJECTED_CALL_ID,
        "accepted_call_id": RetryLinkageSkipReason.MISSING_ACCEPTED_CALL_ID,
    }
    for field, reason in fields.items():
        skipped: Counter[RetryLinkageSkipReason] = Counter()
        assert (
            parse_otlp_retry_linkages(
                _eo_batch(_retry_linkage_record(**{field: None})),
                org_id=ORG,
                skip_counts=skipped,
            )
            == []
        )
        assert skipped == Counter({reason: 1}), field

    missing_time = _retry_linkage_record()
    del missing_time["timeUnixNano"]
    skipped = Counter()
    assert (
        parse_otlp_retry_linkages(
            _eo_batch(missing_time), org_id=ORG, skip_counts=skipped
        )
        == []
    )
    assert skipped == Counter({RetryLinkageSkipReason.MISSING_EVENT_TIME: 1})


def test_retry_linkage_malformed_attributes_fail_soft_and_keep_sibling() -> None:
    malformed = _retry_linkage_record()
    malformed["attributes"] = {"not": "a list"}
    skipped: Counter[RetryLinkageSkipReason] = Counter()
    [fact] = parse_otlp_retry_linkages(
        _eo_batch(malformed, _retry_linkage_record()),
        org_id=ORG,
        skip_counts=skipped,
    )
    assert fact.accepted_call_id == "toolu-accepted"
    assert skipped == Counter({RetryLinkageSkipReason.MALFORMED_ATTRIBUTES: 1})
