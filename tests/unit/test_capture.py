from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from linode_image_lab.capture import CaptureError, capture_plan
from linode_image_lab.linode_api import LinodePreflightError, LinodeTokenError
from linode_image_lab.manifest import serialize_manifest


def validation_check(manifest: dict[str, object], name: str) -> dict[str, object]:
    validation = manifest["validation"]
    assert isinstance(validation, dict)
    checks = validation["checks"]
    assert isinstance(checks, list)
    for check in checks:
        assert isinstance(check, dict)
        if check.get("name") == name:
            return check
    raise AssertionError(f"missing validation check: {name}")


class FakeLinodeClient:
    def __init__(self, *, missing_create_tags: bool = False, disks: list[dict[str, object]] | None = None) -> None:
        self.calls: list[str] = []
        self.create_tags: list[str] = []
        self.image_tags: list[str] = []
        self.deleted: list[int] = []
        self.missing_create_tags = missing_create_tags
        self.disks = disks if disks is not None else [{"disk_id": 456}]

    def preflight(self) -> None:
        self.calls.append("preflight")

    def preflight_region(self, region: str) -> None:
        self.calls.append("preflight_region")

    def preflight_instance_type(self, instance_type: str) -> None:
        self.calls.append("preflight_instance_type")

    def preflight_image(self, image_id: str) -> None:
        self.calls.append("preflight_image")

    def create_instance(
        self,
        *,
        region: str,
        source_image: str,
        instance_type: str,
        label: str,
        tags: list[str],
        root_password: str,
    ) -> dict[str, object]:
        self.calls.append("create_instance")
        self.create_tags = tags
        self.root_password_length = len(root_password)
        return {
            "linode_id": 123,
            "label": label,
            "region": region,
            "status": "provisioning",
            "tags": [] if self.missing_create_tags else tags,
        }

    def wait_instance_ready(self, linode_id: int) -> dict[str, object]:
        self.calls.append("wait_instance_ready")
        return {
            "linode_id": linode_id,
            "region": "us-east",
            "status": "running",
            "tags": [] if self.missing_create_tags else self.create_tags,
        }

    def list_disks(self, linode_id: int) -> list[dict[str, object]]:
        self.calls.append("list_disks")
        return self.disks

    def shutdown_instance(self, linode_id: int) -> dict[str, object]:
        self.calls.append("shutdown_instance")
        return {"linode_id": linode_id}

    def wait_instance_offline(self, linode_id: int) -> dict[str, object]:
        self.calls.append("wait_instance_offline")
        return {
            "linode_id": linode_id,
            "region": "us-east",
            "status": "offline",
            "tags": self.create_tags,
        }

    def capture_image(
        self,
        *,
        disk_id: int,
        label: str,
        tags: list[str],
        description: str,
        cloud_init: bool,
    ) -> dict[str, object]:
        self.calls.append("capture_image")
        self.image_tags = tags
        return {
            "image_id": "private/789",
            "label": label,
            "status": "creating",
            "tags": tags,
        }

    def wait_image_available(self, image_id: str) -> dict[str, object]:
        self.calls.append("wait_image_available")
        return {
            "image_id": image_id,
            "status": "available",
            "tags": self.image_tags,
        }

    def delete_instance(self, linode_id: int) -> dict[str, object]:
        self.calls.append("delete_instance")
        self.deleted.append(linode_id)
        return {"linode_id": linode_id, "deleted": True}


class ExplodingClient:
    def preflight(self) -> None:
        raise AssertionError("dry-run must not call the client")


class InvalidTokenClient(FakeLinodeClient):
    def preflight(self) -> None:
        self.calls.append("preflight")
        raise LinodeTokenError("LINODE_TOKEN is invalid, expired, or rejected by the Linode API")


class InvalidSourceImageClient(FakeLinodeClient):
    def preflight_image(self, image_id: str) -> None:
        self.calls.append("preflight_image")
        raise LinodePreflightError("requested image is unavailable")


class CaptureExecutionTests(unittest.TestCase):
    def test_dry_run_does_not_read_token_or_call_client(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            manifest = capture_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                client=ExplodingClient(),
            )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "dry-run")

    def test_execute_requires_token_without_client(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "LINODE_TOKEN"):
                capture_plan(
                    regions=["us-east"],
                    run_id="run-test",
                    ttl="2030-01-01T00:00:00Z",
                    execute=True,
                    source_image="linode/debian12",
                    instance_type="g6-nanode-1",
                )

    def test_execute_requires_single_region(self) -> None:
        with self.assertRaisesRegex(CaptureError, "exactly one non-empty --region"):
            capture_plan(
                regions=["us-east", "us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=FakeLinodeClient(),
            )

    def test_invalid_token_fails_before_mutation(self) -> None:
        client = InvalidTokenClient()

        with self.assertRaises(CaptureError) as raised:
            capture_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(client.calls, ["preflight"])
        self.assertEqual(raised.exception.manifest["status"], "failed")

    def test_provider_preflight_fails_before_mutation(self) -> None:
        client = InvalidSourceImageClient()

        with self.assertRaises(CaptureError) as raised:
            capture_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="private/invalid-source-image",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(
            client.calls,
            [
                "preflight",
                "preflight_region",
                "preflight_instance_type",
                "preflight_image",
            ],
        )
        self.assertEqual(raised.exception.manifest["status"], "failed")
        self.assertEqual(raised.exception.manifest["errors"], ["requested image is unavailable"])
        self.assertEqual(raised.exception.manifest["resources"], [])

    def test_execute_with_fake_client_records_expected_call_order(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(
            client.calls,
            [
                "preflight",
                "preflight_region",
                "preflight_instance_type",
                "preflight_image",
                "create_instance",
                "wait_instance_ready",
                "list_disks",
                "shutdown_instance",
                "wait_instance_offline",
                "capture_image",
                "wait_image_available",
                "delete_instance",
            ],
        )
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["capture_source"]["linode_id"], 123)
        self.assertEqual(manifest["custom_image"]["image_id"], "private/789")
        self.assertEqual(manifest["cleanup"]["status"], "deleted")
        self.assertEqual(manifest["validation"]["status"], "succeeded")
        self.assertEqual(
            manifest["validation"]["checks"],
            [
                {"name": "source_region_matches", "status": "succeeded", "target": "capture_source"},
                {"name": "source_required_tags_match", "status": "succeeded", "target": "capture_source"},
                {"name": "source_disk_found", "status": "succeeded", "target": "capture_source"},
                {"name": "custom_image_available", "status": "succeeded", "target": "custom_image"},
                {"name": "custom_image_required_tags_match", "status": "succeeded", "target": "custom_image"},
            ],
        )

    def test_execute_uses_exactly_one_suitable_disk(self) -> None:
        client = FakeLinodeClient(
            disks=[
                {"id": 111, "label": "swap", "filesystem": "swap", "status": "ready"},
                {"id": 456, "label": "Debian 12 Disk", "filesystem": "ext4", "status": "ready"},
            ],
        )

        manifest = capture_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["capture_source"]["disk_id"], 456)
        self.assertIn("capture_image", client.calls)

    def test_execute_fails_safely_when_capture_source_has_zero_disks(self) -> None:
        client = FakeLinodeClient(disks=[])

        with self.assertRaises(CaptureError) as raised:
            capture_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=client,
            )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["errors"], ["capture source has no suitable disk to image"])
        self.assertEqual(client.deleted, [123])
        self.assertNotIn("shutdown_instance", client.calls)
        self.assertNotIn("capture_image", client.calls)
        self.assertEqual(
            validation_check(manifest, "source_disk_found"),
            {
                "name": "source_disk_found",
                "status": "failed",
                "target": "capture_source",
                "failure_reason": "capture source has no suitable disk to image",
            },
        )

    def test_execute_fails_safely_when_capture_source_has_multiple_suitable_disks(self) -> None:
        client = FakeLinodeClient(
            disks=[
                {"id": 456, "label": "root", "filesystem": "ext4", "status": "ready"},
                {"id": 789, "label": "data", "filesystem": "ext4", "status": "ready"},
            ],
        )

        with self.assertRaises(CaptureError) as raised:
            capture_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=client,
            )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest["errors"], ["capture source has multiple suitable disks to image"])
        self.assertEqual(client.deleted, [123])
        self.assertNotIn("shutdown_instance", client.calls)
        self.assertNotIn("capture_image", client.calls)
        self.assertEqual(
            validation_check(manifest, "source_disk_found"),
            {
                "name": "source_disk_found",
                "status": "failed",
                "target": "capture_source",
                "failure_reason": "capture source has multiple suitable disks to image",
            },
        )

    def test_execute_ignores_swap_and_non_ready_disks(self) -> None:
        client = FakeLinodeClient(
            disks=[
                {"id": 111, "label": "Debian 12 Disk", "filesystem": "ext4", "status": "not ready"},
                {"id": 222, "label": "Swap Image", "filesystem": "raw", "status": "ready"},
                {"id": 456, "label": "root", "filesystem": "ext4", "status": "ready"},
            ],
        )

        manifest = capture_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(manifest["capture_source"]["disk_id"], 456)
        self.assertEqual(manifest["validation"]["status"], "succeeded")

    def test_execute_fails_when_only_disks_are_unsuitable(self) -> None:
        client = FakeLinodeClient(
            disks=[
                {"id": 111, "label": "swap", "filesystem": "swap", "status": "ready"},
                {"id": 222, "label": "root", "filesystem": "ext4", "status": "not ready"},
                {"label": "missing id", "filesystem": "ext4", "status": "ready"},
            ],
        )

        with self.assertRaises(CaptureError) as raised:
            capture_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(raised.exception.manifest["errors"], ["capture source has no suitable disk to image"])
        self.assertEqual(client.deleted, [123])
        self.assertNotIn("capture_image", client.calls)

    def test_execute_applies_required_tags_to_created_resources(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=client,
        )

        expected_tags = manifest["tags"]
        self.assertEqual(client.create_tags, expected_tags)
        self.assertEqual(client.image_tags, expected_tags)
        self.assertIn("mode=capture", expected_tags)
        self.assertIn("component=capture", expected_tags)

    def test_preserve_source_skips_success_cleanup_delete(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            preserve_source=True,
            client=client,
        )

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["status"], "preserved")
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "requested")
        self.assertEqual(manifest["steps"][-1]["action"], "preserve")

    def test_partial_failure_cleanup_skips_resource_missing_required_tags(self) -> None:
        client = FakeLinodeClient(missing_create_tags=True)

        with self.assertRaises(CaptureError) as raised:
            capture_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(client.deleted, [])
        self.assertIsNotNone(raised.exception.manifest)
        self.assertEqual(raised.exception.manifest["cleanup"]["status"], "preserved")
        self.assertEqual(raised.exception.manifest["cleanup"]["preserved"][0]["reason"], "tag_mismatch")
        self.assertEqual(raised.exception.manifest["validation"]["status"], "failed")
        self.assertEqual(
            validation_check(raised.exception.manifest, "source_required_tags_match"),
            {
                "name": "source_required_tags_match",
                "status": "failed",
                "target": "capture_source",
                "failure_reason": "created resource is missing required capture tags",
            },
        )
        self.assertEqual(validation_check(raised.exception.manifest, "source_disk_found")["status"], "pending")

    def test_serialized_execute_manifest_redacts_provider_ids(self) -> None:
        manifest = capture_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=FakeLinodeClient(),
        )

        exported = json.loads(serialize_manifest(manifest))

        self.assertEqual(exported["capture_source"]["linode_id"], "[REDACTED]")
        self.assertEqual(exported["capture_source"]["disk_id"], "[REDACTED]")
        self.assertEqual(exported["custom_image"]["image_id"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
