"""Deploy command planning and execution orchestration."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any

from .linode_api import LinodeClient, LinodeClientProtocol
from .manifest import REQUIRED_TAG_KEYS, create_manifest, tags_to_dict


class DeployError(ValueError):
    """Raised when deploy cannot safely complete."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class DeployOptions:
    regions: list[str]
    run_id: str | None = None
    ttl: str | None = None
    execute: bool = False
    image_id: str | None = None
    instance_type: str | None = None
    preserve_instance: bool = False


def deploy_plan(
    *,
    regions: list[str],
    run_id: str | None = None,
    ttl: str | None = None,
    execute: bool = False,
    image_id: str | None = None,
    instance_type: str | None = None,
    preserve_instance: bool = False,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    options = DeployOptions(
        regions=regions,
        run_id=run_id,
        ttl=ttl,
        execute=execute,
        image_id=image_id,
        instance_type=instance_type,
        preserve_instance=preserve_instance,
    )
    if not execute:
        return dry_run_manifest(options)
    return execute_deploy(options, client=client)


def dry_run_manifest(options: DeployOptions) -> dict[str, Any]:
    manifest = create_manifest(
        command="deploy",
        mode="deploy",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=True,
        status="planned",
    )
    manifest["execution_mode"] = "dry-run"
    manifest["message"] = "deploy is non-mutating unless --execute is provided"
    return manifest


def execute_deploy(
    options: DeployOptions,
    *,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    validate_execute_options(options)
    manifest = create_manifest(
        command="deploy",
        mode="deploy",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=False,
        status="running",
    )
    manifest["execution_mode"] = "execute"
    manifest["steps"] = []
    manifest["resources"] = []
    manifest["deploy_source"] = {"image_id": required_text(options.image_id)}
    manifest["deploy_instance"] = {}
    manifest["validation"] = {"status": "not_started", "checks": []}
    manifest["cleanup"] = {"status": "not_started", "deleted": [], "preserved": []}
    for action in manifest["planned_actions"]:
        action["mutates"] = True

    run_client = client or LinodeClient.from_env(command="deploy")
    deploy_instance: dict[str, Any] | None = None

    try:
        append_step(manifest, "preflight", mutates=False, status="running")
        run_client.preflight()
        finish_step(manifest, "preflight")

        tags = list(manifest["tags"])
        region = options.regions[0]
        instance_label = f"lil-{safe_label_suffix(manifest['run_id'])}-deploy"

        append_step(manifest, "create_deploy_instance", mutates=True, status="running")
        deploy_instance = run_client.create_instance(
            region=region,
            source_image=required_text(options.image_id),
            instance_type=required_text(options.instance_type),
            label=instance_label,
            tags=tags,
            root_password=secrets.token_urlsafe(32),
        )
        deploy_instance["resource_type"] = "linode"
        manifest["deploy_instance"] = dict(deploy_instance)
        manifest["resources"].append(dict(deploy_instance))
        finish_step(manifest, "create_deploy_instance")

        append_step(manifest, "wait_deploy_instance_ready", mutates=False, status="running")
        deploy_instance = merge_resource(
            deploy_instance,
            run_client.wait_instance_ready(required_int(deploy_instance.get("linode_id"))),
        )
        manifest["deploy_instance"] = dict(deploy_instance)
        manifest["resources"][0] = dict(deploy_instance)
        finish_step(manifest, "wait_deploy_instance_ready")

        append_step(manifest, "validate_deploy_instance", mutates=False, status="running")
        validate_deploy_instance(deploy_instance, required_tags=tags, region=region)
        manifest["validation"] = {
            "status": "succeeded",
            "checks": [
                "instance_running",
                "region_matches",
                "required_tags_match",
            ],
        }
        finish_step(manifest, "validate_deploy_instance")

        cleanup_deploy_instance(
            manifest,
            run_client,
            deploy_instance=deploy_instance,
            preserve_instance=options.preserve_instance,
            required_tags=tags,
        )
        manifest["status"] = "succeeded"
        return manifest
    except Exception as exc:
        mark_running_step_failed(manifest)
        manifest["status"] = "failed"
        manifest["errors"] = [safe_error_message(exc)]
        if deploy_instance is not None and not cleanup_started(manifest):
            try:
                cleanup_deploy_instance(
                    manifest,
                    run_client,
                    deploy_instance=deploy_instance,
                    preserve_instance=options.preserve_instance,
                    required_tags=list(manifest["tags"]),
                )
            except Exception:
                mark_running_step_failed(manifest)
                manifest["cleanup"] = {"status": "failed", "deleted": [], "preserved": []}
        raise DeployError("deploy --execute failed", manifest) from exc


def validate_execute_options(options: DeployOptions) -> None:
    if len(options.regions) != 1:
        raise DeployError("deploy --execute requires exactly one region")
    if not options.image_id:
        raise DeployError("deploy --execute requires --image-id")
    if not options.instance_type:
        raise DeployError("deploy --execute requires --type")


def cleanup_deploy_instance(
    manifest: dict[str, Any],
    client: LinodeClientProtocol,
    *,
    deploy_instance: dict[str, Any],
    preserve_instance: bool,
    required_tags: list[str],
) -> None:
    append_step(manifest, "cleanup_deploy_instance", mutates=not preserve_instance, status="running")
    cleanup = {"status": "not_started", "deleted": [], "preserved": []}
    if preserve_instance:
        cleanup["status"] = "preserved"
        cleanup["preserved"].append({"resource_type": "linode", "linode_id": deploy_instance.get("linode_id")})
    elif has_required_tags(deploy_instance, required_tags):
        client.delete_instance(required_int(deploy_instance.get("linode_id")))
        cleanup["status"] = "deleted"
        cleanup["deleted"].append({"resource_type": "linode", "linode_id": deploy_instance.get("linode_id")})
    else:
        cleanup["status"] = "skipped_tag_mismatch"
        cleanup["preserved"].append({"resource_type": "linode", "linode_id": deploy_instance.get("linode_id")})
    manifest["cleanup"] = cleanup
    finish_step(manifest, "cleanup_deploy_instance")


def append_step(manifest: dict[str, Any], name: str, *, mutates: bool, status: str) -> None:
    manifest["steps"].append({"name": name, "mutates": mutates, "status": status})


def finish_step(manifest: dict[str, Any], name: str) -> None:
    for step in reversed(manifest["steps"]):
        if step["name"] == name:
            step["status"] = "succeeded"
            return


def mark_running_step_failed(manifest: dict[str, Any]) -> None:
    for step in reversed(manifest.get("steps", [])):
        if step.get("status") == "running":
            step["status"] = "failed"
            return


def cleanup_started(manifest: dict[str, Any]) -> bool:
    return any(step.get("name") == "cleanup_deploy_instance" for step in manifest.get("steps", []))


def validate_deploy_instance(
    resource: dict[str, Any],
    *,
    required_tags: list[str],
    region: str,
) -> None:
    if resource.get("region") != region:
        raise DeployError("created deploy instance is not in the requested region")
    if resource.get("status") != "running":
        raise DeployError("created deploy instance is not running")
    if not has_required_tags(resource, required_tags):
        raise DeployError("created resource is missing required deploy tags")


def has_required_tags(resource: dict[str, Any], required_tags: list[str]) -> bool:
    tags = tags_to_dict(resource.get("tags", []))
    expected = tags_to_dict(required_tags)
    return all(key in tags and tags[key] == expected[key] for key in REQUIRED_TAG_KEYS)


def merge_resource(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    merged.update({key: value for key, value in update.items() if value is not None})
    return merged


def required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DeployError("required deploy value is missing")
    return value


def required_int(value: object) -> int:
    if not isinstance(value, int):
        raise DeployError("required provider resource id is missing")
    return value


def safe_label_suffix(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return (normalized or "run")[:32]


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, DeployError):
        return str(exc)
    return exc.__class__.__name__
