# SPDX-License-Identifier: AGPL-3.0-or-later
"""Record an agent's Anthropic Messages traffic through the erode proxy.

E1 of the gateway pruning spec
(``docs/superpowers/specs/2026-09-28-gateway-context-pruning-design.md``)
records real agent Sessions with pruning off, then replays them offline with
``scripts/erode_replay.py``. This tool is the recorder: it runs
``erode.proxy.make_server(upstream, prune=False)`` and saves, for each
``POST /v1/messages`` call, the request body exactly as the agent sent it.

Each recorder process writes one run directory, ``OUTPUT/RUN``:

- ``NNNNNN.json``: a request body whose upstream response succeeded (2xx).
  File names sort in send order, as the replay requires.
- ``NNNNNN.rejected``: a request body the upstream refused, such as a 429 or
  529 that the agent then retried. The provider doesn't bill these, so the
  replay doesn't read them.
- ``NNNNNN.partial``: a request body that never got a response, because the
  upstream was unreachable.
- ``usage.jsonl``: one line per response with counts only: the request
  number, HTTP status, model, and the response's ``usage`` token counts,
  merged across a stream's ``message_start`` and ``message_delta`` events.

Request bodies hold prompts and repository content. The directory and files
are private to the current user; keep them out of Git. ``usage.jsonl`` holds
no content.

The recorder drops the agent's ``Accept-Encoding`` header so the upstream
answers uncompressed and the recorder can read ``usage`` from the stream.
That changes transport only: the request body and every other header reach
the upstream unchanged.

Usage:
    uv run python scripts/erode_record.py --upstream https://api.anthropic.com \\
        --output ~/erode-recordings --run issue-123
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contrib/erode/src"))
from erode import proxy  # noqa: E402

logger = logging.getLogger("erode.record")

RECORDED_PATH = "/v1/messages"


def usage_counts(usage: Any) -> dict[str, Any]:
    """Keep only the numeric fields of a ``usage`` object, nested ones too."""
    if not isinstance(usage, dict):
        return {}
    kept: dict[str, Any] = {}
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            kept[key] = value
        elif isinstance(value, dict) and (nested := usage_counts(value)):
            kept[key] = nested
    return kept


class UsageReader:
    """Read the model and ``usage`` counts from a Messages response body.

    A streamed response carries usage in ``message_start`` and updates it in
    ``message_delta``; a plain JSON response carries it at the top level.
    Content is parsed only to find those fields and is never kept.
    """

    def __init__(self, streamed: bool) -> None:
        self.streamed = streamed
        self.buffer = b""
        self.model: str | None = None
        self.usage: dict[str, Any] = {}

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        if not self.streamed:
            return
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                self._event(line[5:])

    def finish(self) -> None:
        if self.streamed:
            if self.buffer.startswith(b"data:"):
                self._event(self.buffer[5:])
        else:
            self._message(self.buffer)
        self.buffer = b""

    def _event(self, data: bytes) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                self._take(message)
        elif event.get("type") == "message_delta":
            self.usage.update(usage_counts(event.get("usage")))

    def _message(self, body: bytes) -> None:
        try:
            message = json.loads(body)
        except ValueError:
            return
        if isinstance(message, dict):
            self._take(message)

    def _take(self, message: dict[str, Any]) -> None:
        if isinstance(message.get("model"), str):
            self.model = message["model"]
        self.usage.update(usage_counts(message.get("usage")))


class _TeeResponse:
    """Pass the upstream response through, feeding each chunk to a reader."""

    def __init__(self, response: Any, reader: UsageReader) -> None:
        self._response = response
        self._reader = reader

    def read1(self, size: int = -1) -> bytes:
        chunk = self._response.read1(size)
        self._reader.feed(chunk)
        return chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def _private(path: str, flags: int) -> int:
    """Open a recording file readable by the current user only."""
    return os.open(path, flags, 0o600)


class Recorder:
    """Numbers recorded requests and writes them into one run directory."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()

    def save_request(self, body: bytes) -> int:
        with self._lock:
            number = next(self._numbers)
        with open(self._path(number, "partial"), "xb", opener=_private) as handle:
            handle.write(body)
        return number

    def finish(self, number: int, status: int, reader: UsageReader | None) -> None:
        suffix = "json" if 200 <= status < 300 else "rejected"
        self._path(number, "partial").rename(self._path(number, suffix))
        line = {"request": number, "status": status}
        if reader is not None:
            line["model"] = reader.model
            line["usage"] = reader.usage
        usage_path = self.run_dir / "usage.jsonl"
        with self._lock, open(usage_path, "a", opener=_private) as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")

    def _path(self, number: int, suffix: str) -> Path:
        return self.run_dir / f"{number:06d}.{suffix}"


class RecordingHandler(proxy.ProxyHandler):
    recorder: Recorder
    _number: int | None = None

    def _body(self) -> bytes:
        body = super()._body()
        self._number = None
        if self.command == "POST" and urlsplit(self.path).path == RECORDED_PATH:
            self._number = self.recorder.save_request(body)
            del self.headers["Accept-Encoding"]
        return body

    def _relay(self, response: Any) -> None:
        if self._number is None:
            super()._relay(response)
            return
        streamed = "event-stream" in (response.getheader("Content-Type") or "")
        reader = UsageReader(streamed)
        try:
            super()._relay(_TeeResponse(response, reader))
        finally:
            reader.finish()
            self.recorder.finish(self._number, response.status, reader)
            logger.info(
                "erode_record request=%d status=%d usage_fields=%d",
                self._number,
                response.status,
                len(reader.usage),
            )
            self._number = None


def make_server(
    upstream: str, run_dir: Path, host: str = "127.0.0.1", port: int = 8787
) -> proxy.ThreadingHTTPServer:
    """The erode proxy with pruning off, recording into ``run_dir``."""
    server = proxy.make_server(upstream, host=host, port=port, prune=False)
    server.RequestHandlerClass = type(
        "ConfiguredRecordingHandler",
        (RecordingHandler, server.RequestHandlerClass),
        {"recorder": Recorder(run_dir)},
    )
    return server


def prepare_run_dir(output: Path, run: str) -> Path:
    """Create a fresh private run directory; refuse to mix two recordings."""
    if not run or Path(run).name != run or run in (".", ".."):
        raise ValueError("run must be a plain directory name")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = output / run
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(run_dir, 0o700)
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upstream", required=True, help="provider or gateway URL")
    parser.add_argument("--output", type=Path, required=True, help="recordings root")
    parser.add_argument("--run", required=True, help="name of this run's directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        run_dir = prepare_run_dir(args.output, args.run)
        server = make_server(args.upstream, run_dir, args.host, args.port)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host, port = server.server_address[:2]
    print(f"Recording into {run_dir}")
    print(f"Point the agent here: ANTHROPIC_BASE_URL=http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
