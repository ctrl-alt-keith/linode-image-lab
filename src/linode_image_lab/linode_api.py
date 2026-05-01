"""Placeholder Linode API boundary.

M1 intentionally performs no Linode mutations. Future milestones can replace
this boundary with a real client while keeping command modules small.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LinodeClient:
    token_env_name: str = "LINODE_TOKEN"

    def mutation_enabled(self) -> bool:
        return False

    def list_resources(self) -> list[dict[str, object]]:
        return []
