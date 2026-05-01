"""Command line interface for Linode Image Lab."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .cleanup import select_cleanup_candidates
from .freeze import freeze_plan
from .manifest import create_manifest, serialize_manifest
from .regions import parse_regions
from .thaw import thaw_plan


def add_region_args(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--region",
        action="append",
        required=required,
        help="Linode region id. May be repeated or comma-separated.",
    )
    parser.add_argument("--run-id", help="Optional run id for deterministic planning.")
    parser.add_argument("--ttl", help="Optional ISO-8601 TTL timestamp.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linode-image-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Emit a dry-run manifest preview.")
    add_region_args(plan, required=True)
    plan.add_argument(
        "--mode",
        choices=("freeze", "thaw", "freeze-thaw"),
        default="freeze-thaw",
        help="Workflow mode to model.",
    )

    freeze = subparsers.add_parser("freeze", help="Placeholder freeze command.")
    add_region_args(freeze, required=True)

    thaw = subparsers.add_parser("thaw", help="Placeholder thaw command.")
    add_region_args(thaw, required=True)

    freeze_thaw = subparsers.add_parser("freeze-thaw", help="Placeholder combined command.")
    add_region_args(freeze_thaw, required=True)

    cleanup = subparsers.add_parser("cleanup", help="Placeholder cleanup command.")
    cleanup.add_argument("--run-id", help="Optional run id to include in the cleanup preview.")
    cleanup.add_argument("--ttl", help="Optional ISO-8601 TTL timestamp.")

    return parser


def command_manifest(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "plan":
        return create_manifest(
            command="plan",
            mode=args.mode,
            regions=parse_regions(args.region),
            run_id=args.run_id,
            ttl=args.ttl,
            dry_run=True,
            status="planned",
        )

    if args.command == "freeze":
        return freeze_plan(regions=parse_regions(args.region), run_id=args.run_id, ttl=args.ttl)

    if args.command == "thaw":
        return thaw_plan(regions=parse_regions(args.region), run_id=args.run_id, ttl=args.ttl)

    if args.command == "freeze-thaw":
        manifest = create_manifest(
            command="freeze-thaw",
            mode="freeze-thaw",
            regions=parse_regions(args.region),
            run_id=args.run_id,
            ttl=args.ttl,
            dry_run=True,
            status="placeholder",
        )
        manifest["message"] = "freeze-thaw is a non-mutating placeholder in M1"
        return manifest

    if args.command == "cleanup":
        manifest = create_manifest(
            command="cleanup",
            mode="freeze-thaw",
            regions=[],
            run_id=args.run_id,
            ttl=args.ttl,
            dry_run=True,
            status="placeholder",
        )
        manifest["message"] = "cleanup is independently runnable and non-mutating in M1"
        manifest["cleanup_candidates"] = select_cleanup_candidates([])
        return manifest

    raise ValueError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = command_manifest(args)
    except ValueError as exc:
        parser.error(str(exc))
    sys.stdout.write(serialize_manifest(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
