from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from linode_image_lab.cleanup import select_cleanup_candidates


class CleanupSelectionTests(unittest.TestCase):
    def test_selects_only_expired_fully_tagged_resources(self) -> None:
        fixture = Path("tests/fixtures/sanitized/mock_resources.json")
        resources = json.loads(fixture.read_text(encoding="utf-8"))
        resources.append({"id": "untagged", "tags": ["project=linode-image-lab"]})

        selected = select_cleanup_candidates(
            resources,
            now=datetime(2026, 5, 1, tzinfo=UTC),
        )

        self.assertEqual([resource["id"] for resource in selected], ["resource-expired"])


if __name__ == "__main__":
    unittest.main()
