"""Explicit custom image replication planning and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .linode_api import (
    LinodeApiError,
    LinodeClient,
    LinodeClientProtocol,
    LinodePreflightError,
    region_capabilities,
)
from .manifest import create_manifest
from .validation_results import (
    finish_validation,
    mark_validation_check_succeeded,
    record_validation_check,
    start_validation,
)

REPLICATION_REGION_CAPABILITY = "Object Storage"
REPLICATE_VALIDATION_CHECKS = (
    ("image_available", "replication_source"),
    ("requested_regions_valid", "replication_regions"),
    ("replication_submitted", "image_replication"),
)


class ReplicateError(ValueError):
    """Raised when image replication cannot safely complete."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


class ReplicationRegionCapabilityError(ReplicateError):
    """Raised when a requested replication target lacks required capability."""

    def __init__(self, message: str, capability_checks: dict[str, Any]) -> None:
        super().__init__(message)
        self.capability_checks = capability_checks


@dataclass(frozen=True)
class ReplicateOptions:
    regions: list[str]
    run_id: str | None = None
    ttl: str | None = None
    execute: bool = False
    image_id: str | None = None


def replicate_plan(
    *,
    regions: list[str],
    run_id: str | None = None,
    ttl: str | None = None,
    execute: bool = False,
    image_id: str | None = None,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    options = ReplicateOptions(
        regions=unique_region_ids(regions),
        run_id=run_id,
        ttl=ttl,
        execute=execute,
        image_id=image_id,
    )
    if not execute:
        return dry_run_manifest(options)
    return execute_replicate(options, client=client)


def dry_run_manifest(options: ReplicateOptions) -> dict[str, Any]:
    manifest = create_manifest(
        command="replicate",
        mode="replicate",
        component="replicate",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=True,
        status="planned",
    )
    manifest["execution_mode"] = "dry-run"
    manifest["message"] = "replicate is non-mutating unless --execute is provided"
    manifest["replica_status_polling"] = "not_attempted"
    manifest["tag_application"] = "not_applicable"
    manifest["replication_intent"] = {
        "image_id": options.image_id,
        "requested_regions": unique_region_ids(options.regions),
        "provider_request": "execute mode submits existing image regions plus requested regions",
    }
    return manifest


def execute_replicate(
    options: ReplicateOptions,
    *,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    validate_execute_options(options)
    manifest = create_manifest(
        command="replicate",
        mode="replicate",
        component="replicate",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=False,
        status="running",
    )
    manifest["execution_mode"] = "execute"
    manifest["steps"] = []
    manifest["resources"] = []
    manifest["replication_source"] = {"image_id": required_text(options.image_id)}
    manifest["replication_request"] = {
        "image_id": required_text(options.image_id),
        "requested_regions": unique_region_ids(options.regions),
        "submitted_regions": [],
        "region_capability_checks": {
            "required_capability": REPLICATION_REGION_CAPABILITY,
            "checks": [],
        },
    }
    manifest["replication_result"] = {}
    manifest["provider_response_summary"] = {}
    manifest["validation"] = start_validation(REPLICATE_VALIDATION_CHECKS)
    manifest["replica_status_polling"] = "not_attempted"
    manifest["tag_application"] = "not_applicable"
    for action in manifest["planned_actions"]:
        action["mutates"] = True

    run_client = client or LinodeClient.from_env(command="replicate")
    image_details: dict[str, Any] = {}

    try:
        append_step(manifest, "preflight_api_access", mutates=False, status="running")
        run_client.preflight()
        finish_step(manifest, "preflight_api_access", client=run_client)

        image_id = required_text(options.image_id)

        append_step(manifest, "preflight_provider_inputs", mutates=False, status="running")

        def validate_image() -> None:
            nonlocal image_details
            image_details = run_client.get_image_details(image_id)
            validate_image_available(image_details)
            validate_existing_regions_present(image_details)

        record_validation_check(manifest["validation"], "image_available", validate_image)

        def validate_requested_regions() -> None:
            manifest["replication_request"]["region_capability_checks"] = validate_replication_region_capabilities(
                run_client,
                unique_region_ids(options.regions),
            )

        record_validation_check(manifest["validation"], "requested_regions_valid", validate_requested_regions)
        existing_regions = image_region_entries(image_details)
        submitted_regions = merge_regions(existing_region_ids(image_details), options.regions)
        manifest["replication_source"]["existing_regions"] = existing_regions
        manifest["replication_request"]["submitted_regions"] = submitted_regions
        finish_step(manifest, "preflight_provider_inputs", client=run_client)

        append_step(manifest, "submit_image_replication", mutates=True, status="running")
        manifest["replication_result"] = run_client.replicate_image(image_id=image_id, regions=submitted_regions)
        manifest["provider_response_summary"] = provider_response_summary(manifest["replication_result"])
        mark_validation_check_succeeded(manifest["validation"], "replication_submitted")
        finish_step(manifest, "submit_image_replication", client=run_client)

        finish_validation(manifest["validation"])
        manifest["status"] = "succeeded"
        return manifest
    except Exception as exc:
        mark_running_step_failed(manifest, client=run_client)
        manifest["status"] = "failed"
        manifest["errors"] = [safe_error_message(exc)]
        if isinstance(exc, ReplicationRegionCapabilityError):
            manifest["replication_request"]["region_capability_checks"] = exc.capability_checks
        if isinstance(exc, LinodeApiError):
            provider_error = exc.provider_error_details()
            if provider_error is not None:
                manifest["provider_error"] = provider_error
        if manifest.get("validation", {}).get("status") == "running":
            manifest["validation"]["status"] = "failed"
        raise ReplicateError("replicate --execute failed", manifest) from exc


def validate_execute_options(options: ReplicateOptions) -> None:
    if not options.regions:
        raise ReplicateError("replicate --execute requires one or more non-empty --region values")
    if not options.image_id:
        raise ReplicateError("replicate --execute requires --image-id for the custom image to replicate")


def validate_image_available(image: dict[str, Any]) -> None:
    if image.get("status") != "available":
        raise ReplicateError("requested image is not available")


def validate_existing_regions_present(image: dict[str, Any]) -> None:
    if not existing_region_ids(image):
        raise ReplicateError(
            "requested image did not expose existing regions; refusing replication to avoid removing existing replicas"
        )


def validate_replication_region_capabilities(
    client: LinodeClientProtocol,
    regions: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "required_capability": REPLICATION_REGION_CAPABILITY,
        "checks": [],
    }
    for region in unique_region_ids(regions):
        details = client.get_region_details(region)
        if REPLICATION_REGION_CAPABILITY not in region_capabilities(details):
            result["checks"].append(
                {
                    "region": region,
                    "capability": REPLICATION_REGION_CAPABILITY,
                    "status": "failed",
                    "missing_capability": REPLICATION_REGION_CAPABILITY,
                }
            )
            raise ReplicationRegionCapabilityError(
                f"requested replication target region {region} is missing required capability: "
                f"{REPLICATION_REGION_CAPABILITY}",
                result,
            )
        result["checks"].append(
            {
                "region": region,
                "capability": REPLICATION_REGION_CAPABILITY,
                "status": "succeeded",
            }
        )
    return result


def existing_region_ids(image: dict[str, Any]) -> list[str]:
    return [entry["region"] for entry in image_region_entries(image)]


def image_region_entries(image: dict[str, Any]) -> list[dict[str, str]]:
    regions = image.get("regions", [])
    if not isinstance(regions, list):
        return []
    values: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in regions:
        if not isinstance(item, dict):
            continue
        region = item.get("region")
        if not isinstance(region, str) or not region.strip():
            continue
        normalized = region.strip().lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        entry = {"region": normalized}
        status = item.get("status")
        if isinstance(status, str) and status.strip():
            entry["status"] = status.strip()
        values.append(entry)
    return values


def merge_regions(existing_regions: list[str], requested_regions: list[str]) -> list[str]:
    return unique_region_ids([*existing_regions, *requested_regions])


def unique_region_ids(regions: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for region in regions:
        normalized = region.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def provider_response_summary(image: dict[str, Any]) -> dict[str, Any]:
    regions = image_region_entries(image)
    return {
        "image_status": image.get("status"),
        "region_count": len(regions),
        "regions": regions,
    }


def append_step(
    manifest: dict[str, Any],
    name: str,
    *,
    mutates: bool,
    status: str,
) -> None:
    manifest["steps"].append({"name": name, "mutates": mutates, "status": status})


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


def required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ReplicateError("required replication value is missing")
    return value


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, (ReplicateError, LinodePreflightError, LinodeApiError)):
        return str(exc)
    return exc.__class__.__name__
