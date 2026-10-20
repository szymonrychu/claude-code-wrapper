"""`ccw` command line: serve, check-config, version."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections.abc import Sequence
from importlib import resources

from claude_code_wrapper import __version__
from claude_code_wrapper.config import ConfigError, load_config
from claude_code_wrapper.service import run_service
from claude_code_wrapper.tooling import ToolingError

EX_CONFIG = 78
DEFAULT_CONFIG = "/etc/ccw/config.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccw")
    parser.add_argument("--version", action="version", version=f"ccw {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the service")
    serve.add_argument("--config", default=None, help="config path (default $CCW_CONFIG)")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    check = sub.add_parser("check-config", help="validate a config and print it redacted")
    check.add_argument("--config", required=True)
    sub.add_parser("version", help="print the version")
    config = sub.add_parser("config", help="bundled configuration helpers")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("example", help="print the bundled example config.yaml")
    return parser


def missing_prerequisites(binary: str) -> list[str]:
    """Names of required executables (the configured claude binary, git) not found on PATH."""
    return [name for name in (binary, "git") if shutil.which(name) is None]


def _serve(args: argparse.Namespace) -> int:
    path = args.config or os.environ.get("CCW_CONFIG") or DEFAULT_CONFIG
    try:
        cfg = load_config(path)
        if args.host is not None or args.port is not None:
            server = cfg.server.model_copy(
                update={
                    k: v for k, v in (("host", args.host), ("port", args.port)) if v is not None
                }
            )
            cfg = cfg.model_copy(update={"server": server})
            type(cfg).model_validate(cfg.model_dump())  # re-check cross-field rules
    except (ConfigError, ValueError) as exc:
        print(f"ccw: invalid config: {exc}", file=sys.stderr)
        return EX_CONFIG
    missing = missing_prerequisites(cfg.claude.binary)
    if missing:
        print(
            f"ccw: required executable(s) not found on PATH: {', '.join(missing)}. "
            "Install them (claude: npm install -g @anthropic-ai/claude-code) or set "
            "claude.binary to an absolute path.",
            file=sys.stderr,
        )
        return EX_CONFIG
    try:
        asyncio.run(run_service(cfg))
    except KeyboardInterrupt:
        return 0
    except (ToolingError, OSError) as exc:
        print(f"ccw: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "config":
        text = resources.files("claude_code_wrapper.data").joinpath("config.example.yaml")
        sys.stdout.write(text.read_text(encoding="utf-8"))
        return 0
    if args.command == "check-config":
        try:
            cfg = load_config(args.config)
        except ConfigError as exc:
            print(f"ccw: invalid config: {exc}", file=sys.stderr)
            return EX_CONFIG
        print(json.dumps(cfg.redacted(), indent=2))
        return 0
    return _serve(args)
