from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from linode_image_lab.capture_deploy import CaptureDeployError, capture_deploy_plan
from linode_image_lab.manifest import serialize_manifest


def validation_check(manifest: dict[str, object], name: str, target: str) -> dict[str, object]:
    validation = manifest["validation"]
    assert isinstance(validation, dict)
    checks = validation["checks"]
    assert isinstance(checks, list)
    for check in checks:
        assert isinstance(check, dict)
        if check.get("name") == name and check.get("target") == target:
            return check
    raise AssertionError(f"missing validation check: {name} target={target}")


class FakeLinodeClient:
    def __init__(self, *, missing_deploy_tags: bool = False) -> None:
        self.calls: list[str] = []
        self.capture_tags: list[str] = []
        self.deploy_tags: list[str] = []
        self.image_tags: list[str] = []
        self.deleted: list[int] = []
        self.missing_deploy_tags = missing_deploy_tags

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
        if source_image == "private/789":
            self.calls.append("create_deploy_instance")
            self.deploy_tags = tags
            return {
                "linode_id": 321,
                "label": label,
                "region": region,
                "status": "provisioning",
                "tags": [] if self.missing_deploy_tags else tags,
            }

        self.calls.append("create_capture_source")
        self.capture_tags = tags
        return {
            "linode_id": 123,
            "label": label,
            "region": region,
            "status": "provisioning",
            "tags": tags,
        }

    def wait_instance_ready(self, linode_id: int) -> dict[str, object]:
        if linode_id == 321:
            self.calls.append("wait_deploy_instance_ready")
            return {
                "linode_id": linode_id,
                "region": "us-east",
                "status": "running",
                "tags": [] if self.missing_deploy_tags else self.deploy_tags,
            }

        self.calls.append("wait_capture_source_ready")
        return {
            "linode_id": linode_id,
            "region": "us-east",
            "status": "running",
            "tags": self.capture_tags,
        }

    def list_disks(self, linode_id: int) -> list[dict[str, object]]:
        self.calls.append("list_disks")
        return [{"disk_id": 456}]

    def shutdown_instance(self, linode_id: int) -> dict[str, object]:
        self.calls.append("shutdown_capture_source")
        return {"linode_id": linode_id}

    def wait_instance_offline(self, linode_id: int) -> dict[str, object]:
        self.calls.append("wait_capture_source_offline")
        return {
            "linode_id": linode_id,
            "region": "us-east",
            "status": "offline",
            "tags": self.capture_tags,
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
        if linode_id == 123:
            self.calls.append("delete_capture_source")
        else:
            self.calls.append("delete_deploy_instance")
        self.deleted.append(linode_id)
        return {"linode_id": linode_id, "deleted": True}


class ExplodingClient:
    def preflight(self) -> None:
        raise AssertionError("dry-run must not call the client")


class CaptureDeployExecutionTests(unittest.TestCase):
    def test_dry_run_does_not_read_token_or_call_execution(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("linode_image_lab.capture_deploy.execute_capture") as capture,
            patch("linode_image_lab.capture_deploy.execute_deploy") as deploy,
        ):
            manifest = capture_deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                client=ExplodingClient(),
            )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "dry-run")
        capture.assert_not_called()
        deploy.assert_not_called()

    def test_execute_requires_token_without_client(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "LINODE_TOKEN"):
                capture_deploy_plan(
                    regions=["us-east"],
                    run_id="run-test",
                    ttl="2030-01-01T00:00:00Z",
                    execute=True,
                    source_image="linode/debian12",
                    instance_type="g6-nanode-1",
                )

    def test_execute_requires_inputs_before_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CaptureDeployError, "--source-image"):
                capture_deploy_plan(
                    regions=["us-east"],
                    run_id="run-test",
                    ttl="2030-01-01T00:00:00Z",
                    execute=True,
                    instance_type="g6-nanode-1",
                )

    def test_execute_requires_single_region(self) -> None:
        with self.assertRaisesRegex(CaptureDeployError, "exactly one non-empty --region"):
            capture_deploy_plan(
                regions=["us-east", "us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=FakeLinodeClient(),
            )

    def test_execute_with_fake_client_records_capture_deploy_cleanup_order(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_deploy_plan(
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
                "create_capture_source",
                "wait_capture_source_ready",
                "list_disks",
                "shutdown_capture_source",
                "wait_capture_source_offline",
                "capture_image",
                "wait_image_available",
                "preflight",
                "create_deploy_instance",
                "wait_deploy_instance_ready",
                "delete_capture_source",
                "delete_deploy_instance",
            ],
        )
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["capture"]["custom_image"]["image_id"], "private/789")
        self.assertEqual(manifest["capture"]["validation"]["status"], "succeeded")
        self.assertEqual(manifest["deploy"]["deploy_source"]["image_id"], "private/789")
        self.assertEqual(manifest["validation"]["status"], "succeeded")
        self.assertEqual(manifest["capture"]["validation"]["checks"][0]["target"], "capture_source")
        self.assertEqual(manifest["deploy"]["validation"]["checks"][0]["target"], "deploy_instance")
        self.assertEqual(len(manifest["validation"]["checks"]), 8)
        self.assertEqual(
            validation_check(manifest, "custom_image_available", "capture.custom_image"),
            {"name": "custom_image_available", "status": "succeeded", "target": "capture.custom_image"},
        )
        self.assertEqual(
            validation_check(manifest, "required_tags_match", "deploy.deploy_instance"),
            {"name": "required_tags_match", "status": "succeeded", "target": "deploy.deploy_instance"},
        )
        self.assertEqual(manifest["cleanup"]["status"], "completed")
        self.assertEqual(client.deleted, [123, 321])

    def test_execute_applies_component_tags_to_created_resources(self) -> None:
        client = FakeLinodeClient()

        capture_deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertIn("mode=capture-deploy", client.capture_tags)
        self.assertIn("component=capture", client.capture_tags)
        self.assertIn("mode=capture-deploy", client.deploy_tags)
        self.assertIn("component=deploy", client.deploy_tags)

    def test_preserve_instance_keeps_deploy_instance_only(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            preserve_instance=True,
            client=client,
        )

        self.assertEqual(client.deleted, [123])
        self.assertEqual(manifest["deploy"]["cleanup"]["status"], "preserved")
        self.assertEqual(manifest["deploy"]["cleanup"]["preserved"][0]["reason"], "requested")
        self.assertEqual(manifest["deploy"]["steps"][-1]["action"], "preserve")

    def test_cleanup_skips_deploy_resource_missing_current_run_tags(self) -> None:
        client = FakeLinodeClient(missing_deploy_tags=True)

        with self.assertRaises(CaptureDeployError) as raised:
            capture_deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(client.deleted, [123])
        self.assertIsNotNone(raised.exception.manifest)
        self.assertEqual(raised.exception.manifest["deploy"]["cleanup"]["status"], "preserved")
        self.assertEqual(raised.exception.manifest["deploy"]["cleanup"]["preserved"][0]["reason"], "tag_mismatch")
        self.assertEqual(raised.exception.manifest["validation"]["status"], "failed")
        self.assertEqual(
            validation_check(raised.exception.manifest, "required_tags_match", "deploy.deploy_instance"),
            {
                "name": "required_tags_match",
                "status": "failed",
                "target": "deploy.deploy_instance",
                "failure_reason": "created resource is missing required deploy tags",
            },
        )

    def test_serialized_execute_manifest_redacts_provider_ids(self) -> None:
        manifest = capture_deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=FakeLinodeClient(),
        )

        exported = json.loads(serialize_manifest(manifest))

        self.assertEqual(exported["capture"]["capture_source"]["linode_id"], "[REDACTED]")
        self.assertEqual(exported["capture"]["custom_image"]["image_id"], "[REDACTED]")
        self.assertEqual(exported["deploy"]["deploy_source"]["image_id"], "[REDACTED]")
        self.assertEqual(exported["deploy"]["deploy_instance"]["linode_id"], "[REDACTED]")
        self.assertEqual(exported["cleanup"]["preserved"][0]["image_id"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
