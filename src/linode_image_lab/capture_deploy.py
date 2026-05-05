"""Combined capture-deploy execution orchestration."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .capture import (
    CaptureError,
    CaptureOptions,
    cleanup_capture_source,
    execute_capture,
)
from .deploy import (
    DeployError,
    DeployOptions,
    cleanup_deploy_instance,
    execute_deploy,
)
from .linode_api import LinodeClient, LinodeClientProtocol
from .manifest import create_manifest, generate_tags
from .validation_results import combined_validation

MAX_PARALLEL_DEPLOYS = 4


class CaptureDeployError(ValueError):
    """Raised when capture-deploy cannot safely complete."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class CaptureDeployOptions:
    regions: list[str]
    run_id: str | None = None
    ttl: str | None = None
    execute: bool = False
    source_image: str | None = None
    instance_type: str | None = None
    preserve_instance: bool = False


def capture_deploy_plan(
    *,
    regions: list[str],
    run_id: str | None = None,
    ttl: str | None = None,
    execute: bool = False,
    source_image: str | None = None,
    instance_type: str | None = None,
    preserve_instance: bool = False,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    options = CaptureDeployOptions(
        regions=regions,
        run_id=run_id,
        ttl=ttl,
        execute=execute,
        source_image=source_image,
        instance_type=instance_type,
        preserve_instance=preserve_instance,
    )
    if not execute:
        return dry_run_manifest(options)
    return execute_capture_deploy(options, client=client)


def dry_run_manifest(options: CaptureDeployOptions) -> dict[str, Any]:
    manifest = create_manifest(
        command="capture-deploy",
        mode="capture-deploy",
        component="capture",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=True,
        status="planned",
    )
    apply_capture_deploy_shape(manifest, mutates=False)
    manifest["execution_mode"] = "dry-run"
    manifest["message"] = "capture-deploy is non-mutating unless --execute is provided"
    return manifest


def execute_capture_deploy(
    options: CaptureDeployOptions,
    *,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    validate_execute_options(options)
    if len(options.regions) > 1:
        return execute_multi_region_capture_deploy(options, client=client)
    return execute_single_region_capture_deploy(options, client=client)


def execute_multi_region_capture_deploy(
    options: CaptureDeployOptions,
    *,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    manifest = multi_region_manifest(options)
    run_client = client or LinodeClient.from_env(command="capture-deploy")
    capture_region = options.regions[0]

    try:
        capture_manifest = execute_capture(
            CaptureOptions(
                regions=[capture_region],
                run_id=manifest["run_id"],
                ttl=manifest["ttl"],
                execute=True,
                source_image=required_text(options.source_image),
                instance_type=required_text(options.instance_type),
                preserve_source=False,
                command="capture-deploy",
                mode="capture-deploy",
                component="capture",
                defer_cleanup=True,
                label_suffix=capture_region,
            ),
            client=run_client,
        )
    except CaptureError as exc:
        manifest["capture"] = exc.manifest or failed_capture_manifest(
            region=capture_region,
            run_id=manifest["run_id"],
            ttl=manifest["ttl"],
            exc=exc,
        )
        manifest["status"] = "failed"
        raise CaptureDeployError("capture-deploy --execute capture failed", manifest) from exc

    manifest["capture"] = capture_manifest
    image_id = required_text(capture_manifest.get("custom_image", {}).get("image_id"))

    deploy_manifests = execute_region_deploys(
        regions=manifest["summary"]["deploy_regions"],
        run_id=manifest["run_id"],
        ttl=manifest["ttl"],
        image_id=image_id,
        instance_type=required_text(options.instance_type),
        preserve_instance=options.preserve_instance,
        client=client,
    )
    for region in manifest["summary"]["deploy_regions"]:
        deploy_manifest = deploy_manifests[region]
        manifest["deploy_results"][region] = deploy_manifest
        if deploy_manifest.get("status") == "succeeded":
            manifest["summary"]["succeeded"].append(region)
        else:
            manifest["summary"]["failed"].append(region)

    cleanup_deferred_capture(run_client, capture_manifest)
    manifest["capture"] = capture_manifest
    cleanup_failed = cleanup_status(capture_manifest) == "failed"
    if cleanup_failed:
        manifest["summary"]["cleanup"] = cleanup_summary(capture_manifest)
    manifest["status"] = aggregate_status(
        succeeded=manifest["summary"]["succeeded"],
        failed=manifest["summary"]["failed"],
        cleanup_failed=cleanup_failed,
    )
    if manifest["status"] != "succeeded":
        if cleanup_failed and not manifest["summary"]["failed"]:
            raise CaptureDeployError("capture-deploy --execute cleanup failed", manifest)
        raise CaptureDeployError("capture-deploy --execute failed for one or more regions", manifest)
    return manifest


def execute_region_deploys(
    *,
    regions: list[str],
    run_id: str,
    ttl: str,
    image_id: str,
    instance_type: str,
    preserve_instance: bool,
    client: LinodeClientProtocol | None,
) -> dict[str, dict[str, Any]]:
    if len(regions) == 1:
        region = regions[0]
        return {
            region: execute_region_deploy(
                region=region,
                run_id=run_id,
                ttl=ttl,
                image_id=image_id,
                instance_type=instance_type,
                preserve_instance=preserve_instance,
                client=client,
            )
        }

    max_workers = min(len(regions), MAX_PARALLEL_DEPLOYS)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="linode-image-lab-deploy") as executor:
        futures: dict[Future[dict[str, Any]], str] = {
            executor.submit(
                execute_region_deploy,
                region=region,
                run_id=run_id,
                ttl=ttl,
                image_id=image_id,
                instance_type=instance_type,
                preserve_instance=preserve_instance,
                client=client,
            ): region
            for region in regions
        }
        for future in as_completed(futures):
            region = futures[future]
            try:
                results[region] = future.result()
            except Exception as exc:
                results[region] = failed_deploy_manifest(
                    region=region,
                    run_id=run_id,
                    ttl=ttl,
                    image_id=image_id,
                    exc=exc,
                )
    return results


def execute_region_deploy(
    *,
    region: str,
    run_id: str,
    ttl: str,
    image_id: str,
    instance_type: str,
    preserve_instance: bool,
    client: LinodeClientProtocol | None,
) -> dict[str, Any]:
    try:
        return execute_deploy(
            DeployOptions(
                regions=[region],
                run_id=run_id,
                ttl=ttl,
                execute=True,
                image_id=image_id,
                instance_type=instance_type,
                preserve_instance=preserve_instance,
                command="capture-deploy",
                mode="capture-deploy",
                component="deploy",
                defer_cleanup=False,
                label_suffix=region,
            ),
            client=worker_deploy_client(client),
        )
    except DeployError as exc:
        deploy_manifest = exc.manifest or failed_deploy_manifest(
            region=region,
            run_id=run_id,
            ttl=ttl,
            image_id=image_id,
            exc=exc,
        )
        add_region_error_context(deploy_manifest, region=region)
        return deploy_manifest


def worker_deploy_client(client: LinodeClientProtocol | None) -> LinodeClientProtocol:
    if client is None:
        return LinodeClient.from_env(command="capture-deploy")
    if isinstance(client, LinodeClient):
        return LinodeClient(
            token=client.token,
            api_base_url=client.api_base_url,
            timeout_seconds=client.timeout_seconds,
            poll_interval_seconds=client.poll_interval_seconds,
            max_wait_seconds=client.max_wait_seconds,
            max_retry_attempts=client.max_retry_attempts,
            retry_backoff_seconds=client.retry_backoff_seconds,
        )
    return client


def multi_region_manifest(options: CaptureDeployOptions) -> dict[str, Any]:
    base = create_manifest(
        command="capture-deploy",
        mode="capture-deploy",
        component="capture",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=False,
        status="running",
    )
    return {
        "schema_version": base["schema_version"],
        "project": base["project"],
        "command": base["command"],
        "mode": base["mode"],
        "regions": base["regions"],
        "run_id": base["run_id"],
        "ttl": base["ttl"],
        "dry_run": base["dry_run"],
        "execution_mode": "execute",
        "status": base["status"],
        "capture": {},
        "deploy_results": {},
        "summary": {
            "capture_region": options.regions[0],
            "deploy_regions": list(options.regions),
            "succeeded": [],
            "failed": [],
        },
    }


def execute_single_region_capture_deploy(
    options: CaptureDeployOptions,
    *,
    client: LinodeClientProtocol | None = None,
    label_suffix: str | None = None,
) -> dict[str, Any]:
    validate_single_region_execute_options(options)
    manifest = create_manifest(
        command="capture-deploy",
        mode="capture-deploy",
        component="capture",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=False,
        status="running",
    )
    apply_capture_deploy_shape(manifest, mutates=True)
    manifest["execution_mode"] = "execute"
    manifest["steps"] = []
    manifest["resources"] = []
    manifest["capture"] = {}
    manifest["deploy"] = {}
    manifest["validation"] = {"status": "not_started", "checks": []}
    manifest["cleanup"] = {"status": "not_started", "deleted": [], "preserved": []}

    run_client = client or LinodeClient.from_env(command="capture-deploy")
    capture_manifest: dict[str, Any] | None = None
    deploy_manifest: dict[str, Any] | None = None

    try:
        append_step(manifest, "run_capture_phase", mutates=True, status="running")
        capture_manifest = execute_capture(
            CaptureOptions(
                regions=options.regions,
                run_id=manifest["run_id"],
                ttl=manifest["ttl"],
                execute=True,
                source_image=required_text(options.source_image),
                instance_type=required_text(options.instance_type),
                preserve_source=False,
                command="capture-deploy",
                mode="capture-deploy",
                component="capture",
                defer_cleanup=True,
                label_suffix=label_suffix,
            ),
            client=run_client,
        )
        finish_step(manifest, "run_capture_phase")
        sync_manifest(manifest, capture_manifest=capture_manifest, deploy_manifest=deploy_manifest)

        image_id = required_text(capture_manifest.get("custom_image", {}).get("image_id"))

        append_step(manifest, "run_deploy_phase", mutates=True, status="running")
        deploy_manifest = execute_deploy(
            DeployOptions(
                regions=options.regions,
                run_id=manifest["run_id"],
                ttl=manifest["ttl"],
                execute=True,
                image_id=image_id,
                instance_type=required_text(options.instance_type),
                preserve_instance=options.preserve_instance,
                command="capture-deploy",
                mode="capture-deploy",
                component="deploy",
                defer_cleanup=True,
                label_suffix=label_suffix,
            ),
            client=run_client,
        )
        finish_step(manifest, "run_deploy_phase")
        sync_manifest(manifest, capture_manifest=capture_manifest, deploy_manifest=deploy_manifest)

        append_step(manifest, "record_deploy_validation", mutates=False, status="running")
        manifest["validation"] = combined_validation(
            capture_validation=capture_manifest.get("validation", {}),
            deploy_validation=deploy_manifest.get("validation", {}),
        )
        finish_step(manifest, "record_deploy_validation")

        append_step(manifest, "cleanup_temporary_resources", mutates=True, status="running")
        run_deferred_cleanup(
            run_client,
            capture_manifest=capture_manifest,
            deploy_manifest=deploy_manifest,
            preserve_instance=options.preserve_instance,
        )
        finish_step(manifest, "cleanup_temporary_resources")

        sync_manifest(manifest, capture_manifest=capture_manifest, deploy_manifest=deploy_manifest)
        manifest["status"] = "succeeded"
        return manifest
    except Exception as exc:
        mark_running_step_failed(manifest)
        if isinstance(exc, CaptureError) and exc.manifest is not None:
            capture_manifest = exc.manifest
        if isinstance(exc, DeployError) and exc.manifest is not None:
            deploy_manifest = exc.manifest
        try_cleanup_after_failure(
            run_client,
            capture_manifest=capture_manifest,
            deploy_manifest=deploy_manifest,
            preserve_instance=options.preserve_instance,
        )
        sync_manifest(manifest, capture_manifest=capture_manifest, deploy_manifest=deploy_manifest)
        manifest["status"] = "failed"
        manifest["errors"] = [safe_error_message(exc)]
        raise CaptureDeployError("capture-deploy --execute failed", manifest) from exc


def validate_execute_options(options: CaptureDeployOptions) -> None:
    if not options.regions:
        raise CaptureDeployError("capture-deploy --execute requires at least one non-empty --region")
    if not options.source_image:
        raise CaptureDeployError("capture-deploy --execute requires --source-image for the temporary capture Linode")
    if not options.instance_type:
        raise CaptureDeployError("capture-deploy --execute requires --type for temporary capture and deploy Linodes")


def validate_single_region_execute_options(options: CaptureDeployOptions) -> None:
    if len(options.regions) != 1:
        raise CaptureDeployError("capture-deploy single-region execution requires exactly one non-empty --region")
    validate_execute_options(options)


def aggregate_status(*, succeeded: list[str], failed: list[str], cleanup_failed: bool = False) -> str:
    if succeeded and failed:
        return "partial"
    if failed:
        return "failed"
    if cleanup_failed:
        return "partial"
    return "succeeded"


def cleanup_deferred_capture(client: LinodeClientProtocol, capture_manifest: dict[str, Any]) -> None:
    if cleanup_status(capture_manifest) != "deferred":
        return
    try:
        cleanup_capture_source(
            capture_manifest,
            client,
            capture_source=capture_manifest["capture_source"],
            preserve_source=False,
            required_tags=list(capture_manifest["tags"]),
        )
    except Exception:
        capture_manifest["cleanup"] = {"status": "failed", "deleted": [], "preserved": []}
        return
    record_custom_image_deliverable(capture_manifest)


def record_custom_image_deliverable(capture_manifest: dict[str, Any]) -> None:
    custom_image = capture_manifest.get("custom_image")
    if not custom_image:
        return
    cleanup = capture_manifest.setdefault("cleanup", {"status": "not_started", "deleted": [], "preserved": []})
    preserved = cleanup.setdefault("preserved", [])
    if any(item.get("reason") == "deliverable" for item in preserved):
        return
    deliverable = dict(custom_image)
    deliverable["reason"] = "deliverable"
    preserved.append(deliverable)


def failed_capture_manifest(
    *,
    region: str,
    run_id: str,
    ttl: str,
    exc: Exception,
) -> dict[str, Any]:
    manifest = create_manifest(
        command="capture-deploy",
        mode="capture-deploy",
        component="capture",
        regions=[region],
        run_id=run_id,
        ttl=ttl,
        dry_run=False,
        status="failed",
    )
    manifest["execution_mode"] = "execute"
    manifest["steps"] = []
    manifest["resources"] = []
    manifest["capture_source"] = {}
    manifest["custom_image"] = {}
    manifest["validation"] = {"status": "not_started", "checks": []}
    manifest["cleanup"] = {"status": "not_started", "deleted": [], "preserved": []}
    for action in manifest["planned_actions"]:
        action["mutates"] = True
    manifest["errors"] = [safe_error_message(exc)]
    return manifest


def failed_deploy_manifest(
    *,
    region: str,
    run_id: str,
    ttl: str,
    image_id: str,
    exc: Exception,
) -> dict[str, Any]:
    manifest = create_manifest(
        command="capture-deploy",
        mode="capture-deploy",
        component="deploy",
        regions=[region],
        run_id=run_id,
        ttl=ttl,
        dry_run=False,
        status="failed",
    )
    manifest["execution_mode"] = "execute"
    manifest["steps"] = []
    manifest["resources"] = []
    manifest["deploy_source"] = {"image_id": image_id}
    manifest["deploy_instance"] = {}
    manifest["validation"] = {"status": "not_started", "checks": []}
    manifest["cleanup"] = {"status": "not_started", "deleted": [], "preserved": []}
    for action in manifest["planned_actions"]:
        action["mutates"] = True
    manifest["errors"] = [safe_error_message(exc)]
    add_region_error_context(manifest, region=region)
    return manifest


def add_region_error_context(manifest: dict[str, Any], *, region: str) -> None:
    errors = manifest.get("errors")
    if not isinstance(errors, list) or not errors:
        return
    prefix = f"{region}: "
    manifest["errors"] = [error if str(error).startswith(prefix) else f"{prefix}{error}" for error in errors]


def apply_capture_deploy_shape(manifest: dict[str, Any], *, mutates: bool) -> None:
    capture_tags = generate_tags(
        run_id=manifest["run_id"],
        mode="capture-deploy",
        component="capture",
        ttl=manifest["ttl"],
    )
    deploy_tags = generate_tags(
        run_id=manifest["run_id"],
        mode="capture-deploy",
        component="deploy",
        ttl=manifest["ttl"],
    )
    manifest["component_tags"] = {
        "capture": capture_tags,
        "deploy": deploy_tags,
    }
    manifest["planned_actions"] = [
        {
            "action": action,
            "region": region,
            "component": component,
            "mutates": mutates,
            "tags": tags,
        }
        for region in manifest["regions"]
        for action, component, tags in (
            ("capture", "capture", capture_tags),
            ("deploy", "deploy", deploy_tags),
        )
    ]


def run_deferred_cleanup(
    client: LinodeClientProtocol,
    *,
    capture_manifest: dict[str, Any],
    deploy_manifest: dict[str, Any],
    preserve_instance: bool,
) -> None:
    cleanup_capture_source(
        capture_manifest,
        client,
        capture_source=capture_manifest["capture_source"],
        preserve_source=False,
        required_tags=list(capture_manifest["tags"]),
    )
    cleanup_deploy_instance(
        deploy_manifest,
        client,
        deploy_instance=deploy_manifest["deploy_instance"],
        preserve_instance=preserve_instance,
        required_tags=list(deploy_manifest["tags"]),
    )


def try_cleanup_after_failure(
    client: LinodeClientProtocol,
    *,
    capture_manifest: dict[str, Any] | None,
    deploy_manifest: dict[str, Any] | None,
    preserve_instance: bool,
) -> None:
    if capture_manifest is not None and cleanup_status(capture_manifest) == "deferred":
        try:
            cleanup_capture_source(
                capture_manifest,
                client,
                capture_source=capture_manifest["capture_source"],
                preserve_source=False,
                required_tags=list(capture_manifest["tags"]),
            )
        except Exception:
            capture_manifest["cleanup"] = {"status": "failed", "deleted": [], "preserved": []}

    if deploy_manifest is not None and cleanup_status(deploy_manifest) == "deferred":
        try:
            cleanup_deploy_instance(
                deploy_manifest,
                client,
                deploy_instance=deploy_manifest["deploy_instance"],
                preserve_instance=preserve_instance,
                required_tags=list(deploy_manifest["tags"]),
            )
        except Exception:
            deploy_manifest["cleanup"] = {"status": "failed", "deleted": [], "preserved": []}


def sync_manifest(
    manifest: dict[str, Any],
    *,
    capture_manifest: dict[str, Any] | None,
    deploy_manifest: dict[str, Any] | None,
) -> None:
    if capture_manifest is not None:
        manifest["capture"] = {
            "status": capture_manifest.get("status"),
            "steps": capture_manifest.get("steps", []),
            "resources": capture_manifest.get("resources", []),
            "capture_source": capture_manifest.get("capture_source", {}),
            "custom_image": capture_manifest.get("custom_image", {}),
            "validation": capture_manifest.get("validation", {}),
            "cleanup": capture_manifest.get("cleanup", {}),
        }
    if deploy_manifest is not None:
        manifest["deploy"] = {
            "status": deploy_manifest.get("status"),
            "steps": deploy_manifest.get("steps", []),
            "resources": deploy_manifest.get("resources", []),
            "deploy_source": deploy_manifest.get("deploy_source", {}),
            "deploy_instance": deploy_manifest.get("deploy_instance", {}),
            "validation": deploy_manifest.get("validation", {}),
            "cleanup": deploy_manifest.get("cleanup", {}),
        }

    manifest["validation"] = combined_validation(
        capture_validation=capture_manifest.get("validation", {}) if capture_manifest is not None else None,
        deploy_validation=deploy_manifest.get("validation", {}) if deploy_manifest is not None else None,
    )

    resources: list[dict[str, Any]] = []
    if capture_manifest is not None:
        resources.extend(capture_manifest.get("resources", []))
    if deploy_manifest is not None:
        resources.extend(deploy_manifest.get("resources", []))
    manifest["resources"] = resources
    manifest["cleanup"] = combined_cleanup(
        capture_manifest=capture_manifest,
        deploy_manifest=deploy_manifest,
    )


def combined_cleanup(
    *,
    capture_manifest: dict[str, Any] | None,
    deploy_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    deleted: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    capture_cleanup = cleanup_block(capture_manifest)
    deploy_cleanup = cleanup_block(deploy_manifest)
    deleted.extend(capture_cleanup.get("deleted", []))
    deleted.extend(deploy_cleanup.get("deleted", []))
    preserved.extend(capture_cleanup.get("preserved", []))
    preserved.extend(deploy_cleanup.get("preserved", []))
    failed.extend(capture_cleanup.get("failed", []))
    failed.extend(deploy_cleanup.get("failed", []))

    if capture_manifest is not None and capture_manifest.get("custom_image"):
        custom_image = dict(capture_manifest["custom_image"])
        custom_image["reason"] = "deliverable"
        preserved.append(custom_image)

    statuses = [status for status in (capture_cleanup.get("status"), deploy_cleanup.get("status")) if status]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status == "deferred" for status in statuses):
        status = "deferred"
    elif statuses:
        status = "completed"
    else:
        status = "not_started"

    return {
        "status": status,
        "deleted": deleted,
        "preserved": preserved,
        "failed": failed,
        "capture": capture_cleanup,
        "deploy": deploy_cleanup,
    }


def cleanup_block(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if manifest is None:
        return {"status": "not_started", "deleted": [], "preserved": []}
    return dict(manifest.get("cleanup", {"status": "not_started", "deleted": [], "preserved": []}))


def cleanup_status(manifest: dict[str, Any]) -> str:
    return str(manifest.get("cleanup", {}).get("status", "not_started"))


def cleanup_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    cleanup = cleanup_block(manifest)
    return {
        "status": cleanup.get("status", "not_started"),
        "deleted": len(cleanup.get("deleted", [])),
        "preserved": len(cleanup.get("preserved", [])),
        "failed": len(cleanup.get("failed", [])),
    }


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
        raise CaptureDeployError("required capture-deploy value is missing")
    return value


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, (CaptureError, DeployError)) and exc.manifest is not None:
        errors = exc.manifest.get("errors", [])
        if errors:
            return str(errors[0])
    if isinstance(exc, (CaptureDeployError, CaptureError, DeployError)):
        return str(exc)
    return exc.__class__.__name__
