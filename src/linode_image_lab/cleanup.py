"""Cleanup selection and execution logic for tagged lab resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .linode_api import LinodeClient, LinodeClientProtocol
from .manifest import PROJECT, REQUIRED_TAG_KEYS, VALID_COMPONENTS, VALID_MODES, tags_to_dict, validate_run_id

IMAGE_CLEANUP_COMPONENTS = {"capture"}


class CleanupError(ValueError):
    """Raised when cleanup cannot safely complete."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class CleanupOptions:
    run_id: str | None = None
    ttl: str | None = None
    discover: bool = False
    execute: bool = False


def parse_ttl(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resource_tags(resource: dict[str, Any]) -> dict[str, str]:
    return tags_to_dict(resource.get("tags", []))


def is_cleanup_candidate(resource: dict[str, Any], *, now: datetime | None = None) -> bool:
    tags = resource_tags(resource)
    if any(key not in tags for key in REQUIRED_TAG_KEYS):
        return False
    if tags["project"] != PROJECT:
        return False
    if not has_valid_run_id(tags["run_id"]):
        return False
    if tags["mode"] not in VALID_MODES:
        return False
    if tags["component"] not in VALID_COMPONENTS:
        return False
    if resource_type(resource) == "image" and tags["component"] not in IMAGE_CLEANUP_COMPONENTS:
        return False

    ttl = parse_ttl(tags["ttl"])
    if ttl is None:
        return False

    comparison_time = (now or datetime.now(UTC)).astimezone(UTC)
    return ttl <= comparison_time


def select_cleanup_candidates(
    resources: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    return [resource for resource in resources if is_cleanup_candidate(resource, now=now)]


def cleanup_plan(
    *,
    run_id: str | None = None,
    ttl: str | None = None,
    discover: bool = False,
    execute: bool = False,
    client: LinodeClientProtocol | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if run_id is not None:
        validate_run_id(run_id)
    options = CleanupOptions(run_id=run_id, ttl=ttl, discover=discover, execute=execute)
    manifest = base_cleanup_manifest(options)
    if not discover and not execute:
        manifest["message"] = (
            "cleanup is non-mutating; use --discover to list tagged resources "
            "or --execute to delete expired tagged resources"
        )
        manifest["discovery"] = {"status": "not_requested"}
        return manifest

    option = "--execute" if execute else "--discover"
    run_client = client or LinodeClient.from_env(command="cleanup", option=option)
    manifest["steps"] = []
    manifest["cleanup"] = {"status": "running", "deleted": [], "preserved": [], "failed": []}

    try:
        append_step(manifest, "preflight_api_access", mutates=False, status="running")
        run_client.preflight()
        finish_step(manifest, "preflight_api_access", client=run_client)

        append_step(manifest, "discover_managed_linodes", mutates=False, status="running")
        linodes = run_client.list_managed_linodes()
        finish_step(manifest, "discover_managed_linodes", client=run_client)

        append_step(manifest, "discover_managed_images", mutates=False, status="running")
        images = run_client.list_managed_images()
        finish_step(manifest, "discover_managed_images", client=run_client)

        resources = linodes + images
        manifest["resources"] = [resource_summary(resource) for resource in resources]

        assessments = assess_cleanup(resources, run_id=run_id, now=now)
        manifest["cleanup_candidates"] = sorted_cleanup_candidates(
            [item["resource"] for item in assessments if item["action"] == "delete"]
        )
        manifest["cleanup"]["preserved"] = [
            item["resource"] for item in assessments if item["action"] == "preserve"
        ]

        if discover:
            manifest["cleanup"]["status"] = "previewed"
            manifest["status"] = "planned"
            manifest["discovery"] = {"status": "completed"}
            return manifest

        append_step(manifest, "delete_expired_resources", mutates=bool(manifest["cleanup_candidates"]), status="running")
        deleted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        comparison_time = (now or datetime.now(UTC)).astimezone(UTC)
        for item in assessments:
            if item["action"] != "delete":
                continue
            provider_id = item["provider_id"]
            if not valid_provider_id(item["resource"]["resource_type"], provider_id):
                item["resource"]["reason"] = "missing_provider_id"
                manifest["cleanup"]["preserved"].append(item["resource"])
                continue
            try:
                current_resource = refetch_resource(run_client, item["resource"]["resource_type"], provider_id)
            except Exception:
                item["resource"]["reason"] = "refetch_failed"
                manifest["cleanup"]["preserved"].append(item["resource"])
                continue
            current_assessment = assess_resource(current_resource, run_id=run_id, now=comparison_time)
            if current_assessment["action"] != "delete":
                manifest["cleanup"]["preserved"].append(current_assessment["resource"])
                continue
            try:
                delete_resource(run_client, item["resource"]["resource_type"], provider_id)
            except Exception:
                failed_resource = dict(current_assessment["resource"])
                failed_resource["reason"] = "delete_status_unknown"
                failed.append(failed_resource)
                continue
            deleted.append(current_assessment["resource"])
        manifest["cleanup"]["deleted"] = deleted
        manifest["cleanup"]["failed"] = failed
        manifest["cleanup_candidates"] = []
        manifest["discovery"] = {"status": "completed"}
        if failed:
            mark_running_step_failed(manifest, client=run_client)
            manifest["cleanup"]["status"] = "failed"
            manifest["status"] = "failed"
            manifest["errors"] = ["cleanup delete status unknown for one or more resources"]
            raise CleanupError("cleanup --execute failed for one or more deletions", manifest)
        manifest["cleanup"]["status"] = "completed"
        manifest["status"] = "succeeded"
        finish_step(manifest, "delete_expired_resources", client=run_client)
        return manifest
    except CleanupError:
        raise
    except Exception as exc:
        mark_running_step_failed(manifest, client=run_client)
        manifest["status"] = "failed"
        manifest["cleanup"]["status"] = "failed"
        manifest["errors"] = [safe_error_message(exc)]
        message = "cleanup --execute failed" if execute else "cleanup --discover failed"
        raise CleanupError(message, manifest) from exc


def base_cleanup_manifest(options: CleanupOptions) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": PROJECT,
        "command": "cleanup",
        "mode": "cleanup",
        "run_id": options.run_id,
        "regions": [],
        "ttl": options.ttl,
        "dry_run": not options.execute,
        "execution_mode": cleanup_execution_mode(options),
        "status": "running" if options.execute or options.discover else "planned",
        "filters": {"run_id": options.run_id},
        "resources": [],
        "cleanup_candidates": [],
        "cleanup": {"status": "not_started", "deleted": [], "preserved": [], "failed": []},
    }


def cleanup_execution_mode(options: CleanupOptions) -> str:
    if options.execute:
        return "execute"
    if options.discover:
        return "discover"
    return "dry-run"


def assess_cleanup(
    resources: list[dict[str, Any]],
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    comparison_time = (now or datetime.now(UTC)).astimezone(UTC)
    return [assess_resource(resource, run_id=run_id, now=comparison_time) for resource in resources]


def assess_resource(resource: dict[str, Any], *, run_id: str | None, now: datetime) -> dict[str, Any]:
    summary = resource_summary(resource)
    tags = resource_tags(resource)
    provider_id = resource_provider_id(resource)

    if any(key not in tags for key in REQUIRED_TAG_KEYS):
        summary["reason"] = "missing_required_tags"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}
    if tags["project"] != PROJECT:
        summary["reason"] = "tag_mismatch"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}
    if not has_valid_run_id(tags["run_id"]):
        summary["reason"] = "invalid_run_id"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}
    if run_id is not None and tags["run_id"] != run_id:
        summary["reason"] = "run_id_filter_mismatch"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}
    if tags["mode"] not in VALID_MODES or tags["component"] not in VALID_COMPONENTS:
        summary["reason"] = "tag_mismatch"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}
    if summary["resource_type"] == "image" and tags["component"] not in IMAGE_CLEANUP_COMPONENTS:
        summary["reason"] = "tag_mismatch"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}

    ttl = parse_ttl(tags["ttl"])
    if ttl is None:
        summary["reason"] = "ttl_parse_failed"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}
    if ttl > now:
        summary["expires_in_seconds"] = seconds_between(now, ttl)
        summary["reason"] = "ttl_not_expired"
        return {"action": "preserve", "resource": summary, "provider_id": provider_id}

    summary["expired_at"] = format_utc_timestamp(ttl)
    summary["expired_for_seconds"] = seconds_between(ttl, now)
    summary["reason"] = "expired_ttl"
    return {"action": "delete", "resource": summary, "provider_id": provider_id}


def sorted_cleanup_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(candidates, key=cleanup_candidate_sort_key)


def cleanup_candidate_sort_key(candidate: dict[str, Any]) -> tuple[object, ...]:
    return (
        -int(candidate.get("expired_for_seconds", 0)),
        str(candidate.get("resource_type", "")),
        str(candidate.get("linode_id", candidate.get("image_id", ""))),
    )


def seconds_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds())


def format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def has_valid_run_id(value: str) -> bool:
    try:
        validate_run_id(value)
    except ValueError:
        return False
    return True


def resource_summary(resource: dict[str, Any]) -> dict[str, Any]:
    kind = resource_type(resource)
    summary: dict[str, Any] = {
        "resource_type": kind,
        "tags": list(resource.get("tags", [])),
    }
    if kind == "image":
        summary["image_id"] = resource.get("image_id", resource.get("id"))
    else:
        summary["linode_id"] = resource.get("linode_id", resource.get("id"))
    for key in ("label", "region", "status"):
        if key in resource:
            summary[key] = resource[key]
    return summary


def resource_type(resource: dict[str, Any]) -> str:
    kind = resource.get("resource_type")
    if kind in {"linode", "image"}:
        return str(kind)
    if "image_id" in resource:
        return "image"
    return "linode"


def resource_provider_id(resource: dict[str, Any]) -> object:
    if resource_type(resource) == "image":
        return resource.get("image_id", resource.get("id"))
    return resource.get("linode_id", resource.get("id"))


def valid_provider_id(resource_type_name: str, provider_id: object) -> bool:
    if resource_type_name == "image":
        return isinstance(provider_id, str) and bool(provider_id)
    return isinstance(provider_id, int)


def refetch_resource(client: LinodeClientProtocol, resource_type_name: str, provider_id: object) -> dict[str, Any]:
    if resource_type_name == "image":
        return client.get_image(str(provider_id))
    return client.get_instance(int(provider_id))


def delete_resource(client: LinodeClientProtocol, resource_type_name: str, provider_id: object) -> dict[str, Any]:
    if resource_type_name == "image":
        return client.delete_image(str(provider_id))
    return client.delete_instance(int(provider_id))


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


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, CleanupError):
        return str(exc)
    return exc.__class__.__name__
