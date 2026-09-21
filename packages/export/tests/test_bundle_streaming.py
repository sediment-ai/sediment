# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bundle resource behavior through the public storage boundaries."""

from dataclasses import replace
import tracemalloc

import pytest

from sediment_export import derived_bundle as implementation
from sediment_export.jsonl import ExportRow, write_jsonl
from test_derived_bundle_io import _bundle, _tree_bytes
from export_factories import message


def _large_repeated_bundle():
    bundle = _bundle()
    prompt = message("user", "x" * (256 * 1024))
    call = bundle.inference_calls[0].model_copy(update={"input_messages": [prompt]})
    rollout = replace(
        bundle.rollouts[0],
        segments=[[replace(bundle.rollouts[0].segments[0][0], new_messages=[prompt])]],
    )
    return replace(bundle, inference_calls=(call,), rollouts=(rollout,) * 48)


def test_bundle_writer_does_not_materialize_member_bytes(tmp_path):
    bundle = _large_repeated_bundle()
    tracemalloc.start()
    try:
        implementation.write_derived_bundle(bundle, tmp_path / "bundle")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


def test_jsonl_writer_consumes_rows_without_retaining_population(tmp_path):
    def rows():
        for index in range(64):
            yield ExportRow(
                "eval" if index % 2 else "train",
                {"text": str(index) + "x" * (256 * 1024)},
            )

    tracemalloc.start()
    try:
        result = write_jsonl(rows(), tmp_path / "rows.jsonl", split_enabled=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sorted(result.written.values()) == [32, 32]
    assert peak < 4 * 1024 * 1024


def test_file_backed_bundle_is_validated_private_and_repeatable(tmp_path):
    bundle = _bundle()
    source = implementation.write_derived_bundle(bundle, tmp_path / "source")
    expected = _tree_bytes(source)
    with implementation.open_derived_bundle(
        source, temporary_parent=tmp_path
    ) as opened:
        assert not isinstance(opened.inference_calls, tuple)
        assert opened.inference_calls[0] == bundle.inference_calls[0]
        assert tuple(opened.rollouts) == bundle.rollouts
        # The validated view owns a snapshot, not mutable external file paths.
        (source / "rollouts.jsonl").write_text("tampered")
        implementation.write_derived_bundle(opened, tmp_path / "copy")
        assert _tree_bytes(tmp_path / "copy") == expected
        sequence = opened.inference_calls
    with pytest.raises(ValueError, match="closed"):
        sequence[0]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["copy", "source"]


def test_streaming_reader_memory_and_materialization(tmp_path):
    bundle = _large_repeated_bundle()
    source = implementation.write_derived_bundle(bundle, tmp_path / "source")
    tracemalloc.start()
    try:
        with implementation.open_derived_bundle(source) as opened:
            assert len(opened.rollouts) == 48
            assert sum(len(row.segments) for row in opened.rollouts) == 48
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert implementation.read_derived_bundle(source) == bundle


def test_record_and_staging_limits_clean_private_work(tmp_path):
    bundle = _large_repeated_bundle()
    limits = implementation.BundleLimits(max_record_bytes=1024, max_staging_bytes=2048)
    with pytest.raises(implementation.BundleCapacityError):
        implementation.write_derived_bundle(bundle, tmp_path / "refused", limits=limits)
    assert list(tmp_path.iterdir()) == []
    source = implementation.write_derived_bundle(_bundle(), tmp_path / "source")
    with pytest.raises(implementation.BundleCapacityError):
        with implementation.open_derived_bundle(
            source, limits=limits, temporary_parent=tmp_path
        ):
            pytest.fail("oversized input was exposed")
    assert list(tmp_path.iterdir()) == [source]


def test_record_store_lifetime_and_exact_encoding(tmp_path):
    bundle = _bundle()
    with implementation.BundleRecordStore(temporary_parent=tmp_path) as store:
        rows = store.records("inference_calls")
        rows.append(bundle.inference_calls[0])
        rows.seal()
        assert tuple(rows) == bundle.inference_calls
        assert rows[0] == bundle.inference_calls[0]
        with pytest.raises(ValueError, match="sealed"):
            rows.append(bundle.inference_calls[0])
        assert rows.path.read_bytes() == implementation._jsonl_bytes(
            bundle.inference_calls
        )
    assert list(tmp_path.iterdir()) == []


def test_streamed_bytes_match_lossless_reference_for_exceptional_values(tmp_path):
    bundle = _bundle()
    values = [
        float("nan"),
        float("inf"),
        -float("inf"),
        "\ud800\x00é😀",
        -0.0,
        10**100,
        {"z": ["x", 1], "a": None},
    ]
    with implementation.BundleRecordStore(temporary_parent=tmp_path) as store:
        records = store.records("inference_calls")
        for value in values:
            call = bundle.inference_calls[0].model_copy(
                update={"raw": {"value": value}}
            )
            records.append(call)
        records.seal()
        assert records.path.read_bytes() == implementation._jsonl_bytes(
            tuple(
                bundle.inference_calls[0].model_copy(update={"raw": {"value": value}})
                for value in values
            )
        )


def test_materialization_refuses_before_building_complete_tuples(tmp_path, monkeypatch):
    source = implementation.write_derived_bundle(
        _large_repeated_bundle(), tmp_path / "source"
    )
    limits = implementation.BundleLimits(max_materialized_bytes=1024)
    with pytest.raises(implementation.BundleCapacityError, match="materialization"):
        implementation.read_derived_bundle(source, limits=limits)
    with implementation.open_derived_bundle(source, limits=limits) as bundle:
        assert len(bundle.rollouts) == 48


def test_invalid_empty_segments_container_is_not_silently_repaired(tmp_path):
    bundle = _bundle()
    for segments in ({}, "", None):
        rollout = replace(bundle.rollouts[0], segments=segments)
        with pytest.raises(implementation.BundleValidationError):
            implementation.write_derived_bundle(
                replace(bundle, rollouts=(rollout,)), tmp_path / "invalid"
            )


def test_streaming_semantic_failure_never_exposes_unchecked_records(tmp_path):
    import hashlib
    import json

    source = implementation.write_derived_bundle(_bundle(), tmp_path / "source")
    member = source / "rollouts.jsonl"
    data = member.read_bytes().replace(b"write fibonacci", b"changed content")
    member.write_bytes(data)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["rollouts"].update(
        bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
    )
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(implementation.BundleValidationError, match="projection"):
        with implementation.open_derived_bundle(source, temporary_parent=tmp_path):
            pytest.fail("unchecked records escaped the reader")
    assert list(tmp_path.iterdir()) == [source]


def test_validation_keeps_compact_decision_and_outcome_indices(tmp_path):
    from test_derived_bundle_io import _decision, _outcome
    from sediment_derive.repository_identity import repository_identity_evidence_of

    bundle = replace(
        _bundle(),
        repository_identities=tuple(
            repository_identity_evidence_of(
                _outcome(outcome_id=f"outcome-{index}", run_id=f"run-{index}")
            )
            for index in range(32)
        ),
    )
    with implementation.BundleRecordStore(temporary_parent=tmp_path) as stage:
        rows = stage.records("attributed_completions")
        for index in range(32):
            rows.append(
                replace(
                    bundle.attributed_completions[0],
                    file_path=f"file-{index}.py",
                    decisions=[
                        _decision(
                            decision_id=f"decision-{index}",
                            raw={"value": "d" * (256 * 1024)},
                        )
                    ],
                    ci_outcomes=[
                        _outcome(
                            outcome_id=f"outcome-{index}",
                            run_id=f"run-{index}",
                            raw={"value": "c" * (256 * 1024)},
                        )
                    ],
                )
            )
        rows.seal()
        source = implementation.write_derived_bundle(
            replace(bundle, attributed_completions=rows), tmp_path / "source"
        )
    tracemalloc.start()
    try:
        with implementation.open_derived_bundle(source):
            pass
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


def test_private_fact_storage_preserves_surrogate_pair_codepoints(tmp_path):
    bundle = _bundle()
    prompt = message("user", "\ud83d\ude00")
    call = bundle.inference_calls[0].model_copy(update={"input_messages": [prompt]})
    with implementation.BundleRecordStore(
        temporary_parent=tmp_path, private=True
    ) as store:
        records = store.records("inference_calls")
        records.append(call)
        records.seal()
        assert records[0].input_messages[0].parts[0].content == "\ud83d\ude00"
        assert len(records[0].input_messages[0].parts[0].content) == 2


def _session_capacity_bundle(*, prompt_bytes=0, raw_bytes=0):
    bundle = _bundle()
    calls = tuple(
        bundle.inference_calls[0].model_copy(
            update={
                "inference_call_id": f"inference-{index:02}",
                "model_call_id": f"call-{index:02}",
                "input_messages": [message("user", "p" * prompt_bytes)],
                "raw": {"payload": "r" * raw_bytes},
            }
        )
        for index in range(8)
    )
    segments, _, _ = implementation.project_session_turns(calls, [])
    identities = tuple(
        replace(
            bundle.inference_call_identities[0],
            inference_call_id=call.inference_call_id,
            call_ids=(call.model_call_id,),
        )
        for call in calls
    )
    return replace(
        bundle,
        attributed_completions=(),
        inference_calls=calls,
        inference_call_identities=identities,
        rollouts=(replace(bundle.rollouts[0], segments=segments),),
    )


def test_offline_session_limit_precedes_complete_history_hydration(
    tmp_path, monkeypatch
):
    bundle = _session_capacity_bundle(prompt_bytes=256 * 1024)
    source = implementation.write_derived_bundle(bundle, tmp_path / "source")
    monkeypatch.setattr(implementation, "INFERENCE_SESSION_BYTES_LIMIT", 1024 * 1024)
    monkeypatch.setattr(
        implementation,
        "project_session_turns",
        lambda *args, **kwargs: pytest.fail("oversized Session reached hydration"),
    )
    with pytest.raises(implementation.BundleCapacityError, match="Session"):
        with implementation.open_derived_bundle(source, temporary_parent=tmp_path):
            pytest.fail("oversized Session was exposed")
    assert list(tmp_path.iterdir()) == [source]


def test_session_validation_limit_counts_content_without_raw(tmp_path, monkeypatch):
    bundle = _session_capacity_bundle(prompt_bytes=128, raw_bytes=256 * 1024)
    source = implementation.write_derived_bundle(bundle, tmp_path / "source")
    monkeypatch.setattr(implementation, "INFERENCE_SESSION_BYTES_LIMIT", 8 * 1024)
    with implementation.open_derived_bundle(
        source, temporary_parent=tmp_path
    ) as result:
        assert len(result.inference_calls) == 8
        assert len(result.rollouts[0].segments) == 8
    assert list(tmp_path.iterdir()) == [source]


def test_training_revalidates_session_limit_before_publication(tmp_path, monkeypatch):
    from sediment_export.bounded_training import project_training_bundle

    bundle = _session_capacity_bundle(prompt_bytes=256 * 1024)
    monkeypatch.setattr(implementation, "INFERENCE_SESSION_BYTES_LIMIT", 1024 * 1024)
    with pytest.raises(implementation.BundleCapacityError, match="Session"):
        with project_training_bundle(
            bundle, objective="sft", temporary_parent=tmp_path
        ):
            pytest.fail("training accepted a bundle above the Session budget")
    assert list(tmp_path.iterdir()) == []
