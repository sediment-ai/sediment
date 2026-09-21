# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic, private, validated derived-bundle storage."""

from __future__ import annotations

import json
import hashlib
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sediment_core import (
    AgentHarness,
    CIOutcome,
    CIProvider,
    CIResult,
    DeveloperDecision,
    InteractionMode,
)
from sediment_derive import (
    AttributionSource,
    CommitRef,
    Provenance,
    Rollout,
    SessionAbandonment,
    Turn,
    render_scoring_text,
    split_of,
)
from sediment_export import (
    BundleValidationError,
    DerivationPolicy,
    DerivationScope,
    DerivedBundle,
    AttributedCompletion,
    read_derived_bundle,
    write_derived_bundle,
)
from export_factories import inference_call, message
from sediment_derive.repository_identity import repository_identity_evidence_of
from sediment_core.store import InferenceCallIdentity
from sediment_derive import model_call_ids

ORG = "acme-corp"
AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SHA = "1" * 40


def _bundle() -> DerivedBundle:
    policy = DerivationPolicy()
    prompt_message = message("user", "write fibonacci")
    call = inference_call(
        inference_call_id="inference-1",
        org_id=ORG,
        session_id="session-1",
        user_id="alice",
        model="claude-sonnet-5",
        input_messages=[prompt_message],
        output="def fibonacci(n): return n",
        model_call_id="call-1",
        observed_at=AT,
    )
    labeled = AttributedCompletion(
        org_id=ORG,
        session_id="session-1",
        inference_call_id=call.inference_call_id,
        repo="acme-corp/backend-service",
        commit_sha=SHA,
        file_path="math.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=[],
        ci_outcomes=[],
        provenance=Provenance(
            policy_version="5",
            quarantine_revision=0,
            policy_digest=policy.digest,
        ),
        split=split_of("session-1", policy.eval_fraction),
    )
    rollout = Rollout(
        org_id=ORG,
        session_id="session-1",
        segments=[
            [
                Turn(
                    new_messages=[prompt_message],
                    completion=render_scoring_text(call),
                    decisions=(),
                    inference_call_id=call.inference_call_id,
                )
            ]
        ],
        commits=[
            CommitRef(repo="acme-corp/backend-fork", commit_sha=SHA),
            CommitRef(repo="acme-corp/backend-service", commit_sha=SHA),
        ],
        attribution_source=AttributionSource.GIT_NOTES,
        terminal_outcomes=[],
        provenance=Provenance(
            policy_version="4",
            quarantine_revision=0,
            policy_digest=policy.digest,
        ),
        split=split_of("session-1", policy.eval_fraction),
    )
    return DerivedBundle(
        org_id=ORG,
        policy=policy,
        scope=DerivationScope(),
        as_of=AT,
        quarantine_revision=0,
        mirror_revisions={"acme-corp/backend-service": {"refs/heads/main": SHA}},
        attributed_completions=(labeled,),
        rollouts=(rollout,),
        inference_calls=(call,),
        inference_call_identities=(
            InferenceCallIdentity(
                inference_call_id=call.inference_call_id,
                org_id=call.org_id,
                session_id=call.session_id,
                observed_at=call.observed_at,
                call_ids=tuple(sorted(model_call_ids(call))),
            ),
        ),
        skipped={"rollout.no_match": 1},
        excluded={},
    )


def _tree_bytes(path: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in sorted(path.iterdir())}


def test_bundle_round_trip_has_fixed_layout_and_deterministic_bytes(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    first = tmp_path / "first"
    second = tmp_path / "second"

    write_derived_bundle(bundle, first)
    write_derived_bundle(bundle, second)

    assert {item.name for item in first.iterdir()} == {
        "manifest.json",
        "attributed_completions.jsonl",
        "rollouts.jsonl",
        "inference_calls.jsonl",
        "inference_call_identities.jsonl",
        "repository_identities.jsonl",
        "repository_renames.jsonl",
    }
    assert _tree_bytes(first) == _tree_bytes(second)
    assert read_derived_bundle(first) == bundle
    assert (
        len({(commit.repo, commit.commit_sha) for commit in bundle.rollouts[0].commits})
        == 2
    )
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundle_schema_version"] == 4
    assert manifest["policy_digest"] == bundle.policy.digest
    assert manifest["counts"] == {
        "inference_calls": 1,
        "inference_call_identities": 1,
        "repository_identities": 0,
        "repository_renames": 0,
        "attributed_completions": 1,
        "rollouts": 1,
    }
    assert "generated_at" not in manifest


def test_bundle_round_trip_preserves_abandonment_evidence(tmp_path: Path) -> None:
    bundle = _bundle()
    decision = DeveloperDecision(
        org_id=ORG,
        session_id="session-1",
        user_id="alice",
        agent_harness=AgentHarness.CODEX,
        file_path="math.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id="call-1",
        occurred_at=AT,
    )
    abandoned = replace(
        bundle.attributed_completions[0],
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[decision],
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="session-1",
            accepted_decisions=2,
            explicit_accepted_decisions=1,
            last_decision_at=AT,
            as_of=AT,
            provenance=Provenance(
                policy_version="6",
                quarantine_revision=0,
                policy_digest=bundle.policy.digest,
            ),
        ),
    )
    destination = tmp_path / "abandonment"
    write_derived_bundle(
        replace(bundle, attributed_completions=(abandoned,)), destination
    )

    assert read_derived_bundle(destination).attributed_completions == (abandoned,)


def test_bundle_reader_rejects_abandonment_without_explicit_accept(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    decision = DeveloperDecision(
        org_id=ORG,
        session_id="session-1",
        user_id="alice",
        agent_harness=AgentHarness.CODEX,
        file_path="math.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id="call-1",
        occurred_at=AT,
    )
    abandoned = replace(
        bundle.attributed_completions[0],
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[decision],
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="session-1",
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=AT,
            as_of=AT,
            provenance=Provenance(
                policy_version="6",
                quarantine_revision=0,
                policy_digest=bundle.policy.digest,
            ),
        ),
    )
    destination = tmp_path / "invalid-abandonment"
    write_derived_bundle(
        replace(bundle, attributed_completions=(abandoned,)), destination
    )
    artifact_path = destination / "attributed_completions.jsonl"
    row = json.loads(
        json.loads(artifact_path.read_text(encoding="utf-8"))["record_json"]
    )
    row["decisions"] = []
    data = (
        json.dumps(
            {"record_json": json.dumps(row, separators=(",", ":"), sort_keys=True)}
        )
        + "\n"
    ).encode()
    artifact_path.write_bytes(data)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = manifest["files"]["attributed_completions"]
    metadata["bytes"] = len(data)
    metadata["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="explicit accepted decision"):
        read_derived_bundle(destination)


def test_empty_jsonl_files_are_zero_bytes_and_every_mode_is_private(
    tmp_path: Path,
) -> None:
    empty = replace(
        _bundle(),
        attributed_completions=(),
        rollouts=(),
        inference_calls=(),
        skipped={},
    )
    destination = tmp_path / "empty"

    write_derived_bundle(empty, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for name in (
        "manifest.json",
        "attributed_completions.jsonl",
        "rollouts.jsonl",
        "inference_calls.jsonl",
    ):
        assert stat.S_IMODE((destination / name).stat().st_mode) == 0o600
    for name in (
        "attributed_completions.jsonl",
        "rollouts.jsonl",
        "inference_calls.jsonl",
    ):
        assert (destination / name).read_bytes() == b""


def test_writer_refuses_an_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "derived"
    destination.mkdir()

    with pytest.raises(FileExistsError, match="already exists"):
        write_derived_bundle(_bundle(), destination)


def test_reader_rejects_tampered_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "derived"
    write_derived_bundle(_bundle(), destination)
    with (destination / "inference_calls.jsonl").open("ab") as handle:
        handle.write(b"{}\n")

    with pytest.raises(BundleValidationError, match="does not match"):
        read_derived_bundle(destination)


def test_reader_rejects_superseded_schema_and_unsafe_path(tmp_path: Path) -> None:
    unsupported = tmp_path / "unsupported"
    write_derived_bundle(_bundle(), unsupported)
    manifest_path = unsupported / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_schema_version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleValidationError, match="bundle_schema_version"):
        read_derived_bundle(unsupported)

    unsafe = tmp_path / "unsafe"
    write_derived_bundle(_bundle(), unsafe)
    manifest_path = unsafe / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["inference_calls"]["path"] = "../inference_calls.jsonl"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleValidationError, match="path"):
        read_derived_bundle(unsafe)


def test_reader_rejects_boolean_bundle_schema_version(tmp_path: Path) -> None:
    destination = tmp_path / "boolean-version"
    write_derived_bundle(_bundle(), destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_schema_version"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="bundle_schema_version"):
        read_derived_bundle(destination)


def test_reader_rejects_self_consistent_but_invalid_canonical_rows(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "invalid-row"
    write_derived_bundle(_bundle(), destination)
    artifact_path = destination / "attributed_completions.jsonl"
    row = json.loads(
        json.loads(artifact_path.read_text(encoding="utf-8"))["record_json"]
    )
    row["similarity_score"] = "high"
    data = (
        json.dumps(
            {"record_json": json.dumps(row, separators=(",", ":"), sort_keys=True)}
        )
        + "\n"
    ).encode()
    artifact_path.write_bytes(data)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["attributed_completions"]["bytes"] = len(data)
    manifest["files"]["attributed_completions"]["sha256"] = hashlib.sha256(
        data
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="similarity_score"):
        read_derived_bundle(destination)


def test_reader_rejects_ci_outcome_without_schema_version(tmp_path: Path) -> None:
    bundle = _bundle()
    outcome = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run-1",
        repo="acme-corp/backend-service",
        commit_sha=SHA,
        branch="main",
        result=CIResult.PASSED,
        captured_at=AT,
    )
    bundle = replace(
        bundle, repository_identities=(repository_identity_evidence_of(outcome),)
    )
    attributed = replace(bundle.attributed_completions[0], ci_outcomes=[outcome])
    destination = tmp_path / "missing-ci-schema-version"
    write_derived_bundle(
        replace(bundle, attributed_completions=(attributed,)), destination
    )
    artifact_path = destination / "attributed_completions.jsonl"
    row = json.loads(
        json.loads(artifact_path.read_text(encoding="utf-8"))["record_json"]
    )
    del row["ci_outcomes"][0]["schema_version"]
    data = (
        json.dumps(
            {"record_json": json.dumps(row, separators=(",", ":"), sort_keys=True)}
        )
        + "\n"
    ).encode()
    artifact_path.write_bytes(data)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = manifest["files"]["attributed_completions"]
    metadata["bytes"] = len(data)
    metadata["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="CI outcome fields"):
        read_derived_bundle(destination)


def test_reader_rejects_unreferenced_completion(tmp_path: Path) -> None:
    destination = tmp_path / "unreferenced"
    write_derived_bundle(_bundle(), destination)
    path = destination / "inference_calls.jsonl"
    envelope = json.loads(path.read_bytes())
    record = json.loads(envelope["record_json"])
    record.update(inference_call_id="unreferenced", session_id="session-2")
    data = (
        path.read_bytes()
        + json.dumps({"record_json": json.dumps(record)}).encode()
        + b"\n"
    )
    _replace_artifact(destination, "inference_calls", data)
    identity_path = destination / "inference_call_identities.jsonl"
    identity_envelope = json.loads(identity_path.read_bytes())
    identity_record = json.loads(identity_envelope["record_json"])
    identity_record.update(inference_call_id="unreferenced", session_id="session-2")
    _replace_artifact(
        destination,
        "inference_call_identities",
        identity_path.read_bytes()
        + json.dumps({"record_json": json.dumps(identity_record)}).encode()
        + b"\n",
    )
    path = destination / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["counts"]["inference_calls"] = 2
    manifest["files"]["inference_calls"]["rows"] = 2
    manifest["counts"]["inference_call_identities"] = 2
    manifest["files"]["inference_call_identities"]["rows"] = 2
    path.write_text(json.dumps(manifest))
    with pytest.raises(BundleValidationError, match="unreferenced inference call"):
        read_derived_bundle(destination)


@pytest.mark.parametrize("artifact", ["attributed_completion", "rollout"])
def test_reader_rejects_split_that_disagrees_with_policy(
    tmp_path: Path, artifact: str
) -> None:
    bundle = _bundle()
    name = (
        "attributed_completions" if artifact == "attributed_completion" else "rollouts"
    )
    split = "eval" if getattr(bundle, name)[0].split == "train" else "train"
    destination = tmp_path / artifact
    write_derived_bundle(bundle, destination)
    _rewrite_record(destination, name, lambda row: row.update(split=split))
    with pytest.raises(BundleValidationError, match="split does not match policy"):
        read_derived_bundle(destination)


def test_reader_rejects_changed_implementation_versions(tmp_path: Path) -> None:
    destination = tmp_path / "implementation-version"
    write_derived_bundle(_bundle(), destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["implementation_versions"]["rollout"] = "untrusted"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="implementation_versions"):
        read_derived_bundle(destination)


@pytest.mark.parametrize("artifact", ["attributed_completion", "rollout"])
def test_reader_rejects_provenance_that_disagrees_with_manifest(
    tmp_path: Path, artifact: str
) -> None:
    destination = tmp_path / artifact
    write_derived_bundle(_bundle(), destination)
    name = (
        "attributed_completions" if artifact == "attributed_completion" else "rollouts"
    )
    _rewrite_record(
        destination, name, lambda row: row["provenance"].update(policy_version="other")
    )
    with pytest.raises(BundleValidationError, match="provenance does not match"):
        read_derived_bundle(destination)


def _rewrite_record(destination: Path, name: str, mutate) -> None:
    """Change canonical data and repair integrity metadata, not its claims."""
    path = destination / f"{name}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    envelope = "record_json" in rows[0]
    record = json.loads(rows[0]["record_json"]) if envelope else rows[0]
    mutate(record)
    rows[0] = (
        {"record_json": json.dumps(record, ensure_ascii=True)} if envelope else record
    )
    _replace_artifact(
        destination, name, "".join(json.dumps(row) + "\n" for row in rows).encode()
    )


def _replace_artifact(destination: Path, name: str, data: bytes) -> None:
    (destination / f"{name}.jsonl").write_bytes(data)
    path = destination / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][name].update(
        bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
    )
    path.write_text(json.dumps(manifest))


def test_v4_envelopes_and_fragmentation_are_explicit(tmp_path: Path) -> None:
    bundle = replace(_bundle(), fragmented={"prior_output_not_replayed": 2})
    destination = tmp_path / "v2"
    write_derived_bundle(bundle, destination)
    manifest = json.loads((destination / "manifest.json").read_bytes())
    assert manifest["bundle_schema_version"] == 4
    assert manifest["identity_population"] == "organization-through-as-of-v1"
    assert manifest["record_encoding"] == "sediment-record-json-v1"
    assert manifest["fragmented"] == bundle.fragmented
    for name in (
        "attributed_completions",
        "rollouts",
        "inference_calls",
        "inference_call_identities",
    ):
        envelope = json.loads((destination / f"{name}.jsonl").read_bytes())
        assert set(envelope) == {"record_json"}
        assert isinstance(envelope["record_json"], str)
    assert read_derived_bundle(destination) == bundle


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        "\ud800",
        "\udfff",
        "\x00",
        {"record_json": "user", "": []},
        None,
    ],
)
def test_bundle_retains_exceptional_canonical_values(tmp_path: Path, value) -> None:
    import math

    bundle = _bundle()
    call = bundle.inference_calls[0].model_copy(update={"raw": {"value": value}})
    bundle = replace(bundle, inference_calls=(call,))
    destination = tmp_path / "lossless"
    write_derived_bundle(bundle, destination)
    restored = read_derived_bundle(destination).inference_calls[0].raw["value"]
    if isinstance(value, float) and math.isnan(value):
        assert isinstance(restored, float) and math.isnan(restored)
    else:
        assert restored == value
    for path in destination.iterdir():
        assert path.read_bytes().isascii()
        for line in path.read_text().splitlines():
            json.loads(
                line,
                parse_constant=lambda value: pytest.fail(f"non-strict outer {value}"),
            )


@pytest.mark.parametrize("artifact", ["attributed_completions", "rollouts"])
@pytest.mark.parametrize(
    "field,value",
    [("org_id", "other"), ("session_id", "other-session"), ("split", "invalid")],
)
@pytest.mark.parametrize("boundary", ["memory", "disk"])
def test_bundle_checks_artifact_relationships_at_both_boundaries(
    tmp_path: Path, artifact, field, value, boundary
) -> None:
    bundle = _bundle()
    destination = tmp_path / "relationship"
    if boundary == "memory":
        row = replace(getattr(bundle, artifact)[0], **{field: value})
        bundle = replace(
            bundle,
            **{artifact: (row,)},
            repository_identities=(repository_identity_evidence_of(_outcome()),),
        )
        with pytest.raises(
            BundleValidationError, match="org_id|Session|session_id|split"
        ):
            write_derived_bundle(bundle, destination)
        assert not destination.exists()
    else:
        write_derived_bundle(bundle, destination)
        _rewrite_record(destination, artifact, lambda row: row.update({field: value}))
        with pytest.raises(
            BundleValidationError, match="org_id|Session|session_id|split"
        ):
            read_derived_bundle(destination)


@pytest.mark.parametrize(
    "field,value",
    [
        ("quarantine_revision", True),
        ("as_of", "2026-08-01"),
        ("fragmented", {"unknown": 1}),
        ("fragmented", {"prior_output_absent": True}),
        ("skipped", {"x": -1}),
    ],
)
def test_writer_rejects_invalid_metadata_without_publication(
    tmp_path: Path, field, value
) -> None:
    destination = tmp_path / "metadata"
    with pytest.raises(BundleValidationError):
        write_derived_bundle(replace(_bundle(), **{field: value}), destination)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["rows", "bytes"])
@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_reader_metadata_counts_are_exact_nonnegative_integers(
    tmp_path: Path, field, value
) -> None:
    destination = tmp_path / "metadata"
    write_derived_bundle(_bundle(), destination)
    path = destination / "manifest.json"
    manifest = json.loads(path.read_bytes())
    if value is True or value == 1.0:
        value = (
            True
            if field == "rows"
            else float(manifest["files"]["inference_calls"]["bytes"])
        )
    manifest["files"]["inference_calls"][field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(BundleValidationError):
        read_derived_bundle(destination)


@pytest.mark.parametrize("layer", ["manifest", "outer", "inner"])
@pytest.mark.parametrize("defect", ["duplicate", "trailing", "numeric"])
def test_reader_rejects_ambiguous_json_layers(tmp_path: Path, layer, defect) -> None:
    destination = tmp_path / "json"
    write_derived_bundle(_bundle(), destination)
    if layer == "manifest":
        path = destination / "manifest.json"
        raw = path.read_text().strip()
        bad = (
            raw[:-1] + ',"org_id":"acme-corp"}'
            if defect == "duplicate"
            else raw + " {}"
            if defect == "trailing"
            else raw.replace('"quarantine_revision":0', '"quarantine_revision":NaN')
        )
        path.write_text(bad)
    else:
        path = destination / "inference_calls.jsonl"
        outer = json.loads(path.read_bytes())
        raw = (
            outer.get("record_json", json.dumps(outer))
            if layer == "inner"
            else json.dumps(outer)
        )
        key = "inference_call_id" if layer == "inner" else "record_json"
        duplicate_value = (
            '"inference-1"'
            if layer == "inner"
            else json.dumps(outer.get("record_json", "ignored"))
        )
        bad = (
            raw[:-1] + f',"{key}":{duplicate_value}' + "}"
            if defect == "duplicate"
            else raw + " {}"
            if defect == "trailing"
            else '{"record_json":NaN}'
        )
        # Inner extended numeric values are legal only within a canonical record.
        data = (
            json.dumps({"record_json": bad}).encode() + b"\n"
            if layer == "inner"
            else bad.encode() + b"\n"
        )
        _replace_artifact(destination, "inference_calls", data)
    with pytest.raises(BundleValidationError):
        read_derived_bundle(destination)


@pytest.mark.parametrize("boundary", ["memory", "disk"])
@pytest.mark.parametrize(
    "change",
    [
        "org_id",
        "session_id",
        "repo",
        "commit_sha",
        "late",
        "no_as_of",
        "conflicting_id",
    ],
)
def test_bundle_observation_identity_and_time(tmp_path: Path, boundary, change) -> None:
    from datetime import timedelta
    from sediment_core import SessionCommitObservation

    bundle = _bundle()
    observation = SessionCommitObservation(
        observation_id="observation-1",
        org_id=ORG,
        session_id="session-1",
        repo="acme-corp/backend-service",
        commit_sha=SHA,
        source_push_id="push-1",
        captured_at=AT,
    )
    bundle = replace(
        bundle,
        repository_identities=(repository_identity_evidence_of(observation),),
        attributed_completions=(
            replace(
                bundle.attributed_completions[0],
                session_commit_observations=(observation,),
            ),
        ),
        rollouts=(
            replace(bundle.rollouts[0], session_commit_observations=(observation,)),
        ),
    )
    values = {
        "org_id": "other",
        "session_id": "other",
        "repo": "other/repo",
        "commit_sha": "2" * 40,
        "captured_at": AT + timedelta(seconds=1),
        "source_push_id": "other-push",
    }
    key = (
        "captured_at"
        if change == "late"
        else "source_push_id"
        if change == "conflicting_id"
        else change
    )
    destination = tmp_path / "observation"
    if boundary == "memory":
        if change == "no_as_of":
            bundle = replace(bundle, as_of=None)
        else:
            bad = observation.model_copy(update={key: values[key]})
            bundle = replace(
                bundle,
                attributed_completions=(
                    replace(
                        bundle.attributed_completions[0],
                        session_commit_observations=(bad,),
                    ),
                ),
            )
        with pytest.raises(
            BundleValidationError, match="observation|as_of|capture boundary"
        ):
            write_derived_bundle(bundle, destination)
        assert not destination.exists()
    else:
        write_derived_bundle(bundle, destination)
        if change == "no_as_of":
            path = destination / "manifest.json"
            manifest = json.loads(path.read_bytes())
            manifest["as_of"] = None
            path.write_text(json.dumps(manifest))
        else:
            value = values[key].isoformat() if key == "captured_at" else values[key]
            _rewrite_record(
                destination,
                "attributed_completions",
                lambda row: row["session_commit_observations"][0].update({key: value}),
            )
        with pytest.raises(
            BundleValidationError, match="observation|as_of|capture boundary"
        ):
            read_derived_bundle(destination)


@pytest.mark.parametrize("value", ["1", True])
def test_reader_does_not_coerce_fact_integers(tmp_path: Path, value) -> None:
    destination = tmp_path / "coercion"
    write_derived_bundle(_bundle(), destination)
    _rewrite_record(
        destination, "inference_calls", lambda row: row.update(input_tokens=value)
    )
    with pytest.raises(BundleValidationError):
        read_derived_bundle(destination)


@pytest.mark.parametrize(
    "value",
    [
        {1: "numeric key"},
        {"value": datetime(2026, 1, 1, tzinfo=UTC)},
        {"value": (1, 2)},
    ],
)
def test_writer_declines_non_protocol_any_values_without_coercion(
    tmp_path: Path, value
) -> None:
    bundle = _bundle()
    call = bundle.inference_calls[0].model_copy(update={"raw": value})
    with pytest.raises(BundleValidationError, match="canonical|protocol|keys"):
        write_derived_bundle(
            replace(bundle, inference_calls=(call,)), tmp_path / "unsupported"
        )
    assert list(tmp_path.iterdir()) == []


def _decision(**updates) -> DeveloperDecision:
    value = DeveloperDecision(
        decision_id="decision-1",
        org_id=ORG,
        session_id="session-1",
        user_id="alice",
        agent_harness=AgentHarness.CODEX,
        file_path="math.py",
        accepted=True,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id="call-1",
        occurred_at=AT,
        captured_at=AT,
    )
    return value.model_copy(update=updates)


def _outcome(**updates) -> CIOutcome:
    value = CIOutcome(
        outcome_id="outcome-1",
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run-1",
        repo="acme-corp/backend-service",
        commit_sha=SHA,
        branch="main",
        result=CIResult.PASSED,
        captured_at=AT,
    )
    return value.model_copy(update=updates)


@pytest.mark.parametrize("artifact", ["attributed_completions", "rollouts"])
@pytest.mark.parametrize(
    "evidence,key,value",
    [
        ("decision", "org_id", "other"),
        ("decision", "session_id", "other-session"),
        ("outcome", "org_id", "other"),
        ("outcome", "repo", "other/repo"),
        ("outcome", "commit_sha", "2" * 40),
    ],
)
@pytest.mark.parametrize("boundary", ["memory", "disk"])
def test_bundle_qualifies_embedded_evidence(
    tmp_path: Path, artifact, evidence, key, value, boundary
) -> None:
    bundle = _bundle()
    row = getattr(bundle, artifact)[0]
    if artifact == "attributed_completions":
        row = replace(row, decisions=[_decision()], ci_outcomes=[_outcome()])
    else:
        row = replace(
            row,
            segments=[[replace(row.segments[0][0], decisions=(_decision(),))]],
            terminal_outcomes=[_outcome()],
        )
    bundle = replace(
        bundle,
        **{artifact: (row,)},
        repository_identities=(repository_identity_evidence_of(_outcome()),),
    )
    destination = tmp_path / "embedded"

    def change(record):
        records = (
            record["decisions"]
            if artifact == "attributed_completions"
            else record["segments"][0][0]["decisions"]
        )
        if evidence == "outcome":
            records = record[
                "ci_outcomes"
                if artifact == "attributed_completions"
                else "terminal_outcomes"
            ]
        records[0][key] = value

    if boundary == "disk":
        write_derived_bundle(bundle, destination)
        _rewrite_record(destination, artifact, change)
        with pytest.raises(BundleValidationError, match="decision|outcome"):
            read_derived_bundle(destination)
    else:
        if evidence == "decision":
            if artifact == "attributed_completions":
                row = replace(row, decisions=[_decision(**{key: value})])
            else:
                row = replace(
                    row,
                    segments=[
                        [
                            replace(
                                row.segments[0][0],
                                decisions=(_decision(**{key: value}),),
                            )
                        ]
                    ],
                )
        else:
            row = replace(
                row,
                **{
                    "ci_outcomes"
                    if artifact == "attributed_completions"
                    else "terminal_outcomes": [_outcome(**{key: value})]
                },
            )
        with pytest.raises(BundleValidationError, match="decision|outcome"):
            write_derived_bundle(replace(bundle, **{artifact: (row,)}), destination)
        assert not destination.exists()


@pytest.mark.parametrize("evidence", ["decision", "outcome"])
def test_conflicting_embedded_fact_identity_is_rejected(
    tmp_path: Path, evidence
) -> None:
    bundle = _bundle()
    bundle = replace(
        bundle, repository_identities=(repository_identity_evidence_of(_outcome()),)
    )
    row = bundle.attributed_completions[0]
    if evidence == "decision":
        row = replace(row, decisions=[_decision(), _decision(accepted=False)])
    else:
        row = replace(row, ci_outcomes=[_outcome(), _outcome(result=CIResult.FAILED)])
    with pytest.raises(BundleValidationError, match="conflicting.*_id"):
        write_derived_bundle(
            replace(bundle, attributed_completions=(row,)), tmp_path / "conflicting"
        )


def test_nested_facts_and_turns_keep_lossless_values_and_stable_bytes(
    tmp_path: Path,
) -> None:
    import math
    from sediment_core import InferenceMessage, TextPart, ToolCallPart
    from sediment_derive import project_session_turns

    exceptional = {
        "\ud800\x00": [None, "", {}, [], math.nan, math.inf, -math.inf, 0.0, -0.0]
    }
    bundle = _bundle()
    tool = ToolCallPart(id="tool-1", name="write", arguments=exceptional)
    decision = _decision(raw=exceptional)
    outcome = _outcome(raw=exceptional, reason="reason\udfff\x00")
    rollout = bundle.rollouts[0]
    call = bundle.inference_calls[0].model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="answer\ud800\x00"), tool]
                )
            ]
        }
    )
    segments, _, _ = project_session_turns([call], [decision])
    turn = segments[0][0]
    bundle = replace(
        bundle,
        repository_identities=(repository_identity_evidence_of(outcome),),
        inference_calls=(call,),
        inference_call_identities=(
            replace(
                bundle.inference_call_identities[0],
                call_ids=tuple(sorted(model_call_ids(call))),
            ),
        ),
        attributed_completions=(
            replace(
                bundle.attributed_completions[0],
                decisions=[decision],
                ci_outcomes=[outcome],
            ),
        ),
        rollouts=(replace(rollout, segments=[[turn]], terminal_outcomes=[outcome]),),
    )
    first, second = tmp_path / "first", tmp_path / "second"
    write_derived_bundle(bundle, first)
    restored = read_derived_bundle(first)
    write_derived_bundle(restored, second)
    assert _tree_bytes(first) == _tree_bytes(second)
    assert restored.rollouts[0].segments[0][0].completion == turn.completion
    assert restored.attributed_completions[0].ci_outcomes[0].reason == outcome.reason
    for raw in (
        restored.rollouts[0].segments[0][0].tool_calls[0].arguments,
        restored.attributed_completions[0].decisions[0].raw,
        restored.rollouts[0].terminal_outcomes[0].raw,
    ):
        values = raw["\ud800\x00"]
        assert values[:4] == [None, "", {}, []]
        assert math.isnan(values[4])
        assert values[5:8] == [math.inf, -math.inf, 0.0]
        assert math.copysign(1, values[8]) == -1


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"record_json": None},
        {"record_json": []},
        {"record_json": "[]"},
        {"record_json": "{}", "extra": 1},
    ],
)
def test_reader_rejects_malformed_v2_envelopes(tmp_path: Path, envelope) -> None:
    destination = tmp_path / "envelope"
    write_derived_bundle(_bundle(), destination)
    _replace_artifact(
        destination, "inference_calls", json.dumps(envelope).encode() + b"\n"
    )
    with pytest.raises(BundleValidationError):
        read_derived_bundle(destination)


@pytest.mark.parametrize(
    "mutation",
    ["encoding", "fragmented_absent", "fragmented_reason", "counts_bool", "extra"],
)
def test_reader_rejects_unknown_manifest_contracts(tmp_path: Path, mutation) -> None:
    destination = tmp_path / "manifest"
    write_derived_bundle(_bundle(), destination)
    path = destination / "manifest.json"
    manifest = json.loads(path.read_bytes())
    if mutation == "encoding":
        manifest["record_encoding"] = "other"
    elif mutation == "fragmented_absent":
        del manifest["fragmented"]
    elif mutation == "fragmented_reason":
        manifest["fragmented"] = {"unknown": 1}
    elif mutation == "counts_bool":
        manifest["counts"]["inference_calls"] = True
    else:
        manifest["extra"] = None
    path.write_text(json.dumps(manifest))
    with pytest.raises(BundleValidationError):
        read_derived_bundle(destination)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_reader_rejects_earlier_manifest_with_recomputation_instruction(
    tmp_path: Path,
    version,
) -> None:
    destination = tmp_path / "v1"
    destination.mkdir()
    original = json.dumps({"bundle_schema_version": version}).encode()
    (destination / "manifest.json").write_bytes(original)
    with pytest.raises(BundleValidationError, match="unsupported.*recompute"):
        read_derived_bundle(destination)
    assert (destination / "manifest.json").read_bytes() == original


def test_writer_cleans_staged_files_after_io_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from sediment_export import _record_storage as storage

    original = storage._write_private

    def fail_rollouts(path, data):
        if path.name == "rollouts.jsonl":
            raise OSError("disk unavailable")
        original(path, data)

    monkeypatch.setattr(storage, "_write_private", fail_rollouts)
    with pytest.raises(OSError, match="disk unavailable"):
        write_derived_bundle(_bundle(), tmp_path / "failed")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("as_of", "2026-08-01 12:00:00+00:00"),
        ("as_of", "20260801T120000+0000"),
        ("org_id", " ACME-CORP "),
        ("scope", {"since": None, "until": None, "users": [" alice ", "alice"]}),
    ],
)
def test_manifest_reader_does_not_normalize_invalid_metadata(
    tmp_path: Path, field, value
) -> None:
    destination = tmp_path / "normalize"
    write_derived_bundle(_bundle(), destination)
    path = destination / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(BundleValidationError):
        read_derived_bundle(destination)


def test_empty_bundle_requires_canonical_org_identity(tmp_path: Path) -> None:
    bundle = replace(
        _bundle(),
        org_id="\x00",
        attributed_completions=(),
        rollouts=(),
        inference_calls=(),
    )
    with pytest.raises(BundleValidationError):
        write_derived_bundle(bundle, tmp_path / "bad-org")


def test_writer_rejects_naive_fact_timestamp_in_memory(tmp_path: Path) -> None:
    bundle = _bundle()
    call = bundle.inference_calls[0].model_copy(
        update={"observed_at": AT.replace(tzinfo=None)}
    )
    with pytest.raises(BundleValidationError, match="aware|timestamp"):
        write_derived_bundle(
            replace(bundle, inference_calls=(call,)), tmp_path / "naive"
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("boundary", ["memory", "disk"])
def test_train_artifact_cannot_reference_an_eval_session_call(
    tmp_path: Path, boundary
) -> None:
    bundle = _bundle()
    assert bundle.attributed_completions[0].split == "train"
    eval_session = next(
        f"eval-{index}"
        for index in range(1000)
        if split_of(f"eval-{index}", bundle.policy.eval_fraction) == "eval"
    )
    destination = tmp_path / "holdout"
    if boundary == "memory":
        call = bundle.inference_calls[0].model_copy(update={"session_id": eval_session})
        with pytest.raises(BundleValidationError, match="inference-1.*session_id"):
            write_derived_bundle(
                replace(
                    bundle,
                    inference_calls=(call,),
                    inference_call_identities=(
                        replace(
                            bundle.inference_call_identities[0], session_id=eval_session
                        ),
                    ),
                ),
                destination,
            )
    else:
        write_derived_bundle(bundle, destination)
        _rewrite_record(
            destination,
            "inference_calls",
            lambda row: row.update(session_id=eval_session),
        )
        _rewrite_record(
            destination,
            "inference_call_identities",
            lambda row: row.update(session_id=eval_session),
        )
        with pytest.raises(BundleValidationError, match="inference-1.*session_id"):
            read_derived_bundle(destination)


@pytest.mark.parametrize("captured,derived", [(None, 0.75), (0.75, 0.75)])
def test_bundle_preserves_documented_retention_views(
    tmp_path: Path, captured, derived
) -> None:
    bundle = _bundle()
    rollout = bundle.rollouts[0]
    bundle = replace(
        bundle,
        attributed_completions=(
            replace(
                bundle.attributed_completions[0],
                decisions=[_decision(edit_retention_score=derived)],
            ),
        ),
        rollouts=(
            replace(
                rollout,
                segments=[
                    [
                        replace(
                            rollout.segments[0][0],
                            decisions=(_decision(edit_retention_score=captured),),
                        )
                    ]
                ],
            ),
        ),
    )
    destination = tmp_path / "retention"
    write_derived_bundle(bundle, destination)
    assert read_derived_bundle(destination) == bundle


@pytest.mark.parametrize(
    "defect",
    [
        "captured_score",
        "derived_scores",
        "captured_field",
        "missing_derived_score",
        "rollout_scores",
    ],
)
@pytest.mark.parametrize("boundary", ["memory", "disk"])
def test_retention_projection_exception_is_narrow(
    tmp_path: Path, defect, boundary
) -> None:
    bundle = _bundle()
    captured = 0.5 if defect in ("captured_score", "missing_derived_score") else None
    good_derived = captured if captured is not None else 0.75
    attributed = replace(
        bundle.attributed_completions[0],
        decisions=[_decision(edit_retention_score=good_derived)],
    )
    rollout = bundle.rollouts[0]
    rollout = replace(
        rollout,
        segments=[
            [
                replace(
                    rollout.segments[0][0],
                    decisions=(_decision(edit_retention_score=captured),),
                )
            ]
        ],
    )
    bundle = replace(bundle, attributed_completions=(attributed,), rollouts=(rollout,))
    destination = tmp_path / "retention-conflict"

    def mutate(record):
        if defect == "rollout_scores":
            decisions = record["segments"][0][0]["decisions"]
            decisions.append(dict(decisions[0], edit_retention_score=0.2))
        elif defect == "derived_scores":
            record["decisions"].append(
                dict(record["decisions"][0], edit_retention_score=0.2)
            )
        elif defect == "captured_field":
            record["decisions"][0]["accepted"] = False
        else:
            record["decisions"][0]["edit_retention_score"] = (
                None if defect == "missing_derived_score" else 0.75
            )

    if boundary == "disk":
        write_derived_bundle(bundle, destination)
        _rewrite_record(
            destination,
            "rollouts" if defect == "rollout_scores" else "attributed_completions",
            mutate,
        )
        with pytest.raises(BundleValidationError, match="conflicting.*decision_id"):
            read_derived_bundle(destination)
    else:
        if defect == "rollout_scores":
            turn = rollout.segments[0][0]
            rollout = replace(
                rollout,
                segments=[
                    [
                        replace(
                            turn,
                            decisions=(
                                *turn.decisions,
                                _decision(edit_retention_score=0.2),
                            ),
                        )
                    ]
                ],
            )
        elif defect == "derived_scores":
            attributed = replace(
                attributed,
                decisions=[*attributed.decisions, _decision(edit_retention_score=0.2)],
            )
        elif defect == "captured_field":
            attributed = replace(
                attributed,
                decisions=[
                    _decision(edit_retention_score=good_derived, accepted=False)
                ],
            )
        else:
            attributed = replace(
                attributed,
                decisions=[
                    _decision(
                        edit_retention_score=None
                        if defect == "missing_derived_score"
                        else 0.75
                    )
                ],
            )
        bundle = replace(
            bundle, attributed_completions=(attributed,), rollouts=(rollout,)
        )
        with pytest.raises(BundleValidationError, match="conflicting.*decision_id"):
            write_derived_bundle(bundle, destination)
        assert not destination.exists()


@pytest.mark.parametrize("population", ["repository_identities", "repository_renames"])
def test_repository_population_rejects_noncanonical_memory_records(
    tmp_path, population
):
    bundle = replace(_bundle(), **{population: (object(),)})
    with pytest.raises(BundleValidationError, match="repository population"):
        write_derived_bundle(bundle, tmp_path / "invalid-population")
    assert not (tmp_path / "invalid-population").exists()


def _identified_bundle():
    from sediment_core import Push, SessionCommitObservation
    from sediment_derive.repository_identity import repository_identity_of

    bundle = _bundle()
    push = Push(
        push_id="identity-push",
        org_id=ORG,
        provider="github",
        repo="acme-corp/backend-service",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=SHA,
        clone_url="https://github.com/acme-corp/backend-service.git",
        captured_at=AT,
        repository_provider="github",
        repository_host="github.com",
        repository_id="101",
    )
    outcome = _outcome(
        repo="acme-corp/renamed",
        repository_provider=push.repository_provider,
        repository_host=push.repository_host,
        repository_id=push.repository_id,
    )
    # Identity absence inherits only through this exact captured Push edge.
    observation = SessionCommitObservation(
        observation_id="inherited-observation",
        org_id=ORG,
        session_id="session-1",
        repo=push.repo,
        commit_sha=SHA,
        source_push_id=push.push_id,
        captured_at=AT,
    )
    row = replace(
        bundle.attributed_completions[0],
        repository_identity=repository_identity_of(push),
        source_push_id=push.push_id,
        ci_outcomes=[outcome],
        session_commit_observations=(observation,),
    )
    rollout = replace(
        bundle.rollouts[0],
        commits=[
            replace(commit, repository_identity=repository_identity_of(push))
            if commit.repo == push.repo
            else commit
            for commit in bundle.rollouts[0].commits
        ],
    )
    return replace(
        bundle,
        attributed_completions=(row,),
        rollouts=(rollout,),
        repository_identities=tuple(
            repository_identity_evidence_of(fact)
            for fact in (push, outcome, observation)
        ),
    )


@pytest.mark.parametrize("boundary", ["memory", "disk", "export"])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_push",
        "wrong_lifetime",
        "future",
        "missing_observation",
        "observation_rebound",
        "false_ci",
        "duplicate_source",
        "foreign_org",
    ],
)
def test_repository_source_claims_are_checked_at_every_bundle_boundary(
    tmp_path, boundary, mutation
):
    from datetime import timedelta
    from sediment_export.derived_bundle import _jsonl_bytes
    from sediment_export.rlvr import export_rlvr_from_bundle

    bundle = _identified_bundle()
    rows = list(bundle.repository_identities)
    if mutation == "missing_push":
        del rows[0]
    elif mutation == "wrong_lifetime":
        rows[0] = replace(rows[0], repository_id="202")
    elif mutation == "future":
        rows[0] = replace(rows[0], captured_at=AT + timedelta(seconds=1))
    elif mutation == "missing_observation":
        del rows[2]
    elif mutation == "observation_rebound":
        rows[2] = replace(rows[2], source_push_id="another-push")
    elif mutation == "false_ci":
        rows[1] = replace(rows[1], repository_id="202")
    elif mutation == "duplicate_source":
        rows.append(rows[0])
    else:
        rows[0] = replace(rows[0], org_id="other-org")
    bad = replace(bundle, repository_identities=tuple(rows))
    destination = tmp_path / "rejected"
    if boundary == "disk":
        write_derived_bundle(bundle, destination)
        _replace_artifact(
            destination, "repository_identities", _jsonl_bytes(tuple(rows))
        )
        path = destination / "manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["counts"]["repository_identities"] = len(rows)
        manifest["files"]["repository_identities"]["rows"] = len(rows)
        path.write_text(json.dumps(manifest))
        with pytest.raises(BundleValidationError):
            read_derived_bundle(destination)
    elif boundary == "export":
        with pytest.raises(BundleValidationError):
            export_rlvr_from_bundle(bad, None, destination, target="nemo-gym")
        assert not destination.exists()
    else:
        with pytest.raises(BundleValidationError):
            write_derived_bundle(bad, destination)
        assert not destination.exists()


def test_declared_legacy_observation_retains_exact_identity_through_roundtrip(tmp_path):
    from sediment_export.attributed_completions import session_commit_observation_ids
    from sediment_export.derived_bundle import validate_derived_bundle

    bundle = _identified_bundle()
    restored = read_derived_bundle(write_derived_bundle(bundle, tmp_path / "valid"))
    context = validate_derived_bundle(restored)
    row = restored.attributed_completions[0]
    assert row.session_commit_observations[0].repository_id is None
    assert session_commit_observation_ids(row, repository_context=context) == (
        "inherited-observation",
    )
    assert (
        row.session_commit_observations
        == bundle.attributed_completions[0].session_commit_observations
    )


@pytest.mark.parametrize("population", ["repository_identities", "repository_renames"])
def test_bundle_bounds_each_repository_population_independently(
    tmp_path, monkeypatch, population
):
    from sediment_core import RepositoryRename
    import sediment_export.derived_bundle as implementation

    monkeypatch.setattr(implementation, "REPOSITORY_IDENTITY_LIMIT", 1)
    if population == "repository_identities":
        bundle = _identified_bundle()
    else:
        renames = tuple(
            RepositoryRename(
                org_id=ORG,
                rename_id=f"rename-{index}",
                repository_provider="github",
                repository_host="github.com",
                repository_id=str(index + 1),
                old_repo="acme/old",
                new_repo="acme/new",
                captured_at=AT,
            )
            for index in range(2)
        )
        bundle = replace(_bundle(), repository_renames=renames)
    with pytest.raises(
        BundleValidationError, match="repository population exceeds row limit"
    ):
        write_derived_bundle(bundle, tmp_path / "over-limit")
    assert not (tmp_path / "over-limit").exists()


def test_bundle_checks_complete_carried_ci_run_union_before_publication(tmp_path):
    bundle = _bundle()
    first = CIOutcome(
        outcome_id="attempt-one",
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="shared-run",
        run_attempt=1,
        repo="acme-corp/backend-service",
        commit_sha=SHA,
        branch="main",
        result=CIResult.CANCELLED,
        captured_at=AT,
    )
    second = first.model_copy(
        update={
            "outcome_id": "attempt-two",
            "run_attempt": 2,
            "repo": "acme-corp/backend-fork",
            "result": CIResult.PASSED,
        }
    )

    def populated(other):
        return replace(
            bundle,
            repository_identities=tuple(
                repository_identity_evidence_of(row) for row in (first, other)
            ),
            attributed_completions=(
                replace(bundle.attributed_completions[0], ci_outcomes=[first]),
            ),
            rollouts=(replace(bundle.rollouts[0], terminal_outcomes=[first, other]),),
        )

    # An exact repeated Fact and a valid non-verdict are retained across artifacts.
    good = populated(second.model_copy(update={"run_id": "separate-run"}))
    assert read_derived_bundle(write_derived_bundle(good, tmp_path / "good")) == good
    with pytest.raises(BundleValidationError, match="conflicting CI run"):
        write_derived_bundle(populated(second), tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
