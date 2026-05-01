"""Capture command planning and execution orchestration."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any

from .linode_api import LinodeClient, LinodeClientProtocol
from .manifest import REQUIRED_TAG_KEYS, create_manifest, tags_to_dict


class CaptureError(ValueError):
    """Raised when capture cannot safely complete."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class CaptureOptions:
    regions: list[str]
    run_id: str | None = None
    ttl: str | None = None
    execute: bool = False
    source_image: str | None = None
    instance_type: str | None = None
    image_label: str | None = None
    preserve_source: bool = False


def capture_plan(
    *,
    regions: list[str],
    run_id: str | None = None,
    ttl: str | None = None,
    execute: bool = False,
    source_image: str | None = None,
    instance_type: str | None = None,
    image_label: str | None = None,
    preserve_source: bool = False,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    options = CaptureOptions(
        regions=regions,
        run_id=run_id,
        ttl=ttl,
        execute=execute,
        source_image=source_image,
        instance_type=instance_type,
        image_label=image_label,
        preserve_source=preserve_source,
    )
    if not execute:
        return dry_run_manifest(options)
    return execute_capture(options, client=client)


def dry_run_manifest(options: CaptureOptions) -> dict[str, Any]:
    manifest = create_manifest(
        command="capture",
        mode="capture",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=True,
        status="planned",
    )
    manifest["execution_mode"] = "dry-run"
    manifest["message"] = "capture is non-mutating unless --execute is provided"
    return manifest


def execute_capture(
    options: CaptureOptions,
    *,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    validate_execute_options(options)
    manifest = create_manifest(
        command="capture",
        mode="capture",
        regions=options.regions,
        run_id=options.run_id,
        ttl=options.ttl,
        dry_run=False,
        status="running",
    )
    manifest["execution_mode"] = "execute"
    manifest["steps"] = []
    manifest["resources"] = []
    manifest["capture_source"] = {}
    manifest["custom_image"] = {}
    manifest["cleanup"] = {"status": "not_started", "deleted": [], "preserved": []}
    for action in manifest["planned_actions"]:
        action["mutates"] = True

    run_client = client or LinodeClient.from_env()
    capture_source: dict[str, Any] | None = None
    custom_image: dict[str, Any] | None = None

    try:
        append_step(manifest, "preflight", mutates=False, status="running")
        run_client.preflight()
        finish_step(manifest, "preflight")

        tags = list(manifest["tags"])
        region = options.regions[0]
        source_label = f"lil-{safe_label_suffix(manifest['run_id'])}-source"
        image_label = options.image_label or f"lil-{safe_label_suffix(manifest['run_id'])}-image"

        append_step(manifest, "create_capture_source", mutates=True, status="running")
        capture_source = run_client.create_instance(
            region=region,
            source_image=required_text(options.source_image),
            instance_type=required_text(options.instance_type),
            label=source_label,
            tags=tags,
            root_password=secrets.token_urlsafe(32),
        )
        capture_source["resource_type"] = "linode"
        manifest["capture_source"] = dict(capture_source)
        manifest["resources"].append(dict(capture_source))
        finish_step(manifest, "create_capture_source")

        append_step(manifest, "wait_capture_source_ready", mutates=False, status="running")
        capture_source = merge_resource(
            capture_source,
            run_client.wait_instance_ready(required_int(capture_source.get("linode_id"))),
        )
        manifest["capture_source"] = dict(capture_source)
        manifest["resources"][0] = dict(capture_source)
        finish_step(manifest, "wait_capture_source_ready")

        append_step(manifest, "validate_capture_source", mutates=False, status="running")
        validate_created_resource(capture_source, required_tags=tags, region=region)
        disks = run_client.list_disks(required_int(capture_source.get("linode_id")))
        disk = first_disk(disks)
        capture_source["disk_id"] = disk["disk_id"]
        manifest["capture_source"] = dict(capture_source)
        manifest["resources"][0] = dict(capture_source)
        finish_step(manifest, "validate_capture_source")

        append_step(manifest, "shutdown_capture_source", mutates=True, status="running")
        run_client.shutdown_instance(required_int(capture_source.get("linode_id")))
        finish_step(manifest, "shutdown_capture_source")

        append_step(manifest, "wait_capture_source_offline", mutates=False, status="running")
        capture_source = merge_resource(
            capture_source,
            run_client.wait_instance_offline(required_int(capture_source.get("linode_id"))),
        )
        manifest["capture_source"] = dict(capture_source)
        manifest["resources"][0] = dict(capture_source)
        finish_step(manifest, "wait_capture_source_offline")

        append_step(manifest, "capture_custom_image", mutates=True, status="running")
        custom_image = run_client.capture_image(
            disk_id=required_int(capture_source.get("disk_id")),
            label=image_label,
            tags=tags,
            description=f"linode-image-lab capture run {manifest['run_id']}",
            cloud_init=True,
        )
        custom_image["resource_type"] = "image"
        manifest["custom_image"] = dict(custom_image)
        manifest["resources"].append(dict(custom_image))
        finish_step(manifest, "capture_custom_image")

        append_step(manifest, "wait_custom_image_available", mutates=False, status="running")
        custom_image = merge_resource(
            custom_image,
            run_client.wait_image_available(required_text(custom_image.get("image_id"))),
        )
        validate_created_resource(custom_image, required_tags=tags)
        manifest["custom_image"] = dict(custom_image)
        manifest["resources"][1] = dict(custom_image)
        finish_step(manifest, "wait_custom_image_available")

        cleanup_capture_source(
            manifest,
            run_client,
            capture_source=capture_source,
            preserve_source=options.preserve_source,
            required_tags=tags,
        )
        manifest["status"] = "succeeded"
        return manifest
    except Exception as exc:
        mark_running_step_failed(manifest)
        manifest["status"] = "failed"
        manifest["errors"] = [safe_error_message(exc)]
        if capture_source is not None and not cleanup_started(manifest):
            try:
                cleanup_capture_source(
                    manifest,
                    run_client,
                    capture_source=capture_source,
                    preserve_source=options.preserve_source,
                    required_tags=list(manifest["tags"]),
                )
            except Exception:
                mark_running_step_failed(manifest)
                manifest["cleanup"] = {"status": "failed", "deleted": [], "preserved": []}
        raise CaptureError("capture --execute failed", manifest) from exc


def validate_execute_options(options: CaptureOptions) -> None:
    if len(options.regions) != 1:
        raise CaptureError("capture --execute requires exactly one region")
    if not options.source_image:
        raise CaptureError("capture --execute requires --source-image")
    if not options.instance_type:
        raise CaptureError("capture --execute requires --type")


def cleanup_capture_source(
    manifest: dict[str, Any],
    client: LinodeClientProtocol,
    *,
    capture_source: dict[str, Any],
    preserve_source: bool,
    required_tags: list[str],
) -> None:
    append_step(manifest, "cleanup_capture_source", mutates=not preserve_source, status="running")
    cleanup = {"status": "not_started", "deleted": [], "preserved": []}
    if preserve_source:
        cleanup["status"] = "preserved"
        cleanup["preserved"].append({"resource_type": "linode", "linode_id": capture_source.get("linode_id")})
    elif has_required_tags(capture_source, required_tags):
        client.delete_instance(required_int(capture_source.get("linode_id")))
        cleanup["status"] = "deleted"
        cleanup["deleted"].append({"resource_type": "linode", "linode_id": capture_source.get("linode_id")})
    else:
        cleanup["status"] = "skipped_tag_mismatch"
        cleanup["preserved"].append({"resource_type": "linode", "linode_id": capture_source.get("linode_id")})
    manifest["cleanup"] = cleanup
    finish_step(manifest, "cleanup_capture_source")


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
    return any(step.get("name") == "cleanup_capture_source" for step in manifest.get("steps", []))


def validate_created_resource(
    resource: dict[str, Any],
    *,
    required_tags: list[str],
    region: str | None = None,
) -> None:
    if region is not None and resource.get("region") != region:
        raise CaptureError("created capture source is not in the requested region")
    if not has_required_tags(resource, required_tags):
        raise CaptureError("created resource is missing required capture tags")


def has_required_tags(resource: dict[str, Any], required_tags: list[str]) -> bool:
    tags = tags_to_dict(resource.get("tags", []))
    expected = tags_to_dict(required_tags)
    return all(key in tags and tags[key] == expected[key] for key in REQUIRED_TAG_KEYS)


def first_disk(disks: list[dict[str, Any]]) -> dict[str, Any]:
    if not disks:
        raise CaptureError("capture source has no disk to image")
    disk = disks[0]
    disk_id = disk.get("disk_id", disk.get("id"))
    if disk_id is None:
        raise CaptureError("capture source disk is missing an id")
    return {"disk_id": disk_id}


def merge_resource(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    merged.update({key: value for key, value in update.items() if value is not None})
    return merged


def required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CaptureError("required capture value is missing")
    return value


def required_int(value: object) -> int:
    if not isinstance(value, int):
        raise CaptureError("required provider resource id is missing")
    return value


def safe_label_suffix(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return (normalized or "run")[:32]


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, CaptureError):
        return str(exc)
    return exc.__class__.__name__
