# SPDX-License-Identifier: AGPL-3.0-or-later
"""Machine-readable canonical schema publication contracts."""

from __future__ import annotations

import json
from hashlib import sha256
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from sediment_derive import (
    AbandonmentPolicy,
    AttributionPolicy,
    RolloutPolicy,
    AcceptedSessionOutcome,
    Fate,
    MergeMembershipOutcome,
)
from sediment_export import AttributedCompletionPolicy
from sediment_export.rlvr import (
    NemoGymMetadata,
    SWEBenchMetadata,
    SedimentRolloutRow,
    SedimentTaskRow,
)
from sediment_export.schema_contracts import CONTRACTS, SCHEMA_DIALECT


REPO_ROOT = Path(__file__).parents[2]
CATALOG_PATH = REPO_ROOT / "schemas" / "catalog.json"
SCRIPT = REPO_ROOT / "scripts" / "gen_schema_docs.py"
# Canonical content fingerprints preserve the v1 contracts without requiring
# an earlier repository checkout. Update successors instead of these files.
_V1_SCHEMA_SHA256 = {
    "schemas/derived-artifacts/attributed-completion/v1.json": (
        "710c51668dc07bdd3c45be881360e65943b6328b58e693f1d9db07f5dbc01648"
    ),
    "schemas/derived-artifacts/fate/v1.json": (
        "7bd6d43a463dd48665d3e847b158a847d15f04ca921690cce515a26f1aefe7d4"
    ),
    "schemas/derived-artifacts/rollout/v1.json": (
        "7202f7ddb236533336de83218a2fe49be76b96a6114b2954c01861f905310732"
    ),
    "schemas/derived-artifacts/turn/v1.json": (
        "ad4a632ffd40ee3d2e127107e981dd81eef6e22a1d0f80c3fa34a33ebe38ce35"
    ),
    "schemas/facts/developer-decision/v1.json": (
        "fc8ab1e3f2e05ed2c3a104eeadd6b43c8a49bf4f7234551d1b9d10c1728b8e99"
    ),
    "schemas/facts/edit-observation/v1.json": (
        "39cf8b73888b48dee621f184a611d65cc1f778248dfb3b3bcb925687f6ec087b"
    ),
    "schemas/facts/rejected-edit/v1.json": (
        "bea889495a7bf52a02bb140152a3461adc8cfea67cde1b362a5c2901e60a359f"
    ),
    "schemas/facts/retry-linkage/v1.json": (
        "05a1b024b57c1ec11dc46046f97b480e1c0199d35992e707e5817abc716e2064"
    ),
    "schemas/training-rows/nemo-gym-metadata/v1.json": (
        "445e1e51100c36ab2fe1ff682982d60ae45b325ab9ab88f31d5741c55616ec64"
    ),
    "schemas/training-rows/nemo-gym-response/v1.json": (
        "6f719d320b6d7596ef7d968cee43c208d3af68d5686b43c2147681ce857ab15c"
    ),
    "schemas/training-rows/nemo-gym-rollout/v1.json": (
        "ac1335684b8e02bab9c80a635bb53b5c2bede2a4fac658df0c2ffc98313f39ad"
    ),
    "schemas/training-rows/rlvr-decision/v1.json": (
        "f920c7722118b10de8a4812e2e2f7f2b177768e5e32f711214385b63bc87473f"
    ),
    "schemas/training-rows/rlvr-turn/v1.json": (
        "a78ee0d604c2a52892549663189833b08840cae1e52e352aa9b449b669177a24"
    ),
    "schemas/training-rows/sediment-rlvr-rollout/v1.json": (
        "79649c06ba34dee581df9986ec645501a91c163fcb7926a9a3b711272028704e"
    ),
}

_CURSOR_V2_CONTRACTS = {
    ("facts", "developer-decision"),
    ("facts", "edit-observation"),
    ("facts", "rejected-edit"),
    ("facts", "retry-linkage"),
    ("derived-artifacts", "attributed-completion"),
    ("derived-artifacts", "fate"),
    ("derived-artifacts", "rollout"),
    ("derived-artifacts", "turn"),
    ("training-rows", "nemo-gym-response"),
    ("training-rows", "nemo-gym-rollout"),
    ("training-rows", "rlvr-decision"),
    ("training-rows", "rlvr-turn"),
    ("training-rows", "sediment-rlvr-rollout"),
}

spec = importlib.util.spec_from_file_location("canonical_schema_generator", SCRIPT)
gen_schema_docs = importlib.util.module_from_spec(spec)
sys.modules["canonical_schema_generator"] = gen_schema_docs
spec.loader.exec_module(gen_schema_docs)


def test_registry_assigns_one_stable_versioned_id_and_path_per_contract() -> None:
    assert CONTRACTS
    assert len({contract.schema_id for contract in CONTRACTS}) == len(CONTRACTS)
    assert len({contract.output_path for contract in CONTRACTS}) == len(CONTRACTS)

    for contract in CONTRACTS:
        assert contract.version > 0
        assert contract.schema_id.startswith("https://sediment.so/schemas/")
        assert f"/v{contract.version}.json" in contract.schema_id
        assert contract.output_path == Path(
            "schemas",
            contract.artifact_family,
            contract.slug,
            f"v{contract.version}.json",
        )


def test_final_fate_has_a_canonical_derived_artifact_schema() -> None:
    [contract] = [
        contract for contract in CONTRACTS if contract.python_type_object is Fate
    ]
    assert contract.artifact_family == "derived-artifacts"
    assert contract.slug == "fate"


def test_accepted_session_outcome_has_a_canonical_derived_artifact_schema() -> None:
    [contract] = [
        contract
        for contract in CONTRACTS
        if contract.python_type_object is AcceptedSessionOutcome
    ]
    assert contract.artifact_family == "derived-artifacts"
    assert contract.slug == "accepted-session-outcome"


def test_merge_membership_outcome_has_a_canonical_derived_artifact_schema() -> None:
    [contract] = [
        contract
        for contract in CONTRACTS
        if contract.python_type_object is MergeMembershipOutcome
    ]
    assert contract.artifact_family == "derived-artifacts"
    assert contract.slug == "merge-membership-outcome"


def test_cursor_agent_harness_contracts_publish_v2_successors() -> None:
    contracts = {
        (contract.artifact_family, contract.slug): contract for contract in CONTRACTS
    }

    assert all(contracts[key].version >= 2 for key in _CURSOR_V2_CONTRACTS)
    assert all(
        (REPO_ROOT / "schemas" / family / slug / "v2.json").is_file()
        for family, slug in _CURSOR_V2_CONTRACTS
    )
    assert contracts[("training-rows", "nemo-gym-metadata")].version == 4
    assert contracts[("training-rows", "sediment-rlvr-task")].version == 3
    assert contracts[("training-rows", "swe-bench-task")].version == 3


def test_cursor_v2_contracts_preserve_published_v1_schemas() -> None:
    published_v1 = {
        *_CURSOR_V2_CONTRACTS,
        ("training-rows", "nemo-gym-metadata"),
    }
    for family, slug in sorted(published_v1):
        path = f"schemas/{family}/{slug}/v1.json"
        schema = json.loads((REPO_ROOT / path).read_text())
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
        assert sha256(canonical).hexdigest() == _V1_SCHEMA_SHA256[path], path


def test_observation_successors_embed_exact_training_row_identities() -> None:
    sediment_fields = SedimentRolloutRow.__dataclass_fields__
    nemo_fields = NemoGymMetadata.__dataclass_fields__

    assert sediment_fields["schema_version"].default == 4
    assert sediment_fields["schema_id"].default.endswith(
        "/training-rows/sediment-rlvr-rollout/v4.json"
    )
    assert nemo_fields["schema_version"].default == 4
    assert nemo_fields["schema_id"].default.endswith(
        "/training-rows/nemo-gym-rollout/v4.json"
    )
    assert SedimentTaskRow.__dataclass_fields__["schema_version"].default == 3
    assert SWEBenchMetadata.__dataclass_fields__["schema_version"].default == 3


def test_committed_catalog_matches_the_code_owned_registry() -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    assert catalog["$schema"] == SCHEMA_DIALECT
    assert catalog["contracts"] == [
        {
            "schema_id": contract.schema_id,
            "schema_version": contract.version,
            "python_type": contract.python_type,
            "artifact_family": contract.artifact_family,
            "output_path": contract.output_path.as_posix(),
        }
        for contract in CONTRACTS
    ]


def test_every_registered_contract_has_a_committed_draft_2020_12_schema() -> None:
    for contract in CONTRACTS:
        schema = json.loads((REPO_ROOT / contract.output_path).read_text())
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == SCHEMA_DIALECT
        assert schema["$id"] == contract.schema_id
        assert schema["title"] == contract.python_type.rsplit(".", 1)[-1]


def test_canonical_training_rows_publish_schema_identity_outside_inputs() -> None:
    expected_locations = {
        "DPOPair": ("metadata",),
        "SFTSample": ("metadata",),
        "DiffSFTSample": ("metadata",),
        "RecoveryRow": (),
        "SedimentTaskRow": (),
        "SedimentRolloutRow": (),
        "SWEBenchTaskRow": ("metadata",),
        "NemoGymRolloutRow": ("metadata",),
    }
    by_name = {contract.python_type_object.__name__: contract for contract in CONTRACTS}

    for name, location in expected_locations.items():
        contract = by_name[name]
        schema = json.loads((REPO_ROOT / contract.output_path).read_text())
        node = schema
        for key in location:
            reference = node["properties"][key]["$ref"]
            node = schema["$defs"][reference.rsplit("/", 1)[-1]]
        assert node["properties"]["schema_id"] == {
            "const": contract.schema_id,
            "type": "string",
            "description": "The canonical wire-shape contract for this row.",
        }
        assert node["properties"]["schema_version"] == {
            "const": contract.version,
            "type": "integer",
            "description": "The positive integer version of the canonical row schema.",
        }
        assert "schema_id" in node["required"]
        assert "schema_version" in node["required"]

        for trainer_input in ("prompt", "chosen", "rejected", "completion", "tools"):
            if trainer_input in schema.get("properties", {}):
                assert trainer_input not in location


def _schema_for(name: str) -> dict:
    contract = next(
        contract
        for contract in CONTRACTS
        if contract.python_type_object.__name__ == name
    )
    return json.loads((REPO_ROOT / contract.output_path).read_text())


def _valid_dpo_row() -> dict:
    provenance = {
        "policy_version": "1",
        "quarantine_revision": 0,
        "policy_digest": None,
    }
    return {
        "prompt": [{"role": "user", "content": "Fix it"}],
        "chosen": [{"role": "assistant", "content": "Fixed"}],
        "rejected": [{"role": "assistant", "thinking": "Guess"}],
        "tools": [],
        "metadata": {
            "org_id": "acme",
            "source_model": "model-a",
            "chosen_completion_id": "chosen-1",
            "rejected_completion_id": "rejected-1",
            "recipe_id": "dpo_human",
            "recipe_version": 2,
            "chosen_label_source": "explicit_accept",
            "rejected_label_source": "explicit_reject",
            "label_confidence": 1.0,
            "ci_reliability": None,
            "confidence_margin": 1.0,
            "provenance": {"chosen": provenance, "rejected": provenance},
            "split": "train",
            "schema_id": ("https://sediment.so/schemas/training-rows/dpo-pair/v4.json"),
            "schema_version": 4,
            "chosen_repository_identity": None,
            "rejected_repository_identity": None,
            "chosen_attribution_source": "git_notes",
            "rejected_attribution_source": "jaccard",
            "chosen_session_commit_observation_ids": [],
            "rejected_session_commit_observation_ids": [],
        },
    }


def test_dpo_schema_accepts_a_canonical_row() -> None:
    validator = Draft202012Validator(_schema_for("DPOPair"))
    assert not list(validator.iter_errors(_valid_dpo_row()))


def test_generated_schemas_reject_recursive_contract_violations() -> None:
    schema = _schema_for("DPOPair")
    validator = Draft202012Validator(schema)

    malformed_rows = []
    missing_required = _valid_dpo_row()
    del missing_required["metadata"]["schema_id"]
    malformed_rows.append(missing_required)

    invalid_literal = _valid_dpo_row()
    invalid_literal["metadata"]["recipe_id"] = "mixed_evidence"
    malformed_rows.append(invalid_literal)

    boolean_for_integer_literal = _valid_dpo_row()
    boolean_for_integer_literal["metadata"]["recipe_version"] = True
    malformed_rows.append(boolean_for_integer_literal)

    malformed_nested_message = _valid_dpo_row()
    malformed_nested_message["chosen"] = [{"content": "missing role"}]
    malformed_rows.append(malformed_nested_message)

    for row in malformed_rows:
        assert list(validator.iter_errors(row)), row


def test_omitted_when_null_key_rejects_present_null() -> None:
    validator = Draft202012Validator(_schema_for("RLVRInferenceMessageRow"))

    assert not list(validator.iter_errors({"role": "assistant", "parts": []}))
    assert list(
        validator.iter_errors({"role": "assistant", "parts": [], "finish_reason": None})
    )


def test_compatibility_rejects_in_place_breaking_change() -> None:
    path = "schemas/training-rows/dpo-pair/v1.json"
    previous = json.dumps(_schema_for("DPOPair"), sort_keys=True)
    changed_schema = _schema_for("DPOPair")
    changed_schema["required"].append("replacement")
    current = json.dumps(changed_schema, sort_keys=True)

    assert gen_schema_docs.compatibility_errors({path: previous}, {path: current}) == [
        f"{path} changed under its published schema id and version",
    ]


def test_compatibility_allows_a_new_version_without_rewriting_the_old_one() -> None:
    old_path = "schemas/training-rows/dpo-pair/v1.json"
    new_path = "schemas/training-rows/dpo-pair/v2.json"
    old_schema = json.dumps(_schema_for("DPOPair"), sort_keys=True)

    assert not gen_schema_docs.compatibility_errors(
        {old_path: old_schema},
        {old_path: old_schema, new_path: '{"$id":"example/v2"}'},
    )


def _valid_bundle_manifest() -> dict:
    def file_metadata(path: str) -> dict:
        return {
            "path": path,
            "rows": 0,
            "bytes": 0,
            "sha256": "0" * 64,
        }

    return {
        "bundle_schema_version": 4,
        "repository_population": "organization-through-as-of-v1",
        "record_encoding": "sediment-record-json-v1",
        "identity_population": "organization-through-as-of-v1",
        "fragmented": {},
        "org_id": "acme",
        "scope": {"since": None, "until": None, "users": None},
        "policy": {
            "schema_version": 1,
            "attribution": {
                "post_push_grace_period_minutes": 10,
                "max_commits_per_push": 20,
                "git_notes": {
                    "min_similarity": 0.3,
                    "lookback_window_minutes": 10080,
                },
                "jaccard": {
                    "min_similarity": 0.7,
                    "lookback_window_minutes": 60,
                },
            },
            "split": {"eval_fraction": 0.1},
        },
        "policy_digest": "0" * 64,
        "implementation_versions": {
            "abandonment": AbandonmentPolicy().policy_version,
            "attribution": AttributionPolicy().policy_version,
            "attributed_completion": AttributedCompletionPolicy().policy_version,
            "rollout": RolloutPolicy().policy_version,
            "repository_identity": "1",
        },
        "quarantine_revision": 0,
        "as_of": "2026-08-22T00:00:00+00:00",
        "mirror_revisions": {},
        "counts": {
            "attributed_completions": 0,
            "rollouts": 0,
            "inference_calls": 0,
            "inference_call_identities": 0,
            "repository_identities": 0,
            "repository_renames": 0,
        },
        "skipped": {},
        "excluded": {},
        "files": {
            "attributed_completions": file_metadata("attributed_completions.jsonl"),
            "rollouts": file_metadata("rollouts.jsonl"),
            "repository_identities": file_metadata("repository_identities.jsonl"),
            "repository_renames": file_metadata("repository_renames.jsonl"),
            "inference_calls": file_metadata("inference_calls.jsonl"),
            "inference_call_identities": file_metadata(
                "inference_call_identities.jsonl"
            ),
        },
    }


def test_bundle_manifest_schema_matches_the_closed_recursive_reader_contract() -> None:
    validator = Draft202012Validator(
        _schema_for("BundleManifest"), format_checker=FormatChecker()
    )
    assert not list(validator.iter_errors(_valid_bundle_manifest()))

    malformed_manifests = []
    for path, invalid in (
        (("bundle_schema_version",), 1),
        (("bundle_schema_version",), 2),
        (("record_encoding",), "other"),
        (("identity_population",), "exported-calls-only"),
        (("fragmented",), {"unknown": 1}),
        (("fragmented",), {"prior_output_absent": -1}),
        (("scope",), {}),
        (("scope", "since"), "not-a-date"),
        (("scope", "users"), []),
        (("scope", "users"), [""]),
        (("scope", "users"), ["  "]),
        (("policy",), {}),
        (("policy", "schema_version"), 2),
        (("policy", "attribution", "max_commits_per_push"), 0),
        (("files",), {}),
        (("files", "rollouts", "path"), "wrong.jsonl"),
        (("files", "inference_call_identities", "path"), "wrong.jsonl"),
        (("files", "rollouts", "sha256"), "not-a-digest"),
        (("counts", "rollouts"), -1),
        (("quarantine_revision",), -1),
        (("as_of",), "not-a-date"),
    ):
        manifest = _valid_bundle_manifest()
        target = manifest
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = invalid
        malformed_manifests.append(manifest)

    for manifest in malformed_manifests:
        assert list(validator.iter_errors(manifest)), manifest


def test_foundation_policy_owners_match_the_published_schema_closure() -> None:
    from sediment_derive import (
        AttributionSharePolicy,
        CIResolutionPolicy,
        FatePolicy,
        MergeRetentionPolicy,
        RecoveryPolicy,
    )
    from sediment_export import (
        LabelConfidencePolicy,
        LifecycleReportPolicy,
        OutcomeReportPolicy,
    )
    from sediment_export.derived_bundle import BundleImplementationVersions
    from typing import get_args, get_type_hints

    owners = {
        AttributionPolicy: "3",
        AttributedCompletionPolicy: "5",
        RolloutPolicy: "4",
        RecoveryPolicy: "5",
        AbandonmentPolicy: "6",
        MergeRetentionPolicy: "3",
        LifecycleReportPolicy: "3",
        OutcomeReportPolicy: "4",
        CIResolutionPolicy: "2",
        LabelConfidencePolicy: "4",
        FatePolicy: "1",
        AttributionSharePolicy: "2",
    }
    for owner, expected in owners.items():
        kwargs = (
            {"min_cases_for_decline_verdict": 30}
            if owner is AttributionSharePolicy
            else {}
        )
        assert owner(**kwargs).policy_version == expected
        assert (
            owner(policy_version="caller-label", **kwargs).policy_version
            == "caller-label"
        )
        # A custom label stamps this implementation; it does not select a prior
        # algorithm or weaken the published bundle implementation constraints.
    assert {
        name: get_args(value)
        for name, value in get_type_hints(BundleImplementationVersions).items()
    } == {
        "abandonment": ("6",),
        "attribution": ("3",),
        "attributed_completion": ("5",),
        "rollout": ("4",),
        "repository_identity": ("1",),
    }
    for contract in CONTRACTS:
        schema = json.loads((REPO_ROOT / contract.output_path).read_text())
        for node in [schema, *schema.get("$defs", {}).values()]:
            if node.get("title") in {owner.__name__ for owner in owners}:
                owner = next(
                    owner for owner in owners if owner.__name__ == node["title"]
                )
                assert node["properties"]["policy_version"]["default"] == owners[owner]


@pytest.fixture
def published_schema_history(tmp_path, monkeypatch):
    """A real repository whose active catalog advances past published v1 files."""

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Schema test")
    git("config", "user.email", "schema@example.com")
    (tmp_path / "README.md").write_text("Before schema publication.\n")
    git("add", ".")
    git("commit", "-qm", "before schemas")
    before_schemas = git("rev-parse", "HEAD")
    slugs = ("facts/quarantine-record", "training-rows/dpo-pair")
    documents = {}
    for version in (1, 2):
        active = []
        for slug in slugs:
            path = f"schemas/{slug}/v{version}.json"
            schema = {
                "$schema": SCHEMA_DIALECT,
                "$id": f"https://sediment.so/{path}",
                "type": "object",
                "properties": {"schema_version": {"const": version}},
            }
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            documents[path] = json.dumps(schema) + "\n"
            target.write_text(documents[path])
            active.append({"output_path": path})
        (tmp_path / "schemas/catalog.json").write_text(
            json.dumps({"contracts": active})
        )
        git("add", ".")
        git("commit", "-qm", f"publish v{version}")
    base = git("rev-parse", "HEAD")
    monkeypatch.setattr(gen_schema_docs, "REPO_ROOT", tmp_path)
    return tmp_path, base, before_schemas, documents


def test_base_inventory_includes_inactive_schemas_without_current_files(
    published_schema_history,
):
    repo, base, _, documents = published_schema_history
    # Neither a changed working catalog nor a missing working file can erase a
    # published source from the explicit Git revision.
    (repo / "schemas/catalog.json").unlink()
    (repo / "schemas/facts/quarantine-record/v1.json").unlink()
    assert gen_schema_docs._published_schemas_at(base) == documents


@pytest.mark.parametrize("slug", ["facts/quarantine-record", "training-rows/dpo-pair"])
@pytest.mark.parametrize("change", ["mutate", "delete"])
def test_compatibility_rejects_changes_to_inactive_versions(
    published_schema_history, slug, change
):
    repo, base, _, _ = published_schema_history
    path = f"schemas/{slug}/v1.json"
    if change == "delete":
        (repo / path).unlink()
        expected = f"{path} removed after publication"
    else:
        schema = json.loads((repo / path).read_text())
        schema["required"] = ["replacement"]
        (repo / path).write_text(json.dumps(schema))
        expected = f"{path} changed under its published schema id and version"
    previous = gen_schema_docs._published_schemas_at(base)
    current = {
        path: (repo / path).read_text() for path in previous if (repo / path).exists()
    }
    assert gen_schema_docs.compatibility_errors(previous, current) == [expected]


def test_compatibility_accepts_successor_with_all_history_preserved(
    published_schema_history,
):
    repo, base, _, documents = published_schema_history
    path = "schemas/training-rows/dpo-pair/v3.json"
    successor = json.loads(documents["schemas/training-rows/dpo-pair/v2.json"])
    successor["$id"] = f"https://sediment.so/{path}"
    successor["properties"]["schema_version"]["const"] = 3
    (repo / path).write_text(json.dumps(successor))
    (repo / "schemas/catalog.json").write_text(
        json.dumps({"contracts": [{"output_path": path}]})
    )
    previous = gen_schema_docs._published_schemas_at(base)
    assert (
        gen_schema_docs.compatibility_errors(
            previous, {**documents, path: json.dumps(successor)}
        )
        == []
    )
    assert set(previous) == set(documents)


def test_valid_revision_before_schema_publication_has_empty_inventory(
    published_schema_history,
):
    _, _, before_schemas, _ = published_schema_history
    assert gen_schema_docs._published_schemas_at(before_schemas) == {}


@pytest.mark.parametrize(
    "revision", ["", "missing-schema-base", "--help", "HEAD:schemas/catalog.json"]
)
def test_compatibility_cli_rejects_invalid_base(revision, capsys):
    assert gen_schema_docs.main(["--check", f"--compatibility-base={revision}"]) == 1
    captured = capsys.readouterr()
    assert "cannot read schema compatibility base" in captured.err
    assert revision in captured.err
    assert "are current" not in captured.out
