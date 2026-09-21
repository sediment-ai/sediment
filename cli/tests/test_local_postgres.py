# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local command owns one private server root and database lifecycle."""

from __future__ import annotations

import io
import os
import sys
import types
from contextlib import contextmanager

import pytest

from sediment_cli import cli


@pytest.fixture()
def server_environment(monkeypatch):
    monkeypatch.setattr(
        os,
        "environ",
        {k: v for k, v in os.environ.items() if not k.startswith("SEDIMENT_")},
    )


@pytest.mark.parametrize("fail", [False, True])
def test_server_owns_managed_database_until_api_exit(
    tmp_path, monkeypatch, server_environment, fail
):
    from sediment_cli import local_postgres
    import sediment_core.postgres_roles as roles

    root = tmp_path / "server"
    events = []

    @contextmanager
    def database(directory, password):
        assert directory == root
        assert len(password) >= 32
        events.append("database started")
        try:
            yield f"postgresql+psycopg://sediment_bootstrap:{password}@127.0.0.1:45678/sediment"
        finally:
            events.append("database stopped")

    def provision(url, **passwords):
        assert url.startswith("postgresql+psycopg://sediment_bootstrap:")
        assert len(set(passwords.values())) == 3
        events.append("provisioned")

    def serve(*args, **kwargs):
        events.append("api started")
        assert os.environ["SEDIMENT_DATABASE_URL"].startswith(
            "postgresql+psycopg://sediment_runtime:"
        )
        assert not any(
            k.endswith("_PASSWORD") for k in os.environ if k.startswith("SEDIMENT_")
        )
        assert "SEDIMENT_BOOTSTRAP_DATABASE_URL" not in os.environ
        if fail:
            raise OSError("API startup failed")

    monkeypatch.setattr(local_postgres, "managed_postgres", database)
    monkeypatch.setattr(roles, "provision_database", provision)
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=serve))
    assert cli.main(["server", "--root", str(root)]) == int(fail)
    assert events == [
        "database started",
        "provisioned",
        "api started",
        "database stopped",
    ]
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "server.env").stat().st_mode & 0o777 == 0o600


def test_root_lock_refuses_concurrent_server_then_releases(tmp_path):
    from sediment_cli.local_postgres import server_root

    root = tmp_path / "server"
    with server_root(root):
        with pytest.raises(ValueError, match="already running"):
            with server_root(root):
                pytest.fail("second server acquired the same root")
    with server_root(root):
        assert root.is_dir()


def test_root_lock_rejects_symlink_without_changing_target(tmp_path):
    from sediment_cli.local_postgres import server_root

    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    linked = tmp_path / "server"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="private directory"):
        with server_root(linked):
            pytest.fail("followed server root symlink")
    assert target.stat().st_mode & 0o777 == 0o755


def test_download_mismatch_never_installs_binaries(tmp_path, monkeypatch):
    from sediment_cli import local_postgres

    monkeypatch.setattr(
        local_postgres.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(b"untrusted archive"),
    )
    with pytest.raises(ValueError, match="checksum"):
        local_postgres._install_postgres(tmp_path)
    assert not list(tmp_path.rglob("postgres"))


def test_unsupported_platform_explains_external_database(tmp_path, monkeypatch):
    from sediment_cli import local_postgres

    monkeypatch.setattr(local_postgres.platform, "system", lambda: "Windows")
    with pytest.raises(ValueError, match="SEDIMENT_BOOTSTRAP_DATABASE_URL"):
        local_postgres._install_postgres(tmp_path)


def test_root_lock_rejects_hard_link_without_changing_other_file(tmp_path):
    from sediment_cli.local_postgres import server_root

    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    unrelated.chmod(0o644)
    root = tmp_path / "server"
    root.mkdir()
    (root / "server.lock").hardlink_to(unrelated)
    with pytest.raises(ValueError, match="regular private file"):
        with server_root(root):
            pytest.fail("locked an unrelated inode")
    assert unrelated.read_text() == "preserve"
    assert unrelated.stat().st_mode & 0o777 == 0o644


def test_download_refuses_archive_escape_before_installing(tmp_path, monkeypatch):
    import hashlib
    import tarfile

    from sediment_cli import local_postgres

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        member = tarfile.TarInfo("../../escaped")
        member.size = 8
        bundle.addfile(member, io.BytesIO(b"not safe"))
    data = archive.getvalue()
    monkeypatch.setattr(local_postgres.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_postgres.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        local_postgres,
        "_PACKAGES",
        {
            ("Linux", "x86_64"): (
                "x86_64-unknown-linux-gnu",
                hashlib.sha256(data).hexdigest(),
            )
        },
    )
    monkeypatch.setattr(
        local_postgres.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(data)
    )
    with pytest.raises(ValueError, match="safely"):
        local_postgres._install_postgres(tmp_path)
    assert not (tmp_path.parent / "escaped").exists()
    assert list(tmp_path.iterdir()) == []


def test_mac_install_uses_host_ssl_and_reuses_verified_download(tmp_path, monkeypatch):
    import hashlib
    import tarfile

    from sediment_cli import local_postgres

    host = tmp_path / "host-openssl"
    (host / "lib").mkdir(parents=True)
    for filename in ("libssl.3.dylib", "libcrypto.3.dylib"):
        (host / "lib" / filename).write_bytes(b"host library")
    name = f"postgresql-{local_postgres.VERSION}-aarch64-apple-darwin"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        directory = tarfile.TarInfo(f"{name}/lib")
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        for filename in (
            "bin/postgres",
            "bin/initdb",
            "lib/libssl.3.dylib",
            "lib/libcrypto.3.dylib",
        ):
            member = tarfile.TarInfo(f"{name}/{filename}")
            content = b"archive contents"
            member.size = len(content)
            member.mode = 0o755
            bundle.addfile(member, io.BytesIO(content))
    data = archive.getvalue()
    monkeypatch.setattr(local_postgres.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(local_postgres.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        local_postgres,
        "_PACKAGES",
        {
            ("Darwin", "arm64"): (
                "aarch64-apple-darwin",
                hashlib.sha256(data).hexdigest(),
            )
        },
    )
    monkeypatch.setattr(
        local_postgres.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout=str(host)),
    )
    monkeypatch.setattr(
        local_postgres.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(data)
    )
    root = tmp_path / "server"
    root.mkdir()
    binaries = local_postgres._install_postgres(root)
    for filename in ("libssl.3.dylib", "libcrypto.3.dylib"):
        library = binaries.parent / "lib" / filename
        assert library.is_symlink()
        assert library.resolve() == host / "lib" / filename
    monkeypatch.setattr(
        local_postgres.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("downloaded again"),
    )
    assert local_postgres._install_postgres(root) == binaries
