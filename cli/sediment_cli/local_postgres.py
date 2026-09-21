# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private local PostgreSQL installation and foreground server ownership."""

from __future__ import annotations

import hashlib
import os
import platform
import signal
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

VERSION = "17.11.0"
_PACKAGES = {
    ("Darwin", "arm64"): (
        "aarch64-apple-darwin",
        "fd4b62794b160e26973a768a1eef3248aef9d2ff23ebd6d884a4299485e28e57",
    ),
    ("Darwin", "x86_64"): (
        "x86_64-apple-darwin",
        "e43a81b15e1cfe7f9d8fd79c6d4d0366e9001a5f690e322224dca704656602f7",
    ),
    ("Linux", "aarch64"): (
        "aarch64-unknown-linux-gnu",
        "abffda09209280ec1502b73720dc4d254fb7fff9a072e324926c600a5b16c221",
    ),
    ("Linux", "x86_64"): (
        "x86_64-unknown-linux-gnu",
        "b7a1ba6bae6499d8296e3e81b0171eecfd1766ca9aaa0057e41ad3e844e5e2e0",
    ),
}
_EXTERNAL = "Set SEDIMENT_BOOTSTRAP_DATABASE_URL to use external PostgreSQL."


@contextmanager
def private_file(path: Path, flags: int):
    """Open an owned regular file without following links or waiting on a FIFO."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise ValueError(f"{path.name} must be a regular private file")
        os.fchmod(fd, 0o600)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def server_root(root: Path):
    """Hold one root for the entire API and PostgreSQL lifetime."""
    if os.name != "posix":
        raise ValueError("sediment server requires macOS or Linux")
    import fcntl

    if root.is_symlink():
        raise ValueError("server root must be a regular private directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise ValueError("server root must be a regular private directory")
    root.chmod(0o700)
    with private_file(root / "server.lock", os.O_CREAT | os.O_RDWR) as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"a server is already running under {root}") from None
        # Uvicorn restores and re-raises signals after its own graceful shutdown.
        # This handler also covers PostgreSQL initialization and provisioning.
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _interrupt)
        try:
            yield
        finally:
            signal.signal(signal.SIGTERM, previous)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _install_postgres(root: Path) -> Path:
    package = _PACKAGES.get((platform.system(), platform.machine()))
    if package is None:
        raise ValueError(
            f"managed PostgreSQL isn't available on this platform. {_EXTERNAL}"
        )
    target, checksum = package
    name = f"postgresql-{VERSION}-{target}"
    destination = root / name
    host_ssl = None
    if platform.system() == "Darwin":
        try:
            prefix = subprocess.run(
                ["brew", "--prefix", "openssl@3"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            host_ssl = Path(prefix) / "lib"
            if not host_ssl.is_absolute() or not all(
                (host_ssl / name).is_file()
                for name in ("libssl.3.dylib", "libcrypto.3.dylib")
            ):
                raise ValueError
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError(
                "managed PostgreSQL requires Homebrew openssl@3; install the quickstart prerequisites, then retry"
            ) from None
    if destination.is_symlink():
        raise ValueError("PostgreSQL installation must be a regular private directory")
    if destination.exists():
        if not all(
            (destination / "bin" / executable).is_file()
            for executable in ("postgres", "initdb")
        ):
            raise ValueError(f"incomplete PostgreSQL installation at {destination}")
        return destination / "bin"
    print(f"Downloading PostgreSQL {VERSION.removesuffix('.0')} for this machine...")
    url = f"https://github.com/theseus-rs/postgresql-binaries/releases/download/{VERSION}/{name}.tar.gz"
    with tempfile.TemporaryDirectory(
        prefix=".postgres-download-", dir=root
    ) as temporary:
        staging = Path(temporary)
        archive = staging / "postgres.tar.gz"
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                urllib.request.urlopen(url, timeout=30) as response,
                archive.open("wb") as stream,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 128 * 1024 * 1024:
                        raise ValueError("PostgreSQL download exceeds the size limit")
                    digest.update(chunk)
                    stream.write(chunk)
        except OSError:
            raise ValueError(
                f"PostgreSQL download failed; check your connection and retry. {_EXTERNAL}"
            ) from None
        if digest.hexdigest() != checksum:
            raise ValueError(
                "PostgreSQL download checksum mismatch; no binaries were installed"
            )
        try:
            with tarfile.open(archive, mode="r:gz") as bundle:

                def installed_file(member, path):
                    # Use maintained host OpenSSL; never install the archive's
                    # copied libraries. Ordinary loader paths follow the two
                    # links below, including initdb's shell-launched children.
                    if host_ssl is not None and member.name in {
                        f"{name}/lib/libssl.3.dylib",
                        f"{name}/lib/libcrypto.3.dylib",
                    }:
                        return None
                    return tarfile.data_filter(member, path)

                bundle.extractall(staging, filter=installed_file)
        except (tarfile.TarError, OSError):
            raise ValueError(
                "PostgreSQL archive could not be extracted safely"
            ) from None
        installed = staging / name
        if not all(
            (installed / "bin" / executable).is_file()
            for executable in ("postgres", "initdb")
        ):
            raise ValueError("PostgreSQL archive lacks the required executables")
        if host_ssl is not None:
            for library in ("libssl.3.dylib", "libcrypto.3.dylib"):
                (installed / "lib" / library).symlink_to(host_ssl / library)
        installed.rename(destination)
    return destination / "bin"


def _stop(process: subprocess.Popen) -> None:
    """Stop only the child this invocation owns; never act on a saved PID."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)  # Fast, orderly PostgreSQL shutdown.
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@contextmanager
def managed_postgres(root: Path, password: str):
    """Yield a bootstrap URL while an owned loopback PostgreSQL process runs."""
    from sqlalchemy.engine import URL
    from sediment_core.postgres_engine import DatabaseOperationError

    if os.geteuid() == 0:
        raise ValueError(f"managed PostgreSQL must run as a non-root user. {_EXTERNAL}")
    # Keep the existing maintained host driver; fail before downloading or
    # initializing data if it isn't installed.
    try:
        import psycopg
    except ImportError:
        raise ValueError(
            "PostgreSQL client library unavailable; install libpq (macOS) or libpq5 (Debian/Ubuntu), then retry"
        ) from None
    binaries = _install_postgres(root)
    data = root / "postgres"
    if data.is_symlink():
        raise ValueError("PostgreSQL data must be a regular private directory")
    if data.exists() and (
        not (data / "PG_VERSION").is_file()
        or (data / "PG_VERSION").read_text().strip() != "17"
    ):
        raise ValueError(
            f"existing database at {data} isn't PostgreSQL 17; it was preserved"
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SEDIMENT_", "PG"))
    }
    environment["LC_ALL"] = "C"
    log = root / "postgres.log"
    with private_file(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND) as log_fd:
        if not data.exists():
            print(f"Creating PostgreSQL database in {data}")
            with tempfile.TemporaryDirectory(
                prefix=".postgres-init-", dir=root
            ) as temporary:
                staging = Path(temporary)
                password_file = staging / "password"
                with private_file(password_file, os.O_CREAT | os.O_WRONLY) as fd:
                    os.write(fd, password.encode() + b"\n")
                initializer = subprocess.Popen(
                    [
                        str(binaries / "initdb"),
                        "-D",
                        str(staging / "data"),
                        "--username=sediment_bootstrap",
                        f"--pwfile={password_file}",
                        "--auth=scram-sha-256",
                        "--encoding=UTF8",
                        "--locale=C",
                    ],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=log_fd,
                    start_new_session=True,
                )
                try:
                    if initializer.wait(timeout=60):
                        raise ValueError(f"PostgreSQL initialization failed; see {log}")
                except subprocess.TimeoutExpired:
                    raise ValueError(
                        f"PostgreSQL initialization timed out; see {log}"
                    ) from None
                finally:
                    _stop(initializer)
                (staging / "data").rename(data)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = URL.create(
            "postgresql+psycopg",
            username="sediment_bootstrap",
            password=password,
            host="127.0.0.1",
            port=port,
            database="sediment",
        )
        process = subprocess.Popen(
            [
                str(binaries / "postgres"),
                "-D",
                str(data),
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-c",
                "unix_socket_directories=",
                "-c",
                "ssl=off",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise ValueError(f"PostgreSQL startup failed; see {log}")
                try:
                    with psycopg.connect(
                        host="127.0.0.1",
                        port=port,
                        user="sediment_bootstrap",
                        password=password,
                        dbname="postgres",
                        connect_timeout=1,
                        sslmode="disable",
                        autocommit=True,
                    ) as connection:
                        if not connection.execute(
                            "SELECT 1 FROM pg_database WHERE datname = 'sediment'"
                        ).fetchone():
                            connection.execute("CREATE DATABASE sediment")
                    break
                except psycopg.OperationalError:
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            f"PostgreSQL didn't become ready; see {log}"
                        ) from None
                    time.sleep(0.1)
                except psycopg.Error:
                    raise DatabaseOperationError(
                        f"create local database failed; see {log}"
                    ) from None
            yield url.render_as_string(hide_password=False)
        finally:
            _stop(process)
