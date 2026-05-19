"""Command line interface for Linode Image Lab."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
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
    load_authorized_keys_file,
    load_user_data,
    normalize_firewall_id,
    normalize_authorized_key,
)
from .deploy import DeployError, deploy_plan
from .firewall_sync import FirewallSyncError, firewall_sync_plan
from .manifest import PROJECT, create_manifest, serialize_manifest, validate_run_id
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
    parser.add_argument("--run-id", type=run_id_value, help="Optional run id for deterministic planning.")
    parser.add_argument("--ttl", help="Optional ISO-8601 TTL timestamp.")


def add_firewall_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--firewall-id",
        type=positive_firewall_id,
        help="Existing Linode Cloud Firewall id to assign to deploy instances.",
    )


def add_authorized_keys_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--authorized-key",
        action="append",
        type=authorized_key_value,
        help="Public SSH key to append to root's authorized_keys on deploy instances. May be repeated.",
    )
    parser.add_argument(
        "--authorized-keys-file",
        help="Explicit file containing public SSH keys for deploy instances, one key per line.",
    )


def add_user_data_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--user-data-file",
        help="Explicit file containing deploy user data to send as Linode metadata.user_data.",
    )


def add_registry_firewall_sync_args(parser: argparse.ArgumentParser) -> None:
    add_firewall_arg(parser)
    parser.add_argument("--registry-endpoint-url", help="Linode Object Storage endpoint URL.")
    parser.add_argument("--registry-bucket", help="Object Storage bucket containing the trusted registry.")
    parser.add_argument("--registry-object-key", help="Object Storage object key for the trusted registry JSON.")
    parser.add_argument("--registry-region", help="Optional Object Storage signing region.")
    parser.add_argument(
        "--protocol",
        help="Managed inbound firewall rule protocol. Defaults to TCP.",
    )
    parser.add_argument("--ports", help="Managed inbound firewall rule ports for TCP or UDP.")
    parser.add_argument(
        "--managed-label",
        help="Exact Linode firewall rule label owned by this sync.",
    )


def add_manifest_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest-file",
        help="Optional path for an atomic copy of the redacted JSON manifest. Use '-' for stdout only.",
    )


def positive_firewall_id(value: str) -> int:
    try:
        return normalize_firewall_id(value, "--firewall-id")
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def authorized_key_value(value: str) -> str:
    try:
        return normalize_authorized_key(value, "--authorized-key")
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def run_id_value(value: str) -> str:
    try:
        return validate_run_id(value, "--run-id")
    except ValueError as exc:
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
    add_authorized_keys_args(config_validate)
    add_user_data_arg(config_validate)

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
    add_manifest_file_arg(capture)
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
    add_manifest_file_arg(deploy)
    deploy.add_argument("--image-id", help="Custom image id for the temporary deploy Linode.")
    deploy.add_argument("--type", dest="instance_type", help="Linode type for the temporary deploy Linode.")
    add_firewall_arg(deploy)
    add_authorized_keys_args(deploy)
    add_user_data_arg(deploy)
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
    add_manifest_file_arg(capture_deploy)
    capture_deploy.add_argument("--source-image", help="Source image id for the temporary capture Linode.")
    capture_deploy.add_argument(
        "--type",
        dest="instance_type",
        help="Linode type for the temporary capture and deploy Linodes.",
    )
    add_firewall_arg(capture_deploy)
    add_authorized_keys_args(capture_deploy)
    add_user_data_arg(capture_deploy)
    capture_deploy.add_argument(
        "--preserve-instance",
        action="store_true",
        help="Keep the temporary deploy validation Linode after execution.",
    )

    cleanup = subparsers.add_parser("cleanup", help="Plan, discover, or execute tag-scoped cleanup.")
    add_version_arg(cleanup, version_text)
    add_config_arg(cleanup, dest="command_config")
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument("--discover", action="store_true", help="Opt into read-only Linode resource discovery.")
    cleanup_mode.add_argument("--execute", action="store_true", help="Opt into Linode API deletion of expired resources.")
    add_manifest_file_arg(cleanup)
    cleanup.add_argument("--run-id", type=run_id_value, help="Optional run id filter for cleanup selection.")
    cleanup.add_argument("--ttl", help="Optional ISO-8601 TTL timestamp.")

    firewall_sync = subparsers.add_parser(
        "firewall-sync",
        help="Sync a managed Linode firewall allowlist rule from a trusted registry.",
    )
    add_version_arg(firewall_sync, version_text)
    add_config_arg(firewall_sync, dest="command_config")
    firewall_sync.add_argument("--execute", action="store_true", help="Opt into Linode firewall rule mutation.")
    add_manifest_file_arg(firewall_sync)
    add_registry_firewall_sync_args(firewall_sync)

    return parser


def resolve_config_defaults(args: argparse.Namespace) -> None:
    config = load_config(config_path(args))
    defaults = command_defaults(config, args.command)

    if args.command in {"plan", "capture", "deploy", "capture-deploy"}:
        if args.region is None:
            args.region = config_regions(defaults)
        if not parse_regions(args.region):
            raise ValueError("at least one non-empty --region is required")

    if hasattr(args, "ttl") and args.ttl is None and "ttl" in defaults:
        args.ttl = defaults["ttl"]

    if args.command in {"capture", "capture-deploy"}:
        if args.source_image is None and "source_image" in defaults:
            args.source_image = defaults["source_image"]
        if args.instance_type is None and ("type" in defaults or "instance_type" in defaults):
            args.instance_type = config_instance_type(defaults)
        if getattr(args, "image_project_tag", None) is None and "image_project_tag" in defaults:
            args.image_project_tag = defaults["image_project_tag"]

    if args.command == "deploy":
        if args.image_id is None and "image_id" in defaults:
            args.image_id = defaults["image_id"]
        if args.instance_type is None and ("type" in defaults or "instance_type" in defaults):
            args.instance_type = config_instance_type(defaults)
        if args.firewall_id is None and "firewall_id" in defaults:
            args.firewall_id = defaults["firewall_id"]
        args.authorized_keys = merged_authorized_keys(defaults, args)
        args.user_data = resolved_user_data(defaults, args)

    if args.command == "capture-deploy":
        if args.firewall_id is None and "firewall_id" in defaults:
            args.firewall_id = defaults["firewall_id"]
        args.authorized_keys = merged_authorized_keys(defaults, args)
        args.user_data = resolved_user_data(defaults, args)

    if args.command == "firewall-sync":
        for field in (
            "firewall_id",
            "registry_endpoint_url",
            "registry_bucket",
            "registry_object_key",
            "registry_region",
            "protocol",
            "ports",
            "managed_label",
        ):
            if getattr(args, field) is None and field in defaults:
                setattr(args, field, defaults[field])
        missing = [
            option
            for field, option in (
                ("firewall_id", "--firewall-id"),
                ("registry_endpoint_url", "--registry-endpoint-url"),
                ("registry_bucket", "--registry-bucket"),
                ("registry_object_key", "--registry-object-key"),
            )
            if getattr(args, field) is None
        ]
        if missing:
            raise ValueError(f"firewall-sync requires {', '.join(missing)}")


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
        "user_data": args.user_data_file,
        "registry_endpoint_url": getattr(args, "registry_endpoint_url", None),
        "registry_bucket": getattr(args, "registry_bucket", None),
        "registry_object_key": getattr(args, "registry_object_key", None),
        "registry_region": getattr(args, "registry_region", None),
        "protocol": getattr(args, "protocol", None),
        "ports": getattr(args, "ports", None),
        "managed_label": getattr(args, "managed_label", None),
    }
    option_names = {
        "regions": "--region",
        "ttl": "--ttl",
        "source_image": "--source-image",
        "image_id": "--image-id",
        "type": "--type",
        "firewall_id": "--firewall-id",
        "user_data": "--user-data-file",
    }

    for field, value in candidate_values.items():
        if value is None:
            continue
        if field not in allowed_fields:
            raise ValueError(f"{option_names[field]} is not supported for {target_command} config defaults")
        values[field] = value

    if args.authorized_key is not None or args.authorized_keys_file is not None:
        if "authorized_keys" not in allowed_fields:
            option = "--authorized-key" if args.authorized_key is not None else "--authorized-keys-file"
            raise ValueError(f"{option} is not supported for {target_command} config defaults")
        values["authorized_keys"] = {
            "keys": args.authorized_key or [],
            "file": args.authorized_keys_file,
        }

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


def merged_authorized_keys(defaults: dict[str, Any], args: argparse.Namespace) -> list[str] | None:
    keys = list(defaults.get("authorized_keys", []))
    if args.authorized_key is not None:
        keys.extend(args.authorized_key)
    if args.authorized_keys_file is not None:
        keys.extend(load_authorized_keys_file(args.authorized_keys_file, "--authorized-keys-file"))

    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped or None


def resolved_user_data(defaults: dict[str, Any], args: argparse.Namespace) -> Any:
    if args.user_data_file is not None:
        return load_user_data(args.user_data_file, "--user-data-file")
    return defaults.get("user_data")


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
            image_project_tag=getattr(args, "image_project_tag", None),
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
            authorized_keys=args.authorized_keys,
            user_data=args.user_data,
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
            image_project_tag=getattr(args, "image_project_tag", None),
            firewall_id=args.firewall_id,
            authorized_keys=args.authorized_keys,
            user_data=args.user_data,
            preserve_instance=args.preserve_instance,
        )

    if args.command == "cleanup":
        return cleanup_plan(
            run_id=args.run_id,
            ttl=args.ttl,
            discover=args.discover,
            execute=args.execute,
        )

    if args.command == "firewall-sync":
        return firewall_sync_plan(
            firewall_id=args.firewall_id,
            registry_endpoint_url=args.registry_endpoint_url,
            registry_bucket=args.registry_bucket,
            registry_object_key=args.registry_object_key,
            registry_region=args.registry_region,
            protocol=args.protocol or "TCP",
            ports=args.ports,
            managed_label=args.managed_label or "tnr-allowlist",
            execute=args.execute,
            plan_reporter=sys.stderr.write,
        )

    raise ValueError(f"unsupported command: {args.command}")


def manifest_file_path(args: argparse.Namespace) -> Path | None:
    value = getattr(args, "manifest_file", None)
    if value in (None, "-"):
        return None
    return Path(value)


def preflight_manifest_file(args: argparse.Namespace) -> None:
    path = manifest_file_path(args)
    if path is None:
        return

    parent = path.parent if path.parent != Path("") else Path(".")
    if not parent.exists():
        raise ValueError(f"--manifest-file parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise ValueError(f"--manifest-file parent path is not a directory: {parent}")
    if path.exists() and path.is_dir():
        raise ValueError(f"--manifest-file path is a directory: {path}")

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write("")
            temp_file.flush()
            os.fsync(temp_file.fileno())
    except OSError as exc:
        raise ValueError(f"--manifest-file path is not writable: {path}") from exc
    finally:
        if temp_path is not None:
            try:
                Path(temp_path).unlink()
            except FileNotFoundError:
                pass


def write_manifest_file(args: argparse.Namespace, serialized: str) -> None:
    path = manifest_file_path(args)
    if path is None:
        return

    parent = path.parent if path.parent != Path("") else Path(".")
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(serialized)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                Path(temp_path).unlink()
            except FileNotFoundError:
                pass


def emit_manifest(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    serialized = serialize_manifest(manifest)
    write_manifest_file(args, serialized)
    sys.stdout.write(serialized)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        preflight_manifest_file(args)
        if args.command == "config":
            manifest = config_validate_manifest(args)
        else:
            resolve_config_defaults(args)
            manifest = command_manifest(args)
    except CaptureError as exc:
        if exc.manifest is not None:
            emit_manifest(args, exc.manifest)
            sys.stderr.write("capture --execute failed\n")
            return 1
        parser.error(str(exc))
    except DeployError as exc:
        if exc.manifest is not None:
            emit_manifest(args, exc.manifest)
            sys.stderr.write("deploy --execute failed\n")
            return 1
        parser.error(str(exc))
    except CaptureDeployError as exc:
        if exc.manifest is not None:
            emit_manifest(args, exc.manifest)
            sys.stderr.write("capture-deploy --execute failed\n")
            return 1
        parser.error(str(exc))
    except CleanupError as exc:
        if exc.manifest is not None:
            emit_manifest(args, exc.manifest)
            sys.stderr.write(f"{exc}\n")
            return 1
        parser.error(str(exc))
    except FirewallSyncError as exc:
        if exc.manifest is not None:
            emit_manifest(args, exc.manifest)
            sys.stderr.write(f"{exc}\n")
            return 1
        parser.error(str(exc))
    except (ConfigError, ValueError) as exc:
        parser.error(str(exc))
    emit_manifest(args, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
