from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from linode_image_lab.region_policy import (
    generate_region_policy_artifact,
    serialize_validation_report,
    validate_region_policy_artifact,
)


class FakeRegionClient:
    def __init__(self, regions: list[dict[str, object]]) -> None:
        self.regions = regions

    def list_regions(self) -> list[dict[str, object]]:
        return list(self.regions)


class RegionPolicyTests(unittest.TestCase):
    def test_generation_is_deterministic_and_normalizes_capabilities(self) -> None:
        client = FakeRegionClient(
            [
                {
                    "region": "us-sea",
                    "capabilities": ["Object Storage", "Linodes", "Linodes", "", 123],
                },
                {
                    "region": "gb-lon",
                    "capabilities": [" Linodes ", "Object Storage"],
                },
            ]
        )

        first = generate_region_policy_artifact(client=client)
        second = generate_region_policy_artifact(client=client)

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            """schema_version = 1

[provider_regions.gb-lon]
capabilities = ["Linodes", "Object Storage"]

[provider_regions.us-sea]
capabilities = ["Linodes", "Object Storage"]
""",
        )

    def test_generation_preserves_operator_groups_from_existing_policy(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "region-policy.toml"
        policy_path.write_text(
            """schema_version = 1
[provider_regions.old-region]
capabilities = ["Linodes"]
[groups.us]
regions = ["us-sea", "us-east"]
""",
            encoding="utf-8",
        )
        client = FakeRegionClient(
            [
                {"region": "us-east", "capabilities": ["Linodes"]},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"]},
            ]
        )

        artifact = generate_region_policy_artifact(client=client, existing_policy_path=policy_path)

        self.assertIn("[provider_regions.us-east]\ncapabilities = [\"Linodes\"]", artifact)
        self.assertIn("[groups.us]\nregions = [\"us-sea\", \"us-east\"]", artifact)
        self.assertNotIn("old-region", artifact)

    def test_generation_can_explicitly_isolate_provider_sections(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "region-policy.toml"
        policy_path.write_text(
            """schema_version = 1
[provider_regions.us-east]
capabilities = ["Linodes"]
[groups.us]
regions = ["us-east"]
""",
            encoding="utf-8",
        )
        client = FakeRegionClient([{"region": "us-east", "capabilities": ["Linodes"]}])

        artifact = generate_region_policy_artifact(
            client=client,
            existing_policy_path=policy_path,
            replace_groups=True,
        )

        self.assertIn("[provider_regions.us-east]", artifact)
        self.assertNotIn("[groups.us]", artifact)

    def test_validation_accepts_current_policy(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "region-policy.toml"
        policy_path.write_text(
            """schema_version = 1
[provider_regions.us-east]
capabilities = ["Linodes", "Object Storage"]
[provider_regions.us-sea]
capabilities = ["Linodes"]
[groups.us]
regions = ["us-sea", "us-east"]
""",
            encoding="utf-8",
        )
        client = FakeRegionClient(
            [
                {"region": "us-sea", "capabilities": ["Linodes"]},
                {"region": "us-east", "capabilities": ["Object Storage", "Linodes"]},
            ]
        )

        report = validate_region_policy_artifact(path=policy_path, client=client)

        self.assertTrue(report["valid"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["provider_region_count"], 2)
        self.assertEqual(report["group_count"], 1)

    def test_validation_rejects_unknown_provider_region(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "region-policy.toml"
        policy_path.write_text(
            """schema_version = 1
[provider_regions.us-east]
capabilities = ["Linodes"]
[provider_regions.us-ghost]
capabilities = ["Linodes"]
""",
            encoding="utf-8",
        )
        client = FakeRegionClient([{"region": "us-east", "capabilities": ["Linodes"]}])

        report = validate_region_policy_artifact(path=policy_path, client=client)

        self.assertFalse(report["valid"])
        self.assertIn("unknown_provider_region", {error["code"] for error in report["errors"]})

    def test_validation_rejects_group_referencing_unknown_region(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "region-policy.toml"
        policy_path.write_text(
            """schema_version = 1
[provider_regions.us-east]
capabilities = ["Linodes"]
[groups.us]
regions = ["us-east", "us-ghost"]
""",
            encoding="utf-8",
        )
        client = FakeRegionClient([{"region": "us-east", "capabilities": ["Linodes"]}])

        report = validate_region_policy_artifact(path=policy_path, client=client)

        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"][0]["code"], "unknown_group_region")

    def test_validation_rejects_stale_provider_capabilities(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "region-policy.toml"
        policy_path.write_text(
            """schema_version = 1
[provider_regions.us-east]
capabilities = ["Linodes"]
""",
            encoding="utf-8",
        )
        client = FakeRegionClient([{"region": "us-east", "capabilities": ["Linodes", "Object Storage"]}])

        report = validate_region_policy_artifact(path=policy_path, client=client)

        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"][0]["code"], "stale_provider_capabilities")

    def test_validation_rejects_malformed_policy(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "region-policy.toml"
        policy_path.write_text("schema_version =\n", encoding="utf-8")
        client = FakeRegionClient([{"region": "us-east", "capabilities": ["Linodes"]}])

        report = validate_region_policy_artifact(path=policy_path, client=client)

        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"][0]["code"], "malformed_policy")

    def test_validation_output_is_public_safe(self) -> None:
        report = {
            "schema_version": 1,
            "command": "region-policy",
            "action": "validate",
            "path": "policy/region-policy.toml",
            "valid": True,
            "status": "valid",
            "safety": {"mutates": False, "account_data": "not_read"},
            "errors": [],
        }

        serialized = serialize_validation_report(report)
        payload = json.loads(serialized)

        self.assertEqual(payload["safety"]["account_data"], "not_read")
        self.assertNotIn("LINODE_TOKEN", serialized)
        self.assertNotIn("private/", serialized)
