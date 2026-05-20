"""Manifest and tag contract helpers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .redaction import redact

PROJECT = "linode-image-lab"
SCHEMA_VERSION = 1
VALID_MODES = {"capture", "deploy", "capture-deploy", "replicate"}
VALID_COMPONENTS = {"capture", "deploy", "replicate"}
REQUIRED_TAG_KEYS = ("project", "run_id", "mode", "component", "ttl")
RESERVED_TAG_KEYS = frozenset((*REQUIRED_TAG_KEYS, "lifecycle"))
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
RUN_ID_RE = re.compile(RUN_ID_PATTERN)
RELATIVE_TTL_RE = re.compile(r"^\s*(?P<amount>[1-9][0-9]*)\s*(?P<unit>[A-Za-z]+)\s*$")
RELATIVE_TTL_UNITS = {
    "s": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "m": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "h": "hours",
    "hour": "hours",
    "hours": "hours",
    "hr": "hours",
    "hrs": "hours",
    "d": "days",
    "day": "days",
    "days": "days",
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_ttl(now: datetime | None = None) -> str:
    base = now or utc_now()
    return format_timestamp(base + timedelta(hours=4))


def resolve_ttl(value: str | None, *, now: datetime | None = None) -> str:
    """Resolve absolute or relative TTL input to the UTC tag contract."""
    base = now or utc_now()
    if value is None:
        return default_ttl(base)

    text = value.strip()
    relative = parse_relative_ttl(text)
    if relative is not None:
        return format_timestamp(base + relative)

    absolute = parse_absolute_ttl(text)
    if absolute is not None:
        return format_timestamp(absolute)

    raise ValueError("ttl must be an absolute ISO-8601 timestamp or a relative duration like '1 day' or '24h'")


def parse_relative_ttl(value: str) -> timedelta | None:
    match = RELATIVE_TTL_RE.fullmatch(value)
    if match is None:
        return None
    unit = RELATIVE_TTL_UNITS.get(match.group("unit").lower())
    if unit is None:
        return None
    return timedelta(**{unit: int(match.group("amount"))})


def parse_absolute_ttl(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def validate_run_id(value: str, label: str = "run_id") -> str:
    if not isinstance(value, str) or RUN_ID_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be 1-64 characters, start with a letter or digit, "
            "and contain only letters, digits, dot, underscore, or hyphen"
        )
    return value


def generate_tags(*, run_id: str, mode: str, component: str, ttl: str) -> list[str]:
    """Generate rediscoverable tags for every modeled resource."""
    validate_run_id(run_id)
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


def generate_artifact_tags(
    *,
    run_id: str,
    mode: str,
    component: str,
    ttl: str,
    image_project_tag: str | None = None,
) -> list[str]:
    """Generate tags for captured custom image artifacts."""
    validate_run_id(run_id)
    validate_mode(mode)
    validate_component(component)
    project_tag = normalize_image_project_tag(image_project_tag or PROJECT)
    return [
        f"project={project_tag}",
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


def lifecycle_tags_from_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return lifecycle tags, accepting legacy schema-v1 manifests."""
    if "lifecycle_tags" in manifest:
        return list(manifest["lifecycle_tags"])
    return list(manifest["tags"])


def component_for_mode(mode: str) -> str:
    if mode == "replicate":
        return "replicate"
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

    created_now = utc_now()
    created_at = format_timestamp(created_now)
    manifest_run_id = validate_run_id(run_id) if run_id is not None else f"run-{uuid4().hex[:12]}"
    manifest_ttl = resolve_ttl(ttl, now=created_now)
    manifest_component = component or component_for_mode(mode)
    validate_component(manifest_component)
    lifecycle_tags = generate_tags(
        run_id=manifest_run_id,
        mode=mode,
        component=manifest_component,
        ttl=manifest_ttl,
    )
    artifact_tags = generate_artifact_tags(
        run_id=manifest_run_id,
        mode=mode,
        component=manifest_component,
        ttl=manifest_ttl,
        image_project_tag=image_project_tag,
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
        # `tags` is a schema-v1 compatibility alias for `lifecycle_tags`.
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
