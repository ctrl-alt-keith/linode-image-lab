"""Cleanup selection logic for tagged resources."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .manifest import PROJECT, REQUIRED_TAG_KEYS, VALID_COMPONENTS, VALID_MODES, tags_to_dict


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
