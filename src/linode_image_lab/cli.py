"""Command line interface for Linode Image Lab."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .cleanup import CleanupError, cleanup_plan
from .capture import CaptureError, capture_plan
from .capture_deploy import CaptureDeployError, capture_deploy_plan
from .config import ConfigError, command_defaults, load_config
from .deploy import DeployError, deploy_plan
from .manifest import create_manifest, serialize_manifest
from .regions import parse_regions


def add_config_arg(parser: argparse.ArgumentParser, *, dest: str) -> None:
    parser.add_argument(
        "--config",
        dest=dest,
        help="Optional TOML file with execution defaults.",
    )


def add_region_args(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--region",
        action="append",
        required=False,
        help="Linode region id. May be repeated or comma-separated.",
    )
    parser.add_argument("--run-id", help="Optional run id for deterministic planning.")
    parser.add_argument("--ttl", help="Optional ISO-8601 TTL timestamp.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linode-image-lab")
    add_config_arg(parser, dest="global_config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Emit a dry-run manifest preview.")
    add_config_arg(plan, dest="command_config")
    add_region_args(plan, required=True)
    plan.add_argument(
        "--mode",
        choices=("capture", "deploy", "capture-deploy"),
        default="capture-deploy",
        help="Workflow mode to model.",
    )

    capture = subparsers.add_parser("capture", help="Plan or execute a single-region capture.")
    add_config_arg(capture, dest="command_config")
    add_region_args(capture, required=True)
    capture.add_argument("--execute", action="store_true", help="Opt into Linode API mutations.")
    capture.add_argument("--source-image", help="Source image id for the temporary capture Linode.")
    capture.add_argument("--type", dest="instance_type", help="Linode type for the temporary capture Linode.")
    capture.add_argument("--image-label", help="Optional label for the captured custom image.")
    capture.add_argument(
        "--preserve-source",
        action="store_true",
        help="Keep the temporary capture-source Linode after execution.",
    )

    deploy = subparsers.add_parser("deploy", help="Plan or execute a single-region deploy.")
    add_config_arg(deploy, dest="command_config")
    add_region_args(deploy, required=True)
    deploy.add_argument("--execute", action="store_true", help="Opt into Linode API mutations.")
    deploy.add_argument("--image-id", help="Custom image id for the temporary deploy Linode.")
    deploy.add_argument("--type", dest="instance_type", help="Linode type for the temporary deploy Linode.")
    deploy.add_argument(
        "--preserve-instance",
        action="store_true",
        help="Keep the temporary deploy Linode after execution.",
    )

    capture_deploy = subparsers.add_parser("capture-deploy", help="Plan or execute capture plus deploy validation.")
    add_config_arg(capture_deploy, dest="command_config")
    add_region_args(capture_deploy, required=True)
    capture_deploy.add_argument("--execute", action="store_true", help="Opt into Linode API mutations.")
    capture_deploy.add_argument("--source-image", help="Source image id for the temporary capture Linode.")
    capture_deploy.add_argument(
        "--type",
        dest="instance_type",
        help="Linode type for the temporary capture and deploy Linodes.",
    )
    capture_deploy.add_argument(
        "--preserve-instance",
        action="store_true",
        help="Keep the temporary deploy validation Linode after execution.",
    )

    cleanup = subparsers.add_parser("cleanup", help="Plan or execute tag-scoped cleanup.")
    add_config_arg(cleanup, dest="command_config")
    cleanup.add_argument("--execute", action="store_true", help="Opt into Linode API deletion of expired resources.")
    cleanup.add_argument("--run-id", help="Optional run id filter for cleanup selection.")
    cleanup.add_argument("--ttl", help="Optional ISO-8601 TTL timestamp.")

    return parser


def resolve_config_defaults(args: argparse.Namespace) -> None:
    config = load_config(config_path(args))
    defaults = command_defaults(config, args.command)

    if args.command in {"plan", "capture", "deploy", "capture-deploy"}:
        if args.region is None:
            args.region = config_regions(defaults)
        if not parse_regions(args.region):
            raise ValueError("at least one non-empty --region is required")

    if args.ttl is None and "ttl" in defaults:
        args.ttl = defaults["ttl"]

    if args.command in {"capture", "capture-deploy"}:
        if args.source_image is None and "source_image" in defaults:
            args.source_image = defaults["source_image"]
        if args.instance_type is None and "type" in defaults:
            args.instance_type = defaults["type"]

    if args.command == "deploy":
        if args.image_id is None and "image_id" in defaults:
            args.image_id = defaults["image_id"]
        if args.instance_type is None and "type" in defaults:
            args.instance_type = defaults["type"]


def config_path(args: argparse.Namespace) -> str | None:
    global_config = getattr(args, "global_config", None)
    command_config = getattr(args, "command_config", None)
    if global_config is not None and command_config is not None:
        raise ValueError("provide --config either before or after the command, not both")
    return command_config or global_config


def config_regions(defaults: dict[str, Any]) -> list[str] | None:
    if "regions" in defaults:
        return list(defaults["regions"])
    if "region" in defaults:
        return [defaults["region"]]
    return None


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
        return capture_plan(
            regions=parse_regions(args.region),
            run_id=args.run_id,
            ttl=args.ttl,
            execute=args.execute,
            source_image=args.source_image,
            instance_type=args.instance_type,
            image_label=args.image_label,
            preserve_source=args.preserve_source,
        )

    if args.command == "deploy":
        return deploy_plan(
            regions=parse_regions(args.region),
            run_id=args.run_id,
            ttl=args.ttl,
            execute=args.execute,
            image_id=args.image_id,
            instance_type=args.instance_type,
            preserve_instance=args.preserve_instance,
        )

    if args.command == "capture-deploy":
        return capture_deploy_plan(
            regions=parse_regions(args.region),
            run_id=args.run_id,
            ttl=args.ttl,
            execute=args.execute,
            source_image=args.source_image,
            instance_type=args.instance_type,
            preserve_instance=args.preserve_instance,
        )

    if args.command == "cleanup":
        return cleanup_plan(
            run_id=args.run_id,
            ttl=args.ttl,
            execute=args.execute,
        )

    raise ValueError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        resolve_config_defaults(args)
        manifest = command_manifest(args)
    except CaptureError as exc:
        if exc.manifest is not None:
            sys.stdout.write(serialize_manifest(exc.manifest))
            sys.stderr.write("capture --execute failed\n")
            return 1
        parser.error(str(exc))
    except DeployError as exc:
        if exc.manifest is not None:
            sys.stdout.write(serialize_manifest(exc.manifest))
            sys.stderr.write("deploy --execute failed\n")
            return 1
        parser.error(str(exc))
    except CaptureDeployError as exc:
        if exc.manifest is not None:
            sys.stdout.write(serialize_manifest(exc.manifest))
            sys.stderr.write("capture-deploy --execute failed\n")
            return 1
        parser.error(str(exc))
    except CleanupError as exc:
        if exc.manifest is not None:
            sys.stdout.write(serialize_manifest(exc.manifest))
            sys.stderr.write(f"{exc}\n")
            return 1
        parser.error(str(exc))
    except (ConfigError, ValueError) as exc:
        parser.error(str(exc))
    sys.stdout.write(serialize_manifest(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
