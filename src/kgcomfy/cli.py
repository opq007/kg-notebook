"""Minimal CLI for validating configs and dry-running a notebook setup."""

from __future__ import annotations

import argparse
import json
import sys

from .app import KaggleComfy
from .config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kgcomfy",
        description="Kaggle ComfyUI bootstrap helper",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show-config", help="print the resolved config")
    show.add_argument("--config", default="tts-img.yaml")

    prepare = subparsers.add_parser(
        "prepare", help="install/update/link models without starting services"
    )
    prepare.add_argument("--config", default="tts-img.yaml")
    prepare.add_argument("--dry-run", action="store_true")

    run = subparsers.add_parser("run", help="full pipeline (blocks on ComfyUI)")
    run.add_argument("--config", default="tts-img.yaml")
    run.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if getattr(args, "dry_run", False):
            config.dry_run = True
        config.validate()

        if args.command == "show-config":
            print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
            return 0

        app = KaggleComfy(config)
        if args.command == "prepare":
            app.prepare()
        elif args.command == "run":
            app.run()
        return 0
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
