# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consumer materialization refuses oversized populations before publication."""

from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from sediment_export import compatibility
from sediment_export.derived_bundle import (
    BundleCapacityError,
    BundleLimits,
    BundleRecordStore,
)
from sediment_export.staged_rows import ExportRowStore
from test_consumer_compatibility import _dpo_rows, _sft_rows
from test_derived_bundle_io import _bundle


def _limit(monkeypatch, size):
    monkeypatch.setattr(
        compatibility,
        "_PROFILE_LIMITS",
        BundleLimits(max_materialized_bytes=size),
        raising=False,
    )


@pytest.mark.parametrize(
    "name", ["fireworks-sft-v1", "hf-trl-sft-v1", "fireworks-dpo-v2", "hf-trl-dpo-v2"]
)
def test_profile_refuses_file_backed_population_before_decoding(
    tmp_path, monkeypatch, name
):
    source = _sft_rows() if "sft" in name else _dpo_rows()
    with ExportRowStore(temporary_parent=tmp_path) as stage:
        rows = stage.records("source")
        rows.extend(source)
        rows.seal()
        _limit(monkeypatch, rows.encoded_bytes - 1)

        def oversized_decode(value):
            pytest.fail("oversized file-backed population was decoded")

        monkeypatch.setattr(rows, "_restore", oversized_decode)
        with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
            compatibility.adapt_training_rows(rows, name)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "name", ["fireworks-sft-v1", "hf-trl-sft-v1", "fireworks-dpo-v2", "hf-trl-dpo-v2"]
)
def test_profile_stops_generator_before_accumulating_whole_population(
    monkeypatch, name
):
    [row] = _sft_rows() if "sft" in name else _dpo_rows()
    _limit(monkeypatch, 8 * 1024)

    def repeated():
        for _ in range(100):
            yield row
        pytest.fail("profile exhausted an oversized population")

    with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
        compatibility.adapt_training_rows(repeated(), name)


@pytest.mark.parametrize(
    "name", ["fireworks-sft-v1", "hf-trl-sft-v1", "fireworks-dpo-v2", "hf-trl-dpo-v2"]
)
def test_file_backed_profile_keeps_exact_small_population_results(tmp_path, name):
    source = _sft_rows() if "sft" in name else _dpo_rows()
    expected = compatibility.adapt_training_rows(source, name)
    with ExportRowStore(temporary_parent=tmp_path) as stage:
        rows = stage.records("source")
        rows.extend(source)
        rows.seal()
        assert json.dumps(
            asdict(compatibility.adapt_training_rows(rows, name))
        ) == json.dumps(asdict(expected))


@pytest.mark.parametrize("name", ["swe-bench-tasks-v1", "nemo-gym-rollouts-v1"])
def test_rlvr_profile_preflights_sources_before_hydration(tmp_path, monkeypatch, name):
    from sediment_export.consumer_rlvr import ConsumerSettings, export_rlvr_profile

    base = _bundle()
    profile = compatibility.get_profile(name)
    versions = dict(profile.dependencies)
    monkeypatch.setattr(compatibility, "version", versions.__getitem__)
    with BundleRecordStore(temporary_parent=tmp_path) as stage:
        rollouts = stage.records("rollouts")
        rollouts.extend(base.rollouts)
        rollouts.seal()
        calls = stage.records("inference_calls")
        calls.extend(base.inference_calls)
        calls.seal()
        bundle = replace(base, rollouts=rollouts, inference_calls=calls)
        _limit(monkeypatch, 1)

        def oversized_decode(value):
            pytest.fail("oversized Rollout population was decoded")

        monkeypatch.setattr(rollouts, "_restore", oversized_decode)
        with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
            export_rlvr_profile(
                bundle, None, tmp_path / "absent", name, ConsumerSettings()
            )
        assert not (tmp_path / "absent").exists()


def test_profile_refusal_preserves_destination_and_removes_staging(
    tmp_path, monkeypatch
):
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "sentinel").write_text("preserved")
    _limit(monkeypatch, 1)
    with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
        compatibility.write_compatible_export(
            _sft_rows(), destination, "fireworks-sft-v1", split_enabled=False
        )
    assert list(destination.iterdir()) == [destination / "sentinel"]
    assert (destination / "sentinel").read_text() == "preserved"
    assert list(tmp_path.iterdir()) == [destination]


def test_profile_publication_hashes_without_reading_complete_files(
    tmp_path, monkeypatch
):
    def no_readback(path):
        pytest.fail("profile hashing read the complete file")

    monkeypatch.setattr(Path, "read_bytes", no_readback)
    result = compatibility.write_compatible_export(
        _sft_rows(), tmp_path / "result", "fireworks-sft-v1", split_enabled=False
    )
    assert result["rows"] == 1


def test_nemo_projection_refuses_expanded_rows_before_complete_collection(
    tmp_path, monkeypatch
):
    import tempfile
    from datetime import timedelta
    from sediment_core import (
        CIOutcome,
        CIProvider,
        CIResult,
        InferenceMessage,
        TextPart,
    )
    from sediment_core.store import InferenceCallIdentity
    from sediment_derive.repository_identity import repository_identity_evidence_of
    from sediment_derive import model_call_ids
    from sediment_derive.rollout import project_session_turns
    from sediment_export import rlvr
    from sediment_export.consumer_rlvr import ConsumerSettings, export_rlvr_profile
    from sediment_export.derived_bundle import _json_chunks, validate_derived_bundle

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    base = _bundle()
    calls = tuple(
        base.inference_calls[0].model_copy(
            update={
                "inference_call_id": f"call-{index}",
                "model_call_id": f"model-{index}",
                "input_messages": [
                    InferenceMessage(
                        role="user", parts=[TextPart(content=f"unrelated {index}")]
                    )
                ],
                "observed_at": base.as_of - timedelta(seconds=20 - index),
            }
        )
        for index in range(20)
    )
    segments, _, _ = project_session_turns(calls, [])
    assert len(segments) == 20
    outcome = CIOutcome(
        outcome_id="outcome",
        org_id=base.org_id,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run",
        repo=base.rollouts[0].commits[0].repo,
        commit_sha=base.rollouts[0].commits[0].commit_sha,
        branch="main",
        result=CIResult.PASSED,
        captured_at=base.as_of,
        raw={"content": "x" * 20_000},
    )
    bundle = replace(
        base,
        attributed_completions=(),
        inference_calls=calls,
        repository_identities=(repository_identity_evidence_of(outcome),),
        rollouts=(
            replace(base.rollouts[0], segments=segments, terminal_outcomes=[outcome]),
        ),
        inference_call_identities=tuple(
            InferenceCallIdentity(
                call.inference_call_id,
                call.org_id,
                call.session_id,
                call.observed_at,
                tuple(sorted(model_call_ids(call))),
            )
            for call in calls
        ),
    )
    validate_derived_bundle(bundle)
    size = sum(
        len(chunk)
        for rows in (bundle.inference_calls, bundle.rollouts)
        for row in rows
        for chunk in _json_chunks(row)
    )
    _limit(monkeypatch, size + 1)
    versions = dict(compatibility.get_profile("nemo-gym-rollouts-v1").dependencies)
    monkeypatch.setattr(compatibility, "version", versions.__getitem__)
    original_body = rlvr._body
    emitted = 0

    def counted_body(row):
        nonlocal emitted
        emitted += 1
        assert emitted < 10, "projector retained the oversized complete population"
        return original_body(row)

    monkeypatch.setattr(rlvr, "_body", counted_body)
    config = ConsumerSettings.model_validate(
        {"nemo": {"parallel_tool_calls": False, "tool_choice": "auto", "tools": []}}
    )
    with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
        export_rlvr_profile(
            bundle, None, tmp_path / "absent", "nemo-gym-rollouts-v1", config
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("target", ["nemo", "swe"])
def test_native_projection_sink_preserves_rows_counts_and_failure(
    tmp_path, postgres_store_factory, target
):
    from test_rlvr import _scenario
    from sediment_export.rlvr import project_nemo_gym_rollouts, project_swe_bench_tasks
    from sediment_export import VerifierCommands

    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    owner = project_nemo_gym_rollouts if target == "nemo" else project_swe_bench_tasks
    args = (
        (rollouts, VerifierCommands.empty())
        if target == "nemo"
        else (rollouts, mirrors, VerifierCommands.empty())
    )
    expected = owner(*args)
    assert expected.rows
    captured = []
    result = owner(*args, row_sink=captured.append)
    assert result.rows == []
    assert result.skipped == expected.skipped
    assert captured == expected.rows

    def failed_sink(row):
        raise BundleCapacityError("sink capacity")

    with pytest.raises(BundleCapacityError, match="sink capacity"):
        owner(*args, row_sink=failed_sink)


def test_adapted_data_and_evidence_share_one_budget(monkeypatch):
    from sediment_export.jsonl import ExportRow

    _limit(monkeypatch, 2_000)
    consumed = 0

    def pairs():
        nonlocal consumed
        for _ in range(100):
            consumed += 1
            yield ExportRow("train", {"data": "x" * 700}), {"source": "y" * 700}

    with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
        compatibility.aligned_rows(pairs())
    assert consumed == 2


def test_profile_publication_rejects_oversized_adapted_population(
    tmp_path, monkeypatch
):
    adapted = compatibility.adapt_training_rows(_sft_rows(), "fireworks-sft-v1")
    _limit(monkeypatch, 1)
    with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
        compatibility.publish_compatible_rows(
            adapted,
            tmp_path / "absent",
            compatibility.get_profile("fireworks-sft-v1"),
            split_enabled=False,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("loader", ["settings", "hf"])
def test_consumer_file_capacity_precedes_decoding(tmp_path, monkeypatch, loader):
    from sediment_export.consumer_rlvr import load_consumer_settings

    source = tmp_path / "oversized.json"
    source.write_text("x" * 100)
    _limit(monkeypatch, 50)
    if loader == "settings":
        read = load_consumer_settings
    else:
        # Exercise file decoding without requiring the optional upstream runtime.
        monkeypatch.setattr(compatibility, "hf_dataset", lambda rows, name: list(rows))

        def read(path):
            return compatibility.load_hf_dataset(path, "hf-trl-sft-v1")

    with pytest.raises(BundleCapacityError, match="consumer profile.*byte budget"):
        read(source)
