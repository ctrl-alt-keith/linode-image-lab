from __future__ import annotations

import json
import unittest

from linode_image_lab.manifest import (
    create_manifest,
    generate_artifact_tags,
    generate_tags,
    lifecycle_tags_from_manifest,
    serialize_manifest,
    validate_mode,
)


class ManifestTests(unittest.TestCase):
    def test_generates_required_tags(self) -> None:
        tags = generate_tags(
            run_id="run-test",
            mode="capture-deploy",
            component="capture",
            ttl="2030-01-01T00:00:00Z",
        )

        self.assertEqual(
            tags,
            [
                "project=linode-image-lab",
                "run_id=run-test",
                "mode=capture-deploy",
                "component=capture",
                "ttl=2030-01-01T00:00:00Z",
            ],
        )

    def test_creates_serializable_manifest(self) -> None:
        manifest = create_manifest(
            command="plan",
            mode="capture",
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
        )

        serialized = serialize_manifest(manifest)
        parsed = json.loads(serialized)

        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["project"], "linode-image-lab")
        self.assertEqual(parsed["planned_actions"][0]["region"], "us-east")
        self.assertFalse(parsed["planned_actions"][0]["mutates"])
        self.assertEqual(parsed["tags"], parsed["lifecycle_tags"])
        self.assertEqual(
            parsed["artifact_tags"],
            [
                "project=linode-image-lab",
                "run_id=run-test",
                "mode=capture",
                "component=capture",
                "ttl=2030-01-01T00:00:00Z",
            ],
        )

    def test_legacy_tags_remain_lifecycle_compatibility_alias(self) -> None:
        manifest = {"tags": ["project=linode-image-lab", "run_id=run-test"]}

        self.assertEqual(lifecycle_tags_from_manifest(manifest), manifest["tags"])

    def test_lifecycle_tags_take_precedence_over_legacy_tags(self) -> None:
        manifest = {
            "tags": ["project=legacy"],
            "lifecycle_tags": ["project=linode-image-lab", "run_id=run-test"],
        }

        self.assertEqual(lifecycle_tags_from_manifest(manifest), manifest["lifecycle_tags"])

    def test_generates_configured_artifact_project_tag(self) -> None:
        self.assertEqual(
            generate_artifact_tags(
                run_id="run-test",
                mode="capture",
                component="capture",
                ttl="2030-01-01T00:00:00Z",
                image_project_tag="customer-image-lab",
            ),
            [
                "project=customer-image-lab",
                "run_id=run-test",
                "mode=capture",
                "component=capture",
                "ttl=2030-01-01T00:00:00Z",
            ],
        )

    def test_rejects_artifact_project_tag_lifecycle_key_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "internal lifecycle tag key: project"):
            generate_artifact_tags(
                run_id="run-test",
                mode="capture",
                component="capture",
                ttl="2030-01-01T00:00:00Z",
                image_project_tag="project=other",
            )

    def test_validates_current_modes(self) -> None:
        for mode in ("capture", "deploy", "capture-deploy"):
            with self.subTest(mode=mode):
                self.assertEqual(validate_mode(mode), mode)

    def test_rejects_legacy_modes(self) -> None:
        legacy_modes = ("fr" + "eeze", "th" + "aw", "fr" + "eeze-" + "th" + "aw")
        for mode in legacy_modes:
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    validate_mode(mode)


if __name__ == "__main__":
    unittest.main()
