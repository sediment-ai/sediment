# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise the installer with fake package managers and isolated user state."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).parents[2] / "install.sh"
FAKE_TOOL = r'''
import json
import os
import pathlib
import subprocess
import sys

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "uname":
    print(os.environ.get("TEST_MACHINE", "x86_64") if args == ["-m"] else
          os.environ.get("TEST_PLATFORM", "Linux"))
elif name == "id":
    print(os.environ.get("TEST_UID", "1000"))
elif name == "sudo":
    if os.environ.get("TEST_FAIL") == "sudo":
        sys.exit(1)
    sys.exit(subprocess.run(args, check=False).returncode)
elif name in ("apt-get", "brew"):
    if os.environ.get("TEST_FAIL") in (name, args[0]):
        sys.exit(1)
    if name == "brew" and args[0] == "--prefix":
        print(os.environ["TEST_BREW_PREFIX"])
elif name == "curl":
    target = pathlib.Path(args[args.index("-o") + 1])
    target.write_text("""#!/bin/sh
printf 'bootstrap-ran\n' > "$HOME/bootstrap-ran"
[ "${TEST_FAIL:-}" != bootstrap ] || exit 1
[ "${TEST_FAIL:-}" != bootstrap-no-uv ] || exit 0
mkdir -p "$UV_INSTALL_DIR"
cp "$TEST_UV_STUB" "$UV_INSTALL_DIR/uv"
""")
    if os.environ.get("TEST_FAIL") == "curl":
        sys.exit(22)
elif name in ("uv", "pipx", "python3", "python3.12"):
    if args[:2] == ["tool", "dir"]:
        print(os.environ["UV_TOOL_BIN_DIR"])
    elif args[:1] == ["environment"]:
        print(os.environ["PIPX_BIN_DIR"])
    elif args[:3] == ["-m", "site", "--user-base"]:
        print(pathlib.Path(os.environ["HOME"]) / ".local")
    elif args[:1] == ["-c"]:
        sys.exit(1 if os.environ.get("TEST_FAIL") == "python-version" else 0)
    elif "install" in args:
        if os.environ.get("TEST_FAIL") == "install-cli":
            sys.exit(1)
        target = pathlib.Path(os.environ.get("UV_TOOL_BIN_DIR") or
                              os.environ.get("PIPX_BIN_DIR") or
                              str(pathlib.Path(os.environ["HOME"]) / ".local/bin"))
        target.mkdir(parents=True, exist_ok=True)
        executable = target / "sediment"
        executable.write_text("#!/bin/sh\n[ \"${TEST_FAIL:-}\" != verify ]\n")
        executable.chmod(0o755)
    else:
        sys.exit(2)
else:
    sys.exit(2)
'''


@pytest.fixture
def installer(tmp_path: Path):
    home = tmp_path / "user's home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "commands.jsonl"
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
    uv_stub.chmod(0o755)
    for command in ("mkdir", "mktemp", "rm", "sed", "cp", "sh"):
        (tools / command).symlink_to(shutil.which(command))
    for command in ("uname", "id", "sudo", "apt-get", "brew", "curl", "uv"):
        target = tools / command
        target.write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
        target.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": str(tools),
        "TEST_LOG": str(log),
        "TMPDIR": str(tmp_path),
        "TEST_UV_STUB": str(uv_stub),
        "TEST_BREW_PREFIX": str(tmp_path / "homebrew"),
    }

    def run(*args: str, changes: dict[str, str] | None = None):
        log.unlink(missing_ok=True)
        return subprocess.run(
            ["/bin/sh", str(INSTALL_SH), *args],
            env=env | (changes or {}),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def commands():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return run, commands, home, tools, env


def test_default_prepares_linux_and_installs_python_312_tool(installer):
    run, commands, home, _, _ = installer
    result = run()
    assert result.returncode == 0, result.stderr
    calls = commands()
    assert ["sudo", "apt-get", "update"] in calls
    assert [
        "apt-get",
        "install",
        "-y",
        "git",
        "ca-certificates",
        "libpq5",
        "libxml2",
        "libzstd1",
        "liblz4-1",
        "zlib1g",
    ] in calls
    assert [
        "uv",
        "tool",
        "install",
        "--python",
        "3.12",
        "--upgrade",
        "sediment-cli",
    ] in calls
    assert (home / ".local/bin/sediment").is_file()
    assert "sediment server" in result.stdout
    assert "Installing host packages with sudo apt-get" in result.stdout
    export = next(
        line.strip() for line in result.stdout.splitlines() if "export PATH=" in line
    )
    probe = subprocess.run(
        ["/bin/sh", "-c", export + "\ncommand -v sediment"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == str(home / ".local/bin/sediment")


def test_mac_installs_maintained_libraries(installer):
    run, commands, _, _, _ = installer
    result = run(changes={"TEST_PLATFORM": "Darwin"})
    assert result.returncode == 0, result.stderr
    assert ["brew", "install", "libpq", "openssl@3"] in commands()
    assert not any(call[0] in ("apt-get", "sudo") for call in commands())


def test_uses_known_user_bin_already_on_parent_path(installer):
    run, _, home, tools, _ = installer
    user_bin = home / "bin"
    user_bin.mkdir()
    result = run(changes={"PATH": f"{tools}:{user_bin}"})
    assert result.returncode == 0, result.stderr
    assert (user_bin / "sediment").is_file()
    assert "export PATH=" not in result.stdout


def test_bootstraps_uv_without_falling_back_to_system_python(installer):
    run, commands, home, tools, _ = installer
    (tools / "uv").unlink()
    (tools / "python3").write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
    (tools / "python3").chmod(0o755)
    result = run()
    assert result.returncode == 0, result.stderr
    assert (home / "bootstrap-ran").exists()
    assert not any(call[0] == "python3" for call in commands())
    assert (home / ".local/bin/sediment").is_file()


@pytest.mark.parametrize("failure", ["curl", "bootstrap", "bootstrap-no-uv"])
def test_bootstrap_failure_never_reports_success(installer, failure):
    run, commands, home, tools, _ = installer
    (tools / "uv").unlink()
    result = run(changes={"TEST_FAIL": failure})
    assert result.returncode != 0
    assert not any(call[:3] == ["uv", "tool", "install"] for call in commands())
    assert "sediment server" not in result.stdout
    assert not list(home.parent.glob("sediment-uv.*"))
    if failure == "curl":
        assert not (home / "bootstrap-ran").exists()


@pytest.mark.parametrize(
    "failure", ["sudo", "update", "install", "install-cli", "verify"]
)
def test_failures_never_report_ready(installer, failure):
    run, _, _, _, _ = installer
    result = run(changes={"TEST_FAIL": failure})
    assert result.returncode != 0
    assert "sediment server" not in result.stdout


@pytest.mark.parametrize(
    "platform,missing", [("Darwin", "brew"), ("Linux", "apt-get"), ("Linux", "sudo")]
)
def test_missing_package_manager_or_privilege_tool_fails(installer, platform, missing):
    run, commands, _, tools, _ = installer
    (tools / missing).unlink()
    result = run(changes={"TEST_PLATFORM": platform})
    assert result.returncode != 0
    assert not any(call[:3] == ["uv", "tool", "install"] for call in commands())


def test_default_refuses_root_before_installing_packages(installer):
    run, commands, _, _, _ = installer
    result = run(changes={"TEST_UID": "0"})
    assert result.returncode != 0
    assert "normal user" in result.stderr
    assert not any(call[0] in ("apt-get", "brew", "sudo", "uv") for call in commands())


def test_capture_only_does_not_require_system_package_access(installer):
    run, commands, _, tools, _ = installer
    (tools / "apt-get").unlink()
    (tools / "sudo").unlink()
    result = run("--capture-only")
    assert result.returncode == 0, result.stderr
    assert "sediment login" in result.stdout
    assert "sediment server" not in result.stdout
    assert not any(call[0] in ("apt-get", "brew", "sudo") for call in commands())


@pytest.mark.parametrize(
    "method,expected",
    [
        ("uv", "uv tool install --python 3.12 --upgrade sediment-cli==0.2.0"),
        ("pipx", "pipx install --python python3.12 sediment-cli==0.2.0"),
        ("pip", "python3.12 -m pip install --user sediment-cli==0.2.0"),
    ],
)
def test_dry_run_preserves_explicit_methods_and_version(installer, method, expected):
    run, commands, home, _, _ = installer
    result = run("--dry-run", "--method", method, "--version", "0.2.0")
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert not (home / ".local").exists()
    assert not any(
        call[0] in ("apt-get", "brew", "sudo", "curl", "uv") for call in commands()
    )


def test_dry_run_describes_bootstrap_without_executing_it(installer):
    run, commands, home, tools, _ = installer
    (tools / "uv").unlink()
    result = run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert "https://astral.sh/uv/install.sh" in result.stdout
    assert "sudo apt-get install" in result.stdout
    assert not (home / "bootstrap-ran").exists()
    assert not any(
        call[0] in ("apt-get", "brew", "sudo", "curl", "uv") for call in commands()
    )


def test_env_method_is_preserved(installer):
    run, _, _, _, _ = installer
    result = run("--dry-run", changes={"SEDIMENT_INSTALL_METHOD": "pipx"})
    assert result.returncode == 0
    assert "pipx install --python python3.12" in result.stdout


@pytest.mark.parametrize(
    "args", [("--method", "conda"), ("--bad-option",), ("--version",)]
)
def test_invalid_option_fails_before_installing(installer, args):
    run, _, _, _, _ = installer
    result = run(*args)
    assert result.returncode != 0
    assert "error:" in result.stderr


def test_homebrew_failure_stops_before_python_installation(installer):
    run, commands, _, _, _ = installer
    result = run(changes={"TEST_PLATFORM": "Darwin", "TEST_FAIL": "brew"})
    assert result.returncode != 0
    assert not any(call[:3] == ["uv", "tool", "install"] for call in commands())


def test_unsupported_native_platform_fails_before_package_changes(installer):
    run, commands, _, _, _ = installer
    result = run(changes={"TEST_MACHINE": "armv7l"})
    assert result.returncode != 0
    assert not any(call[0] in ("apt-get", "brew", "sudo", "uv") for call in commands())


@pytest.mark.parametrize("method", ["pipx", "pip"])
def test_explicit_alternate_method_installs_and_verifies(installer, method):
    run, _, home, tools, _ = installer
    for command in ("python3.12", "pipx"):
        target = tools / command
        target.write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
        target.chmod(0o755)
    result = run("--method", method)
    assert result.returncode == 0, result.stderr
    assert (home / ".local/bin/sediment").is_file()


@pytest.mark.parametrize("method", ["pipx", "pip"])
def test_alternate_method_rejects_unsupported_python(installer, method):
    run, _, home, tools, _ = installer
    target = tools / "python3"
    target.write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
    target.chmod(0o755)
    result = run("--method", method, changes={"TEST_FAIL": "python-version"})
    assert result.returncode != 0
    assert "requires Python 3.12" in result.stderr
    assert not (home / ".local/bin/sediment").exists()
