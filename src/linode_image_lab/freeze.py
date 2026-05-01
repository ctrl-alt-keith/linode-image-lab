"""Freeze command placeholder."""

from __future__ import annotations

from typing import Any

from .manifest import create_manifest


def freeze_plan(*, regions: list[str], run_id: str | None = None, ttl: str | None = None) -> dict[str, Any]:
    manifest = create_manifest(
        command="freeze",
        mode="freeze",
        regions=regions,
        run_id=run_id,
        ttl=ttl,
        dry_run=True,
        status="placeholder",
    )
    manifest["message"] = "freeze is a non-mutating placeholder in M1"
    return manifest
