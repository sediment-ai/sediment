# SPDX-License-Identifier: AGPL-3.0-or-later
"""Record an agent's Anthropic Messages requests through the erode proxy.

E1 of the gateway pruning spec
(``docs/superpowers/specs/2026-09-28-gateway-context-pruning-design.md``)
records real agent Sessions with pruning off, then replays them offline with
``scripts/erode_replay.py``. This is the recorder: the erode proxy with
pruning off, saving each ``POST /v1/messages`` body before forwarding it
unchanged. It saves no headers, so no credentials, and no responses.

Each body lands in ``OUT_DIR`` as a zero-padded sequence number in send order
(``00001.json``, ``00002.json``, ...), which is the layout the replay reads:
use one ``OUT_DIR`` per recorded run. Files are private to the user
(mode 0600), because they hold prompts and repository content.

Usage:
    uv run python scripts/erode_record.py OUT_DIR --upstream https://api.anthropic.com
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contrib/erode/src"))
from erode.proxy import make_server  # noqa: E402

RECORDED_ROUTE = "/v1/messages"


def recording_server(upstream: str, out: Path, host: str, port: int):
    """The erode proxy with pruning off, saving each Messages request body."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError(f"output directory is not empty: {out}")
    server = make_server(upstream, host=host, port=port, prune=False)
    numbers = itertools.count(1)
    lock = threading.Lock()
    base = server.RequestHandlerClass

    class RecordingHandler(base):
        def _body(self) -> bytes:
            body = super()._body()
            if self.command == "POST" and urlsplit(self.path).path == RECORDED_ROUTE:
                with lock:  # numbering and writing together keep send order
                    path = out / f"{next(numbers):05d}.json"
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as file:
                        file.write(body)
            return body

    server.RequestHandlerClass = RecordingHandler
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path, help="empty directory for this run")
    parser.add_argument("--upstream", required=True, help="provider or gateway URL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    try:
        server = recording_server(args.upstream, args.out, args.host, args.port)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"recording {RECORDED_ROUTE} to {args.out} on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
