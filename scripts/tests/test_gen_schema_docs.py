# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated schema reference.

The page's promise is that a field cannot ship undocumented and that the
types it prints are honest, so the checks are: every documented shape lists
every one of its fields, an unmapped field fails loudly instead of rendering
a blank cell, the type renderer does not silently flatten a timestamp or a
boolean, no cell smuggles a pipe into a markdown table, and ``--check``
actually fails on a stale page.

The type renderer must handle two Python type relationships:
``AwareDatetime`` does not subclass ``datetime`` (so a subclass test alone
renders timestamps as ``object``), and ``bool`` subclasses ``int`` (so scalar
order decides whether a boolean renders as an integer).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from typing import Annotated
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import AwareDatetime, BaseModel, Field

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_schema_docs.py"
PAGE = REPO_ROOT / "docs" / "reference" / "schema.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_schema_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_schema_docs"] = module
    spec.loader.exec_module(module)
    return module


module = _load_module()


def _sections(page: str) -> dict[str, str]:
    """Page split per `### <Shape>` heading, so an assertion about one shape
    cannot be satisfied by another shape's table."""
    out: dict[str, str] = {}
    name, buf = None, []
    for line in page.splitlines():
        if line.startswith("### "):
            if name:
                out[name] = "\n".join(buf)
            name, buf = line[4:].strip(), []
        elif name:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return out


def test_every_shape_documents_every_field():
    sections = _sections(module.render())
    for _, _, members in module.GROUPS:
        for cls, _purpose in members:
            assert cls.__name__ in sections, f"{cls.__name__} has no section"
            body = sections[cls.__name__]
            for name in module._field_names(cls):
                assert f"| `{name}` |" in body, f"{cls.__name__}.{name} missing"


def test_unmapped_field_fails_loudly():

    @dataclasses.dataclass(frozen=True)
    class Unmapped:
        a_field_nobody_described: str

    with pytest.raises(SystemExit) as excinfo:
        module._describe(Unmapped, "a_field_nobody_described")
    assert "a_field_nobody_described" in str(excinfo.value)


def test_timestamps_are_not_flattened_to_object():
    """AwareDatetime is a marker class, not a datetime subclass."""
    assert module._render_type(AwareDatetime, set()) == "string (RFC 3339)"

    page = module.render()
    for line in page.splitlines():
        if line.startswith(("| `captured_at`", "| `occurred_at`")):
            assert "string (RFC 3339)" in line, line


def test_booleans_do_not_render_as_integers():
    """bool subclasses int, so scalar order decides this."""
    assert module._render_type(bool, set()) == "boolean"
    assert module._render_type(int, set()) == "integer"


def test_decimal_prices_render_as_exact_json_strings():
    assert module._render_type(Decimal, set()) == "decimal string"
    assert module._schema_for_type(Decimal, {}) == {
        "type": "string",
        "format": "decimal",
        "pattern": r"^(?:-0(?:\.0+)?|0(?:\.\d+)?|[1-9]\d*(?:\.\d+)?)$",
    }


def test_variadic_tuple_constraints_are_preserved_in_json_schema():
    annotation = Annotated[tuple[str, ...], Field(min_length=1)]

    assert module._schema_for_type(annotation, {})["minItems"] == 1


def test_optional_renders_without_a_table_breaking_pipe():
    assert module._render_type(str | None, set()) == "string or null"

    for line in module.render().splitlines():
        if line.startswith("| `") and not line.startswith("|---"):
            assert line.count("|") == 4, f"cell count wrong, stray pipe: {line}"


def test_nested_shapes_and_enums_link_to_their_sections():
    page = module.render()
    assert "array of [`InferenceMessage`](#inferencemessage)" in page
    assert "[`AttributionSource`](#attributionsource)" in page
    for anchor in ("#inferencemessage", "#attributionsource", "#cioutcome"):
        assert f"### {anchor[1:]}" in page.lower()


def test_trainer_message_contracts_are_documented() -> None:
    sections = _sections(module.render())

    for name in (
        "TrainerFunctionCall",
        "TrainerToolCall",
        "TrainerTextMessage",
        "TrainerAssistantContentMessage",
        "TrainerAssistantThinkingMessage",
        "TrainerAssistantToolCallMessage",
        "TrainerToolMessage",
    ):
        assert name in sections

    assert "[`TrainerTextMessage`](#trainertextmessage)" in sections["DPOPair"]
    assert "array of never (must be empty)" in sections["DPOPair"]
    assert (
        "| `thinking` | string (optional) |"
        in sections["TrainerAssistantContentMessage"]
    )
    assert "| `content` | string |" in sections["TrainerAssistantContentMessage"]


def test_rlvr_nested_contracts_are_documented() -> None:
    sections = _sections(module.render())

    for name in (
        "RLVRDecisionRow",
        "RLVRInferenceMessageRow",
        "RLVRTurnRow",
        "NemoGymResponsesCreateParams",
        "NemoGymResponse",
    ):
        assert name in sections

    assert "array of [`RLVRTurnRow`](#rlvrturnrow)" in sections["SedimentRolloutRow"]
    assert "array of [`CIOutcome`](#cioutcome)" in sections["SedimentTaskRow"]
    assert "[`CIResolution`](#ciresolution)" in sections["SedimentTaskRow"]
    assert "(optional)" in sections["NemoGymMetadata"]
    assert "or null (optional)" not in sections["NemoGymMetadata"]
    assert "| `reward` | number (optional) |" in sections["NemoGymRolloutRow"]
    assert (
        "| `finish_reason` | string (optional) |" in sections["RLVRInferenceMessageRow"]
    )


def test_no_row_claims_null_on_a_non_nullable_field():
    """COMMON prose is shared across shapes, so a description mentioning null
    can land on a field that is required in one of them — the type column and
    the sentence beside it then disagree. `RejectedEdit.call_id` inheriting
    `call_id`'s "Null when the source emits none" was exactly that.
    """
    for line in module.render().splitlines():
        if not line.startswith("| `") or line.startswith("|---"):
            continue
        _, field, type_cell, meaning, _ = line.split("|")
        if "null" in type_cell:
            continue
        assert "Null" not in meaning and "null" not in meaning, (
            f"{field.strip()} is not nullable but its description mentions "
            f"null:{meaning}"
        )


def test_render_is_deterministic():
    assert module.render() == module.render()


def test_committed_page_is_current():
    assert PAGE.read_text(encoding="utf-8") == module.render(), (
        "docs/reference/schema.md is stale — run "
        "`uv run python scripts/gen_schema_docs.py`"
    )


def test_check_mode_fails_on_a_stale_page(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "schema.md"
    stale.write_text("# Schema reference\n\nnot what the generator makes\n")
    monkeypatch.setattr(module, "OUT_PATH", stale)

    assert module.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err

    assert module.main([]) == 0
    assert module.main(["--check"]) == 0


def test_facts_are_pydantic_and_derived_shapes_are_frozen():
    """The grouping is a claim about the code, not just a heading: facts are
    models, everything downstream is a frozen dataclass (AGENTS.md rule 5)."""
    groups = {title: members for title, _, members in module.GROUPS}
    for cls, _ in groups["Facts"]:
        assert issubclass(cls, BaseModel), cls
    for title in ("Derived artifacts", "Training rows"):
        for cls, _ in groups[title]:
            assert dataclasses.is_dataclass(cls), cls
            assert cls.__dataclass_params__.frozen, cls


def test_canonical_content_length_and_identity_pattern_are_published():
    import json
    from sediment_core import CIReason, ScalarIdentity

    reason = module._schema_for_type(CIReason, {})
    assert reason == {"type": "string", "maxLength": 4096}
    identity = module._schema_for_type(ScalarIdentity, {})
    assert identity["pattern"] == r"^[^\u0000\uD800-\uDFFF]*$"
    assert json.loads(json.dumps(identity)) == identity
