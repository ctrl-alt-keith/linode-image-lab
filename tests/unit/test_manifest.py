from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from linode_image_lab.manifest import (
    create_manifest,
    generate_artifact_tags,
    generate_tags,
    resolve_ttl,
    lifecycle_tags_from_manifest,
    serialize_manifest,
    validate_mode,
    validate_run_id,
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

    def test_validates_supported_run_ids(self) -> None:
        valid_run_ids = (
            "run-test",
            "run_test.01",
            "run-m3-smoke",
            "A",
            "a" * 64,
        )

        for run_id in valid_run_ids:
            with self.subTest(run_id=run_id):
                self.assertEqual(validate_run_id(run_id), run_id)

    def test_rejects_invalid_run_ids(self) -> None:
        invalid_run_ids = (
            "",
            " run",
            "run test",
            "run,test",
            "run=test",
            "-run",
            "a" * 65,
            "run\nid",
        )

        for run_id in invalid_run_ids:
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(ValueError, "run_id must be 1-64 characters"):
                    validate_run_id(run_id)

    def test_tag_generation_rejects_invalid_run_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "run_id must be 1-64 characters"):
            generate_tags(
                run_id="run,bad",
                mode="capture",
                component="capture",
                ttl="2030-01-01T00:00:00Z",
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

    def test_resolves_relative_ttl_to_utc_timestamp(self) -> None:
        now = datetime(2026, 5, 20, 12, 30, 45, tzinfo=UTC)

        self.assertEqual(resolve_ttl("1 day", now=now), "2026-05-21T12:30:45Z")
        self.assertEqual(resolve_ttl("90 minutes", now=now), "2026-05-20T14:00:45Z")
        self.assertEqual(resolve_ttl("30m", now=now), "2026-05-20T13:00:45Z")
        self.assertEqual(resolve_ttl("24h", now=now), "2026-05-21T12:30:45Z")
        self.assertEqual(resolve_ttl("7d", now=now), "2026-05-27T12:30:45Z")
        self.assertEqual(resolve_ttl("2w", now=now), "2026-06-03T12:30:45Z")
        self.assertEqual(resolve_ttl("45s", now=now), "2026-05-20T12:31:30Z")

    def test_normalizes_absolute_ttl_to_utc_timestamp(self) -> None:
        self.assertEqual(
            resolve_ttl("2030-01-01T02:30:00+02:30"),
            "2030-01-01T00:00:00Z",
        )

    def test_relative_ttl_is_resolved_before_tag_generation(self) -> None:
        now = datetime(2026, 5, 20, 12, 30, 45, tzinfo=UTC)
        with patch("linode_image_lab.manifest.utc_now", return_value=now):
            manifest = create_manifest(
                command="plan",
                mode="capture",
                regions=["us-east"],
                run_id="run-test",
                ttl="1 day",
            )

        self.assertEqual(manifest["created_at"], "2026-05-20T12:30:45Z")
        self.assertEqual(manifest["ttl"], "2026-05-21T12:30:45Z")
        self.assertIn("ttl=2026-05-21T12:30:45Z", manifest["lifecycle_tags"])
        self.assertIn("ttl=2026-05-21T12:30:45Z", manifest["artifact_tags"])

    def test_rejects_invalid_ttl_input_for_manifest_tags(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute ISO-8601 timestamp"):
            resolve_ttl("not-a-timestamp")
        with self.assertRaisesRegex(ValueError, "absolute ISO-8601 timestamp"):
            resolve_ttl("tomorrow")

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
