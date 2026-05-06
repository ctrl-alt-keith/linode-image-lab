"""Command line interface for Linode Image Lab."""

from __future__ import annotations

import argparse
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .cleanup import CleanupError, cleanup_plan
from .capture import CaptureError, capture_plan
from .capture_deploy import CaptureDeployError, capture_deploy_plan
from .config import (
    COMMAND_DEFAULT_FIELDS,
    ConfigError,
    command_defaults,
    effective_command_defaults,
    load_config,
    normalize_firewall_id,
)
from .deploy import DeployError, deploy_plan
from .manifest import PROJECT, create_manifest, serialize_manifest
from .regions import parse_regions

PACKAGE_NAME = "linode-image-lab"


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        return str(pyproject["project"]["version"])


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


def add_firewall_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--firewall-id",
        type=positive_firewall_id,
        help="Existing Linode Cloud Firewall id to assign to deploy instances.",
    )


def positive_firewall_id(value: str) -> int:
    try:
        return normalize_firewall_id(value, "--firewall-id")
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_version_arg(parser: argparse.ArgumentParser, version_text: str) -> None:
    parser.add_argument(
        "--version",
        action="version",
        version=version_text,
        help="Print the installed package version and exit.",
    )


def build_parser() -> argparse.ArgumentParser:
    version_text = package_version()
    parser = argparse.ArgumentParser(prog="linode-image-lab")
    add_version_arg(parser, version_text)
    add_config_arg(parser, dest="global_config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser("config", help="Validate and inspect config defaults.")
    config_subparsers = config.add_subparsers(dest="config_action", required=True)
    config_validate = config_subparsers.add_parser(
        "validate",
        help="Validate a config file and show effective command defaults.",
    )
    add_config_arg(config_validate, dest="command_config")
    config_validate.add_argument(
        "--command",
        dest="target_command",
        choices=tuple(COMMAND_DEFAULT_FIELDS),
        required=True,
        help="Command whose effective defaults should be resolved.",
    )
    config_validate.add_argument(
        "--region",
        action="append",
        help="Optional CLI region override to include in effective-defaults resolution.",
    )
    config_validate.add_argument("--ttl", help="Optional CLI TTL override to include in resolution.")
    config_validate.add_argument(
        "--source-image",
        help="Optional CLI source image override to include in resolution.",
    )
    config_validate.add_argument("--image-id", help="Optional CLI image id override to include in resolution.")
    config_validate.add_argument(
        "--type",
        dest="instance_type",
        help="Optional CLI Linode type override to include in resolution.",
    )
    add_firewall_arg(config_validate)

    plan = subparsers.add_parser("plan", help="Emit a dry-run manifest preview.")
    add_version_arg(plan, version_text)
    add_config_arg(plan, dest="command_config")
    add_region_args(plan, required=True)
    plan.add_argument(
        "--mode",
        choices=("capture", "deploy", "capture-deploy"),
        default="capture-deploy",
        help="Workflow mode to model.",
    )

    capture = subparsers.add_parser("capture", help="Plan or execute a single-region capture.")
    add_version_arg(capture, version_text)
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
    add_version_arg(deploy, version_text)
    add_config_arg(deploy, dest="command_config")
    add_region_args(deploy, required=True)
    deploy.add_argument("--execute", action="store_true", help="Opt into Linode API mutations.")
    deploy.add_argument("--image-id", help="Custom image id for the temporary deploy Linode.")
    deploy.add_argument("--type", dest="instance_type", help="Linode type for the temporary deploy Linode.")
    add_firewall_arg(deploy)
    deploy.add_argument(
        "--preserve-instance",
        action="store_true",
        help="Keep the temporary deploy Linode after execution.",
    )

    capture_deploy = subparsers.add_parser("capture-deploy", help="Plan or execute capture plus deploy validation.")
    add_version_arg(capture_deploy, version_text)
    add_config_arg(capture_deploy, dest="command_config")
    add_region_args(capture_deploy, required=True)
    capture_deploy.add_argument("--execute", action="store_true", help="Opt into Linode API mutations.")
    capture_deploy.add_argument("--source-image", help="Source image id for the temporary capture Linode.")
    capture_deploy.add_argument(
        "--type",
        dest="instance_type",
        help="Linode type for the temporary capture and deploy Linodes.",
    )
    add_firewall_arg(capture_deploy)
    capture_deploy.add_argument(
        "--preserve-instance",
        action="store_true",
        help="Keep the temporary deploy validation Linode after execution.",
    )

    cleanup = subparsers.add_parser("cleanup", help="Plan, discover, or execute tag-scoped cleanup.")
    add_version_arg(cleanup, version_text)
    add_config_arg(cleanup, dest="command_config")
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument("--discover", action="store_true", help="Opt into read-only Linode discovery.")
    cleanup_mode.add_argument("--execute", action="store_true", help="Opt into Linode API deletion of expired resources.")
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
        if args.instance_type is None and ("type" in defaults or "instance_type" in defaults):
            args.instance_type = config_instance_type(defaults)

    if args.command == "deploy":
        if args.image_id is None and "image_id" in defaults:
            args.image_id = defaults["image_id"]
        if args.instance_type is None and ("type" in defaults or "instance_type" in defaults):
            args.instance_type = config_instance_type(defaults)
        if args.firewall_id is None and "firewall_id" in defaults:
            args.firewall_id = defaults["firewall_id"]

    if args.command == "capture-deploy":
        if args.firewall_id is None and "firewall_id" in defaults:
            args.firewall_id = defaults["firewall_id"]


def config_validate_manifest(args: argparse.Namespace) -> dict[str, Any]:
    path = config_path(args)
    if path is None:
        raise ValueError("config validate requires --config PATH")

    config = load_config(path)
    target_command = args.target_command
    cli_defaults = config_validation_cli_defaults(args)
    resolution = effective_command_defaults(config, target_command, cli_defaults=cli_defaults)

    return {
        "schema_version": 1,
        "project": PROJECT,
        "command": "config",
        "action": "validate",
        "target_command": target_command,
        "status": "valid",
        "valid": True,
        "dry_run": True,
        "safety": {
            "mutates": False,
            "auth_lookup": "not_attempted",
        },
        **resolution,
    }


def config_validation_cli_defaults(args: argparse.Namespace) -> dict[str, Any]:
    target_command = args.target_command
    allowed_fields = set(COMMAND_DEFAULT_FIELDS[target_command])
    values: dict[str, Any] = {}

    candidate_values = {
        "regions": args.region,
        "ttl": args.ttl,
        "source_image": args.source_image,
        "image_id": args.image_id,
        "type": args.instance_type,
        "firewall_id": args.firewall_id,
    }
    option_names = {
        "regions": "--region",
        "ttl": "--ttl",
        "source_image": "--source-image",
        "image_id": "--image-id",
        "type": "--type",
        "firewall_id": "--firewall-id",
    }

    for field, value in candidate_values.items():
        if value is None:
            continue
        if field not in allowed_fields:
            raise ValueError(f"{option_names[field]} is not supported for {target_command} config defaults")
        values[field] = value

    return values


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


def config_instance_type(defaults: dict[str, Any]) -> str | None:
    if "instance_type" in defaults:
        return str(defaults["instance_type"])
    if "type" in defaults:
        return str(defaults["type"])
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
            firewall_id=args.firewall_id,
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
            firewall_id=args.firewall_id,
            preserve_instance=args.preserve_instance,
        )

    if args.command == "cleanup":
        return cleanup_plan(
            run_id=args.run_id,
            ttl=args.ttl,
            discover=args.discover,
            execute=args.execute,
        )

    raise ValueError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            manifest = config_validate_manifest(args)
        else:
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
