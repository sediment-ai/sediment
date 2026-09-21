# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exact consumer selection rejects incompatible requests before reading a bundle."""

import pytest
from sediment_cli.cli import main


@pytest.mark.parametrize(
    "format,profile", [("sft", "hf-trl-dpo-v2"), ("dpo", "hf-trl-sft-v1")]
)
def test_wrong_objective_is_rejected_at_argument_boundary(tmp_path, format, profile):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "export",
                format,
                "--profile",
                profile,
                "--from",
                str(tmp_path / "missing"),
                "--out",
                str(tmp_path / "out"),
            ]
        )
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def test_target_and_profile_must_agree_before_bundle_read(tmp_path, capsys):
    result = main(
        [
            "export",
            "rlvr",
            "--target",
            "swe-bench",
            "--profile",
            "nemo-gym-rollouts-v1",
            "--from",
            str(tmp_path / "missing"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert result == 1
    assert "profile and --target" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("consumer", ["hf-trl", "fireworks"])
def test_retired_dpo_name_explains_successor_before_reading_bundle(
    consumer, tmp_path, capsys
):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "export",
                "dpo",
                "--profile",
                f"{consumer}-dpo-v1",
                "--from",
                str(tmp_path / "missing"),
                "--out",
                str(tmp_path / "absent"),
            ]
        )
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "retired" in error
    assert f"{consumer}-dpo-v2" in error
    assert "re-export" in error
    assert not (tmp_path / "absent").exists()
