"""Manifest and tag contract helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .redaction import redact

PROJECT = "linode-image-lab"
SCHEMA_VERSION = 1
VALID_MODES = {"capture", "deploy", "capture-deploy"}
VALID_COMPONENTS = {"capture", "deploy"}
REQUIRED_TAG_KEYS = ("project", "run_id", "mode", "component", "ttl")
RESERVED_TAG_KEYS = frozenset((*REQUIRED_TAG_KEYS, "lifecycle"))


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_ttl(now: datetime | None = None) -> str:
    base = now or utc_now()
    return format_timestamp(base + timedelta(hours=4))


def validate_mode(mode: str) -> str:
    if mode not in VALID_MODES:
        choices = ", ".join(sorted(VALID_MODES))
        raise ValueError(f"mode must be one of: {choices}")
    return mode


def validate_component(component: str) -> str:
    if component not in VALID_COMPONENTS:
        choices = ", ".join(sorted(VALID_COMPONENTS))
        raise ValueError(f"component must be one of: {choices}")
    return component


def generate_tags(*, run_id: str, mode: str, component: str, ttl: str) -> list[str]:
    """Generate rediscoverable tags for every modeled resource."""
    validate_mode(mode)
    validate_component(component)
    return [
        f"project={PROJECT}",
        f"run_id={run_id}",
        f"mode={mode}",
        f"component={component}",
        f"ttl={ttl}",
    ]


def normalize_image_project_tag(value: object, label: str = "image_project_tag") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty tag value")
    normalized = value.strip()
    if "=" in normalized:
        key = normalized.split("=", 1)[0].strip().lower()
        if key in RESERVED_TAG_KEYS:
            raise ValueError(f"{label} must not configure internal lifecycle tag key: {key}")
        raise ValueError(f"{label} must be a tag value, not a key=value tag")
    return normalized


def generate_artifact_tags(*, image_project_tag: str | None = None) -> list[str]:
    """Generate tags for captured custom image artifacts."""
    project_tag = normalize_image_project_tag(image_project_tag or PROJECT)
    return [f"project={project_tag}"]


def tags_to_dict(tags: list[str] | dict[str, str]) -> dict[str, str]:
    if isinstance(tags, dict):
        return {str(key): str(value) for key, value in tags.items()}

    parsed: dict[str, str] = {}
    for tag in tags:
        if "=" not in tag:
            continue
        key, value = tag.split("=", 1)
        parsed[key] = value
    return parsed


def component_for_mode(mode: str) -> str:
    return "deploy" if mode == "deploy" else "capture"


def create_manifest(
    *,
    command: str,
    mode: str,
    regions: list[str],
    component: str | None = None,
    run_id: str | None = None,
    ttl: str | None = None,
    image_project_tag: str | None = None,
    dry_run: bool = True,
    status: str = "planned",
) -> dict[str, Any]:
    """Create a JSON-compatible manifest for a CLI command."""
    validate_mode(mode)
    if not regions and command != "cleanup":
        raise ValueError("at least one non-empty --region is required")

    created_at = format_timestamp(utc_now())
    manifest_run_id = run_id or f"run-{uuid4().hex[:12]}"
    manifest_ttl = ttl or default_ttl()
    manifest_component = component or component_for_mode(mode)
    validate_component(manifest_component)
    lifecycle_tags = generate_tags(
        run_id=manifest_run_id,
        mode=mode,
        component=manifest_component,
        ttl=manifest_ttl,
    )
    artifact_tags = generate_artifact_tags(image_project_tag=image_project_tag)

    return {
        "schema_version": SCHEMA_VERSION,
        "project": PROJECT,
        "command": command,
        "mode": mode,
        "run_id": manifest_run_id,
        "regions": regions,
        "created_at": created_at,
        "ttl": manifest_ttl,
        "dry_run": dry_run,
        "status": status,
        "tags": lifecycle_tags,
        "lifecycle_tags": lifecycle_tags,
        "artifact_tags": artifact_tags,
        "planned_actions": [
            {
                "action": command,
                "region": region,
                "component": manifest_component,
                "mutates": False,
                "tags": lifecycle_tags,
                "lifecycle_tags": lifecycle_tags,
            }
            for region in regions
        ],
    }


def serialize_manifest(manifest: dict[str, Any]) -> str:
    """Serialize a sanitized manifest with stable formatting."""
    return json.dumps(redact(manifest), indent=2, sort_keys=True) + "\n"
