# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator CLI: every verb against a real store on a tmp DB.

``main`` reads ``settings`` at call time, so patching the singleton's paths
per test (the same trick the ``client`` fixture uses) is enough.
"""

from __future__ import annotations

import json
import enum
import copy
import os
import subprocess
import sys
import types
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Literal,
    Never,
    NotRequired,
    Required,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

import pytest
from jsonschema import Draft202012Validator
from pydantic import AwareDatetime, BaseModel
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    FactStore,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)

from sediment_cli.cli import main
from sediment_export import DPOPair, DiffSFTSample, RecoveryRow, SFTSample
from sediment_export.schema_contracts import CONTRACTS
from sediment_export.rlvr import (
    NemoGymRolloutRow,
    RLVRTurnRow,
    SWEBenchTaskRow,
    SedimentRolloutRow,
    SedimentTaskRow,
)
from sediment_export.trainer import TrainerMessage


def _completion(**over) -> InferenceCall:
    base = dict(
        org_id="testorg",
        session_id="sess-1",
        user_id="dev-1",
        gateway_provider=GatewayProvider.LITELLM,
        model="gpt-4o",
        input_messages=[
            InferenceMessage(
                role="user", parts=[TextPart(content="Write an add function.")]
            )
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[TextPart(content="def add(a, b):\n    return a + b")],
            )
        ],
        input_tokens=10,
        output_tokens=8,
        duration_ms=200,
        model_call_id="call-1",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    base.update(over)
    return InferenceCall(**base)


@pytest.fixture()
def cli_db(postgres_engine, postgres_database_url, tmp_path, monkeypatch):
    """Point the settings singleton at PostgreSQL; return its engine."""
    from sediment_api.config import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", postgres_database_url)
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirror"))
    monkeypatch.setenv("SEDIMENT_MIRROR_PATH", str(tmp_path / "mirror"))
    return postgres_engine


def test_facts_counts_total_and_visible(cli_db, capsys) -> None:
    store = FactStore(cli_db)
    store.store_inference_call(_completion())
    store.quarantine_fact(
        "testorg", "inference_calls", "x-does-not-exist", reason="test"
    )

    assert main(["facts"]) == 0
    out = capsys.readouterr().out
    assert "inference_calls" in out and "quarantine_revision: 1" in out
    # 1 stored, still visible (the quarantined id doesn't exist).
    assert any(line.split()[-2:] == ["1", "1"] for line in out.splitlines())


def test_quarantine_release_roundtrip_and_log(cli_db, capsys) -> None:
    store = FactStore(cli_db)
    c = _completion()
    store.store_inference_call(c)

    assert (
        main(
            [
                "quarantine",
                "inference_calls",
                c.inference_call_id,
                "--reason",
                "bad fact",
            ]
        )
        == 0
    )
    store = FactStore(cli_db)
    assert store.read_inference_calls("testorg") == []

    assert (
        main(
            [
                "release",
                "inference_calls",
                c.inference_call_id,
                "--reason",
                "false alarm",
            ]
        )
        == 0
    )
    store = FactStore(cli_db)
    assert len(store.read_inference_calls("testorg")) == 1

    assert main(["quarantine-log"]) == 0
    out = capsys.readouterr().out
    assert "bad fact" in out and "false alarm" in out and "2 rows" in out


def test_bulk_dry_runs_by_default(cli_db, capsys) -> None:
    store = FactStore(cli_db)
    store.store_inference_call(_completion(model_call_id="a", session_id="sess-a"))
    store.store_inference_call(_completion(model_call_id="b", session_id="sess-b"))

    assert (
        main(
            [
                "quarantine-inference-calls",
                "--session-id",
                "sess-a",
                "--reason",
                "r",
            ]
        )
        == 0
    )
    assert "1 model-call facts would be quarantined" in capsys.readouterr().out
    store = FactStore(cli_db)
    assert len(store.read_inference_calls("testorg")) == 2  # nothing written

    assert (
        main(
            [
                "quarantine-inference-calls",
                "--session-id",
                "sess-a",
                "--reason",
                "r",
                "--apply",
            ]
        )
        == 0
    )
    store = FactStore(cli_db)
    assert len(store.read_inference_calls("testorg")) == 1


def test_bulk_refuses_filterless_without_all(cli_db, capsys) -> None:
    store = FactStore(cli_db)
    store.store_inference_call(_completion())

    assert main(["quarantine-inference-calls", "--reason", "r"]) == 1
    assert "--all" in capsys.readouterr().err
    assert (
        main(["quarantine-inference-calls", "--all", "--apply", "--reason", "r"]) == 0
    )
    store = FactStore(cli_db)
    assert store.read_inference_calls("testorg") == []


def test_bulk_rejects_naive_between_bounds(cli_db, capsys) -> None:
    assert (
        main(
            [
                "quarantine-inference-calls",
                "--between",
                "2026-07-01T00:00:00",
                "2026-07-02T00:00:00",
                "--reason",
                "r",
            ]
        )
        == 1
    )
    assert "timezone-aware" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bounds",
    [
        ("2026-07-01T00:00:00", "2026-07-02T00:00:00+00:00"),
        ("2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00"),
    ],
)
def test_bulk_rejects_mixed_timezone_between_bounds(cli_db, capsys, bounds) -> None:
    assert (
        main(
            [
                "quarantine-inference-calls",
                "--between",
                *bounds,
                "--reason",
                "r",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "timezone-aware" in captured.err


@pytest.mark.parametrize(
    "args",
    [
        (
            "quarantine-inference-calls",
            "--provider",
            "not-a-provider",
            "--reason",
            "r",
        ),
        ("report", "model", "--bootstrap-iterations", "200"),
        (
            "report",
            "label-confidence-inspection",
            "--knob",
            "ci_pass_multiplier",
        ),
        (
            "report",
            "precision",
            "--manifest",
            "{manifest}",
            "--threshold",
            "-0.01",
        ),
        ("mirror-gc", "--retention-days", "0"),
        ("install", "--out", "{out}"),
    ],
)
def test_installed_cli_semantic_validation_exits_one(
    tmp_path, postgres_database_url, args
) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")
    rendered = [value.format(manifest=manifest, out=tmp_path / "out") for value in args]
    env = os.environ.copy()
    env.update(
        SEDIMENT_DEV_MODE="true",
        SEDIMENT_ORG_ID="testorg",
        SEDIMENT_DATABASE_URL=postgres_database_url,
        SEDIMENT_MIRROR_PATH=str(tmp_path / "mirrors"),
    )

    result = subprocess.run(
        [str(Path(sys.executable).with_name("sediment")), *rendered],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            [
                "quarantine-inference-calls",
                "--provider",
                "not-a-provider",
                "--reason",
                "r",
            ],
            "--provider must be one of",
        ),
        (
            [
                "quarantine-inference-calls",
                "--between",
                "2026-07-01T00:00:00",
                "2026-07-02T00:00:00+00:00",
                "--reason",
                "r",
            ],
            "--between bounds must be timezone-aware",
        ),
        (
            ["quarantine-inference-calls", "--reason", "r"],
            "no filters given",
        ),
        (["quarantine-log", "--tail", "0"], "--tail must be >= 1"),
        (
            ["derive", "--out", "unused", "--sample", "-1"],
            "--sample must be zero or greater",
        ),
        (
            ["quarantine", "inference_calls", "fact-1", "--reason", "   "],
            "reason is required",
        ),
        (
            ["release", "inference_calls", "   ", "--reason", "r"],
            "must be a non-empty id",
        ),
        (
            [
                "quarantine-inference-calls",
                "--session-id",
                "session-1",
                "--reason",
                "   ",
            ],
            "reason is required",
        ),
    ],
)
def test_quarantine_validation_precedes_deployment_settings(args, message) -> None:
    env = os.environ.copy()
    for name in (
        "SEDIMENT_DEV_MODE",
        "SEDIMENT_ORG_ID",
        "SEDIMENT_DATABASE_URL",
        "SEDIMENT_MIRROR_PATH",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [str(Path(sys.executable).with_name("sediment")), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_deployment_settings_failure_is_clean_in_installed_cli() -> None:
    env = os.environ.copy()
    for name in (
        "SEDIMENT_DEV_MODE",
        "SEDIMENT_ORG_ID",
        "SEDIMENT_DATABASE_URL",
        "SEDIMENT_MIRROR_PATH",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [str(Path(sys.executable).with_name("sediment")), "quarantine-log"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert "Traceback" not in result.stderr


def test_reason_is_required(cli_db) -> None:
    with pytest.raises(SystemExit):
        main(["quarantine", "inference_calls", "some-id"])


def test_empty_reason_is_clean_error_not_traceback(cli_db, capsys) -> None:
    assert main(["quarantine", "inference_calls", "some-id", "--reason", "  "]) == 1
    assert "error:" in capsys.readouterr().err


def test_between_reversed_bounds_fail(cli_db, capsys) -> None:
    assert (
        main(
            [
                "quarantine-inference-calls",
                "--between",
                "2026-07-02T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
                "--reason",
                "r",
            ]
        )
        == 1
    )
    assert "reversed" in capsys.readouterr().err


def test_server_rejects_a_regular_file_root_without_traceback(tmp_path, capsys) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("occupied", encoding="utf-8")

    assert main(["server", "--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_server_rejects_invalid_external_database_before_mutating_its_root(
    tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "server-root"
    monkeypatch.delenv("SEDIMENT_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "SEDIMENT_BOOTSTRAP_DATABASE_URL", "https://example.com/unwanted"
    )

    assert main(["server", "--root", str(root)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: bootstrap URL must name an explicit PostgreSQL host and database\n"
    )
    assert not root.exists()


def test_store_open_failure_is_a_clean_error(tmp_path, monkeypatch, capsys) -> None:
    from sediment_api.config import settings
    from pydantic import SecretStr

    monkeypatch.setattr(
        settings,
        "database_url",
        SecretStr("postgresql+psycopg://sentinel:secret@127.0.0.1:1/sediment"),
    )

    assert main(["quarantine-log"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "sentinel" not in captured.err
    assert "secret" not in captured.err
    assert "psycopg" not in captured.err


def test_report_input_file_failure_is_a_clean_error(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "report",
                "precision",
                "--org",
                "testorg",
                "--manifest",
                str(tmp_path / "missing.jsonl"),
                "--database-url",
                "postgresql+psycopg://sentinel:secret@127.0.0.1:1/sediment",
                "--mirror-path",
                str(tmp_path / "mirrors"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_mirror_gc_store_failure_is_a_clean_error(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "mirror-gc",
                "--org",
                "testorg",
                "--database-url",
                "postgresql+psycopg://sentinel:secret@127.0.0.1:1/sediment",
                "--mirror-path",
                str(tmp_path / "mirrors"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_quarantine_log_rejects_nonpositive_tail(cli_db, capsys) -> None:
    assert main(["quarantine-log", "--tail", "-1"]) == 1
    assert "--tail must be >= 1" in capsys.readouterr().err
    assert main(["quarantine-log", "--tail", "0"]) == 1


def test_quarantine_log_tails_by_default(cli_db, capsys) -> None:
    store = FactStore(cli_db)
    for i in range(25):
        store.quarantine_fact("testorg", "inference_calls", f"id-{i}", reason="r")
    assert main(["quarantine-log"]) == 0
    out = capsys.readouterr().out
    assert "showing last 20 of 25" in out and "id-4" not in out and "id-24" in out


def test_export_rlvr_writes_rollouts(cli_db, tmp_path, capsys) -> None:
    store = FactStore(cli_db)
    store.store_inference_call(_completion())
    out = tmp_path / "export"

    assert main(["export", "rlvr", "--target", "sediment", "--out", str(out)]) == 0
    stdout = capsys.readouterr().out
    assert "rollouts derived: 1" in stdout
    # No commits → no tasks; the segment is still a rollout row.
    rollout_files = list(out.glob("rollouts.*.jsonl"))
    assert rollout_files
    assert sum(len(path.read_text().splitlines()) for path in rollout_files) == 1
    assert not list(out.glob("tasks.*.jsonl"))


@pytest.mark.parametrize("fmt", ["dpo", "sft", "diff-sft", "recovery", "rlvr"])
def test_export_missing_mirror_fails(cli_db, capsys, monkeypatch, fmt) -> None:
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", "")
    argv = ["export", fmt, "--out", "/tmp"]
    if fmt == "rlvr":
        argv = ["export", fmt, "--target", "nemo-gym", "--out", "/tmp"]
    assert main(argv) == 1
    assert "SEDIMENT_MIRROR_PATH" in capsys.readouterr().err


def test_export_subcommand_requires_format(cli_db) -> None:
    with pytest.raises(SystemExit):
        main(["export"])


# Real-pipeline export tests (never mocked — AGENTS.md).
#
# One seeded scenario drives all four formats end-to-end: a note-stamped
# red→green commit pair, a pushed + mirrored remote, two completions in the
# same (prompt, model) bucket with an accept and a reject decision, and CI
# fail→pass. Regression anchor: the projections take a inference_call_id →
# InferenceCall mapping; passing the store's list crashed DPO/SFT/diff-SFT and
# silently zeroed recovery's split (review closeout, 2026-08-11).

_FIB_RED = "def fibonacci(n):\n    return n\n"
_FIB_GREEN = "def fibonacci(n):\n    return n if n <= 1 else fibonacci(n - 1)\n"


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _commit_all(work, message):
    _git(work, "add", "-A")
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(work, "rev-parse", "HEAD").strip()


def _note(session_id):
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": session_id,
                    "stamped_at": "2026-07-15T00:00:00+00:00",
                }
            ],
        }
    )


def _seed_export_scenario(cli_db, tmp_path, *, response_contrast="distinct"):
    """Real facts + a real mirror at the settings-pinned mirror path."""
    from sediment_api.config import settings

    org, repo = "testorg", "testorg/backend"
    chosen_session = "sess-exp-chosen"
    rejected_session = "sess-exp-rejected"
    equal_session = "sess-exp-equal"
    recent = datetime.now(UTC) - timedelta(minutes=5)

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    (work / "README.md").write_text("# fixture\n", encoding="utf-8")
    base = _commit_all(work, "base: initialize fixture")
    (work / "math_utils.py").write_text(_FIB_RED)
    red = _commit_all(work, "red: naive fibonacci")
    (work / "math_utils.py").write_text(_FIB_GREEN)
    green = _commit_all(work, "green: fix fibonacci")
    _git(
        work,
        "notes",
        "--ref=sediment",
        "add",
        "-m",
        _note(rejected_session),
        red,
    )
    _git(
        work,
        "notes",
        "--ref=sediment",
        "add",
        "-m",
        _note(chosen_session),
        green,
    )

    after = green
    if response_contrast == "mixed":
        (work / "math_duplicate.py").write_text(_FIB_GREEN)
        after = _commit_all(work, "rejected: duplicate fibonacci implementation")
        _git(work, "notes", "--ref=sediment", "add", "-m", _note(equal_session), after)

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")

    push = Push(
        org_id=org,
        provider=ForgeProvider.GITHUB,
        repo=repo,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=after,
    )
    from sediment_derive import MirrorManager

    MirrorManager(settings.mirror_path).ensure(push)

    store = FactStore(cli_db)
    store.store_push(push)
    prompt = [InferenceMessage(role="user", parts=[TextPart(content="fix fibonacci")])]
    calls = (
        ("call-a", "a", chosen_session, _FIB_GREEN),
        (
            "call-b",
            "b",
            rejected_session,
            _FIB_GREEN if response_contrast == "equal" else _FIB_RED,
        ),
    )
    if response_contrast == "mixed":
        calls += (("call-c", "c", equal_session, _FIB_GREEN),)
    for call_id, cid_suffix, session_id, output in calls:
        store.store_inference_call(
            _completion(
                session_id=session_id,
                model_call_id=call_id,
                inference_call_id=f"inference-{cid_suffix}",
                input_messages=prompt,
                output_messages=[
                    InferenceMessage(role="assistant", parts=[TextPart(content=output)])
                ],
                observed_at=recent,
            )
        )
    decisions = (
        ("call-a", chosen_session, True),
        ("call-b", rejected_session, False),
    )
    if response_contrast == "mixed":
        decisions += (("call-c", equal_session, False),)
    for call_id, session_id, accepted in decisions:
        store.store_decision(
            DeveloperDecision(
                org_id=org,
                session_id=session_id,
                user_id="dev-1",
                agent_harness=AgentHarness.CLAUDE_CODE,
                file_path="math_utils.py",
                accepted=accepted,
                explicit=True,
                interaction_mode=InteractionMode.AGENT,
                call_id=call_id,
                occurred_at=recent,
            )
        )
    # The failure must precede the pass in fact-time, or the red-to-green
    # transition is an ordering tie and the pair derivation is ambiguous.
    ci_seq = (
        (red, CIResult.FAILED, "r1", recent),
        (green, CIResult.PASSED, "r2", recent + timedelta(minutes=1)),
    )
    if response_contrast == "mixed":
        ci_seq += ((after, CIResult.FAILED, "r3", recent + timedelta(minutes=2)),)
    for sha, result, run, at in ci_seq:
        store.store_ci_outcome(
            CIOutcome(
                org_id=org,
                provider=CIProvider.GITHUB_ACTIONS,
                run_id=run,
                repo=repo,
                commit_sha=sha,
                branch="main",
                workflow_name="CI",
                workflow_path=".github/workflows/ci.yml",
                result=result,
                run_url=f"https://ci.example/{run}",
                captured_at=at,
            )
        )
    return green


def _jsonl_rows(paths) -> list[dict]:
    return [
        json.loads(line)
        for path in paths
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _assert_same_export(direct: Path, reviewed: Path) -> None:
    """Direct and bundle-backed exports agree on parsed rows and row order.

    Bytes are compared only within one mode: the canonical bundle stores nested
    objects with sorted keys while a direct export keeps the source Fact's key
    order, so the same row can serialize differently across modes (ADR 0020).
    """
    assert sorted(path.name for path in direct.iterdir()) == sorted(
        path.name for path in reviewed.iterdir()
    )
    for path in sorted(direct.iterdir()):
        if path.suffix == ".jsonl":
            assert _jsonl_rows([path]) == _jsonl_rows([reviewed / path.name]), path.name
        else:
            assert path.read_bytes() == (reviewed / path.name).read_bytes(), path.name


def _assert_json_shape(value: object, annotation: object) -> None:
    """Assert that decoded JSON exactly matches a derived export dataclass."""
    if annotation is Any:
        return
    if annotation is Never:
        raise AssertionError(f"{value!r} is forbidden by an empty-list contract")
    if isinstance(annotation, TypeAliasType):
        _assert_json_shape(value, annotation.__value__)
        return
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        _assert_json_shape(value, arguments[0])
        return
    if origin in (Required, NotRequired):
        _assert_json_shape(value, arguments[0])
        return
    if origin in (types.UnionType, Union):
        for option in arguments:
            try:
                _assert_json_shape(value, option)
            except AssertionError:
                continue
            return
        raise AssertionError(f"{value!r} does not match {annotation}")
    if origin is Literal:
        assert any(
            type(value) is type(option) and value == option for option in arguments
        )
        return
    if origin in (list, tuple):
        assert isinstance(value, list)
        for item in value:
            _assert_json_shape(item, arguments[0])
        return
    if origin is dict or annotation is dict:
        assert isinstance(value, dict)
        if arguments:
            for key, item in value.items():
                _assert_json_shape(key, arguments[0])
                _assert_json_shape(item, arguments[1])
        return
    if is_typeddict(annotation):
        assert isinstance(value, dict)
        annotations = get_type_hints(annotation, include_extras=True)
        required_keys = {
            key
            for key, field_annotation in annotations.items()
            if get_origin(field_annotation) is Required
            or (
                annotation.__total__ and get_origin(field_annotation) is not NotRequired
            )
        }
        assert required_keys <= set(value) <= set(annotations)
        for key, item in value.items():
            _assert_json_shape(item, annotations[key])
        return
    if isinstance(annotation, type) and is_dataclass(annotation):
        assert isinstance(value, dict)
        annotations = get_type_hints(annotation, include_extras=True)
        declared_fields = {field.name: field for field in fields(annotation)}
        required = {
            name
            for name, declared_field in declared_fields.items()
            if not declared_field.metadata.get("omit_none")
        }
        assert required <= set(value) <= set(declared_fields)
        for name, item in value.items():
            declared_field = declared_fields[name]
            if declared_field.metadata.get("omit_none"):
                assert item is not None
            _assert_json_shape(item, annotations[name])
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        assert isinstance(value, dict)
        annotations = get_type_hints(annotation, include_extras=True)
        allowed = set(annotation.model_fields)
        assert set(value) == allowed
        for name, item in value.items():
            _assert_json_shape(item, annotations[name])
        return
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        assert value in {member.value for member in annotation}
        return
    if annotation in (datetime, AwareDatetime):
        assert isinstance(value, str)
        assert datetime.fromisoformat(value).tzinfo is not None
        return
    if annotation is float:
        assert type(value) in (int, float)
        return
    assert type(value) is annotation


def _assert_published_json_schema(rows: list[dict], contract: type) -> None:
    schema_contract = next(
        item for item in CONTRACTS if item.python_type_object is contract
    )
    schema_path = Path(__file__).parents[2] / schema_contract.output_path
    validator = Draft202012Validator(json.loads(schema_path.read_text()))
    for row in rows:
        assert not list(validator.iter_errors(row))


def test_export_shape_rejects_null_metadata() -> None:
    malformed = {
        "prompt": [],
        "chosen": [],
        "rejected": [],
        "tools": [],
        "metadata": None,
    }

    with pytest.raises(AssertionError):
        _assert_json_shape(malformed, DPOPair)


def test_export_shape_accepts_closed_trainer_messages() -> None:
    _assert_json_shape(
        {"role": "assistant", "content": "done"},
        TrainerMessage,
    )
    _assert_json_shape(
        {
            "role": "assistant",
            "thinking": "inspect first",
            "tool_calls": [
                {
                    "id": "tool-1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": {"path": "app.py"},
                    },
                }
            ],
        },
        TrainerMessage,
    )


def test_export_shape_rejects_malformed_later_trainer_message() -> None:
    messages = [
        {"role": "user", "content": "inspect the file"},
        {"role": "assistant", "content": 42},
    ]

    with pytest.raises(AssertionError):
        _assert_json_shape(messages, list[TrainerMessage])


@pytest.mark.parametrize(
    "message",
    [
        {"content": "missing role"},
        {"role": "assistant"},
    ],
)
def test_export_shape_requires_trainer_message_discriminator_and_payload(
    message,
) -> None:
    with pytest.raises(AssertionError):
        _assert_json_shape(message, TrainerMessage)


def test_export_shape_rejects_malformed_nested_rlvr_message() -> None:
    malformed_turn = {
        "inference_call_id": "inference-1",
        "new_messages": [{"parts": []}],
        "completion": "done",
        "tool_calls": [],
        "decisions": [],
    }

    with pytest.raises(AssertionError):
        _assert_json_shape(malformed_turn, RLVRTurnRow)


def test_export_dpo_and_sft_write_real_rows(cli_db, tmp_path, capsys) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    out = tmp_path / "export"

    assert main(["export", "dpo", "--out", str(out)]) == 0
    dpo_out = capsys.readouterr().out
    assert "pairs projected:" in dpo_out
    dpo_files = list(out.glob("dpo.*.jsonl"))
    dpo_rows = _jsonl_rows(dpo_files)
    assert dpo_rows
    _assert_json_shape(dpo_rows, list[DPOPair])
    _assert_published_json_schema(dpo_rows, DPOPair)

    assert main(["export", "sft", "--out", str(out)]) == 0
    sft_out = capsys.readouterr().out
    assert "samples projected:" in sft_out
    sft_files = list(out.glob("sft.*.jsonl"))
    sft_rows = _jsonl_rows(sft_files)
    assert sft_rows
    _assert_json_shape(sft_rows, list[SFTSample])
    _assert_published_json_schema(sft_rows, SFTSample)
    assert {row["metadata"]["recipe_id"] for row in sft_rows} == {"sft_curated"}


@pytest.mark.parametrize(
    ("format_name", "recipe", "command_name"),
    [
        ("dpo", "dpo_outcome", "cmd_export_dpo"),
        ("sft", "sft_verified", "cmd_export_sft"),
        ("diff-sft", "sft_verified", "cmd_export_diff_sft"),
    ],
)
def test_export_recipe_selection_reaches_the_projection_policy(
    cli_db, tmp_path, monkeypatch, format_name, recipe, command_name
) -> None:
    from sediment_cli import cli as cli_module

    captured = {}

    def capture_pipeline(*_args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_module, "_mirrors", lambda *_args: object())
    monkeypatch.setattr(cli_module, "_attributed_completion_pipeline", capture_pipeline)
    args = cli_module.build_parser().parse_args(
        ["export", format_name, "--recipe", recipe, "--out", str(tmp_path)]
    )
    store = FactStore(cli_db)
    assert getattr(cli_module, command_name)(store, "testorg", args) == 0

    assert captured["extra"]["policy"].recipe_id == recipe


def test_export_diff_sft_runs_the_real_pipeline(cli_db, tmp_path, capsys) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    out = tmp_path / "export"
    assert main(["export", "diff-sft", "--out", str(out)]) == 0
    stdout = capsys.readouterr().out
    assert "samples projected:" in stdout
    diff_rows = _jsonl_rows(out.glob("diff_sft.*.jsonl"))
    assert diff_rows
    _assert_json_shape(diff_rows, list[DiffSFTSample])
    _assert_published_json_schema(diff_rows, DiffSFTSample)


@pytest.mark.parametrize(
    ("target", "artifact", "contract"),
    [
        ("sediment", "tasks.*.jsonl", SedimentTaskRow),
        ("sediment", "rollouts.*.jsonl", SedimentRolloutRow),
        ("swe-bench", "tasks.*.jsonl", SWEBenchTaskRow),
        ("nemo-gym", "rollouts.*.jsonl", NemoGymRolloutRow),
    ],
)
def test_export_rlvr_rows_match_closed_target_contracts(
    cli_db, tmp_path, capsys, target, artifact, contract
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    out = tmp_path / f"rlvr-{target}"

    assert main(["export", "rlvr", "--target", target, "--out", str(out)]) == 0
    capsys.readouterr()
    rows = _jsonl_rows(out.glob(artifact))
    assert rows
    _assert_json_shape(rows, list[contract])
    _assert_published_json_schema(rows, contract)

    if contract is SedimentRolloutRow:
        wrong_recipe_type = copy.deepcopy(rows[0])
        wrong_recipe_type["recipe_version"] = True
        with pytest.raises(AssertionError):
            _assert_json_shape(wrong_recipe_type, contract)

        present_null = copy.deepcopy(rows[0])
        present_null["reward_source"] = None
        with pytest.raises(AssertionError):
            _assert_json_shape(present_null, contract)

    if contract is NemoGymRolloutRow:
        present_null = copy.deepcopy(rows[0])
        present_null["reward"] = None
        with pytest.raises(AssertionError):
            _assert_json_shape(present_null, contract)

        nested_null = copy.deepcopy(rows[0])
        nested_null["responses_create_params"]["input"][0]["finish_reason"] = None
        with pytest.raises(AssertionError):
            _assert_json_shape(nested_null, contract)


def test_derive_writes_complete_bundle_and_summary(cli_db, tmp_path, capsys) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    destination = tmp_path / "derived"

    assert main(["derive", "--out", str(destination), "--users", "dev-1"]) == 0

    assert {path.name for path in destination.iterdir()} == {
        "manifest.json",
        "attributed_completions.jsonl",
        "rollouts.jsonl",
        "inference_calls.jsonl",
        "inference_call_identities.jsonl",
        "repository_identities.jsonl",
        "repository_renames.jsonl",
    }
    stdout = capsys.readouterr().out
    assert "attributed completions:" in stdout
    assert "rollouts:" in stdout
    assert "fragmented:" in stdout
    assert "policy digest:" in stdout
    assert "sha256:" in stdout
    for path in destination.iterdir():
        assert f"sha256: {path.name} " in stdout


@pytest.mark.parametrize("fmt", ["dpo", "sft", "diff-sft"])
def test_bundle_backed_export_matches_direct_without_opening_fact_store(
    cli_db, tmp_path, capsys, monkeypatch, fmt
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    direct = tmp_path / "direct"
    reviewed = tmp_path / "reviewed"
    direct.mkdir()
    reviewed.mkdir()
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()
    assert main(["export", fmt, "--out", str(direct)]) == 0
    capsys.readouterr()

    def fail_store(*_args, **_kwargs):
        raise AssertionError("bundle-backed export opened the live fact store")

    monkeypatch.setattr("sediment_cli.cli.FactStore", fail_store)
    assert main(["export", fmt, "--from", str(bundle), "--out", str(reviewed)]) == 0
    assert _jsonl_rows(sorted(direct.iterdir()))
    _assert_same_export(direct, reviewed)


def test_bundle_backed_rlvr_matches_direct(cli_db, tmp_path, capsys) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    direct = tmp_path / "direct-rlvr"
    reviewed = tmp_path / "reviewed-rlvr"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()
    assert main(["export", "rlvr", "--target", "sediment", "--out", str(direct)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "export",
                "rlvr",
                "--target",
                "sediment",
                "--from",
                str(bundle),
                "--out",
                str(reviewed),
            ]
        )
        == 0
    )
    _assert_same_export(direct, reviewed)


def test_bundle_backed_nemo_gym_export_runs_without_a_mirror(
    cli_db, tmp_path, capsys, monkeypatch
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    direct = tmp_path / "direct-nemo"
    reviewed = tmp_path / "reviewed-nemo"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()
    assert main(["export", "rlvr", "--target", "nemo-gym", "--out", str(direct)]) == 0
    capsys.readouterr()

    def fail_store(*_args, **_kwargs):
        raise AssertionError("bundle-backed export opened the live fact store")

    monkeypatch.setattr("sediment_cli.cli.FactStore", fail_store)
    monkeypatch.delenv("SEDIMENT_MIRROR_PATH", raising=False)
    assert (
        main(
            [
                "export",
                "rlvr",
                "--target",
                "nemo-gym",
                "--from",
                str(bundle),
                "--out",
                str(reviewed),
            ]
        )
        == 0
    )
    _assert_same_export(direct, reviewed)
    rows = _jsonl_rows(reviewed.glob("rollouts.*.jsonl"))
    assert rows
    _assert_json_shape(rows, list[NemoGymRolloutRow])
    _assert_published_json_schema(rows, NemoGymRolloutRow)


@pytest.mark.parametrize("target", ["sediment", "swe-bench"])
def test_bundle_backed_rlvr_patched_targets_still_require_a_mirror(
    cli_db, tmp_path, capsys, monkeypatch, target
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()
    monkeypatch.delenv("SEDIMENT_MIRROR_PATH", raising=False)
    assert (
        main(
            [
                "export",
                "rlvr",
                "--target",
                target,
                "--from",
                str(bundle),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 1
    )
    assert "SEDIMENT_MIRROR_PATH" in capsys.readouterr().err


def test_bundle_exports_do_not_require_database_settings(
    cli_db, tmp_path, capsys
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()

    env = os.environ.copy()
    env.pop("SEDIMENT_DATABASE_URL", None)
    env["SEDIMENT_MIRROR_PATH"] = str(tmp_path / "mirror")
    commands = (
        ["export", "diff-sft", "--from", str(bundle)],
        ["export", "rlvr", "--target", "sediment", "--from", str(bundle)],
    )
    for index, command in enumerate(commands):
        result = subprocess.run(
            [
                str(Path(sys.executable).with_name("sediment")),
                *command,
                "--out",
                str(tmp_path / f"storage-free-{index}"),
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_bundle_backed_export_rejects_tampering_without_live_fallback(
    cli_db, tmp_path, capsys
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    assert main(["derive", "--out", str(bundle)]) == 0
    with (bundle / "inference_calls.jsonl").open("ab") as handle:
        handle.write(b"{}\n")

    assert (
        main(
            [
                "export",
                "sft",
                "--from",
                str(bundle),
                "--out",
                str(tmp_path / "export"),
            ]
        )
        == 1
    )
    assert "does not match" in capsys.readouterr().err


def test_export_recovery_writes_real_rows_without_phantom_skips(
    cli_db, tmp_path, capsys
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    out = tmp_path / "export"
    assert main(["export", "recovery", "--out", str(out)]) == 0
    stdout = capsys.readouterr().out
    assert "pairs derived: 1" in stdout
    rows = _jsonl_rows(out.glob("recovery.*.jsonl"))
    assert rows
    _assert_json_shape(rows, list[RecoveryRow])
    _assert_published_json_schema(rows, RecoveryRow)
    # Regression: the list-instead-of-mapping bug tallied every completion
    # as inference_call_not_found while still exiting 0.
    assert "inference_call_not_found" not in stdout


def test_export_recovery_uses_canonical_eval_fraction(cli_db, tmp_path, capsys) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    out = tmp_path / "export"
    assert main(["export", "recovery", "--out", str(out)]) == 0
    # Split mode writes per-split files, never the unsplit name.
    assert not (out / "recovery.jsonl").exists()
    split_files = sorted(f.name for f in out.glob("recovery.*.jsonl"))
    assert split_files, "expected recovery.train/eval jsonl in split mode"
    total = sum(len(f.read_text().splitlines()) for f in out.glob("recovery.*.jsonl"))
    assert total >= 1


# Per-report behavior is covered by scripts/tests; these prove the CLI
# forwards argv verbatim, never needs Settings, and defaults --org from env.


def test_report_model_forwards_argv(
    tmp_path, postgres_database_url, capsys, monkeypatch
) -> None:
    monkeypatch.delenv("SEDIMENT_ORG_ID", raising=False)
    code = main(
        [
            "report",
            "model",
            "--org",
            "acme-corp",
            "--database-url",
            postgres_database_url,
            "--mirror-path",
            str(tmp_path / "mirror"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "org: acme-corp" in out
    assert "no data" in out


def test_report_org_defaults_from_env(
    tmp_path, postgres_database_url, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("SEDIMENT_ORG_ID", "acme-corp")
    code = main(
        [
            "report",
            "model",
            "--database-url",
            postgres_database_url,
            "--mirror-path",
            str(tmp_path / "mirror"),
        ]
    )
    assert code == 0
    assert "org: acme-corp" in capsys.readouterr().out


def test_report_unknown_name_fails(capsys) -> None:
    assert main(["report", "nope"]) == 1
    assert "unknown report 'nope'" in capsys.readouterr().err


def test_report_bare_lists_choices_on_stderr_and_exits_2(capsys) -> None:
    assert main(["report"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "model" in captured.err
    assert "attribution-share" in captured.err


def test_report_help_lists_choices_on_stdout(capsys) -> None:
    assert main(["report", "--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "attribution-share" in captured.out


def test_mirror_gc_dispatches_dry_run(tmp_path, postgres_database_url, capsys) -> None:
    code = main(
        [
            "mirror-gc",
            "--org",
            "acme-corp",
            "--database-url",
            postgres_database_url,
            "--mirror-path",
            str(tmp_path / "mirror"),
        ]
    )
    assert code == 0
    assert "no mirrored repos found" in capsys.readouterr().out


@pytest.fixture()
def local_server_stub(monkeypatch):
    import sediment_core.postgres_roles as roles

    monkeypatch.setattr(
        os,
        "environ",
        {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("SEDIMENT_")
        },
    )
    monkeypatch.setenv(
        "SEDIMENT_BOOTSTRAP_DATABASE_URL",
        "postgresql://bootstrap:private-bootstrap@localhost/sediment",
    )
    monkeypatch.setitem(
        sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None)
    )
    monkeypatch.setattr(roles, "provision_database", lambda *a, **k: None)


def test_server_never_generates_a_shadow_token_over_env(
    tmp_path, monkeypatch, capsys, local_server_stub
) -> None:
    monkeypatch.setenv("SEDIMENT_API_BEARER_TOKEN", "env-token-425")
    monkeypatch.setenv("SEDIMENT_OPERATOR_TOKEN", "env-operator-425")
    root = tmp_path / "server-root"
    assert main(["server", "--root", str(root)]) == 0
    output = capsys.readouterr()
    saved = (root / "server.env").read_text()
    for name, token in (
        ("SEDIMENT_API_BEARER_TOKEN", "env-token-425"),
        ("SEDIMENT_OPERATOR_TOKEN", "env-operator-425"),
    ):
        assert name not in saved
        assert token not in saved + output.out + output.err
        assert os.environ[name] == token


def test_server_second_run_names_the_env_file_not_the_environment(
    tmp_path, monkeypatch, capsys, local_server_stub
) -> None:
    root = tmp_path / "server-root"
    assert main(["server", "--root", str(root)]) == 0
    assert "Generated server credentials" in capsys.readouterr().out
    original = (root / "server.env").read_bytes()
    for name in (
        "SEDIMENT_API_BEARER_TOKEN",
        "SEDIMENT_OPERATOR_TOKEN",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET",
    ):
        os.environ.pop(name, None)
    monkeypatch.setenv(
        "SEDIMENT_BOOTSTRAP_DATABASE_URL",
        "postgresql://bootstrap:private-bootstrap@localhost/sediment",
    )
    assert main(["server", "--root", str(root)]) == 0
    assert f"Using credentials from {root / 'server.env'}" in capsys.readouterr().out
    assert (root / "server.env").read_bytes() == original


def test_server_keeps_generated_tokens_out_of_output_and_logs(
    tmp_path, monkeypatch, capsys, caplog, local_server_stub
) -> None:
    import secrets

    tokens = [f"generated-private-secret-{index}" for index in range(6)]
    generated = iter(tokens)
    monkeypatch.setattr(secrets, "token_hex", lambda _size: next(generated))
    root = tmp_path / "server-root"
    assert main(["server", "--root", str(root)]) == 0
    captured = capsys.readouterr()
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in tokens:
        assert secret not in captured.out + captured.err + rendered_logs
        assert secret in (root / "server.env").read_text()
    assert (root / "server.env").stat().st_mode & 0o777 == 0o600


def test_server_preserves_a_complete_existing_credential_file(
    tmp_path, local_server_stub
) -> None:
    root = tmp_path / "server-root"
    root.mkdir()
    env_file = root / "server.env"
    original = (
        "# operator-owned credential file\n"
        + "".join(
            f"SEDIMENT_{name}=existing-{name.lower()}-592\n"
            for name in (
                "API_BEARER_TOKEN",
                "OPERATOR_TOKEN",
                "GITHUB_WEBHOOK_SECRET",
                "MIGRATOR_PASSWORD",
                "RUNTIME_PASSWORD",
                "OPERATOR_PASSWORD",
            )
        )
        + "OPERATOR_NOTE=preserve-this-line\n"
    )
    env_file.write_text(original)
    env_file.chmod(0o644)
    assert main(["server", "--root", str(root)]) == 0
    assert env_file.read_text() == original
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_derive_streams_hashes_and_samples_without_whole_file_readback(
    cli_db, tmp_path, capsys, monkeypatch
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    destination = tmp_path / "derived"
    read_bytes, read_text = Path.read_bytes, Path.read_text

    def bounded_bytes(path, *args, **kwargs):
        assert path.parent != destination, "derive read back a complete bundle file"
        return read_bytes(path, *args, **kwargs)

    def bounded_text(path, *args, **kwargs):
        assert path.parent != destination, "derive sampled a complete bundle file"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", bounded_bytes)
    monkeypatch.setattr(Path, "read_text", bounded_text)
    assert main(["derive", "--out", str(destination), "--sample", "1"]) == 0
    stdout = capsys.readouterr().out
    assert stdout.count("sha256:") == 7
    assert stdout.count("sample attributed_completions.jsonl:") == 1
    assert stdout.count("sample rollouts.jsonl:") == 1


@pytest.mark.parametrize("format_name", ["dpo", "sft", "diff-sft", "rlvr"])
def test_offline_public_exports_keep_file_backed_bundle_alive(
    cli_db, tmp_path, capsys, monkeypatch, format_name
) -> None:
    from sediment_cli import cli as cli_module

    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()

    def forbid_materializer(*args, **kwargs):
        pytest.fail("offline public export called the small-data materializer")

    monkeypatch.setattr(
        cli_module, "read_derived_bundle", forbid_materializer, raising=False
    )
    command = [
        "export",
        format_name,
        "--from",
        str(bundle),
        "--out",
        str(tmp_path / "export"),
    ]
    if format_name == "rlvr":
        command += ["--target", "nemo-gym"]
    assert main(command) == 0
    assert any((tmp_path / "export").glob("*.jsonl"))


@pytest.mark.parametrize(
    ("format_name", "recipe"),
    [
        ("dpo", "dpo_outcome"),
        ("sft", "sft_verified"),
        ("diff-sft", "sft_verified"),
    ],
)
def test_alternate_recipe_direct_and_offline_public_exports_match(
    cli_db, tmp_path, capsys, format_name, recipe
) -> None:
    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()
    outputs, summaries = [], []
    for mode in ("direct", "offline"):
        destination = tmp_path / mode
        command = ["export", format_name, "--recipe", recipe, "--out", str(destination)]
        if mode == "offline":
            command += ["--from", str(bundle)]
        assert main(command) == 0
        summaries.append(
            next(
                line
                for line in capsys.readouterr().out.splitlines()
                if " projected:" in line
            )
        )
        rows = _jsonl_rows(destination.glob("*.jsonl"))
        assert rows
        assert {row["metadata"]["recipe_id"] for row in rows} == {recipe}
        outputs.append(destination)
    _assert_same_export(*outputs)
    assert summaries[0] == summaries[1]


@pytest.mark.parametrize("fmt", ["sft", "dpo"])
def test_bundle_consumer_profile_publishes_aligned_private_artifacts(
    cli_db, tmp_path, capsys, fmt
) -> None:
    from sediment_export import DPOPolicy, SFTPolicy, project_dpo, project_sft
    from sediment_export.derived_bundle import read_derived_bundle

    _seed_export_scenario(cli_db, tmp_path)
    bundle = tmp_path / "derived-consumer"
    destination = tmp_path / "consumer"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()
    profile = "fireworks-dpo-v2" if fmt == "dpo" else "fireworks-sft-v1"
    assert (
        main(
            [
                "export",
                fmt,
                "--from",
                str(bundle),
                "--profile",
                profile,
                "--out",
                str(destination),
            ]
        )
        == 0
    )
    manifest = json.loads((destination / "compatibility.json").read_text())
    source = read_derived_bundle(bundle)
    project = project_sft if fmt == "sft" else project_dpo
    policy = SFTPolicy() if fmt == "sft" else DPOPolicy()
    projection = project(
        source.attributed_completions,
        {call.inference_call_id: call for call in source.inference_calls},
        policy,
    )
    assert manifest["canonical_skipped"] == dict(projection.skipped)
    if fmt == "sft":
        assert manifest["canonical_skipped"]["explicit_reject"] > 0
    assert manifest["skipped"] == {}
    assert manifest["profile"]["id"] == profile
    assert manifest["rows"] > 0
    assert destination.stat().st_mode & 0o777 == 0o700
    assert "dataset diagnostics:" in capsys.readouterr().out
    for path in destination.glob("data*.jsonl"):
        evidence = destination / path.name.replace("data", "evidence", 1)
        assert len(path.read_text().splitlines()) == len(
            evidence.read_text().splitlines()
        )


@pytest.mark.parametrize("recipe", ["dpo_human", "dpo_outcome"])
def test_dpo_contrast_matches_direct_and_offline_bundle(
    cli_db, tmp_path, capsys, monkeypatch, recipe
):
    _seed_export_scenario(cli_db, tmp_path, response_contrast="mixed")
    bundle = tmp_path / "derived"
    direct, offline = tmp_path / "direct", tmp_path / "offline"
    assert main(["derive", "--out", str(bundle)]) == 0
    capsys.readouterr()
    assert main(["export", "dpo", "--recipe", recipe, "--out", str(direct)]) == 0
    direct_summary = capsys.readouterr().out
    assert "pairs projected: 1  skipped: {'identical_responses': 1}" in direct_summary
    before = FactStore(cli_db).read_inference_calls("testorg")

    def fail_live(*_args, **_kwargs):
        raise AssertionError("offline DPO opened the live store or mirror")

    monkeypatch.setattr("sediment_cli.cli.FactStore", fail_live)
    monkeypatch.setattr("sediment_cli.cli._mirrors", fail_live)
    assert (
        main(
            [
                "export",
                "dpo",
                "--recipe",
                recipe,
                "--from",
                str(bundle),
                "--out",
                str(offline),
            ]
        )
        == 0
    )
    offline_summary = capsys.readouterr().out
    assert "pairs projected: 1  skipped: {'identical_responses': 1}" in offline_summary
    assert {p.name: p.read_bytes() for p in direct.iterdir()} == {
        p.name: p.read_bytes() for p in offline.iterdir()
    }
    [row] = _jsonl_rows(direct.glob("*.jsonl"))
    assert row["chosen"] == [{"role": "assistant", "content": _FIB_GREEN}]
    assert row["rejected"] == [{"role": "assistant", "content": _FIB_RED}]
    assert row["metadata"]["recipe_id"] == recipe
    assert row["metadata"]["recipe_version"] == 2
    assert row["metadata"]["schema_version"] == 4
    assert row["metadata"]["chosen_completion_id"] == "inference-a"
    assert row["metadata"]["rejected_completion_id"] == "inference-b"
    assert FactStore(cli_db).read_inference_calls("testorg") == before


@pytest.mark.parametrize("from_bundle", [False, True])
def test_all_equal_dpo_keeps_existing_dataset_and_reports_no_write(
    cli_db, tmp_path, capsys, monkeypatch, from_bundle
):
    _seed_export_scenario(cli_db, tmp_path, response_contrast="equal")
    bundle = tmp_path / "derived"
    args = ["export", "dpo"]
    if from_bundle:
        assert main(["derive", "--out", str(bundle)]) == 0
        args += ["--from", str(bundle)]

        def fail_live(*_args, **_kwargs):
            raise AssertionError("offline DPO opened the live store")

        monkeypatch.setattr("sediment_cli.cli.FactStore", fail_live)
    destination = tmp_path / "prior"
    destination.mkdir()
    prior = destination / "dpo.train.jsonl"
    prior.write_bytes(b'{"earlier": "dataset"}\n')
    capsys.readouterr()
    assert main(args + ["--out", str(destination)]) == 0
    summary = capsys.readouterr().out
    assert "pairs projected: 0  skipped: {'identical_responses': 1}" in summary
    assert (
        "nothing written (empty projections leave existing files untouched)" in summary
    )
    assert "wrote " not in summary
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == {
        "dpo.train.jsonl": b'{"earlier": "dataset"}\n'
    }


def test_direct_and_offline_rlvr_agree_by_value_and_keep_each_modes_bytes(
    cli_db, tmp_path, capsys
) -> None:
    """ADR 0020: the canonical bundle sorts nested object keys; a direct export
    keeps the source Fact's key order. Both byte sequences stay as published."""
    green = _seed_export_scenario(cli_db, tmp_path)
    FactStore(cli_db).store_ci_outcome(
        CIOutcome(
            org_id="testorg",
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="r3",
            repo="testorg/backend",
            commit_sha=green,
            branch="main",
            workflow_name="CI",
            workflow_path=".github/workflows/ci.yml",
            result=CIResult.PASSED,
            run_url="https://ci.example/r3",
            captured_at=datetime.now(UTC) - timedelta(minutes=3),
            raw={"zz": 1, "aaa": {"q": 2}},
        )
    )
    bundle, direct, offline = (tmp_path / name for name in ("derived", "d", "o"))
    assert main(["derive", "--out", str(bundle)]) == 0
    assert main(["export", "rlvr", "--target", "sediment", "--out", str(direct)]) == 0
    command = ["export", "rlvr", "--target", "sediment", "--from", str(bundle)]
    assert main(command + ["--out", str(offline)]) == 0
    capsys.readouterr()
    _assert_same_export(direct, offline)
    direct_text = "".join(p.read_text() for p in sorted(direct.glob("rollouts*.jsonl")))
    offline_text = "".join(
        p.read_text() for p in sorted(offline.glob("rollouts*.jsonl"))
    )
    assert '"zz": 1, "aaa": {"q": 2}' in direct_text, (
        "direct export lost Fact key order"
    )
    assert '"aaa": {"q": 2}, "zz": 1' in offline_text, "bundle export lost sorted keys"
    assert direct_text != offline_text
