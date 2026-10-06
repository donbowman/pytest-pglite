"""Standalone server entry point: ``pytest-pglite``."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from .config import PGliteConfig
from .server import PGliteServer


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pytest-pglite",
        description="Run a PGlite server (PostgreSQL 17.5 in WebAssembly).",
    )
    parser.add_argument("--work-dir", default=None, help="Database directory.")
    parser.add_argument(
        "--extensions",
        default="",
        help="Comma separated extensions to create at startup.",
    )
    parser.add_argument(
        "--tcp-address",
        default=None,
        metavar="HOST:PORT",
        help="Listen on TCP as well (port 0 = OS-assigned).",
    )
    parser.add_argument("--wasm", default=None, help="Custom bin/pglite.wasi build.")
    parser.add_argument("--keep-tmp", action="store_true", help="Keep files.")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    host = "127.0.0.1"
    port = 0
    if args.tcp_address:
        parsed_host, _, parsed_port = args.tcp_address.rpartition(":")
        host = parsed_host or "127.0.0.1"
        if parsed_port.isdigit():
            port = int(parsed_port)
    extensions = tuple(
        part.strip() for part in args.extensions.split(",") if part.strip()
    )
    server = PGliteServer(
        PGliteConfig(
            work_dir=Path(args.work_dir) if args.work_dir else None,
            extensions=extensions,
            tcp=bool(args.tcp_address),
            tcp_host=host,
            tcp_port=port,
            wasm_path=Path(args.wasm) if args.wasm else None,
            keep_tmp=bool(args.keep_tmp),
            log_level=args.log_level or "INFO",
        )
    )
    server.start()
    print(f"PGlite ready: {server.dsn}")
    print(f"work dir: {server.work_dir}")
    stopped = False

    def _handle_signal(signum: int, frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        while not stopped:
            time.sleep(0.2)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
