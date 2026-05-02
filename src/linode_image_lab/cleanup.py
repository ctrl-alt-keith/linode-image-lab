"""Cleanup selection and execution logic for tagged temporary Linodes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .linode_api import TOKEN_ENV_NAME, LinodeClient, LinodeClientProtocol
from .manifest import PROJECT, REQUIRED_TAG_KEYS, VALID_COMPONENTS, VALID_MODES, tags_to_dict


class CleanupError(ValueError):
    """Raised when cleanup cannot safely complete."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class CleanupOptions:
    run_id: str | None = None
    ttl: str | None = None
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
    if tags["mode"] not in VALID_MODES:
        return False
    if tags["component"] not in VALID_COMPONENTS:
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
    execute: bool = False,
    client: LinodeClientProtocol | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    options = CleanupOptions(run_id=run_id, ttl=ttl, execute=execute)
    manifest = base_cleanup_manifest(options)
    if not execute and client is None:
        if TOKEN_ENV_NAME not in os.environ:
            manifest["message"] = "cleanup is non-mutating; set LINODE_TOKEN to preview discovered Linodes"
            manifest["discovery"] = {"status": "not_requested", "reason": "token_not_provided"}
            return manifest

    run_client = client or LinodeClient.from_env(command="cleanup")
    manifest["steps"] = []
    manifest["cleanup"] = {"status": "running", "deleted": [], "preserved": []}

    try:
        if execute:
            append_step(manifest, "preflight_api_access", mutates=False, status="running")
            run_client.preflight()
            finish_step(manifest, "preflight_api_access")

        append_step(manifest, "discover_managed_linodes", mutates=False, status="running")
        resources = run_client.list_managed_linodes()
        manifest["resources"] = [resource_summary(resource) for resource in resources]
        finish_step(manifest, "discover_managed_linodes")

        assessments = assess_cleanup(resources, run_id=run_id, now=now)
        manifest["cleanup_candidates"] = [item["resource"] for item in assessments if item["action"] == "delete"]
        manifest["cleanup"]["preserved"] = [
            item["resource"] for item in assessments if item["action"] == "preserve"
        ]

        if not execute:
            manifest["cleanup"]["status"] = "previewed"
            manifest["status"] = "planned"
            manifest["discovery"] = {"status": "completed"}
            return manifest

        append_step(manifest, "delete_expired_linodes", mutates=bool(manifest["cleanup_candidates"]), status="running")
        deleted: list[dict[str, Any]] = []
        for item in assessments:
            if item["action"] != "delete":
                continue
            linode_id = item["linode_id"]
            if not isinstance(linode_id, int):
                item["resource"]["reason"] = "missing_provider_id"
                manifest["cleanup"]["preserved"].append(item["resource"])
                continue
            run_client.delete_instance(linode_id)
            deleted.append(item["resource"])
        manifest["cleanup"]["deleted"] = deleted
        manifest["cleanup_candidates"] = []
        manifest["cleanup"]["status"] = "completed"
        manifest["status"] = "succeeded"
        manifest["discovery"] = {"status": "completed"}
        finish_step(manifest, "delete_expired_linodes")
        return manifest
    except Exception as exc:
        mark_running_step_failed(manifest)
        manifest["status"] = "failed"
        manifest["cleanup"]["status"] = "failed"
        manifest["errors"] = [safe_error_message(exc)]
        message = "cleanup --execute failed" if execute else "cleanup dry-run failed"
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
        "execution_mode": "execute" if options.execute else "dry-run",
        "status": "running" if options.execute else "planned",
        "filters": {"run_id": options.run_id},
        "resources": [],
        "cleanup_candidates": [],
        "cleanup": {"status": "not_started", "deleted": [], "preserved": []},
    }


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

    if any(key not in tags for key in REQUIRED_TAG_KEYS):
        summary["reason"] = "missing_required_tags"
        return {"action": "preserve", "resource": summary, "linode_id": resource.get("linode_id")}
    if tags["project"] != PROJECT:
        summary["reason"] = "tag_mismatch"
        return {"action": "preserve", "resource": summary, "linode_id": resource.get("linode_id")}
    if run_id is not None and tags["run_id"] != run_id:
        summary["reason"] = "run_id_filter_mismatch"
        return {"action": "preserve", "resource": summary, "linode_id": resource.get("linode_id")}
    if tags["mode"] not in VALID_MODES or tags["component"] not in VALID_COMPONENTS:
        summary["reason"] = "tag_mismatch"
        return {"action": "preserve", "resource": summary, "linode_id": resource.get("linode_id")}

    ttl = parse_ttl(tags["ttl"])
    if ttl is None:
        summary["reason"] = "ttl_parse_failed"
        return {"action": "preserve", "resource": summary, "linode_id": resource.get("linode_id")}
    if ttl > now:
        summary["reason"] = "ttl_not_expired"
        return {"action": "preserve", "resource": summary, "linode_id": resource.get("linode_id")}

    summary["reason"] = "expired_ttl"
    return {"action": "delete", "resource": summary, "linode_id": resource.get("linode_id")}


def resource_summary(resource: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "resource_type": "linode",
        "linode_id": resource.get("linode_id", resource.get("id")),
        "tags": list(resource.get("tags", [])),
    }
    for key in ("label", "region", "status"):
        if key in resource:
            summary[key] = resource[key]
    return summary


def append_step(
    manifest: dict[str, Any],
    name: str,
    *,
    mutates: bool,
    status: str,
) -> None:
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


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, CleanupError):
        return str(exc)
    return exc.__class__.__name__
