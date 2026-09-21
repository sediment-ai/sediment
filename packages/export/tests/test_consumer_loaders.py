# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in qualification against installed pinned upstream implementations."""

import copy
import json
import os
from dataclasses import replace

import pytest

if not os.environ.get("SEDIMENT_COMPAT_TESTS"):
    pytest.skip(
        "run the isolated consumer compatibility matrix", allow_module_level=True
    )

from sediment_export.compatibility import (
    CompatibilityError,
    adapt_training_rows,
    get_profile,
    hf_dataset,
    publish_compatible_rows,
    require_dependencies,
    write_compatible_export,
)
from test_consumer_compatibility import _dpo_rows, _sft_rows
from test_rlvr_consumer_profiles import _trajectory


def test_hf_empty_projection_preserves_no_write_guard(tmp_path):
    summary = write_compatible_export(
        [], tmp_path / "absent", "hf-trl-sft-v1", split_enabled=True
    )
    assert summary["rows"] == 0
    assert not (tmp_path / "absent").exists()


def test_hf_loader_preserves_late_nested_shapes_and_reasoning():
    profile = "hf-trl-sft-v1"
    adapted = adapt_training_rows(_sft_rows(), profile)
    first = copy.deepcopy(adapted.rows[0].body)
    first["completion"] = [{"role": "assistant", "content": "x" * 100_000}]
    later = copy.deepcopy(adapted.rows[0].body)
    later["completion"][0]["tool_calls"][0]["function"]["arguments"] = {
        "late": [True, None, {"different": 3}]
    }
    values = [first] * 12 + [later]
    dataset = hf_dataset(values, profile)
    assert list(dataset) == values
    with pytest.raises(CompatibilityError):
        hf_dataset([{**later, "unexpected": True}], profile)


def test_hf_real_trl_template_keeps_target_reasoning_and_tool_arguments():
    from transformers import AutoTokenizer
    from trl import apply_chat_template

    path = os.environ.get("SEDIMENT_COMPAT_TOKENIZER", "Qwen/Qwen3-0.6B")
    tokenizer = AutoTokenizer.from_pretrained(
        path, revision="c1899de289a04d12100db370d81485cdf75e47ca"
    )
    [row] = adapt_training_rows(_sft_rows(), "hf-trl-sft-v1").rows
    rendered = apply_chat_template(
        hf_dataset([row.body], "hf-trl-sft-v1")[0], tokenizer
    )
    assert "reasoning sentinel" in rendered["completion"]
    assert '"key": [1, null]' in rendered["completion"]
    assert "Earlier answer" not in rendered["completion"]
    assert "Earlier answer" in rendered["prompt"]


def test_hf_dpo_uses_real_recipe_projection():
    canonical = _dpo_rows()
    [row] = adapt_training_rows(canonical, "hf-trl-dpo-v2").rows
    restored = hf_dataset([row.body], "hf-trl-dpo-v2")[0]
    assert restored == row.body
    assert restored["chosen"] == [
        {
            "role": "assistant",
            "content": "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)",
        }
    ]
    assert restored["rejected"] == [
        {"role": "assistant", "content": "def fib(n): return 0"}
    ]
    with pytest.raises(CompatibilityError):
        hf_dataset([row.body | {"unsupported": True}], "hf-trl-dpo-v2")


def test_fireworks_profiles_publish_documented_format_and_reject_unsupported(tmp_path):
    canonical = _dpo_rows()
    [fireworks] = adapt_training_rows(canonical, "fireworks-dpo-v2").rows
    assert set(fireworks.body) == {"input", "preferred_output", "non_preferred_output"}
    assert len(fireworks.body["preferred_output"]) == 1
    for name, rows in (
        ("fireworks-dpo-v2", canonical),
        ("fireworks-sft-v1", _sft_rows()),
    ):
        summary = write_compatible_export(
            rows, tmp_path / name, name, split_enabled=True
        )
        assert summary["rows"] == 1
        # The malformed canonical boundary must fail before publication.
        malformed = replace(rows[0], body=rows[0].body | {"unknown": True})
        with pytest.raises(CompatibilityError):
            write_compatible_export(
                [malformed], tmp_path / "absent", name, split_enabled=True
            )
        assert not (tmp_path / "absent").exists()
    changed = copy.deepcopy(canonical[0].body)
    changed["prompt"][0]["role"] = "developer"
    with pytest.raises(CompatibilityError, match="developer"):
        write_compatible_export(
            [replace(canonical[0], body=changed)],
            tmp_path / "absent",
            "fireworks-dpo-v2",
            split_enabled=True,
        )


@pytest.mark.parametrize("name", ["fireworks-sft-v1", "fireworks-dpo-v2"])
@pytest.mark.parametrize("invalid", ["developer", "late_system", "repeated_system"])
def test_fireworks_profiles_reject_unsupported_roles_before_publication(
    name, invalid, tmp_path
):
    [canonical] = _sft_rows() if "sft" in name else _dpo_rows()
    body = copy.deepcopy(canonical.body)
    instruction = {"role": "system", "content": "Use the captured evidence."}
    if invalid == "developer":
        body["prompt"].insert(0, instruction | {"role": "developer"})
    else:
        body["prompt"].append(instruction)
        if invalid == "repeated_system":
            body["prompt"].insert(0, instruction)
    destination = tmp_path / "absent"
    with pytest.raises(CompatibilityError, match="Fireworks"):
        write_compatible_export(
            [replace(canonical, body=body)], destination, name, split_enabled=True
        )
    assert not destination.exists()


@pytest.mark.parametrize("name", ["fireworks-sft-v1", "fireworks-dpo-v2"])
def test_fireworks_profiles_preserve_leading_system_message(name, tmp_path):
    [canonical] = _sft_rows() if "sft" in name else _dpo_rows()
    body = copy.deepcopy(canonical.body)
    instruction = {"role": "system", "content": "Use the captured evidence."}
    body["prompt"].insert(0, instruction)
    summary = write_compatible_export(
        [replace(canonical, body=body)], tmp_path / "export", name, split_enabled=True
    )
    assert summary["rows"] == 1
    exported = json.loads((tmp_path / "export" / "data.train.jsonl").read_text())
    messages = exported["messages"] if "sft" in name else exported["input"]["messages"]
    assert messages[0] == instruction


def test_nemo_public_parser_roundtrips_real_projection_and_rejects_loss(tmp_path):
    from sediment_export.consumer_rlvr import (
        ConsumerSettings,
        adapt_nemo_rows,
        validate_rlvr_consumer_rows,
    )

    profile = get_profile("nemo-gym-rollouts-v1")
    require_dependencies(profile)
    rows, calls = _trajectory()
    settings = ConsumerSettings.model_validate(
        {"nemo": {"parallel_tool_calls": False, "tool_choice": "auto", "tools": []}}
    )
    adapted = adapt_nemo_rows(rows, calls, settings)
    summary = publish_compatible_rows(
        adapted, tmp_path / "nemo", profile, split_enabled=True
    )
    assert summary["rows"] == 1
    extra_rows, extra_calls = _trajectory(prefix="other-")
    both = rows + extra_rows
    lookup = calls | extra_calls
    _assert_deterministic_files(
        adapt_nemo_rows(both, lookup, settings),
        adapt_nemo_rows(reversed(both), dict(reversed(list(lookup.items()))), settings),
        profile,
        tmp_path,
    )
    [row] = adapted.rows
    malformed = copy.deepcopy(row.body)
    malformed["response"]["output"][0]["content"] = [
        {"type": "reasoning_text", "text": "would be lost"}
    ]
    with pytest.raises(CompatibilityError, match="altered or dropped"):
        validate_rlvr_consumer_rows([replace(row, body=malformed)], "nemo-gym")


def test_swe_real_projection_loads_and_builds_runtime(tmp_path, postgres_store_factory):
    from test_rlvr import _scenario, ORG
    from export_factories import inference_call, message
    from sediment_export.derived_bundle import build_derived_bundle
    from sediment_export import VerifierCommands
    from sediment_export.rlvr import project_swe_bench_tasks
    from sediment_export.consumer_rlvr import (
        ConsumerSettings,
        adapt_swe_rows,
        export_rlvr_profile,
        validate_rlvr_consumer_rows,
    )
    from swebench.harness.constants import START_TEST_OUTPUT, END_TEST_OUTPUT

    profile = get_profile("swe-bench-tasks-v1")
    require_dependencies(profile)
    rollouts, mirrors, *_, store = _scenario(postgres_store_factory, tmp_path)
    canonical = project_swe_bench_tasks(
        rollouts, mirrors, VerifierCommands.empty()
    ).rows
    [row] = canonical
    config = ConsumerSettings.model_validate_json(
        json.dumps(
            {
                "tasks": {
                    row.body["instance_id"]: {
                        "repo": row.body["repo"],
                        "base_commit": row.body["base_commit"],
                        "problem_statement": "Implement fibonacci and pass the supplied test.",
                        "test_patch": "",
                        "hints_text": "",
                        "created_at": "2026-09-01T00:00:00Z",
                        "version": "fixture-1",
                        "environment_setup_commit": row.body["base_commit"],
                        "FAIL_TO_PASS": ["test_fibonacci"],
                        "PASS_TO_PASS": [],
                        "image": "fixture-image",
                        "eval_script": f"echo {START_TEST_OUTPUT}\npython -m pytest -rA\necho {END_TEST_OUTPUT}",
                        "log_parser": "parse_log_pytest",
                        "eval_type": "pass_and_fail",
                    }
                }
            }
        )
    )
    adapted = adapt_swe_rows(canonical, config)
    # Real unmatched work contributes canonical skips and broken continuation
    # contributes fragmentation; neither population belongs to adapter skips.
    for index in range(2):
        store.store_inference_call(
            inference_call(
                f"unattributed-{index}",
                org_id=ORG,
                session_id="unattributed",
                input_messages=[message("user", f"Unrelated request {index}")],
                output="Unattributed output",
            )
        )
    bundle = build_derived_bundle(store, mirrors, ORG)
    summary = export_rlvr_profile(bundle, mirrors, tmp_path / "swe", profile.id, config)
    assert summary["rows"] == 1
    manifest = json.loads((tmp_path / "swe" / "compatibility.json").read_text())
    assert manifest["canonical_skipped"] == summary["canonical_skipped"]
    assert manifest["canonical_skipped"]["no_attributed_commit"] > 0
    assert manifest["fragmented"] == dict(bundle.fragmented)
    assert manifest["fragmented"]
    assert manifest["skipped"] == {}
    another = replace(row, body=row.body | {"instance_id": "other-instance"})
    more_config = config.model_copy(
        update={
            "tasks": config.tasks
            | {"other-instance": config.tasks[row.body["instance_id"]]}
        }
    )
    _assert_deterministic_files(
        adapt_swe_rows([row, another], more_config),
        adapt_swe_rows([another, row], more_config),
        profile,
        tmp_path,
    )
    with pytest.raises(CompatibilityError, match="missing"):
        adapt_swe_rows(canonical, ConsumerSettings())
    bad = copy.deepcopy(adapted.rows[0].body)
    bad["log_parser"] = "not_registered"
    with pytest.raises(CompatibilityError, match="not registered"):
        validate_rlvr_consumer_rows([replace(adapted.rows[0], body=bad)], "swe-bench")


@pytest.fixture(autouse=True)
def canary_uses_installed_upstream_without_changing_product_pins(monkeypatch):
    if not os.environ.get("SEDIMENT_COMPAT_CANARY"):
        return
    import sediment_export.compatibility as compatibility
    from importlib.metadata import version

    # Test-process-only override: unchanged adapters exercise actual latest APIs.
    # An installer failure (including Python incompatibility) is canary drift too.
    from importlib.metadata import PackageNotFoundError

    profiles = []
    for profile in compatibility.PROFILES:
        try:
            dependencies = tuple(
                (package, version(package)) for package, _ in profile.dependencies
            )
        except PackageNotFoundError:
            profiles.append(profile)
        else:
            profiles.append(replace(profile, dependencies=dependencies))
    monkeypatch.setattr(compatibility, "PROFILES", tuple(profiles))


def _assert_deterministic_files(first, reordered, profile, tmp_path):
    assert first == reordered
    for directory, rows in (
        ("first", first),
        ("reordered", reordered),
        ("repeated", first),
    ):
        publish_compatible_rows(rows, tmp_path / directory, profile, split_enabled=True)
    expected = {p.name: p.read_bytes() for p in (tmp_path / "first").iterdir()}
    for directory in ("reordered", "repeated"):
        assert expected == {
            p.name: p.read_bytes() for p in (tmp_path / directory).iterdir()
        }


@pytest.mark.parametrize(
    "name", ["hf-trl-sft-v1", "hf-trl-dpo-v2", "fireworks-sft-v1", "fireworks-dpo-v2"]
)
def test_training_profiles_repeat_and_shuffle_identically(name, tmp_path):
    canonical = _sft_rows() if "sft" in name else _dpo_rows()
    another = replace(canonical[0], body=copy.deepcopy(canonical[0].body))
    another.body["prompt"][0]["content"] = "Another captured request"
    canonical.append(another)
    _assert_deterministic_files(
        adapt_training_rows(canonical, name),
        adapt_training_rows(reversed(canonical), name),
        get_profile(name),
        tmp_path,
    )


@pytest.mark.parametrize("name", ["hf-trl-dpo-v2", "fireworks-dpo-v2"])
def test_pinned_dpo_successors_publish_v2_evidence_and_refuse_historical_rows(
    name, tmp_path
):
    canonical = _dpo_rows()
    destination = tmp_path / "current"
    summary = write_compatible_export(canonical, destination, name, split_enabled=False)
    assert summary["rows"] == 1
    data = json.loads((destination / "data.jsonl").read_text())
    evidence = json.loads((destination / "evidence.jsonl").read_text())
    assert data["chosen" if name.startswith("hf") else "preferred_output"] == [
        {
            "role": "assistant",
            "content": "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)",
        }
    ]
    assert data["rejected" if name.startswith("hf") else "non_preferred_output"] == [
        {"role": "assistant", "content": "def fib(n): return 0"}
    ]
    assert evidence["metadata"]["recipe_version"] == 2
    assert evidence["metadata"]["schema_version"] == 4
    assert (
        evidence["canonical_schema"]
        == "https://sediment.so/schemas/training-rows/dpo-pair/v4.json"
    )
    historical = copy.deepcopy(canonical[0].body)
    historical["metadata"].update(
        recipe_version=1,
        schema_version=3,
        schema_id="https://sediment.so/schemas/training-rows/dpo-pair/v3.json",
    )
    before = copy.deepcopy(historical)
    with pytest.raises(CompatibilityError, match="canonical"):
        write_compatible_export(
            [replace(canonical[0], body=historical)],
            tmp_path / "historical",
            name,
            split_enabled=False,
        )
    assert not (tmp_path / "historical").exists()
    assert historical == before
    with pytest.raises(CompatibilityError, match="retired.*re-export"):
        write_compatible_export(
            canonical,
            tmp_path / "retired",
            name.removesuffix("v2") + "v1",
            split_enabled=False,
        )
    assert not (tmp_path / "retired").exists()
