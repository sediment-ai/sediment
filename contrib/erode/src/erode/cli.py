# SPDX-License-Identifier: MIT
"""The ``erode`` command."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from erode.proxy import make_server

MODES = ("supersede", "off")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="erode")
    commands = parser.add_subparsers(dest="command", required=True)
    proxy = commands.add_parser(
        "proxy",
        help="Forward model requests to one upstream, stubbing stale tool output.",
    )
    proxy.add_argument(
        "--upstream",
        default=os.environ.get("ERODE_UPSTREAM"),
        help="Gateway or provider base URL (default: $ERODE_UPSTREAM)",
    )
    proxy.add_argument("--host", default="127.0.0.1", help="Bind address")
    proxy.add_argument("--port", type=int, default=8787, help="Bind port")
    proxy.add_argument(
        "--mode",
        choices=MODES,
        default=os.environ.get("ERODE_MODE", "supersede"),
        help="supersede prunes; off forwards every request unchanged "
        "(default: $ERODE_MODE or supersede)",
    )
    args = parser.parse_args(argv)
    if not args.upstream:
        proxy.error("--upstream or ERODE_UPSTREAM is required")
    if args.mode not in MODES:
        proxy.error(f"ERODE_MODE must be one of {', '.join(MODES)}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        server = make_server(
            args.upstream, args.host, args.port, prune=args.mode == "supersede"
        )
    except (ValueError, OSError) as exc:
        proxy.error(str(exc))
    logging.getLogger("erode.proxy").info(
        "erode_proxy listening=%s:%d upstream=%s mode=%s",
        args.host,
        args.port,
        args.upstream,
        args.mode,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
