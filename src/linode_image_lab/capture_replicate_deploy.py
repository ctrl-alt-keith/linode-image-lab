"""Capture, explicitly replicate, then deploy a custom image."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .capture import CaptureError, CaptureOptions, execute_capture
from .capture_deploy import (
    aggregate_status,
    cleanup_deferred_capture,
    cleanup_status,
    cleanup_summary,
    execute_region_deploys,
)
from .linode_api import LinodeApiError, LinodeClient, LinodeClientProtocol
from .manifest import create_manifest, generate_tags
from .replicate import (
    ReplicateError,
    existing_region_ids,
    image_region_entries,
    merge_regions,
    provider_response_summary,
    validate_existing_regions_present,
    validate_image_available,
)
from .user_data import DeployUserData

COMMAND = "capture-replicate-deploy"
MODE = "capture-replicate-deploy"


class CaptureReplicateDeployError(ValueError):
    """Raised when capture-replicate-deploy cannot safely complete."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class CaptureReplicateDeployOptions:
    regions: list[str]
    run_id: str | None = None
    ttl: str | None = None
    execute: bool = False
    source_image: str | None = None
    instance_type: str | None = None
    image_project_tag: str | None = None
    firewall_id: int | None = None
    authorized_keys: list[str] | None = None
    user_data: DeployUserData | None = None
    preserve_instance: bool = False


def capture_replicate_deploy_plan(
    *,
    regions: list[str],
    run_id: str | None = None,
    ttl: str | None = None,
    execute: bool = False,
    source_image: str | None = None,
    instance_type: str | None = None,
    image_project_tag: str | None = None,
    firewall_id: int | None = None,
    authorized_keys: list[str] | None = None,
    user_data: DeployUserData | None = None,
    preserve_instance: bool = False,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    options = CaptureReplicateDeployOptions(
        regions=regions,
        run_id=run_id,
        ttl=ttl,
        execute=execute,
        source_image=source_image,
        instance_type=instance_type,
        image_project_tag=image_project_tag,
        firewall_id=firewall_id,
        authorized_keys=authorized_keys,
        user_data=user_data,
        preserve_instance=preserve_instance,
    )
    if not execute:
        return dry_run_manifest(options)
    return execute_capture_replicate_deploy(options, client=client)


def dry_run_manifest(options: CaptureReplicateDeployOptions) -> dict[str, Any]:
    validate_regions(options)
    manifest = base_manifest(options, dry_run=True, status="planned")
    manifest["execution_mode"] = "dry-run"
    manifest["message"] = "capture-replicate-deploy is non-mutating unless --execute is provided"
    manifest["provider_calls"] = "not_attempted"
    manifest["capture_plan"] = {
        "capture_region": options.regions[0],
        "source_image": options.source_image,
        "instance_type": options.instance_type,
    }
    manifest["replication_plan"] = {
        "requested_regions": list(options.regions),
        "replication_target_regions": list(options.regions),
        "provider_request": "execute mode submits existing image regions plus requested deploy regions",
        "replica_status_check": "execute mode waits for requested region replicas to report available",
    }
    manifest["deploy_plan"] = {"deploy_regions": list(options.regions)}
    manifest["cleanup_expectations"] = {
        "capture_source": "temporary Linode deleted after capture or failure when tags match",
        "deploy_instances": "temporary validation Linodes deleted after each deploy unless preserved",
        "custom_image": "captured image is preserved as the workflow deliverable",
    }
    attach_deploy_config(manifest, options)
    return manifest


def execute_capture_replicate_deploy(
    options: CaptureReplicateDeployOptions,
    *,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    validate_execute_options(options)
    manifest = base_manifest(options, dry_run=False, status="running")
    manifest["execution_mode"] = "execute"
    manifest["steps"] = []
    manifest["capture"] = {}
    manifest["replication"] = empty_replication_block(options)
    manifest["deploy_results"] = {}
    manifest["validation"] = {"status": "not_started", "capture": {}, "replication": {}, "deploy": {}}
    manifest["cleanup"] = {"status": "not_started", "capture": {}, "deploy": {}}
    attach_deploy_config(manifest, options)

    run_client = client or LinodeClient.from_env(command=COMMAND)
    capture_manifest: dict[str, Any] | None = None

    try:
        append_step(manifest, "run_capture_phase", mutates=True, status="running")
        capture_manifest = execute_capture(
            CaptureOptions(
                regions=[options.regions[0]],
                run_id=manifest["run_id"],
                ttl=manifest["ttl"],
                execute=True,
                source_image=required_text(options.source_image),
                instance_type=required_text(options.instance_type),
                image_project_tag=options.image_project_tag,
                preserve_source=False,
                command=COMMAND,
                mode=MODE,
                component="capture",
                defer_cleanup=True,
                label_suffix=options.regions[0],
            ),
            client=run_client,
        )
        manifest["capture"] = capture_manifest
        manifest["validation"]["capture"] = capture_manifest.get("validation", {})
        finish_step(manifest, "run_capture_phase")

        image_id = required_text(capture_manifest.get("custom_image", {}).get("image_id"))
        run_replication_phase(manifest, run_client, image_id=image_id, deploy_regions=options.regions)

        deploy_manifests = execute_region_deploys(
            regions=manifest["summary"]["deploy_regions"],
            run_id=manifest["run_id"],
            ttl=manifest["ttl"],
            image_id=image_id,
            instance_type=required_text(options.instance_type),
            firewall_id=options.firewall_id,
            authorized_keys=options.authorized_keys,
            user_data=options.user_data,
            preserve_instance=options.preserve_instance,
            client=client,
            command=COMMAND,
            mode=MODE,
        )
        for region in manifest["summary"]["deploy_regions"]:
            deploy_manifest = deploy_manifests[region]
            manifest["deploy_results"][region] = deploy_manifest
            manifest["validation"]["deploy"][region] = deploy_manifest.get("validation", {})
            if deploy_manifest.get("status") == "succeeded":
                manifest["summary"]["succeeded"].append(region)
            else:
                manifest["summary"]["failed"].append(region)

        cleanup_deferred_capture(run_client, capture_manifest)
        manifest["capture"] = capture_manifest
        finish_workflow_manifest(manifest, capture_manifest=capture_manifest)
        if manifest["status"] != "succeeded":
            raise CaptureReplicateDeployError(
                "capture-replicate-deploy --execute failed for one or more regions",
                manifest,
            )
        return manifest
    except Exception as exc:
        mark_running_step_failed(manifest)
        if isinstance(exc, CaptureError) and exc.manifest is not None:
            capture_manifest = exc.manifest
            manifest["capture"] = capture_manifest
        if capture_manifest is not None and cleanup_status(capture_manifest) == "deferred":
            cleanup_deferred_capture(run_client, capture_manifest)
            manifest["capture"] = capture_manifest
        finish_workflow_manifest(manifest, capture_manifest=capture_manifest)
        manifest["status"] = "failed" if not manifest["summary"]["succeeded"] else "partial"
        if not manifest.get("errors"):
            manifest["errors"] = [safe_error_message(exc)]
        if isinstance(exc, CaptureReplicateDeployError) and exc.manifest is manifest:
            raise
        raise CaptureReplicateDeployError("capture-replicate-deploy --execute failed", manifest) from exc


def run_replication_phase(
    manifest: dict[str, Any],
    client: LinodeClientProtocol,
    *,
    image_id: str,
    deploy_regions: list[str],
) -> None:
    manifest["replication"]["source"] = {"image_id": image_id}

    append_step(manifest, "preflight_replication_inputs", mutates=False, status="running")
    image_details = client.get_image_details(image_id)
    validate_image_available(image_details)
    validate_existing_regions_present(image_details)
    for region in deploy_regions:
        client.preflight_region(region)
    existing_regions = image_region_entries(image_details)
    submitted_regions = merge_regions(existing_region_ids(image_details), deploy_regions)
    manifest["replication"]["source"]["existing_regions"] = existing_regions
    manifest["replication"]["request"] = {
        "image_id": image_id,
        "requested_regions": list(deploy_regions),
        "submitted_regions": submitted_regions,
    }
    finish_step(manifest, "preflight_replication_inputs")

    append_step(manifest, "submit_image_replication", mutates=True, status="running")
    try:
        replication_result = client.replicate_image(image_id=image_id, regions=submitted_regions)
    except LinodeApiError as exc:
        provider_error = exc.provider_error_details()
        if provider_error is not None:
            manifest["replication"]["provider_error"] = provider_error
            manifest["provider_error"] = provider_error
        manifest["replication"]["status"] = "failed"
        manifest["validation"]["replication"] = {"status": "failed"}
        raise
    manifest["replication"]["result"] = replication_result
    manifest["replication"]["provider_response_summary"] = provider_response_summary(replication_result)
    manifest["replication"]["status"] = "submitted"
    finish_step(manifest, "submit_image_replication")

    append_step(manifest, "wait_replica_regions_available", mutates=False, status="running")
    final_details = client.wait_image_regions_available(image_id, deploy_regions)
    manifest["replication"]["replica_status_checks"] = {
        "status": "succeeded",
        "checked_regions": list(deploy_regions),
        "final_image_status": final_details.get("status"),
        "regions": image_region_entries(final_details),
    }
    manifest["replication"]["status"] = "available"
    manifest["validation"]["replication"] = {"status": "succeeded"}
    finish_step(manifest, "wait_replica_regions_available")


def base_manifest(options: CaptureReplicateDeployOptions, *, dry_run: bool, status: str) -> dict[str, Any]:
    base = create_manifest(
        command=COMMAND,
        mode=MODE,
        component="capture",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        image_project_tag=options.image_project_tag,
        dry_run=dry_run,
        status=status,
    )
    component_tags = component_lifecycle_tags(run_id=base["run_id"], ttl=base["ttl"])
    base["component_tags"] = component_tags
    base["planned_actions"] = [
        planned_action("capture", options.regions[0], "capture", component_tags["capture"], mutates=not dry_run),
        planned_action("replicate", "all", "replicate", component_tags["replicate"], mutates=not dry_run),
        *[
            planned_action("deploy", region, "deploy", component_tags["deploy"], mutates=not dry_run)
            for region in options.regions
        ],
        planned_action("cleanup", "temporary-resources", "capture", component_tags["capture"], mutates=not dry_run),
    ]
    base["summary"] = {
        "capture_region": options.regions[0],
        "deploy_regions": list(options.regions),
        "replication_target_regions": list(options.regions),
        "succeeded": [],
        "failed": [],
    }
    return base


def component_lifecycle_tags(*, run_id: str, ttl: str) -> dict[str, list[str]]:
    return {
        "capture": generate_tags(run_id=run_id, mode=MODE, component="capture", ttl=ttl),
        "replicate": generate_tags(run_id=run_id, mode=MODE, component="replicate", ttl=ttl),
        "deploy": generate_tags(run_id=run_id, mode=MODE, component="deploy", ttl=ttl),
    }


def planned_action(
    action: str,
    region: str,
    component: str,
    tags: list[str],
    *,
    mutates: bool,
) -> dict[str, Any]:
    return {
        "action": action,
        "region": region,
        "component": component,
        "mutates": mutates,
        "tags": tags,
        "lifecycle_tags": tags,
    }


def empty_replication_block(options: CaptureReplicateDeployOptions) -> dict[str, Any]:
    return {
        "status": "not_started",
        "source": {},
        "request": {
            "requested_regions": list(options.regions),
            "submitted_regions": [],
        },
        "result": {},
        "provider_response_summary": {},
        "replica_status_checks": {"status": "not_started"},
    }


def finish_workflow_manifest(
    manifest: dict[str, Any],
    *,
    capture_manifest: dict[str, Any] | None,
) -> None:
    cleanup_failed = capture_manifest is not None and cleanup_status(capture_manifest) == "failed"
    if capture_manifest is not None:
        manifest["validation"]["capture"] = capture_manifest.get("validation", {})
        manifest["cleanup"]["capture"] = capture_manifest.get("cleanup", {})
    manifest["cleanup"]["deploy"] = {
        region: deploy_manifest.get("cleanup", {})
        for region, deploy_manifest in manifest.get("deploy_results", {}).items()
    }
    manifest["cleanup"]["summary"] = cleanup_summary(capture_manifest) if capture_manifest is not None else {}
    manifest["cleanup"]["status"] = aggregate_cleanup_status(manifest["cleanup"])
    manifest["validation"]["status"] = aggregate_validation_status(manifest["validation"])
    manifest["status"] = aggregate_status(
        succeeded=manifest["summary"]["succeeded"],
        failed=manifest["summary"]["failed"],
        cleanup_failed=cleanup_failed,
    )


def aggregate_cleanup_status(cleanup: dict[str, Any]) -> str:
    statuses = []
    capture = cleanup.get("capture", {})
    if isinstance(capture, dict) and capture.get("status"):
        statuses.append(str(capture["status"]))
    deploy = cleanup.get("deploy", {})
    if isinstance(deploy, dict):
        for value in deploy.values():
            if isinstance(value, dict) and value.get("status"):
                statuses.append(str(value["status"]))
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "deferred" for status in statuses):
        return "deferred"
    if statuses:
        return "completed"
    return "not_started"


def aggregate_validation_status(validation: dict[str, Any]) -> str:
    statuses: list[str] = []
    for key, value in validation.items():
        if key == "status":
            continue
        if isinstance(value, dict) and isinstance(value.get("status"), str):
            statuses.append(value["status"])
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, dict) and isinstance(nested.get("status"), str):
                    statuses.append(nested["status"])
    if any(status == "failed" for status in statuses):
        return "failed"
    if statuses and all(status == "succeeded" for status in statuses):
        return "succeeded"
    if statuses:
        return "partial"
    return "not_started"


def attach_deploy_config(manifest: dict[str, Any], options: CaptureReplicateDeployOptions) -> None:
    deploy_config: dict[str, Any] = {}
    if options.firewall_id is not None:
        deploy_config["firewall"] = {"enabled": True, "firewall_id": options.firewall_id}
    if options.authorized_keys:
        deploy_config["authorized_keys"] = {"enabled": True, "authorized_key_count": len(options.authorized_keys)}
    if options.user_data is not None:
        deploy_config["user_data"] = {
            "enabled": True,
            "source": options.user_data.source,
            "byte_count": options.user_data.byte_count,
        }
    if deploy_config:
        manifest["deploy_config"] = deploy_config


def validate_regions(options: CaptureReplicateDeployOptions) -> None:
    if not options.regions:
        raise CaptureReplicateDeployError("capture-replicate-deploy requires at least one non-empty --region")


def validate_execute_options(options: CaptureReplicateDeployOptions) -> None:
    validate_regions(options)
    if not options.source_image:
        raise CaptureReplicateDeployError(
            "capture-replicate-deploy --execute requires --source-image for the temporary capture Linode"
        )
    if not options.instance_type:
        raise CaptureReplicateDeployError(
            "capture-replicate-deploy --execute requires --type for temporary capture and deploy Linodes"
        )


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


def required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CaptureReplicateDeployError("required capture-replicate-deploy value is missing")
    return value


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, CaptureError) and exc.manifest is not None:
        errors = exc.manifest.get("errors", [])
        if errors:
            return str(errors[0])
    if isinstance(exc, (CaptureReplicateDeployError, CaptureError, ReplicateError, LinodeApiError)):
        return str(exc)
    return exc.__class__.__name__
