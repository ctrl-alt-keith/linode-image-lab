from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from linode_image_lab.capture import CaptureError, capture_plan
from linode_image_lab.linode_api import LinodeTokenError
from linode_image_lab.manifest import serialize_manifest


class FakeLinodeClient:
    def __init__(self, *, missing_create_tags: bool = False) -> None:
        self.calls: list[str] = []
        self.create_tags: list[str] = []
        self.image_tags: list[str] = []
        self.deleted: list[int] = []
        self.missing_create_tags = missing_create_tags

    def preflight(self) -> None:
        self.calls.append("preflight")

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
        return [{"disk_id": 456}]

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
        raise LinodeTokenError("LINODE_TOKEN was rejected by the Linode API")


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
        with self.assertRaisesRegex(CaptureError, "exactly one region"):
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
        self.assertEqual(raised.exception.manifest["cleanup"]["status"], "skipped_tag_mismatch")

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
