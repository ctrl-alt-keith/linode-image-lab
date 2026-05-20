from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from linode_image_lab.linode_api import LinodeApiError, LinodePreflightError
from linode_image_lab.replicate import ReplicateError, replicate_plan


class FakeReplicateClient:
    def __init__(
        self,
        *,
        image_status: str = "available",
        image_regions: list[dict[str, object]] | None = None,
        fail_region: str | None = None,
        fail_submit: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.image_status = image_status
        self.image_regions = image_regions if image_regions is not None else [{"region": "us-east", "status": "available"}]
        self.fail_region = fail_region
        self.fail_submit = fail_submit
        self.submitted_regions: list[str] = []

    def preflight(self) -> None:
        self.calls.append("preflight")

    def get_image_details(self, image_id: str) -> dict[str, object]:
        self.calls.append("get_image_details")
        return {
            "image_id": image_id,
            "status": self.image_status,
            "regions": list(self.image_regions),
        }

    def preflight_region(self, region: str) -> None:
        self.calls.append(f"preflight_region:{region}")
        if region == self.fail_region:
            raise LinodePreflightError("requested region is unavailable")

    def replicate_image(self, *, image_id: str, regions: list[str]) -> dict[str, object]:
        self.calls.append("replicate_image")
        self.submitted_regions = list(regions)
        if self.fail_submit:
            raise LinodeApiError("Linode API request failed with status 500")
        return {
            "image_id": image_id,
            "status": "available",
            "regions": [{"region": region, "status": "pending replication"} for region in regions],
        }


class ExplodingClient:
    def preflight(self) -> None:
        raise AssertionError("dry-run must not call the client")


class ReplicateExecutionTests(unittest.TestCase):
    def test_dry_run_does_not_read_token_or_call_client(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            manifest = replicate_plan(
                regions=["us-west", "us-sea"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                image_id="private/789",
                client=ExplodingClient(),
            )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "dry-run")
        self.assertEqual(manifest["replication_intent"]["requested_regions"], ["us-west", "us-sea"])
        self.assertEqual(manifest["replica_status_polling"], "not_attempted")

    def test_execute_requires_token_without_client(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "LINODE_TOKEN"):
                replicate_plan(
                    regions=["us-west"],
                    run_id="run-test",
                    ttl="2030-01-01T00:00:00Z",
                    execute=True,
                    image_id="private/789",
                )

    def test_execute_requires_image_id(self) -> None:
        with self.assertRaisesRegex(ReplicateError, "--image-id"):
            replicate_plan(
                regions=["us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                client=FakeReplicateClient(),
            )

    def test_execute_preserves_existing_regions_in_replication_request(self) -> None:
        client = FakeReplicateClient(
            image_regions=[
                {"region": "us-east", "status": "available"},
                {"region": "us-west", "status": "available"},
            ]
        )

        manifest = replicate_plan(
            regions=["us-west", "us-sea"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
            execute=True,
            image_id="private/789",
            client=client,
        )

        self.assertEqual(
            client.calls,
            [
                "preflight",
                "get_image_details",
                "preflight_region:us-west",
                "preflight_region:us-sea",
                "replicate_image",
            ],
        )
        self.assertEqual(client.submitted_regions, ["us-east", "us-west", "us-sea"])
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["replication_request"]["requested_regions"], ["us-west", "us-sea"])
        self.assertEqual(manifest["replication_request"]["submitted_regions"], ["us-east", "us-west", "us-sea"])
        self.assertEqual(manifest["validation"]["status"], "succeeded")
        self.assertEqual(manifest["replica_status_polling"], "not_attempted")

    def test_execute_fails_closed_when_existing_regions_are_not_exposed(self) -> None:
        client = FakeReplicateClient(image_regions=[])

        with self.assertRaises(ReplicateError) as raised:
            replicate_plan(
                regions=["us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                client=client,
            )

        self.assertNotIn("replicate_image", client.calls)
        self.assertEqual(raised.exception.manifest["status"], "failed")
        self.assertIn("refusing replication", raised.exception.manifest["errors"][0])
        self.assertEqual(raised.exception.manifest["validation"]["status"], "failed")

    def test_execute_preflights_requested_regions_before_mutation(self) -> None:
        client = FakeReplicateClient(fail_region="us-west")

        with self.assertRaises(ReplicateError) as raised:
            replicate_plan(
                regions=["us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                client=client,
            )

        self.assertEqual(client.calls, ["preflight", "get_image_details", "preflight_region:us-west"])
        self.assertEqual(raised.exception.manifest["status"], "failed")
        self.assertEqual(raised.exception.manifest["resources"], [])
        self.assertEqual(raised.exception.manifest["errors"], ["requested region is unavailable"])

    def test_execute_submission_failure_records_partial_manifest(self) -> None:
        client = FakeReplicateClient(fail_submit=True)

        with self.assertRaises(ReplicateError) as raised:
            replicate_plan(
                regions=["us-west"],
                run_id="run-test",
                ttl="2030-01-01T00:00:00Z",
                execute=True,
                image_id="private/789",
                client=client,
            )

        self.assertEqual(client.calls[-1], "replicate_image")
        manifest = raised.exception.manifest
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["steps"][-1], {"name": "submit_image_replication", "mutates": True, "status": "failed"})
        self.assertEqual(manifest["errors"], ["Linode API request failed with status 500"])


if __name__ == "__main__":
    unittest.main()
