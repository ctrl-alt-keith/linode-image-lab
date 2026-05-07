from __future__ import annotations

from concurrent.futures import Future
import json
import os
import threading
import unittest
from unittest.mock import patch

from linode_image_lab.capture_deploy import CaptureDeployError, capture_deploy_plan
from linode_image_lab.linode_api import LinodeClient, LinodePreflightError
from linode_image_lab.manifest import serialize_manifest
from linode_image_lab.user_data import DeployUserData


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
        self.current_region = "us-east"
        self.capture_count = 0
        self.deploy_count = 0
        self.created_regions: list[str] = []
        self.deploy_source_images: list[str] = []
        self.deploy_firewall_ids: list[int | None] = []
        self.deploy_authorized_keys: list[list[str] | None] = []
        self.capture_metadata_user_data: list[str | None] = []
        self.deploy_metadata_user_data: list[str | None] = []
        self.instance_regions: dict[int, str] = {}
        self.instance_tags: dict[int, list[str]] = {}
        self.deploy_instance_ids: set[int] = set()
        self._lock = threading.Lock()
        self._local = threading.local()

    def preflight(self) -> None:
        with self._lock:
            self.calls.append("preflight")

    def preflight_region(self, region: str) -> None:
        self._local.current_region = region
        with self._lock:
            self.current_region = region
            self.calls.append("preflight_region")

    def preflight_instance_type(self, instance_type: str) -> None:
        with self._lock:
            self.calls.append("preflight_instance_type")

    def preflight_image(self, image_id: str) -> None:
        with self._lock:
            self.calls.append("preflight_image")

    def preflight_firewall(self, firewall_id: int) -> None:
        with self._lock:
            self.calls.append("preflight_firewall")

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
        with self._lock:
            if source_image == "private/789":
                linode_id = 321 + self.deploy_count
                self.deploy_count += 1
                self.calls.append("create_deploy_instance")
                self.deploy_source_images.append(source_image)
                self.deploy_firewall_ids.append(firewall_id)
                self.deploy_authorized_keys.append(authorized_keys)
                self.deploy_metadata_user_data.append(metadata_user_data)
                self.deploy_tags = tags
                self.created_regions.append(region)
                self.instance_regions[linode_id] = region
                self.instance_tags[linode_id] = [] if self.missing_deploy_tags else tags
                self.deploy_instance_ids.add(linode_id)
                return {
                    "linode_id": linode_id,
                    "label": label,
                    "region": region,
                    "status": "provisioning",
                    "tags": [] if self.missing_deploy_tags else tags,
                }

            linode_id = 123 + self.capture_count
            self.capture_count += 1
            self.calls.append("create_capture_source")
            self.capture_tags = tags
            self.capture_metadata_user_data.append(metadata_user_data)
            self.created_regions.append(region)
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
        with self._lock:
            is_deploy = linode_id in self.deploy_instance_ids
            region = self.instance_regions[linode_id]
            tags = self.instance_tags[linode_id]
            self.calls.append("wait_deploy_instance_ready" if is_deploy else "wait_capture_source_ready")
        if is_deploy:
            return {
                "linode_id": linode_id,
                "region": region,
                "status": "running",
                "tags": tags,
            }

        return {
            "linode_id": linode_id,
            "region": region,
            "status": "running",
            "tags": tags,
        }

    def list_disks(self, linode_id: int) -> list[dict[str, object]]:
        with self._lock:
            self.calls.append("list_disks")
        return [{"disk_id": 456}]

    def shutdown_instance(self, linode_id: int) -> dict[str, object]:
        with self._lock:
            self.calls.append("shutdown_capture_source")
        return {"linode_id": linode_id}

    def wait_instance_offline(self, linode_id: int) -> dict[str, object]:
        with self._lock:
            region = self.instance_regions[linode_id]
            tags = self.instance_tags[linode_id]
            self.calls.append("wait_capture_source_offline")
        return {
            "linode_id": linode_id,
            "region": region,
            "status": "offline",
            "tags": tags,
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
        with self._lock:
            self.calls.append("capture_image")
            self.image_tags = tags
        return {
            "image_id": "private/789",
            "label": label,
            "status": "creating",
            "tags": tags,
        }

    def wait_image_available(self, image_id: str) -> dict[str, object]:
        with self._lock:
            self.calls.append("wait_image_available")
            tags = self.image_tags
        return {
            "image_id": image_id,
            "status": "available",
            "tags": tags,
        }

    def delete_instance(self, linode_id: int) -> dict[str, object]:
        with self._lock:
            if linode_id in self.deploy_instance_ids:
                self.calls.append("delete_deploy_instance")
            else:
                self.calls.append("delete_capture_source")
            self.deleted.append(linode_id)
        return {"linode_id": linode_id, "deleted": True}

    def current_thread_region(self) -> str:
        return str(getattr(self._local, "current_region", self.current_region))


class ExplodingClient:
    def preflight(self) -> None:
        raise AssertionError("dry-run must not call the client")


class InvalidCapturedImageClient(FakeLinodeClient):
    def preflight_image(self, image_id: str) -> None:
        self.calls.append("preflight_image")
        if image_id == "private/789":
            raise LinodePreflightError("requested image is unavailable")


class RegionDeployPreflightFailureClient(FakeLinodeClient):
    def __init__(self, failing_regions: set[str]) -> None:
        super().__init__()
        self.failing_regions = failing_regions

    def preflight_image(self, image_id: str) -> None:
        with self._lock:
            self.calls.append("preflight_image")
        region = self.current_thread_region()
        if image_id == "private/789" and region in self.failing_regions:
            raise LinodePreflightError(f"deploy image unavailable in {region}")


class CaptureValidationFailureClient(FakeLinodeClient):
    def list_disks(self, linode_id: int) -> list[dict[str, object]]:
        self.calls.append("list_disks")
        raise LinodePreflightError("capture source disk unavailable")


class CaptureCleanupFailureClient(FakeLinodeClient):
    def delete_instance(self, linode_id: int) -> dict[str, object]:
        if linode_id in self.deploy_instance_ids:
            return super().delete_instance(linode_id)
        with self._lock:
            self.calls.append("delete_capture_source")
        raise ValueError("provider response included private details")


class RecordingExecutor:
    max_workers: int | None = None
    submitted_regions: list[str] = []

    def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
        type(self).max_workers = max_workers
        self.thread_name_prefix = thread_name_prefix

    def __enter__(self) -> "RecordingExecutor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def submit(self, fn: object, **kwargs: object) -> Future[dict[str, object]]:
        region = kwargs["region"]
        assert isinstance(region, str)
        type(self).submitted_regions.append(region)
        future: Future[dict[str, object]] = Future()
        future.set_result(
            {
                "status": "succeeded",
                "regions": [region],
                "deploy_source": {"image_id": kwargs["image_id"]},
                "deploy_instance": {},
            }
        )
        return future


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

    def test_dry_run_multi_region_remains_non_mutating(self) -> None:
        manifest = capture_deploy_plan(
            regions=["us-east", "us-west"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            client=ExplodingClient(),
        )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "dry-run")
        self.assertEqual(manifest["regions"], ["us-east", "us-west"])
        self.assertEqual(
            [(action["action"], action["region"]) for action in manifest["planned_actions"]],
            [("capture", "us-east"), ("deploy", "us-east"), ("capture", "us-west"), ("deploy", "us-west")],
        )

    def test_execute_multi_region_all_succeed_records_aggregate_manifest(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_deploy_plan(
            regions=["us-east", "us-west"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["regions"], ["us-east", "us-west"])
        self.assertEqual(
            manifest["summary"],
            {
                "capture_region": "us-east",
                "deploy_regions": ["us-east", "us-west"],
                "succeeded": ["us-east", "us-west"],
                "failed": [],
            },
        )
        self.assertEqual(set(manifest["deploy_results"]), {"us-east", "us-west"})
        self.assertEqual(manifest["capture"]["regions"], ["us-east"])
        self.assertEqual(manifest["deploy_results"]["us-east"]["regions"], ["us-east"])
        self.assertEqual(manifest["deploy_results"]["us-west"]["regions"], ["us-west"])
        self.assertEqual(manifest["deploy_results"]["us-east"]["deploy_source"]["image_id"], "private/789")
        self.assertEqual(manifest["deploy_results"]["us-west"]["deploy_source"]["image_id"], "private/789")
        self.assertNotIn("resources", manifest)
        self.assertNotIn("cleanup", manifest)
        self.assertNotIn("validation", manifest)

    def test_execute_multi_region_uses_bounded_parallel_executor(self) -> None:
        client = FakeLinodeClient()
        RecordingExecutor.max_workers = None
        RecordingExecutor.submitted_regions = []
        regions = ["us-east", "us-west", "us-lax", "us-sea", "us-ord", "eu-central"]

        with patch("linode_image_lab.capture_deploy.ThreadPoolExecutor", RecordingExecutor):
            manifest = capture_deploy_plan(
                regions=regions,
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=client,
            )

        self.assertEqual(RecordingExecutor.max_workers, 4)
        self.assertEqual(RecordingExecutor.submitted_regions, regions)
        self.assertEqual(list(manifest["deploy_results"]), regions)

    def test_parallel_deploy_workers_clone_real_linode_clients(self) -> None:
        injected_client = LinodeClient(
            "test-token",
            api_base_url="https://api.example.invalid/v4",
            timeout_seconds=7,
            poll_interval_seconds=1,
            max_wait_seconds=11,
            max_retry_attempts=2,
            retry_backoff_seconds=(0.1,),
        )
        worker_clients: list[LinodeClient] = []
        worker_lock = threading.Lock()

        def capture_manifest(*args: object, **kwargs: object) -> dict[str, object]:
            self.assertIs(kwargs["client"], injected_client)
            return {
                "status": "succeeded",
                "custom_image": {"image_id": "private/789"},
                "cleanup": {"status": "completed", "deleted": [], "preserved": []},
            }

        def deploy_manifest(*args: object, **kwargs: object) -> dict[str, object]:
            client = kwargs["client"]
            self.assertIsInstance(client, LinodeClient)
            assert isinstance(client, LinodeClient)
            with worker_lock:
                worker_clients.append(client)
            region = args[0].regions[0]
            return {"status": "succeeded", "regions": [region], "deploy_source": {"image_id": "private/789"}}

        with (
            patch("linode_image_lab.capture_deploy.execute_capture", side_effect=capture_manifest),
            patch("linode_image_lab.capture_deploy.execute_deploy", side_effect=deploy_manifest),
        ):
            manifest = capture_deploy_plan(
                regions=["us-east", "us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=injected_client,
            )

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(len(worker_clients), 2)
        self.assertTrue(all(client is not injected_client for client in worker_clients))
        self.assertIsNot(worker_clients[0], worker_clients[1])
        self.assertTrue(all(client.api_base_url == injected_client.api_base_url for client in worker_clients))
        self.assertTrue(all(client.retry_events == [] for client in worker_clients))

    def test_execute_single_region_does_not_use_parallel_executor(self) -> None:
        with patch("linode_image_lab.capture_deploy.ThreadPoolExecutor") as executor:
            capture_deploy_plan(
                regions=["us-east"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                client=FakeLinodeClient(),
            )

        executor.assert_not_called()

    def test_execute_multi_region_captures_once_then_deploys_to_each_region(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_deploy_plan(
            regions=["us-east", "us-west"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            client=client,
        )

        self.assertEqual(client.calls.count("capture_image"), 1)
        self.assertEqual(client.deploy_source_images, ["private/789", "private/789"])
        self.assertEqual(client.created_regions[0], "us-east")
        self.assertEqual(sorted(client.created_regions[1:]), ["us-east", "us-west"])
        self.assertEqual(sorted(client.deleted[:2]), [321, 322])
        self.assertEqual(client.deleted[-1], 123)
        self.assertEqual(manifest["capture"]["cleanup"]["deleted"][0]["linode_id"], 123)
        self.assertEqual(manifest["capture"]["cleanup"]["preserved"][0]["reason"], "deliverable")

    def test_execute_multi_region_capture_cleanup_failure_affects_aggregate_status(self) -> None:
        client = CaptureCleanupFailureClient()

        with self.assertRaises(CaptureDeployError) as raised:
            capture_deploy_plan(
                regions=["us-east", "us-west"],
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
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["summary"]["succeeded"], ["us-east", "us-west"])
        self.assertEqual(manifest["summary"]["failed"], [])
        self.assertEqual(manifest["summary"]["cleanup"]["status"], "failed")
        self.assertEqual(manifest["capture"]["cleanup"]["status"], "failed")
        self.assertEqual(manifest["deploy_results"]["us-east"]["status"], "succeeded")
        self.assertEqual(manifest["deploy_results"]["us-west"]["status"], "succeeded")

    def test_execute_multi_region_capture_failure_prevents_deploy_attempts(self) -> None:
        client = CaptureValidationFailureClient()

        with self.assertRaises(CaptureDeployError) as raised:
            capture_deploy_plan(
                regions=["us-east", "us-west"],
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
        self.assertEqual(manifest["capture"]["status"], "failed")
        self.assertEqual(manifest["deploy_results"], {})
        self.assertNotIn("create_deploy_instance", client.calls)
        self.assertEqual(client.deleted, [123])

    def test_execute_multi_region_partial_success_returns_partial_manifest(self) -> None:
        client = RegionDeployPreflightFailureClient({"us-west"})

        with self.assertRaises(CaptureDeployError) as raised:
            capture_deploy_plan(
                regions=["us-east", "us-west", "us-lax"],
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
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(
            manifest["summary"],
            {
                "capture_region": "us-east",
                "deploy_regions": ["us-east", "us-west", "us-lax"],
                "succeeded": ["us-east", "us-lax"],
                "failed": ["us-west"],
            },
        )
        self.assertEqual(manifest["deploy_results"]["us-east"]["status"], "succeeded")
        self.assertEqual(manifest["deploy_results"]["us-west"]["status"], "failed")
        self.assertEqual(manifest["deploy_results"]["us-lax"]["status"], "succeeded")
        self.assertEqual(client.created_regions[0], "us-east")
        self.assertEqual(sorted(client.created_regions[1:]), ["us-east", "us-lax"])
        self.assertEqual(sorted(client.deleted[:2]), [321, 322])
        self.assertEqual(client.deleted[-1], 123)
        self.assertEqual(manifest["deploy_results"]["us-west"]["errors"], ["us-west: deploy image unavailable in us-west"])

    def test_execute_multi_region_unexpected_worker_exception_records_region_failure(self) -> None:
        def capture_manifest(*args: object, **kwargs: object) -> dict[str, object]:
            return {
                "status": "succeeded",
                "custom_image": {"image_id": "private/789"},
                "cleanup": {"status": "completed", "deleted": [], "preserved": []},
            }

        def deploy_or_raise(*, region: str, **kwargs: object) -> dict[str, object]:
            if region == "us-west":
                raise RuntimeError("provider id private/789 leaked internally")
            return {"status": "succeeded", "regions": [region], "deploy_source": {"image_id": "private/789"}}

        with (
            patch("linode_image_lab.capture_deploy.execute_capture", side_effect=capture_manifest),
            patch("linode_image_lab.capture_deploy.execute_region_deploy", side_effect=deploy_or_raise),
        ):
            with self.assertRaises(CaptureDeployError) as raised:
                capture_deploy_plan(
                    regions=["us-east", "us-west", "us-lax"],
                    run_id="run-test",
                    ttl="2030-01-01T00:00:00Z",
                    execute=True,
                    source_image="linode/debian12",
                    instance_type="g6-nanode-1",
                    client=FakeLinodeClient(),
                )

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(list(manifest["deploy_results"]), ["us-east", "us-west", "us-lax"])
        self.assertEqual(manifest["summary"]["succeeded"], ["us-east", "us-lax"])
        self.assertEqual(manifest["summary"]["failed"], ["us-west"])
        self.assertEqual(manifest["deploy_results"]["us-west"]["status"], "failed")
        self.assertEqual(manifest["deploy_results"]["us-west"]["errors"], ["us-west: RuntimeError"])

    def test_execute_multi_region_all_fail_records_failed_manifest(self) -> None:
        client = RegionDeployPreflightFailureClient({"us-east", "us-west"})

        with self.assertRaises(CaptureDeployError) as raised:
            capture_deploy_plan(
                regions=["us-east", "us-west"],
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
        self.assertEqual(
            manifest["summary"],
            {
                "capture_region": "us-east",
                "deploy_regions": ["us-east", "us-west"],
                "succeeded": [],
                "failed": ["us-east", "us-west"],
            },
        )
        self.assertEqual(manifest["deploy_results"]["us-east"]["status"], "failed")
        self.assertEqual(manifest["deploy_results"]["us-west"]["status"], "failed")
        self.assertEqual(client.created_regions, ["us-east"])
        self.assertEqual(client.deleted, [123])

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
                "preflight_region",
                "preflight_instance_type",
                "preflight_image",
                "create_capture_source",
                "wait_capture_source_ready",
                "list_disks",
                "shutdown_capture_source",
                "wait_capture_source_offline",
                "capture_image",
                "wait_image_available",
                "preflight",
                "preflight_region",
                "preflight_instance_type",
                "preflight_image",
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

    def test_capture_deploy_passes_firewall_to_deploy_phase_only(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            firewall_id=12345,
            client=client,
        )

        self.assertEqual(client.calls.count("preflight_firewall"), 1)
        self.assertEqual(client.deploy_firewall_ids, [12345])
        self.assertEqual(manifest["deploy"]["deploy_config"]["firewall"], {"enabled": True, "firewall_id": 12345})

    def test_capture_deploy_passes_authorized_keys_to_deploy_phase_only(self) -> None:
        client = FakeLinodeClient()
        authorized_keys = [
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA operator@example"
        ]

        manifest = capture_deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            authorized_keys=authorized_keys,
            client=client,
        )

        self.assertEqual(client.deploy_authorized_keys, [authorized_keys])
        self.assertEqual(
            manifest["deploy"]["deploy_config"]["authorized_keys"],
            {"enabled": True, "authorized_key_count": 1},
        )

    def test_capture_deploy_passes_user_data_to_deploy_phase_only(self) -> None:
        client = FakeLinodeClient()
        user_data = DeployUserData(encoded="I2Nsb3VkLWNvbmZpZwo=", byte_count=14)

        manifest = capture_deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            user_data=user_data,
            client=client,
        )

        self.assertEqual(client.capture_metadata_user_data, [None])
        self.assertEqual(client.deploy_metadata_user_data, ["I2Nsb3VkLWNvbmZpZwo="])
        self.assertEqual(
            manifest["deploy"]["deploy_config"]["user_data"],
            {"enabled": True, "source": "file", "byte_count": 14},
        )

    def test_deploy_provider_preflight_fails_before_deploy_mutation(self) -> None:
        client = InvalidCapturedImageClient()

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

        self.assertNotIn("create_deploy_instance", client.calls)
        self.assertIn("delete_capture_source", client.calls)
        self.assertEqual(raised.exception.manifest["status"], "failed")
        self.assertEqual(raised.exception.manifest["errors"], ["requested image is unavailable"])
        self.assertEqual(raised.exception.manifest["deploy"]["resources"], [])

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

    def test_configured_image_project_tag_flows_to_captured_image_only(self) -> None:
        client = FakeLinodeClient()

        manifest = capture_deploy_plan(
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            source_image="linode/debian12",
            instance_type="g6-nanode-1",
            image_project_tag="customer-image-lab",
            client=client,
        )

        self.assertEqual(client.image_tags, ["project=customer-image-lab"])
        self.assertEqual(manifest["artifact_tags"], ["project=customer-image-lab"])
        self.assertEqual(manifest["capture"]["custom_image"]["tags"], ["project=customer-image-lab"])
        self.assertIn("project=linode-image-lab", client.capture_tags)
        self.assertIn("project=linode-image-lab", client.deploy_tags)
        self.assertNotIn("run_id=run-test", client.image_tags)

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

    def test_serialized_multi_region_execute_manifest_redacts_provider_ids(self) -> None:
        manifest = capture_deploy_plan(
            regions=["us-east", "us-west"],
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
        self.assertEqual(exported["capture"]["cleanup"]["deleted"][0]["linode_id"], "[REDACTED]")
        self.assertEqual(exported["capture"]["cleanup"]["preserved"][0]["image_id"], "[REDACTED]")
        self.assertEqual(exported["deploy_results"]["us-east"]["deploy_source"]["image_id"], "[REDACTED]")
        self.assertEqual(exported["deploy_results"]["us-east"]["deploy_instance"]["linode_id"], "[REDACTED]")
        self.assertEqual(exported["deploy_results"]["us-west"]["deploy_source"]["image_id"], "[REDACTED]")
        self.assertEqual(exported["deploy_results"]["us-west"]["deploy_instance"]["linode_id"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
