"""Region parsing helpers."""

from __future__ import annotations

from collections.abc import Iterable


def parse_regions(value: str | Iterable[str] | None) -> list[str]:
    """Parse one or many region values into a stable, de-duplicated list."""
    if value is None:
        return []

    raw_values = [value] if isinstance(value, str) else list(value)
    regions: list[str] = []
    seen: set[str] = set()

    for raw_value in raw_values:
        for part in str(raw_value).split(","):
            region = part.strip().lower()
            if not region:
                continue
            if region in seen:
                continue
            seen.add(region)
            regions.append(region)

    return regions
