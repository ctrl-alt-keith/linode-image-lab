from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from linode_image_lab.deploy import DeployError, deploy_plan
from linode_image_lab.linode_api import LinodeTokenError
from linode_image_lab.manifest import serialize_manifest


class FakeLinodeClient:
    def __init__(self, *, missing_create_tags: bool = False, status: str = "running") -> None:
        self.calls: list[str] = []
        self.create_tags: list[str] = []
        self.deleted: list[int] = []
        self.missing_create_tags = missing_create_tags
        self.status = status

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
        self.source_image = source_image
        self.instance_type = instance_type
        self.root_password_length = len(root_password)
        return {
            "linode_id": 321,
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
            "status": self.status,
            "tags": [] if self.missing_create_tags else self.create_tags,
        }

    def list_disks(self, linode_id: int) -> list[dict[str, object]]:
        raise AssertionError("deploy must not inspect disks")

    def shutdown_instance(self, linode_id: int) -> dict[str, object]:
        raise AssertionError("deploy must not shut down the instance")

    def wait_instance_offline(self, linode_id: int) -> dict[str, object]:
        raise AssertionError("deploy must not wait for offline status")

    def capture_image(
        self,
        *,
        disk_id: int,
        label: str,
        tags: list[str],
        description: str,
        cloud_init: bool,
    ) -> dict[str, object]:
        raise AssertionError("deploy must not capture an image")

    def wait_image_available(self, image_id: str) -> dict[str, object]:
        raise AssertionError("deploy must not wait on image availability")

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


class DeployExecutionTests(unittest.TestCase):
    def test_dry_run_does_not_read_token_or_call_client(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            manifest = deploy_plan(
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
                deploy_plan(
                    regions=["us-east"],
                    run_id="run-test",
                    ttl="2030-01-01T00:00:00Z",
                    execute=True,
                    image_id="private/789",
                    instance_type="g6-nanode-1",
                )

    def test_execute_requires_single_region(self) -> None:
        with self.assertRaisesRegex(DeployError, "exactly one region"):
            deploy_plan(
                regions=["us-east", "us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                instance_type="g6-nanode-1",
                client=FakeLinodeClient(),
            )

    def test_execute_requires_image_id(self) -> None:
        with self.assertRaisesRegex(DeployError, "--image-id"):
            deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                instance_type="g6-nanode-1",
                client=FakeLinodeClient(),
            )

    def test_execute_requires_type(self) -> None:
        with self.assertRaisesRegex(DeployError, "--type"):
            deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                client=FakeLinodeClient(),
            )

    def test_invalid_token_fails_before_mutation(self) -> None:
        client = InvalidTokenClient()

        with self.assertRaises(DeployError) as raised:
            deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(client.calls, ["preflight"])
        self.assertEqual(raised.exception.manifest["status"], "failed")

    def test_execute_with_fake_client_records_expected_call_order(self) -> None:
        client = FakeLinodeClient()

        manifest = deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            image_id="private/789",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(
            client.calls,
            [
                "preflight",
                "create_instance",
                "wait_instance_ready",
                "delete_instance",
            ],
        )
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["deploy_instance"]["linode_id"], 321)
        self.assertEqual(manifest["validation"]["status"], "succeeded")
        self.assertEqual(manifest["cleanup"]["status"], "deleted")

    def test_execute_applies_required_tags_to_created_resource(self) -> None:
        client = FakeLinodeClient()

        manifest = deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            image_id="private/789",
            instance_type="g6-nanode-1",
            client=client,
        )

        expected_tags = manifest["tags"]
        self.assertEqual(client.create_tags, expected_tags)
        self.assertIn("mode=deploy", expected_tags)
        self.assertIn("component=deploy", expected_tags)

    def test_preserve_instance_skips_success_cleanup_delete(self) -> None:
        client = FakeLinodeClient()

        manifest = deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            image_id="private/789",
            instance_type="g6-nanode-1",
            preserve_instance=True,
            client=client,
        )

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["status"], "preserved")

    def test_partial_failure_cleanup_skips_resource_missing_required_tags(self) -> None:
        client = FakeLinodeClient(missing_create_tags=True)

        with self.assertRaises(DeployError) as raised:
            deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(client.deleted, [])
        self.assertIsNotNone(raised.exception.manifest)
        self.assertEqual(raised.exception.manifest["cleanup"]["status"], "skipped_tag_mismatch")

    def test_partial_failure_deletes_tagged_resource_by_default(self) -> None:
        client = FakeLinodeClient(status="offline")

        with self.assertRaises(DeployError) as raised:
            deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(client.deleted, [321])
        self.assertIsNotNone(raised.exception.manifest)
        self.assertEqual(raised.exception.manifest["cleanup"]["status"], "deleted")

    def test_serialized_execute_manifest_redacts_provider_ids(self) -> None:
        manifest = deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            image_id="private/789",
            instance_type="g6-nanode-1",
            client=FakeLinodeClient(),
        )

        exported = json.loads(serialize_manifest(manifest))

        self.assertEqual(exported["deploy_source"]["image_id"], "[REDACTED]")
        self.assertEqual(exported["deploy_instance"]["linode_id"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
