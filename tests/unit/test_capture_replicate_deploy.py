from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linode_image_lab.capture_replicate_deploy import (
    CaptureReplicateDeployError,
    capture_replicate_deploy_plan,
)
from linode_image_lab.linode_api import LinodeApiError, LinodePreflightError
from linode_image_lab.manifest import serialize_manifest
from linode_image_lab.region_policy import (
    DEFAULT_REGION_POLICY_PATH,
    RegionPolicyGroupResolutionError,
    generated_region_groups,
)
from linode_image_lab.user_data import DeployUserData


class FakeCaptureReplicateDeployClient:
    def __init__(
        self,
        *,
        image_regions: list[dict[str, object]] | None = None,
        fail_replicate_submit: bool = False,
        fail_replica_wait: bool = False,
        fail_deploy_regions: set[str] | None = None,
        fail_capture_disk: bool = False,
        region_capabilities: dict[str, list[str]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.image_regions = image_regions if image_regions is not None else [{"region": "us-east", "status": "available"}]
        self.fail_replicate_submit = fail_replicate_submit
        self.fail_replica_wait = fail_replica_wait
        self.fail_deploy_regions = fail_deploy_regions or set()
        self.fail_capture_disk = fail_capture_disk
        self.region_capabilities = region_capabilities or {}
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

    def get_region_details(self, region: str) -> dict[str, object]:
        self.calls.append(f"get_region_details:{region}")
        return {
            "region": region,
            "capabilities": self.region_capabilities.get(region, ["Linodes", "Object Storage"]),
        }

    def preflight_region_capability(self, region: str, capability: str) -> dict[str, object]:
        self.calls.append(f"preflight_region_capability:{region}:{capability}")
        details = self.get_region_details(region)
        capabilities = details.get("capabilities", [])
        if not isinstance(capabilities, list) or capability not in capabilities:
            raise LinodePreflightError(f"requested region {region} is missing required capability: {capability}")
        return details

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
        if self.fail_replicate_submit:
            raise LinodeApiError(
                "Linode API request failed with status 400",
                status_code=400,
                provider_errors=[
                    {
                        "reason": "Image private/789 cannot be replicated to requested region with token=secret",
                        "field": "regions",
                    }
                ],
            )
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


class FakeRegionPolicyClient:
    def __init__(self, regions: list[dict[str, object]]) -> None:
        self.regions = regions
        self.calls: list[str] = []

    def list_regions(self) -> list[dict[str, object]]:
        self.calls.append("list_regions")
        return list(self.regions)


class CaptureReplicateDeployTests(unittest.TestCase):
    def test_dry_run_resolves_deploy_group_to_deploy_targets(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={},
            groups={"operator_deploy": ["us-sea"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("linode_image_lab.linode_api.LinodeClient.from_env", side_effect=AssertionError("token lookup")),
        ):
            manifest = capture_replicate_deploy_plan(
                regions=[],
                deploy_groups=["operator_deploy"],
                replication_regions=["us-east"],
                region_policy_file=str(policy_path),
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                client=ExplodingClient(),
                region_policy_client=policy_client,
            )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["provider_calls"], "not_attempted")
        self.assertEqual(manifest["summary"]["explicit_deploy_regions"], [])
        self.assertEqual(manifest["summary"]["requested_deploy_groups"], ["operator_deploy"])
        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-sea"])
        self.assertEqual(manifest["summary"]["capture_region"], "us-sea")
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east"])
        self.assertEqual(
            manifest["deploy_plan"]["group_sources"],
            [{"group": "operator_deploy", "source": "groups", "regions": ["us-sea"]}],
        )
        self.assertEqual(manifest["region_policy"]["requested_deploy_groups"], ["operator_deploy"])

    def test_mixed_deploy_regions_and_deploy_groups_dedupe_deterministically(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={},
            groups={"operator_deploy": ["us-sea", "us-lax"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["US-EAST", "us-east", "us-sea"],
            deploy_groups=["operator_deploy", "operator_deploy"],
            replication_regions=["us-east"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-east", "us-sea", "us-lax"])
        self.assertEqual(manifest["summary"]["capture_region"], "us-east")
        self.assertEqual(manifest["deploy_plan"]["deploy_regions"], ["us-east", "us-sea", "us-lax"])

    def test_deploy_group_prefers_operator_group_over_generated_group(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={},
            groups={"country_us": ["us-lax"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-east"],
            deploy_groups=["country_us"],
            replication_regions=["us-east"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-east", "us-lax"])
        self.assertEqual(
            manifest["region_policy"]["deploy_group_sources"],
            [{"group": "country_us", "source": "groups", "regions": ["us-lax"]}],
        )

    def test_deploy_groups_and_replication_groups_do_not_cross_over(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ],
            generated_groups={},
            groups={
                "operator_deploy": ["us-west"],
                "operator_replication": ["us-east"],
            },
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=[],
            deploy_groups=["operator_deploy"],
            replication_groups=["operator_replication"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-west"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east"])
        self.assertNotIn("us-west", manifest["summary"]["replication_target_regions"])
        self.assertNotIn("us-east", manifest["summary"]["deploy_regions"])

    def test_no_replication_input_defaults_to_resolved_deploy_targets(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={},
            groups={"operator_deploy": ["us-sea"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-east"],
            deploy_groups=["operator_deploy"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-east", "us-sea"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east", "us-sea"])
        self.assertEqual(manifest["summary"]["replication_target_source"], "deploy_regions_default")
        self.assertTrue(manifest["summary"]["replication_enabled"])

    def test_replication_disabled_does_not_default_deploy_targets_to_replication_targets(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ],
            generated_groups={},
            groups={"operator_deploy": ["us-west"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-east"],
            deploy_groups=["operator_deploy"],
            replication_enabled=False,
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-east", "us-west"])
        self.assertFalse(manifest["summary"]["replication_enabled"])
        self.assertEqual(manifest["summary"]["replication_skip_reason"], "replication_enabled=false")
        self.assertEqual(manifest["summary"]["replication_target_regions"], [])
        self.assertEqual(manifest["summary"]["replication_target_source"], "replication_disabled")
        self.assertEqual(manifest["replication_plan"]["status"], "skipped")
        self.assertEqual(manifest["replication_plan"]["skip_reason"], "replication_enabled=false")
        self.assertEqual(manifest["region_policy"]["requested_deploy_groups"], ["operator_deploy"])

    def test_replication_disabled_rejects_replication_inputs(self) -> None:
        with self.assertRaises(CaptureReplicateDeployError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-east"],
                replication_regions=["us-sea"],
                replication_enabled=False,
            )

        self.assertIn("replication_enabled=false", str(raised.exception))

    def test_execute_with_deploy_groups_captures_and_deploys_resolved_deploy_targets_only(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={},
            groups={"operator_deploy": ["us-sea", "us-east"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )
        client = FakeCaptureReplicateDeployClient(image_regions=[{"region": "us-east", "status": "available"}])

        manifest = capture_replicate_deploy_plan(
            regions=[],
            deploy_groups=["operator_deploy"],
            replication_regions=["us-east"],
            region_policy_file=str(policy_path),
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/alpine3.23",
            instance_type="g6-nanode-1",
            client=client,
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["summary"]["capture_region"], "us-sea")
        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-sea", "us-east"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east"])
        self.assertEqual(client.created_regions[0], "us-sea")
        self.assertEqual(set(manifest["deploy_results"]), {"us-sea", "us-east"})
        self.assertEqual(client.submitted_regions, ["us-east"])

    def test_execute_with_replication_disabled_skips_replication_api_path(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ],
            generated_groups={},
            groups={"operator_deploy": ["us-west", "us-east"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ]
        )
        client = FakeCaptureReplicateDeployClient(region_capabilities={"us-west": ["Linodes"]})

        manifest = capture_replicate_deploy_plan(
            regions=[],
            deploy_groups=["operator_deploy"],
            replication_enabled=False,
            region_policy_file=str(policy_path),
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/alpine3.23",
            instance_type="g6-nanode-1",
            client=client,
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["status"], "succeeded")
        self.assertFalse(manifest["summary"]["replication_enabled"])
        self.assertEqual(manifest["replication"]["status"], "skipped")
        self.assertEqual(manifest["validation"]["replication"]["status"], "skipped")
        self.assertEqual(manifest["validation"]["status"], "succeeded")
        self.assertEqual(manifest["summary"]["replication_target_regions"], [])
        self.assertEqual(set(manifest["deploy_results"]), {"us-west", "us-east"})
        self.assertIn("create_deploy_instance:us-west", client.calls)
        self.assertIn("create_deploy_instance:us-east", client.calls)
        self.assertNotIn("get_image_details", client.calls)
        self.assertNotIn("replicate_image", client.calls)
        self.assertNotIn("wait_image_regions_available", client.calls)
        self.assertEqual([call for call in client.calls if call.startswith("get_region_details:")], [])

    def test_dry_run_resolves_generated_group_to_replication_targets(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={"country_us": ["us-east", "us-sea"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("linode_image_lab.linode_api.LinodeClient.from_env", side_effect=AssertionError("token lookup")),
        ):
            manifest = capture_replicate_deploy_plan(
                regions=["us-east"],
                replication_groups=["country_us"],
                region_policy_file=str(policy_path),
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                client=ExplodingClient(),
                region_policy_client=policy_client,
            )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(policy_client.calls, ["list_regions"])
        self.assertEqual(manifest["provider_calls"], "not_attempted")
        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-east"])
        self.assertEqual(manifest["summary"]["requested_replication_groups"], ["country_us"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east", "us-sea"])
        self.assertEqual(
            manifest["replication_plan"]["group_sources"],
            [{"group": "country_us", "source": "generated_groups", "regions": ["us-east", "us-sea"]}],
        )

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
        self.assertEqual(manifest["replication_plan"]["replication_target_source"], "deploy_regions_default")
        self.assertEqual(manifest["provider_calls"], "not_attempted")

    def test_unknown_group_fails_before_mutation(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[{"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"}],
            generated_groups={"country_us": ["us-east"]},
        )
        policy_client = FakeRegionPolicyClient(
            [{"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"}]
        )
        client = FakeCaptureReplicateDeployClient()

        with self.assertRaises(RegionPolicyGroupResolutionError):
            capture_replicate_deploy_plan(
                regions=["us-east"],
                replication_groups=["missing"],
                region_policy_file=str(policy_path),
                execute=True,
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=client,
                region_policy_client=policy_client,
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(client.created_regions, [])

    def test_stale_policy_validation_failure_fails_before_mutation(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[{"region": "us-east", "capabilities": ["Linodes"], "country": "us"}],
            generated_groups={"country_us": ["us-east"]},
        )
        policy_client = FakeRegionPolicyClient(
            [{"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"}]
        )
        client = FakeCaptureReplicateDeployClient()

        with self.assertRaises(RegionPolicyGroupResolutionError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-east"],
                replication_groups=["country_us"],
                region_policy_file=str(policy_path),
                execute=True,
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=client,
                region_policy_client=policy_client,
            )

        self.assertIn("stale_provider_capabilities", str(raised.exception))
        self.assertEqual(client.calls, [])

    def test_deploy_regions_are_not_auto_added_when_replication_inputs_exist(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ],
            generated_groups={"country_us_object_storage": ["us-east", "us-sea"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-west"],
            replication_groups=["country_us_object_storage"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-west"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east", "us-sea"])
        self.assertEqual(manifest["summary"]["replication_target_source"], "replication_inputs")
        self.assertNotIn("us-west", manifest["summary"]["replication_target_regions"])

    def test_default_region_policy_path_is_used_for_replication_groups(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        policy_path = root / DEFAULT_REGION_POLICY_PATH
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text(
            self.policy_text(
                provider_regions=[{"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"}],
                generated_groups={"country_us": ["us-east"]},
            ),
            encoding="utf-8",
        )
        policy_client = FakeRegionPolicyClient(
            [{"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"}]
        )

        original_cwd = Path.cwd()
        try:
            os.chdir(root)
            manifest = capture_replicate_deploy_plan(
                regions=["us-east"],
                replication_groups=["country_us"],
                region_policy_client=policy_client,
            )
        finally:
            os.chdir(original_cwd)

        self.assertEqual(manifest["region_policy"]["path"], "policy/region-policy.toml")

    def test_explicit_region_policy_file_override_is_used(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[{"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"}],
            generated_groups={"country_us": ["us-east"]},
        )
        policy_client = FakeRegionPolicyClient(
            [{"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"}]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-east"],
            replication_groups=["country_us"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["region_policy"]["path"], str(policy_path))

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
        self.assertEqual(
            manifest["replication"]["region_capability_checks"],
            {
                "required_capability": "Object Storage",
                "checks": [
                    {"region": "us-sea", "capability": "Object Storage", "status": "succeeded"},
                    {"region": "us-east", "capability": "Object Storage", "status": "succeeded"},
                ],
            },
        )
        self.assertEqual(client.submitted_regions, ["us-east", "us-west", "us-sea"])
        self.assertLess(client.calls.index("wait_image_regions_available"), client.calls.index("create_deploy_instance:us-sea"))
        self.assertEqual(set(manifest["deploy_results"]), {"us-sea", "us-east"})
        self.assertEqual(manifest["replication"]["replica_status_checks"]["status"], "succeeded")
        self.assertEqual(manifest["validation"]["replication"]["status"], "succeeded")
        self.assertEqual(
            manifest["validation"]["replication"]["region_capability_checks"],
            manifest["replication"]["region_capability_checks"],
        )
        self.assertEqual(manifest["capture"]["cleanup"]["preserved"][0]["reason"], "deliverable")

    def test_execute_deploys_only_to_explicit_deploy_regions(self) -> None:
        client = FakeCaptureReplicateDeployClient(
            image_regions=[{"region": "us-east", "status": "available"}],
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-east"],
            replication_regions=["us-sea", "us-lax"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/alpine3.23",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-east"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-sea", "us-lax"])
        self.assertEqual(client.submitted_regions, ["us-east", "us-sea", "us-lax"])
        self.assertIn("create_deploy_instance:us-east", client.calls)
        self.assertNotIn("create_deploy_instance:us-sea", client.calls)
        self.assertNotIn("create_deploy_instance:us-lax", client.calls)
        self.assertEqual(set(manifest["deploy_results"]), {"us-east"})

    def test_group_targets_do_not_check_deploy_region_capability_and_preserve_existing_region(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ],
            generated_groups={"country_us_object_storage": ["us-east", "us-sea"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ]
        )
        client = FakeCaptureReplicateDeployClient(
            image_regions=[{"region": "us-west", "status": "available"}],
            region_capabilities={
                "us-east": ["Linodes", "Object Storage"],
                "us-sea": ["Linodes", "Object Storage"],
                "us-west": ["Linodes"],
            },
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-west"],
            replication_groups=["country_us_object_storage"],
            region_policy_file=str(policy_path),
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/alpine3.23",
            instance_type="g6-nanode-1",
            client=client,
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-west"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east", "us-sea"])
        self.assertEqual(
            [call for call in client.calls if call.startswith("get_region_details:")],
            ["get_region_details:us-east", "get_region_details:us-sea"],
        )
        self.assertEqual(client.submitted_regions, ["us-west", "us-east", "us-sea"])
        self.assertEqual(
            manifest["replication"]["request"]["submitted_regions"],
            ["us-west", "us-east", "us-sea"],
        )
        self.assertIn("create_deploy_instance:us-west", client.calls)
        self.assertEqual(set(manifest["deploy_results"]), {"us-west"})

    def test_duplicate_explicit_and_group_regions_dedupe_deterministically(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={"country_us": ["us-sea", "us-lax"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["US-EAST", "us-east"],
            replication_regions=["us-east", "us-sea", "us-sea"],
            replication_groups=["country_us"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["deploy_regions"], ["us-east"])
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east", "us-sea", "us-lax"])

    def test_capability_validation_applies_to_group_resolved_targets(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ],
            generated_groups={"country_us": ["us-east", "us-west"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-west", "capabilities": ["Linodes"], "country": "us"},
            ]
        )
        client = FakeCaptureReplicateDeployClient(region_capabilities={"us-west": ["Linodes"]})

        with self.assertRaises(CaptureReplicateDeployError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-east"],
                replication_groups=["country_us"],
                region_policy_file=str(policy_path),
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/alpine3.23",
                instance_type="g6-nanode-1",
                client=client,
                region_policy_client=policy_client,
            )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-east", "us-west"])
        self.assertEqual(manifest["summary"]["failed"], ["us-west"])
        self.assertNotIn("create_capture_source:us-east", client.calls)

    def test_operator_and_generated_groups_both_resolve(self) -> None:
        policy_path = self.write_policy(
            provider_regions=[
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ],
            generated_groups={"country_us": ["us-east", "us-sea"]},
            groups={"operator_edge": ["us-lax"]},
        )
        policy_client = FakeRegionPolicyClient(
            [
                {"region": "us-east", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-lax", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
                {"region": "us-sea", "capabilities": ["Linodes", "Object Storage"], "country": "us"},
            ]
        )

        manifest = capture_replicate_deploy_plan(
            regions=["us-east"],
            replication_groups=["operator_edge", "country_us"],
            region_policy_file=str(policy_path),
            region_policy_client=policy_client,
        )

        self.assertEqual(manifest["summary"]["replication_target_regions"], ["us-lax", "us-east", "us-sea"])
        self.assertEqual(
            manifest["region_policy"]["group_sources"],
            [
                {"group": "operator_edge", "source": "groups", "regions": ["us-lax"]},
                {"group": "country_us", "source": "generated_groups", "regions": ["us-east", "us-lax", "us-sea"]},
            ],
        )

    def test_unsupported_replication_regions_fail_before_capture_after_full_check(self) -> None:
        client = FakeCaptureReplicateDeployClient(
            region_capabilities={
                "us-sea": ["Linodes", "Object Storage"],
                "us-west": ["Linodes"],
                "us-east": ["Linodes", "Object Storage"],
                "us-lax": ["Linodes"],
            }
        )

        with self.assertRaises(CaptureReplicateDeployError) as raised:
            capture_replicate_deploy_plan(
                regions=["us-sea", "us-west", "us-east", "us-lax"],
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
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(
            manifest["errors"],
            [
                "requested replication target regions us-west, us-lax are missing required capability: "
                "Object Storage"
            ],
        )
        self.assertEqual(
            manifest["replication"]["region_capability_checks"],
            {
                "required_capability": "Object Storage",
                "checks": [
                    {"region": "us-sea", "capability": "Object Storage", "status": "succeeded"},
                    {
                        "region": "us-west",
                        "capability": "Object Storage",
                        "status": "failed",
                        "missing_capability": "Object Storage",
                    },
                    {"region": "us-east", "capability": "Object Storage", "status": "succeeded"},
                    {
                        "region": "us-lax",
                        "capability": "Object Storage",
                        "status": "failed",
                        "missing_capability": "Object Storage",
                    },
                ],
            },
        )
        self.assertEqual(
            [call for call in client.calls if call.startswith("get_region_details:")],
            [
                "get_region_details:us-sea",
                "get_region_details:us-west",
                "get_region_details:us-east",
                "get_region_details:us-lax",
            ],
        )
        self.assertEqual(manifest["validation"]["replication"]["status"], "failed")
        self.assertEqual(manifest["summary"]["failed"], ["us-west", "us-lax"])
        self.assertEqual(manifest["summary"]["succeeded"], [])
        self.assertEqual(manifest["capture"], {})
        self.assertEqual(manifest["deploy_results"], {})
        self.assertEqual(client.created_regions, [])
        self.assertNotIn("create_capture_source:us-sea", client.calls)
        self.assertNotIn("capture_image", client.calls)
        self.assertNotIn("replicate_image", client.calls)
        self.assertNotIn("create_deploy_instance:us-sea", client.calls)

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

    def test_execute_records_provider_error_details_when_replication_submit_fails(self) -> None:
        client = FakeCaptureReplicateDeployClient(fail_replicate_submit=True)

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
        self.assertEqual(client.calls.count("replicate_image"), 1)
        self.assertNotIn("wait_image_regions_available", client.calls)
        self.assertNotIn("create_deploy_instance:us-sea", client.calls)
        self.assertEqual(manifest["replication"]["status"], "failed")
        self.assertEqual(
            manifest["replication"]["provider_error"],
            {
                "status_code": 400,
                "errors": [
                    {
                        "reason": "Image [REDACTED] cannot be replicated to requested region with token=[REDACTED]",
                        "field": "regions",
                    }
                ],
            },
        )
        self.assertEqual(manifest["provider_error"], manifest["replication"]["provider_error"])
        self.assertEqual(manifest["capture"]["cleanup"]["deleted"][0]["linode_id"], 123)
        self.assertEqual(manifest["deploy_results"], {})
        self.assertNotIn("private/789", serialize_manifest(manifest))

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

    def write_policy(
        self,
        *,
        provider_regions: list[dict[str, object]],
        generated_groups: dict[str, list[str]],
        groups: dict[str, list[str]] | None = None,
    ) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "region-policy.toml"
        path.write_text(
            self.policy_text(
                provider_regions=provider_regions,
                generated_groups=generated_groups,
                groups=groups,
            ),
            encoding="utf-8",
        )
        return path

    def policy_text(
        self,
        *,
        provider_regions: list[dict[str, object]],
        generated_groups: dict[str, list[str]],
        groups: dict[str, list[str]] | None = None,
    ) -> str:
        lines = ["schema_version = 1", ""]
        for region in provider_regions:
            lines.append(f"[provider_regions.{region['region']}]")
            capabilities = ", ".join(f'"{capability}"' for capability in region["capabilities"])
            lines.append(f"capabilities = [{capabilities}]")
            lines.append("")
        rendered_generated_groups = generated_region_groups(provider_regions)
        for group, regions in sorted(rendered_generated_groups.items()):
            lines.append(f"[generated_groups.{group}]")
            lines.append("regions = [" + ", ".join(f'"{region}"' for region in regions) + "]")
            lines.append("")
        for group, regions in sorted((groups or {}).items()):
            lines.append(f"[groups.{group}]")
            lines.append("regions = [" + ", ".join(f'"{region}"' for region in regions) + "]")
            lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":
    unittest.main()
