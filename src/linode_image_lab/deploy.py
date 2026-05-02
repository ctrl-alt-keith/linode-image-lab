"""Deploy command planning and execution orchestration."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any

from .linode_api import LinodeClient, LinodeClientProtocol, LinodePreflightError
from .manifest import REQUIRED_TAG_KEYS, create_manifest, tags_to_dict
from .validation_results import finish_validation, record_validation_check, start_validation

DEPLOY_VALIDATION_CHECKS = (
    ("instance_running", "deploy_instance"),
    ("region_matches", "deploy_instance"),
    ("required_tags_match", "deploy_instance"),
)


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
    command: str = "deploy"
    mode: str = "deploy"
    component: str = "deploy"
    defer_cleanup: bool = False
    label_suffix: str | None = None


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
        command=options.command,
        mode=options.mode,
        component=options.component,
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
        command=options.command,
        mode=options.mode,
        component=options.component,
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

    run_client = client or LinodeClient.from_env(command=options.command)
    deploy_instance: dict[str, Any] | None = None

    try:
        append_step(manifest, "preflight_api_access", mutates=False, status="running")
        run_client.preflight()
        finish_step(manifest, "preflight_api_access", client=run_client)

        tags = list(manifest["tags"])
        region = options.regions[0]
        image_id = required_text(options.image_id)
        instance_type = required_text(options.instance_type)
        instance_label = resource_label(manifest["run_id"], "deploy", suffix=options.label_suffix)

        append_step(manifest, "preflight_provider_inputs", mutates=False, status="running")
        run_client.preflight_region(region)
        run_client.preflight_instance_type(instance_type)
        run_client.preflight_image(image_id)
        finish_step(manifest, "preflight_provider_inputs", client=run_client)

        append_step(manifest, "create_deploy_instance", mutates=True, status="running")
        deploy_instance = run_client.create_instance(
            region=region,
            source_image=image_id,
            instance_type=instance_type,
            label=instance_label,
            tags=tags,
            root_password=secrets.token_urlsafe(32),
        )
        deploy_instance["resource_type"] = "linode"
        manifest["deploy_instance"] = dict(deploy_instance)
        manifest["resources"].append(dict(deploy_instance))
        finish_step(manifest, "create_deploy_instance", client=run_client)

        append_step(manifest, "wait_deploy_instance_ready", mutates=False, status="running")
        deploy_instance = merge_resource(
            deploy_instance,
            run_client.wait_instance_ready(required_int(deploy_instance.get("linode_id"))),
        )
        manifest["deploy_instance"] = dict(deploy_instance)
        manifest["resources"][0] = dict(deploy_instance)
        finish_step(manifest, "wait_deploy_instance_ready", client=run_client)

        append_step(manifest, "validate_deploy_instance_api", mutates=False, status="running")
        manifest["validation"] = start_validation(DEPLOY_VALIDATION_CHECKS)
        record_validation_check(
            manifest["validation"],
            "region_matches",
            lambda: validate_instance_region(deploy_instance, region),
        )
        record_validation_check(
            manifest["validation"],
            "instance_running",
            lambda: validate_instance_running(deploy_instance),
        )
        record_validation_check(
            manifest["validation"],
            "required_tags_match",
            lambda: validate_required_tags(deploy_instance, required_tags=tags),
        )
        finish_validation(manifest["validation"])
        finish_step(manifest, "validate_deploy_instance_api", client=run_client)

        if options.defer_cleanup:
            manifest["cleanup"] = {"status": "deferred", "deleted": [], "preserved": []}
        else:
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
        mark_running_step_failed(manifest, client=run_client)
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
                mark_running_step_failed(manifest, client=run_client)
                manifest["cleanup"] = {"status": "failed", "deleted": [], "preserved": []}
        if manifest.get("validation", {}).get("status") == "running":
            manifest["validation"]["status"] = "failed"
        raise DeployError("deploy --execute failed", manifest) from exc


def validate_execute_options(options: DeployOptions) -> None:
    if len(options.regions) != 1:
        raise DeployError("deploy --execute requires exactly one non-empty --region")
    if not options.image_id:
        raise DeployError("deploy --execute requires --image-id for the custom image to deploy")
    if not options.instance_type:
        raise DeployError("deploy --execute requires --type for the temporary deploy Linode")


def cleanup_deploy_instance(
    manifest: dict[str, Any],
    client: LinodeClientProtocol,
    *,
    deploy_instance: dict[str, Any],
    preserve_instance: bool,
    required_tags: list[str],
) -> None:
    can_delete = has_required_tags(deploy_instance, required_tags)
    cleanup_action = "delete" if not preserve_instance and can_delete else "preserve"
    append_step(
        manifest,
        "cleanup_deploy_instance",
        mutates=cleanup_action == "delete",
        status="running",
        action=cleanup_action,
    )
    cleanup = {"status": "not_started", "deleted": [], "preserved": []}
    if preserve_instance:
        cleanup["status"] = "preserved"
        cleanup["preserved"].append(
            {"resource_type": "linode", "linode_id": deploy_instance.get("linode_id"), "reason": "requested"}
        )
    elif can_delete:
        client.delete_instance(required_int(deploy_instance.get("linode_id")))
        cleanup["status"] = "deleted"
        cleanup["deleted"].append(
            {"resource_type": "linode", "linode_id": deploy_instance.get("linode_id"), "reason": "tag_match"}
        )
    else:
        cleanup["status"] = "preserved"
        cleanup["preserved"].append(
            {"resource_type": "linode", "linode_id": deploy_instance.get("linode_id"), "reason": "tag_mismatch"}
        )
    manifest["cleanup"] = cleanup
    finish_step(manifest, "cleanup_deploy_instance", client=client)


def append_step(
    manifest: dict[str, Any],
    name: str,
    *,
    mutates: bool,
    status: str,
    action: str | None = None,
) -> None:
    step = {"name": name, "mutates": mutates, "status": status}
    if action is not None:
        step["action"] = action
    manifest["steps"].append(step)


def finish_step(manifest: dict[str, Any], name: str, *, client: object | None = None) -> None:
    for step in reversed(manifest["steps"]):
        if step["name"] == name:
            attach_retry_events(step, client)
            step["status"] = "succeeded"
            return


def mark_running_step_failed(manifest: dict[str, Any], *, client: object | None = None) -> None:
    for step in reversed(manifest.get("steps", [])):
        if step.get("status") == "running":
            attach_retry_events(step, client)
            step["status"] = "failed"
            return


def attach_retry_events(step: dict[str, Any], client: object | None) -> None:
    consume = getattr(client, "consume_retry_events", None)
    if not callable(consume):
        return
    retry_events = consume()
    if retry_events:
        step["api_retries"] = retry_events


def cleanup_started(manifest: dict[str, Any]) -> bool:
    return any(step.get("name") == "cleanup_deploy_instance" for step in manifest.get("steps", []))


def validate_deploy_instance(
    resource: dict[str, Any],
    *,
    required_tags: list[str],
    region: str,
) -> None:
    validate_instance_region(resource, region)
    validate_instance_running(resource)
    validate_required_tags(resource, required_tags=required_tags)


def validate_instance_region(resource: dict[str, Any], region: str) -> None:
    if resource.get("region") != region:
        raise DeployError("created deploy instance is not in the requested region")


def validate_instance_running(resource: dict[str, Any]) -> None:
    if resource.get("status") != "running":
        raise DeployError("created deploy instance is not running")


def validate_required_tags(resource: dict[str, Any], *, required_tags: list[str]) -> None:
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


def resource_label(run_id: str, resource: str, *, suffix: str | None = None) -> str:
    parts = ["lil", safe_label_suffix(run_id)]
    if suffix:
        parts.append(safe_label_suffix(suffix))
    parts.append(resource)
    return "-".join(parts)


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, (DeployError, LinodePreflightError)):
        return str(exc)
    return exc.__class__.__name__
