"""Command line interface for Linode Image Lab."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .cleanup import select_cleanup_candidates
from .capture import capture_plan
from .deploy import deploy_plan
from .manifest import create_manifest, serialize_manifest
from .regions import parse_regions


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
        choices=("capture", "deploy", "capture-deploy"),
        default="capture-deploy",
        help="Workflow mode to model.",
    )

    capture = subparsers.add_parser("capture", help="Placeholder capture command.")
    add_region_args(capture, required=True)

    deploy = subparsers.add_parser("deploy", help="Placeholder deploy command.")
    add_region_args(deploy, required=True)

    capture_deploy = subparsers.add_parser("capture-deploy", help="Placeholder combined command.")
    add_region_args(capture_deploy, required=True)

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

    if args.command == "capture":
        return capture_plan(regions=parse_regions(args.region), run_id=args.run_id, ttl=args.ttl)

    if args.command == "deploy":
        return deploy_plan(regions=parse_regions(args.region), run_id=args.run_id, ttl=args.ttl)

    if args.command == "capture-deploy":
        manifest = create_manifest(
            command="capture-deploy",
            mode="capture-deploy",
            regions=parse_regions(args.region),
            run_id=args.run_id,
            ttl=args.ttl,
            dry_run=True,
            status="placeholder",
        )
        manifest["message"] = "capture-deploy is a non-mutating placeholder in M1"
        return manifest

    if args.command == "cleanup":
        manifest = create_manifest(
            command="cleanup",
            mode="capture-deploy",
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
