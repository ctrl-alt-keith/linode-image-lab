"""Capture, explicitly replicate, then deploy a custom image."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
from .region_policy import (
    DEFAULT_REGION_POLICY_PATH,
    ResolvedRegionPolicyGroups,
    resolve_region_policy_groups,
)
from .replicate import (
    ReplicateError,
    ReplicationRegionCapabilityError,
    existing_region_ids,
    image_region_entries,
    merge_regions,
    provider_response_summary,
    validate_existing_regions_present,
    validate_image_available,
    validate_replication_region_capabilities,
    unique_region_ids,
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
    explicit_deploy_regions: list[str]
    deploy_regions: list[str]
    deploy_groups: list[str]
    replication_regions: list[str]
    replication_groups: list[str]
    replication_enabled: bool
    replication_target_regions: list[str]
    replication_target_source: str
    region_policy_file: Path | None = None
    deploy_policy_resolution: ResolvedRegionPolicyGroups | None = None
    replication_policy_resolution: ResolvedRegionPolicyGroups | None = None
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
    deploy_groups: list[str] | None = None,
    replication_regions: list[str] | None = None,
    replication_groups: list[str] | None = None,
    replication_enabled: bool = True,
    region_policy_file: str | None = None,
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
    region_policy_client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    explicit_deploy_regions = unique_region_ids(regions)
    requested_deploy_groups = unique_region_ids(deploy_groups or [])
    explicit_replication_regions = unique_region_ids(replication_regions or [])
    requested_replication_groups = unique_region_ids(replication_groups or [])
    if not replication_enabled and (explicit_replication_regions or requested_replication_groups):
        raise CaptureReplicateDeployError(
            "replication_enabled=false cannot be combined with replication regions or groups"
        )
    if region_policy_file is not None and not requested_deploy_groups and not requested_replication_groups:
        raise CaptureReplicateDeployError(
            "--region-policy-file requires at least one --deploy-group or --replication-group"
        )
    policy_file = resolved_policy_file(
        region_policy_file=region_policy_file,
        deploy_groups=requested_deploy_groups,
        replication_groups=requested_replication_groups,
    )
    deploy_policy_resolution = resolve_policy_groups(
        policy_file=policy_file,
        group_names=requested_deploy_groups,
        target="deploy",
        client=region_policy_client,
    )
    replication_policy_resolution = None
    if replication_enabled:
        replication_policy_resolution = resolve_policy_groups(
            policy_file=policy_file,
            group_names=requested_replication_groups,
            target="replication",
            client=region_policy_client,
        )
    deploy_group_regions = deploy_policy_resolution.regions if deploy_policy_resolution is not None else []
    deploy_regions = resolve_deploy_target_regions(
        explicit_deploy_regions=explicit_deploy_regions,
        group_regions=deploy_group_regions,
    )
    replication_group_regions = (
        replication_policy_resolution.regions if replication_policy_resolution is not None else []
    )
    if replication_enabled:
        replication_target_regions, replication_target_source = resolve_replication_target_regions(
            deploy_regions=deploy_regions,
            replication_regions=explicit_replication_regions,
            group_regions=replication_group_regions,
        )
    else:
        replication_target_regions = []
        replication_target_source = "replication_disabled"
    options = CaptureReplicateDeployOptions(
        explicit_deploy_regions=explicit_deploy_regions,
        deploy_regions=deploy_regions,
        deploy_groups=requested_deploy_groups,
        replication_regions=explicit_replication_regions,
        replication_groups=requested_replication_groups,
        replication_enabled=replication_enabled,
        replication_target_regions=replication_target_regions,
        replication_target_source=replication_target_source,
        region_policy_file=policy_file,
        deploy_policy_resolution=deploy_policy_resolution,
        replication_policy_resolution=replication_policy_resolution,
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
        "capture_region": options.deploy_regions[0],
        "source_image": options.source_image,
        "instance_type": options.instance_type,
    }
    manifest["replication_plan"] = {
        "replication_enabled": options.replication_enabled,
        "status": "planned" if options.replication_enabled else "skipped",
        "skip_reason": replication_skip_reason(options),
        "explicit_deploy_regions": list(options.explicit_deploy_regions),
        "requested_deploy_groups": list(options.deploy_groups),
        "deploy_group_sources": deploy_group_sources(options),
        "deploy_target_regions": list(options.deploy_regions),
        "explicit_replication_regions": list(options.replication_regions),
        "requested_replication_groups": list(options.replication_groups),
        "region_policy_file": str(options.region_policy_file) if options.region_policy_file is not None else None,
        "replication_group_sources": replication_group_sources(options),
        "group_sources": replication_group_sources(options),
        "replication_target_regions": list(options.replication_target_regions),
        "replication_target_source": options.replication_target_source,
        "provider_request": replication_provider_request_manifest(options),
        "replica_status_check": replication_status_check_manifest(options),
    }
    manifest["region_policy"] = region_policy_manifest(options)
    manifest["deploy_plan"] = {
        "explicit_deploy_regions": list(options.explicit_deploy_regions),
        "requested_deploy_groups": list(options.deploy_groups),
        "group_sources": deploy_group_sources(options),
        "deploy_regions": list(options.deploy_regions),
    }
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
    manifest["validation"] = {
        "status": "not_started",
        "policy": policy_validation_manifest(options),
        "capture": {},
        "replication": {},
        "deploy": {},
    }
    manifest["cleanup"] = {"status": "not_started", "capture": {}, "deploy": {}}
    manifest["region_policy"] = region_policy_manifest(options)
    attach_deploy_config(manifest, options)

    run_client = client or LinodeClient.from_env(command=COMMAND)
    capture_manifest: dict[str, Any] | None = None

    try:
        append_step(manifest, "preflight_api_access", mutates=False, status="running")
        run_client.preflight()
        finish_step(manifest, "preflight_api_access")

        if options.replication_enabled:
            append_step(manifest, "preflight_replication_target_regions", mutates=False, status="running")
            manifest["replication"]["region_capability_checks"] = validate_replication_region_capabilities(
                run_client,
                options.replication_target_regions,
            )
            manifest["validation"]["replication"] = {
                "status": "succeeded",
                "region_capability_checks": manifest["replication"]["region_capability_checks"],
            }
            finish_step(manifest, "preflight_replication_target_regions")
        else:
            manifest["validation"]["replication"] = replication_validation_summary(manifest, status="skipped")

        append_step(manifest, "run_capture_phase", mutates=True, status="running")
        capture_manifest = execute_capture(
            CaptureOptions(
                regions=[options.deploy_regions[0]],
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
                label_suffix=options.deploy_regions[0],
            ),
            client=run_client,
        )
        manifest["capture"] = capture_manifest
        manifest["validation"]["capture"] = capture_manifest.get("validation", {})
        finish_step(manifest, "run_capture_phase")

        image_id = required_text(capture_manifest.get("custom_image", {}).get("image_id"))
        if options.replication_enabled:
            run_replication_phase(
                manifest,
                run_client,
                image_id=image_id,
                deploy_regions=options.deploy_regions,
                replication_target_regions=options.replication_target_regions,
            )

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
        if isinstance(exc, ReplicationRegionCapabilityError):
            manifest["replication"]["region_capability_checks"] = exc.capability_checks
            manifest["validation"]["replication"] = {
                "status": "failed",
                "region_capability_checks": exc.capability_checks,
            }
            manifest["summary"]["failed"] = failed_capability_regions(exc.capability_checks)
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
    replication_target_regions: list[str],
) -> None:
    manifest["replication"]["source"] = {"image_id": image_id}

    append_step(manifest, "preflight_replication_inputs", mutates=False, status="running")
    image_details = client.get_image_details(image_id)
    validate_image_available(image_details)
    validate_existing_regions_present(image_details)
    for region in replication_target_regions:
        client.preflight_region(region)
    existing_regions = image_region_entries(image_details)
    submitted_regions = merge_regions(existing_region_ids(image_details), replication_target_regions)
    manifest["replication"]["source"]["existing_regions"] = existing_regions
    manifest["replication"]["request"] = {
        "image_id": image_id,
        "deploy_regions": list(deploy_regions),
        "requested_regions": list(replication_target_regions),
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
        manifest["validation"]["replication"] = replication_validation_summary(manifest, status="failed")
        raise
    manifest["replication"]["result"] = replication_result
    manifest["replication"]["provider_response_summary"] = provider_response_summary(replication_result)
    manifest["replication"]["status"] = "submitted"
    finish_step(manifest, "submit_image_replication")

    append_step(manifest, "wait_replica_regions_available", mutates=False, status="running")
    final_details = client.wait_image_regions_available(image_id, replication_target_regions)
    manifest["replication"]["replica_status_checks"] = {
        "status": "succeeded",
        "checked_regions": list(replication_target_regions),
        "final_image_status": final_details.get("status"),
        "regions": image_region_entries(final_details),
    }
    manifest["replication"]["status"] = "available"
    manifest["validation"]["replication"] = replication_validation_summary(manifest, status="succeeded")
    finish_step(manifest, "wait_replica_regions_available")


def replication_validation_summary(manifest: dict[str, Any], *, status: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"status": status}
    if status == "skipped":
        summary["reason"] = "replication_enabled=false"
    capability_checks = manifest["replication"].get("region_capability_checks")
    if capability_checks:
        summary["region_capability_checks"] = capability_checks
    return summary


def replication_skip_reason(options: CaptureReplicateDeployOptions) -> str | None:
    if options.replication_enabled:
        return None
    return "replication_enabled=false"


def replication_provider_request_manifest(options: CaptureReplicateDeployOptions) -> str:
    if not options.replication_enabled:
        return "skipped because replication_enabled=false"
    return "execute mode submits existing image regions plus resolved replication target regions"


def replication_status_check_manifest(options: CaptureReplicateDeployOptions) -> str:
    if not options.replication_enabled:
        return "skipped because replication_enabled=false"
    return "execute mode waits for resolved replication target replicas to report available"


def failed_capability_regions(capability_checks: dict[str, Any]) -> list[str]:
    checks = capability_checks.get("checks", [])
    if not isinstance(checks, list):
        return []
    failed: list[str] = []
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "failed":
            continue
        region = check.get("region")
        if isinstance(region, str) and region.strip():
            failed.append(region.strip())
    return failed


def base_manifest(options: CaptureReplicateDeployOptions, *, dry_run: bool, status: str) -> dict[str, Any]:
    base = create_manifest(
        command=COMMAND,
        mode=MODE,
        component="capture",
        regions=options.deploy_regions,
        run_id=options.run_id,
        ttl=options.ttl,
        image_project_tag=options.image_project_tag,
        dry_run=dry_run,
        status=status,
    )
    component_tags = component_lifecycle_tags(run_id=base["run_id"], ttl=base["ttl"])
    base["component_tags"] = component_tags
    planned_actions = [
        planned_action(
            "capture",
            options.deploy_regions[0],
            "capture",
            component_tags["capture"],
            mutates=not dry_run,
        ),
    ]
    if options.replication_enabled:
        planned_actions.append(
            planned_action("replicate", "all", "replicate", component_tags["replicate"], mutates=not dry_run)
        )
    else:
        skipped_replication = planned_action(
            "skip_replication",
            "all",
            "replicate",
            component_tags["replicate"],
            mutates=False,
        )
        skipped_replication["reason"] = replication_skip_reason(options)
        planned_actions.append(skipped_replication)
    planned_actions.extend(
        planned_action("deploy", region, "deploy", component_tags["deploy"], mutates=not dry_run)
        for region in options.deploy_regions
    )
    planned_actions.append(
        planned_action("cleanup", "temporary-resources", "capture", component_tags["capture"], mutates=not dry_run)
    )
    base["planned_actions"] = planned_actions
    base["summary"] = {
        "capture_region": options.deploy_regions[0],
        "explicit_deploy_regions": list(options.explicit_deploy_regions),
        "requested_deploy_groups": list(options.deploy_groups),
        "deploy_regions": list(options.deploy_regions),
        "replication_enabled": options.replication_enabled,
        "replication_skip_reason": replication_skip_reason(options),
        "explicit_replication_regions": list(options.replication_regions),
        "requested_replication_groups": list(options.replication_groups),
        "replication_target_regions": list(options.replication_target_regions),
        "replication_target_source": options.replication_target_source,
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
    if not options.replication_enabled:
        return {
            "status": "skipped",
            "enabled": False,
            "skip_reason": replication_skip_reason(options),
            "source": {},
            "request": {
                "deploy_regions": list(options.deploy_regions),
                "requested_regions": [],
                "submitted_regions": [],
            },
            "result": {},
            "provider_response_summary": {},
            "replica_status_checks": {"status": "skipped", "reason": replication_skip_reason(options)},
            "region_capability_checks": {
                "status": "skipped",
                "reason": replication_skip_reason(options),
                "required_capability": "Object Storage",
                "checks": [],
            },
        }
    return {
        "status": "not_started",
        "enabled": True,
        "source": {},
        "request": {
            "deploy_regions": list(options.deploy_regions),
            "requested_regions": list(options.replication_target_regions),
            "submitted_regions": [],
        },
        "result": {},
        "provider_response_summary": {},
        "replica_status_checks": {"status": "not_started"},
        "region_capability_checks": {
            "required_capability": "Object Storage",
            "checks": [],
        },
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
    active_statuses = [status for status in statuses if status != "skipped"]
    if active_statuses and all(status == "succeeded" for status in active_statuses):
        return "succeeded"
    if statuses and not active_statuses:
        return "skipped"
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


def resolved_policy_file(
    *,
    region_policy_file: str | None,
    deploy_groups: list[str],
    replication_groups: list[str],
) -> Path | None:
    if region_policy_file is not None:
        return Path(region_policy_file)
    if deploy_groups or replication_groups:
        return DEFAULT_REGION_POLICY_PATH
    return None


def resolve_deploy_target_regions(
    *,
    explicit_deploy_regions: list[str],
    group_regions: list[str],
) -> list[str]:
    return unique_region_ids([*explicit_deploy_regions, *group_regions])


def resolve_replication_target_regions(
    *,
    deploy_regions: list[str],
    replication_regions: list[str],
    group_regions: list[str],
) -> tuple[list[str], str]:
    requested_targets = unique_region_ids([*replication_regions, *group_regions])
    if requested_targets:
        return requested_targets, "replication_inputs"
    return list(deploy_regions), "deploy_regions_default"


def resolve_policy_groups(
    *,
    policy_file: Path | None,
    group_names: list[str],
    target: str,
    client: LinodeClientProtocol | None,
) -> ResolvedRegionPolicyGroups | None:
    if not group_names:
        return None
    if policy_file is None:
        raise CaptureReplicateDeployError(f"{target} groups require a region policy file")
    return resolve_region_policy_groups(path=policy_file, group_names=group_names, client=client)


def deploy_group_sources(options: CaptureReplicateDeployOptions) -> list[dict[str, Any]]:
    if options.deploy_policy_resolution is None:
        return []
    return [dict(source) for source in options.deploy_policy_resolution.group_sources]


def replication_group_sources(options: CaptureReplicateDeployOptions) -> list[dict[str, Any]]:
    if options.replication_policy_resolution is None:
        return []
    return [dict(source) for source in options.replication_policy_resolution.group_sources]


def region_policy_manifest(options: CaptureReplicateDeployOptions) -> dict[str, Any]:
    if options.region_policy_file is None:
        return {
            "status": "not_configured",
            "path": None,
            "replication_enabled": options.replication_enabled,
            "requested_deploy_groups": [],
            "deploy_group_sources": [],
            "requested_replication_groups": [],
            "replication_group_sources": [],
            "requested_groups": [],
            "group_sources": [],
        }
    resolved = options.deploy_policy_resolution is not None or options.replication_policy_resolution is not None
    return {
        "status": "resolved" if resolved else "not_used",
        "path": str(options.region_policy_file),
        "replication_enabled": options.replication_enabled,
        "requested_deploy_groups": list(options.deploy_groups),
        "deploy_group_sources": deploy_group_sources(options),
        "requested_replication_groups": list(options.replication_groups),
        "replication_group_sources": replication_group_sources(options),
        "requested_groups": list(options.replication_groups),
        "group_sources": replication_group_sources(options),
    }


def policy_validation_manifest(options: CaptureReplicateDeployOptions) -> dict[str, Any]:
    resolution = options.deploy_policy_resolution or options.replication_policy_resolution
    if resolution is None:
        return {"status": "not_required"}
    report = resolution.validation_report
    return {
        "status": "succeeded",
        "region_policy": {
            "path": report.get("path"),
            "valid": report.get("valid"),
            "status": report.get("status"),
            "provider_region_count": report.get("provider_region_count"),
            "policy_provider_region_count": report.get("policy_provider_region_count"),
            "generated_group_count": report.get("generated_group_count"),
            "group_count": report.get("group_count"),
        },
    }


def validate_regions(options: CaptureReplicateDeployOptions) -> None:
    if not options.deploy_regions:
        raise CaptureReplicateDeployError("capture-replicate-deploy requires at least one non-empty --region")
    if options.replication_enabled and not options.replication_target_regions:
        raise CaptureReplicateDeployError(
            "capture-replicate-deploy requires at least one resolved replication target region"
        )


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
