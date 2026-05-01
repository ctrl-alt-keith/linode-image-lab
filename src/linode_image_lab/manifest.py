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
    run_id: str | None = None,
    ttl: str | None = None,
    dry_run: bool = True,
    status: str = "planned",
) -> dict[str, Any]:
    """Create a JSON-compatible manifest for a CLI command."""
    validate_mode(mode)
    if not regions and command != "cleanup":
        raise ValueError("at least one region is required")

    created_at = format_timestamp(utc_now())
    manifest_run_id = run_id or f"run-{uuid4().hex[:12]}"
    manifest_ttl = ttl or default_ttl()
    component = component_for_mode(mode)
    tags = generate_tags(
        run_id=manifest_run_id,
        mode=mode,
        component=component,
        ttl=manifest_ttl,
    )

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
        "tags": tags,
        "planned_actions": [
            {
                "action": command,
                "region": region,
                "component": component,
                "mutates": False,
                "tags": tags,
            }
            for region in regions
        ],
    }


def serialize_manifest(manifest: dict[str, Any]) -> str:
    """Serialize a sanitized manifest with stable formatting."""
    return json.dumps(redact(manifest), indent=2, sort_keys=True) + "\n"
