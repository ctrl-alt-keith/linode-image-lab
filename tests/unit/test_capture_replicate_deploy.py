from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from linode_image_lab.capture_replicate_deploy import (
    CaptureReplicateDeployError,
    capture_replicate_deploy_plan,
)
from linode_image_lab.linode_api import LinodeApiError, LinodePreflightError
from linode_image_lab.manifest import serialize_manifest
from linode_image_lab.user_data import DeployUserData


class FakeCaptureReplicateDeployClient:
    def __init__(
        self,
        *,
        image_regions: list[dict[str, object]] | None = None,
        fail_replica_wait: bool = False,
        fail_deploy_regions: set[str] | None = None,
        fail_capture_disk: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.image_regions = image_regions if image_regions is not None else [{"region": "us-east", "status": "available"}]
        self.fail_replica_wait = fail_replica_wait
        self.fail_deploy_regions = fail_deploy_regions or set()
        self.fail_capture_disk = fail_capture_disk
        self.capture_tags: list[str] = []
        self.image_tags: list[str] = []
        self.submitted_regions: list[str] = []
        self.created_regions: list[str] = []
        self.deleted: list[int] = []
        self.deploy_firewall_ids: list[int | None] = []
        self.deploy_authorized_keys: list[list[str] | None] = []
        self.deploy_metadata_user_data: list[str | None] = []
        self.deploy_instance_ids: set[int] = set()
        self.instance_regions: dict[int, str] = {}
        self.instance_tags: dict[int, list[str]] = {}
        self.deploy_count = 0
        self.current_region = ""

    def preflight(self) -> None:
        self.calls.append("preflight")

    def preflight_region(self, region: str) -> None:
        self.calls.append(f"preflight_region:{region}")
        self.current_region = region

    def preflight_instance_type(self, instance_type: str) -> None:
        self.calls.append(f"preflight_instance_type:{instance_type}")

    def preflight_image(self, image_id: str) -> None:
        self.calls.append(f"preflight_image:{image_id}")
        if image_id == "private/789" and self.current_region in self.fail_deploy_regions:
            raise LinodePreflightError(f"deploy image unavailable in {self.current_region}")

    def preflight_firewall(self, firewall_id: int) -> None:
        self.calls.append(f"preflight_firewall:{firewall_id}")

    def create_instance(
        self,
        *,
        region: str,
        source_image: str,
        instance_type: str,
        label: str,
        tags: list[str],
        root_password: str,
        firewall_id: int | None = None,
        authorized_keys: list[str] | None = None,
        metadata_user_data: str | None = None,
    ) -> dict[str, object]:
        self.created_regions.append(region)
        if source_image == "private/789":
            linode_id = 321 + self.deploy_count
            self.deploy_count += 1
            self.calls.append(f"create_deploy_instance:{region}")
            self.deploy_firewall_ids.append(firewall_id)
            self.deploy_authorized_keys.append(authorized_keys)
            self.deploy_metadata_user_data.append(metadata_user_data)
            self.deploy_instance_ids.add(linode_id)
        else:
            linode_id = 123
            self.calls.append(f"create_capture_source:{region}")
            self.capture_tags = tags
        self.instance_regions[linode_id] = region
        self.instance_tags[linode_id] = tags
        return {
            "linode_id": linode_id,
            "label": label,
            "region": region,
            "status": "provisioning",
            "tags": tags,
        }

    def wait_instance_ready(self, linode_id: int) -> dict[str, object]:
        is_deploy = linode_id in self.deploy_instance_ids
        self.calls.append("wait_deploy_instance_ready" if is_deploy else "wait_capture_source_ready")
        return {
            "linode_id": linode_id,
            "region": self.instance_regions[linode_id],
            "status": "running",
            "tags": self.instance_tags[linode_id],
        }

    def list_disks(self, linode_id: int) -> list[dict[str, object]]:
        self.calls.append("list_disks")
        if self.fail_capture_disk:
            raise LinodePreflightError("capture source disk unavailable")
        return [{"disk_id": 456}]

    def shutdown_instance(self, linode_id: int) -> dict[str, object]:
        self.calls.append("shutdown_capture_source")
        return {"linode_id": linode_id}

    def wait_instance_offline(self, linode_id: int) -> dict[str, object]:
        self.calls.append("wait_capture_source_offline")
        return {
            "linode_id": linode_id,
            "region": self.instance_regions[linode_id],
            "status": "offline",
            "tags": self.instance_tags[linode_id],
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
        return {"image_id": "private/789", "label": label, "status": "creating", "tags": tags}

    def wait_image_available(self, image_id: str) -> dict[str, object]:
        self.calls.append("wait_image_available")
        return {"image_id": image_id, "status": "available", "tags": self.image_tags}

    def get_image_details(self, image_id: str) -> dict[str, object]:
        self.calls.append("get_image_details")
        return {"image_id": image_id, "status": "available", "regions": list(self.image_regions)}

    def replicate_image(self, *, image_id: str, regions: list[str]) -> dict[str, object]:
        self.calls.append("replicate_image")
        self.submitted_regions = list(regions)
        return {
            "image_id": image_id,
            "status": "pending",
            "regions": [{"region": region, "status": "pending replication"} for region in regions],
        }

    def wait_image_regions_available(self, image_id: str, regions: list[str]) -> dict[str, object]:
        self.calls.append("wait_image_regions_available")
        if self.fail_replica_wait:
            raise LinodeApiError("timed out waiting for Linode resource readiness")
        return {
            "image_id": image_id,
            "status": "available",
            "regions": [{"region": region, "status": "available"} for region in self.submitted_regions],
        }

    def delete_instance(self, linode_id: int) -> dict[str, object]:
        self.calls.append("delete_deploy_instance" if linode_id in self.deploy_instance_ids else "delete_capture_source")
        self.deleted.append(linode_id)
        return {"linode_id": linode_id, "deleted": True}


class ExplodingClient:
    def preflight(self) -> None:
        raise AssertionError("dry-run must not call the client")


class CaptureReplicateDeployTests(unittest.TestCase):
    def test_dry_run_does_not_read_token_or_call_provider(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            manifest = capture_replicate_deploy_plan(
                regions=["us-sea", "us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=ExplodingClient(),
            )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "dry-run")
        self.assertEqual(manifest["summary"]["capture_region"], "us-sea")
        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-sea", "us-east"])
        self.assertEqual(manifest["replication_plan"]["replication_target_regions"], ["us-sea", "us-east"])
        self.assertEqual(manifest["provider_calls"], "not_attempted")

    def test_execute_captures_in_first_region_then_replicates_and_deploys(self) -> None:
        client = FakeCaptureReplicateDeployClient(
            image_regions=[
                {"region": "us-east", "status": "available"},
                {"region": "us-west", "status": "available"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-sea", "us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/alpine3.23",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["summary"]["capture_region"], "us-sea")
        self.assertEqual(client.created_regions[0], "us-sea")
        self.assertEqual(client.submitted_regions, ["us-east", "us-west", "us-sea"])
        self.assertLess(client.calls.index("wait_image_regions_available"), client.calls.index("create_deploy_instance:us-sea"))
        self.assertEqual(set(manifest["deploy_results"]), {"us-sea", "us-east"})
        self.assertEqual(manifest["replication"]["replica_status_checks"]["status"], "succeeded")
        self.assertEqual(manifest["capture"]["cleanup"]["preserved"][0]["reason"], "deliverable")

    def test_execute_fails_closed_when_existing_image_regions_are_missing(self) -> None:
        client = FakeCaptureReplicateDeployClient(image_regions=[])

        with self.assertRaises(CaptureReplicateDeployError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-sea", "us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=client,
            )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertNotIn("replicate_image", client.calls)
        self.assertNotIn("create_deploy_instance:us-sea", client.calls)
        self.assertIn("refusing replication", manifest["errors"][0])
        self.assertEqual(manifest["capture"]["cleanup"]["deleted"][0]["linode_id"], 123)

    def test_execute_fails_closed_when_replica_wait_times_out(self) -> None:
        client = FakeCaptureReplicateDeployClient(fail_replica_wait=True)

        with self.assertRaises(CaptureReplicateDeployError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-sea", "us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=client,
            )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(client.submitted_regions, ["us-east", "us-sea"])
        self.assertNotIn("create_deploy_instance:us-sea", client.calls)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["capture"]["cleanup"]["deleted"][0]["linode_id"], 123)

    def test_execute_cleans_up_after_capture_failure(self) -> None:
        client = FakeCaptureReplicateDeployClient(fail_capture_disk=True)

        with self.assertRaises(CaptureReplicateDeployError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-sea", "us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=client,
            )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(client.deleted, [123])
        self.assertEqual(manifest["capture"]["cleanup"]["deleted"][0]["linode_id"], 123)
        self.assertNotIn("replicate_image", client.calls)

    def test_execute_records_partial_deploy_failure_and_cleans_successes(self) -> None:
        client = FakeCaptureReplicateDeployClient(fail_deploy_regions={"us-east"})

        with self.assertRaises(CaptureReplicateDeployError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-sea", "us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=client,
            )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["summary"]["succeeded"], ["us-sea"])
        self.assertEqual(manifest["summary"]["failed"], ["us-east"])
        self.assertIn(321, client.deleted)
        self.assertIn(123, client.deleted)

    def test_execute_forwards_firewall_authorized_keys_and_user_data_to_deploys(self) -> None:
        client = FakeCaptureReplicateDeployClient()
        user_data = DeployUserData(
            encoded="I2Nsb3VkLWNvbmZpZwo=",
            byte_count=14,
            source="test-cloud-init.yaml",
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-sea"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/alpine3.23",
            instance_type="g6-nanode-1",
            firewall_id=12345,
            authorized_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test@example"],
            user_data=user_data,
            client=client,
        )

        self.assertEqual(client.deploy_firewall_ids, [12345])
        self.assertEqual(len(client.deploy_authorized_keys[0] or []), 1)
        self.assertEqual(client.deploy_metadata_user_data, ["I2Nsb3VkLWNvbmZpZwo="])
        self.assertEqual(manifest["deploy_config"]["firewall"]["firewall_id"], 12345)
        self.assertEqual(manifest["deploy_config"]["authorized_keys"]["authorized_key_count"], 1)
        self.assertEqual(manifest["deploy_config"]["user_data"]["source"], "test-cloud-init.yaml")

    def test_serialized_manifest_redacts_provider_identifiers(self) -> None:
        client = FakeCaptureReplicateDeployClient()
        manifest = capture_replicate_deploy_plan(
            regions=["us-sea"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/alpine3.23",
            instance_type="g6-nanode-1",
            firewall_id=12345,
            client=client,
        )

        serialized = serialize_manifest(manifest)
        self.assertNotIn("private/789", serialized)
        self.assertNotIn("12345", serialized)
        self.assertNotIn('"linode_id": 123', serialized)
        self.assertIn("[REDACTED]", serialized)


if __name__ == "__main__":
    unittest.main()
