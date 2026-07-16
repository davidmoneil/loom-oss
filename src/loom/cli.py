"""Command-line entry point for Loom."""

from __future__ import annotations

import argparse
import os

import uvicorn

from loom.config import LoomConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Loom LLM Gateway")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the gateway server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--config", default=None)

    args = parser.parse_args()

    if args.command in ("serve", None):
        if getattr(args, "config", None):
            os.environ["LOOM_CONFIG"] = args.config

        config = LoomConfig.load()
        host = getattr(args, "host", None) or config.server.host
        port = getattr(args, "port", None) or config.server.port

        uvicorn.run(
            "loom.gateway.app:app",
            host=host,
            port=port,
            log_config=None,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
