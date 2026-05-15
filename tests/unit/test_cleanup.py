from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from linode_image_lab.cleanup import CleanupError, cleanup_plan, select_cleanup_candidates
from linode_image_lab.manifest import serialize_manifest


NOW = datetime(2026, 5, 1, tzinfo=UTC)


class FakeCleanupClient:
    def __init__(
        self,
        resources: list[dict[str, object]],
        *,
        images: list[dict[str, object]] | None = None,
        refreshed_resources: dict[int, dict[str, object]] | None = None,
        refreshed_images: dict[str, dict[str, object]] | None = None,
        refetch_failure: bool = False,
        delete_failures: set[int] | None = None,
        image_delete_failures: set[str] | None = None,
    ) -> None:
        self.resources = resources
        self.images = images or []
        self.refreshed_resources = refreshed_resources or {}
        self.refreshed_images = refreshed_images or {}
        self.refetch_failure = refetch_failure
        self.delete_failures = delete_failures or set()
        self.image_delete_failures = image_delete_failures or set()
        self.preflight_count = 0
        self.list_count = 0
        self.image_list_count = 0
        self.get_count = 0
        self.image_get_count = 0
        self.deleted: list[int] = []
        self.deleted_images: list[str] = []

    def preflight(self) -> None:
        self.preflight_count += 1

    def list_managed_linodes(self) -> list[dict[str, object]]:
        self.list_count += 1
        return list(self.resources)

    def list_managed_images(self) -> list[dict[str, object]]:
        self.image_list_count += 1
        return list(self.images)

    def get_instance(self, linode_id: int) -> dict[str, object]:
        self.get_count += 1
        if self.refetch_failure:
            raise ValueError("provider response included private details")
        if linode_id in self.refreshed_resources:
            return self.refreshed_resources[linode_id]
        for resource in self.resources:
            if resource.get("linode_id") == linode_id:
                return dict(resource)
        raise ValueError("missing resource")

    def get_image(self, image_id: str) -> dict[str, object]:
        self.image_get_count += 1
        if self.refetch_failure:
            raise ValueError("provider response included private details")
        if image_id in self.refreshed_images:
            return self.refreshed_images[image_id]
        for image in self.images:
            if image.get("image_id") == image_id:
                return dict(image)
        raise ValueError("missing image")

    def delete_instance(self, linode_id: int) -> dict[str, object]:
        self.deleted.append(linode_id)
        if linode_id in self.delete_failures:
            raise ValueError("provider response included private details")
        return {"linode_id": linode_id, "deleted": True}

    def delete_image(self, image_id: str) -> dict[str, object]:
        self.deleted_images.append(image_id)
        if image_id in self.image_delete_failures:
            raise ValueError("provider response included private details")
        return {"image_id": image_id, "deleted": True}


def linode_resource(*, linode_id: int = 123, ttl: str = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {
        "linode_id": linode_id,
        "label": "lil-run-test",
        "region": "us-east",
        "status": "running",
        "tags": [
            "project=linode-image-lab",
            "run_id=run-test",
            "mode=capture-deploy",
            "component=capture",
            f"ttl={ttl}",
        ],
    }


def image_resource(
    *,
    image_id: str = "private/789",
    ttl: str = "2026-01-01T00:00:00Z",
    project: str = "linode-image-lab",
) -> dict[str, object]:
    return {
        "resource_type": "image",
        "image_id": image_id,
        "label": "lil-run-test-image",
        "status": "available",
        "tags": [
            f"project={project}",
            "run_id=run-test",
            "mode=capture-deploy",
            "component=capture",
            f"ttl={ttl}",
        ],
    }


class CleanupSelectionTests(unittest.TestCase):
    def test_selects_only_expired_fully_tagged_resources(self) -> None:
        fixture = Path("tests/fixtures/sanitized/mock_resources.json")
        resources = json.loads(fixture.read_text(encoding="utf-8"))
        resources.append({"id": "untagged", "tags": ["project=linode-image-lab"]})

        selected = select_cleanup_candidates(
            resources,
            now=NOW,
        )

        self.assertEqual([resource["id"] for resource in selected], ["resource-expired"])

    def test_select_cleanup_candidates_rejects_invalid_run_id_tag(self) -> None:
        resource = linode_resource()
        resource["id"] = "resource-invalid-run-id"
        resource["tags"] = [
            "project=linode-image-lab",
            "run_id=run,bad",
            "mode=capture-deploy",
            "component=capture",
            "ttl=2026-01-01T00:00:00Z",
        ]

        selected = select_cleanup_candidates([resource], now=NOW)

        self.assertEqual(selected, [])

    def test_plain_cleanup_does_not_discover_or_delete(self) -> None:
        client = FakeCleanupClient([linode_resource()])

        manifest = cleanup_plan(client=client, now=NOW)

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "dry-run")
        self.assertEqual(manifest["cleanup"]["status"], "not_started")
        self.assertEqual(manifest["cleanup_candidates"], [])
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.preflight_count, 0)
        self.assertEqual(client.list_count, 0)
        self.assertEqual(client.image_list_count, 0)

    def test_discover_lists_but_does_not_delete(self) -> None:
        client = FakeCleanupClient([linode_resource()])

        manifest = cleanup_plan(discover=True, client=client, now=NOW)

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "discover")
        self.assertEqual(manifest["cleanup"]["status"], "previewed")
        self.assertEqual(len(manifest["cleanup_candidates"]), 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.deleted_images, [])
        self.assertEqual(client.preflight_count, 1)
        self.assertEqual(client.list_count, 1)
        self.assertEqual(client.image_list_count, 1)

    def test_discover_reports_expiration_metadata_and_orders_longest_expired_first(self) -> None:
        client = FakeCleanupClient(
            [
                linode_resource(linode_id=101, ttl="2026-05-01T00:00:00Z"),
                linode_resource(linode_id=202, ttl="2026-04-30T23:00:00Z"),
                linode_resource(linode_id=303, ttl="2026-05-01T01:00:00Z"),
                linode_resource(linode_id=404, ttl="not-a-timestamp"),
            ]
        )

        manifest = cleanup_plan(discover=True, client=client, now=NOW)

        self.assertEqual([item["linode_id"] for item in manifest["cleanup_candidates"]], [202, 101])
        self.assertEqual(
            manifest["cleanup_candidates"][0],
            {
                "resource_type": "linode",
                "linode_id": 202,
                "label": "lil-run-test",
                "region": "us-east",
                "status": "running",
                "tags": [
                    "project=linode-image-lab",
                    "run_id=run-test",
                    "mode=capture-deploy",
                    "component=capture",
                    "ttl=2026-04-30T23:00:00Z",
                ],
                "expired_at": "2026-04-30T23:00:00Z",
                "expired_for_seconds": 3600,
                "reason": "expired_ttl",
            },
        )
        self.assertEqual(manifest["cleanup_candidates"][1]["expired_at"], "2026-05-01T00:00:00Z")
        self.assertEqual(manifest["cleanup_candidates"][1]["expired_for_seconds"], 0)

        unexpired = next(item for item in manifest["cleanup"]["preserved"] if item["reason"] == "ttl_not_expired")
        self.assertEqual(unexpired["expires_in_seconds"], 3600)
        self.assertNotIn("expired_at", unexpired)
        self.assertNotIn("expired_for_seconds", unexpired)

        malformed = next(item for item in manifest["cleanup"]["preserved"] if item["reason"] == "ttl_parse_failed")
        self.assertNotIn("expired_at", malformed)
        self.assertNotIn("expired_for_seconds", malformed)
        self.assertNotIn("expires_in_seconds", malformed)

    def test_execute_deletes_expired_tagged_linode(self) -> None:
        client = FakeCleanupClient([linode_resource(linode_id=456)])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertFalse(manifest["dry_run"])
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(client.preflight_count, 1)
        self.assertEqual(client.get_count, 1)
        self.assertEqual(client.deleted, [456])
        self.assertEqual(manifest["cleanup"]["deleted"][0]["reason"], "expired_ttl")
        self.assertEqual(manifest["cleanup"]["failed"], [])

    def test_discover_includes_expired_tagged_custom_image_without_deleting(self) -> None:
        client = FakeCleanupClient([], images=[image_resource()])

        manifest = cleanup_plan(discover=True, client=client, now=NOW)

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["cleanup"]["status"], "previewed")
        self.assertEqual(manifest["cleanup_candidates"][0]["resource_type"], "image")
        self.assertEqual(manifest["cleanup_candidates"][0]["image_id"], "private/789")
        self.assertEqual(client.deleted_images, [])

    def test_execute_deletes_expired_tagged_custom_image(self) -> None:
        client = FakeCleanupClient([], images=[image_resource(image_id="private/456")])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertFalse(manifest["dry_run"])
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(client.image_get_count, 1)
        self.assertEqual(client.deleted_images, ["private/456"])
        self.assertEqual(manifest["cleanup"]["deleted"][0]["resource_type"], "image")
        self.assertEqual(manifest["cleanup"]["deleted"][0]["reason"], "expired_ttl")

    def test_execute_reports_delete_failure_and_continues_later_candidates(self) -> None:
        client = FakeCleanupClient(
            [linode_resource(linode_id=456), linode_resource(linode_id=789)],
            delete_failures={456},
        )

        with self.assertRaises(CleanupError) as raised:
            cleanup_plan(execute=True, client=client, now=NOW)

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(client.deleted, [456, 789])
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["cleanup"]["status"], "failed")
        self.assertEqual([item["linode_id"] for item in manifest["cleanup"]["deleted"]], [789])
        self.assertEqual([item["linode_id"] for item in manifest["cleanup"]["failed"]], [456])
        self.assertEqual(manifest["cleanup"]["failed"][0]["reason"], "delete_status_unknown")
        self.assertNotIn("provider response", json.dumps(manifest["cleanup"]["failed"]))
        self.assertEqual(manifest["steps"][-1]["status"], "failed")

    def test_execute_reports_image_delete_failure(self) -> None:
        client = FakeCleanupClient(
            [],
            images=[image_resource(image_id="private/456")],
            image_delete_failures={"private/456"},
        )

        with self.assertRaises(CleanupError) as raised:
            cleanup_plan(execute=True, client=client, now=NOW)

        manifest = raised.exception.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(client.deleted_images, ["private/456"])
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["cleanup"]["deleted"], [])
        self.assertEqual(manifest["cleanup"]["failed"][0]["resource_type"], "image")
        self.assertEqual(manifest["cleanup"]["failed"][0]["reason"], "delete_status_unknown")

    def test_execute_preserves_candidate_that_becomes_unexpired_before_delete(self) -> None:
        initial = linode_resource(linode_id=456, ttl="2026-01-01T00:00:00Z")
        refreshed = linode_resource(linode_id=456, ttl="2030-01-01T00:00:00Z")
        client = FakeCleanupClient([initial], refreshed_resources={456: refreshed})

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.get_count, 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["deleted"], [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "ttl_not_expired")

    def test_execute_preserves_image_that_becomes_unexpired_before_delete(self) -> None:
        initial = image_resource(image_id="private/456", ttl="2026-01-01T00:00:00Z")
        refreshed = image_resource(image_id="private/456", ttl="2030-01-01T00:00:00Z")
        client = FakeCleanupClient([], images=[initial], refreshed_images={"private/456": refreshed})

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.image_get_count, 1)
        self.assertEqual(client.deleted_images, [])
        self.assertEqual(manifest["cleanup"]["deleted"], [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["resource_type"], "image")
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "ttl_not_expired")

    def test_execute_preserves_candidate_that_loses_required_tag_before_delete(self) -> None:
        initial = linode_resource(linode_id=456)
        refreshed = linode_resource(linode_id=456)
        refreshed["tags"] = [
            "project=linode-image-lab",
            "run_id=run-test",
            "mode=capture-deploy",
            "ttl=2026-01-01T00:00:00Z",
        ]
        client = FakeCleanupClient([initial], refreshed_resources={456: refreshed})

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.get_count, 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["deleted"], [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "missing_required_tags")

    def test_execute_preserves_candidate_when_refetch_fails(self) -> None:
        client = FakeCleanupClient([linode_resource(linode_id=456)], refetch_failure=True)

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.get_count, 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["deleted"], [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "refetch_failed")
        self.assertNotIn("provider response", json.dumps(manifest["cleanup"]["preserved"]))

    def test_unexpired_linode_is_preserved(self) -> None:
        client = FakeCleanupClient([linode_resource(ttl="2030-01-01T00:00:00Z")])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "ttl_not_expired")

    def test_malformed_ttl_is_preserved(self) -> None:
        client = FakeCleanupClient([linode_resource(ttl="not-a-timestamp")])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "ttl_parse_failed")

    def test_malformed_image_ttl_is_preserved(self) -> None:
        client = FakeCleanupClient([], images=[image_resource(ttl="not-a-timestamp")])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted_images, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["resource_type"], "image")
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "ttl_parse_failed")

    def test_missing_required_tags_are_preserved(self) -> None:
        client = FakeCleanupClient(
            [
                {
                    "linode_id": 789,
                    "tags": ["project=linode-image-lab", "run_id=run-test"],
                }
            ]
        )

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "missing_required_tags")

    def test_image_missing_required_tags_is_preserved(self) -> None:
        client = FakeCleanupClient(
            [],
            images=[
                {
                    "resource_type": "image",
                    "image_id": "private/789",
                    "tags": ["project=linode-image-lab", "run_id=run-test"],
                }
            ],
        )

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted_images, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["resource_type"], "image")
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "missing_required_tags")

    def test_mismatched_managed_tags_are_preserved(self) -> None:
        resource = linode_resource()
        resource["tags"] = [
            "project=linode-image-lab",
            "run_id=run-test",
            "mode=unexpected",
            "component=capture",
            "ttl=2026-01-01T00:00:00Z",
        ]
        client = FakeCleanupClient([resource])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "tag_mismatch")

    def test_invalid_discovered_run_id_is_preserved(self) -> None:
        resource = linode_resource()
        resource["tags"] = [
            "project=linode-image-lab",
            "run_id=run=bad",
            "mode=capture-deploy",
            "component=capture",
            "ttl=2026-01-01T00:00:00Z",
        ]
        client = FakeCleanupClient([resource])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "invalid_run_id")

    def test_invalid_run_id_filter_fails_before_discovery(self) -> None:
        client = FakeCleanupClient([linode_resource()])

        with self.assertRaisesRegex(ValueError, "run_id must be 1-64 characters"):
            cleanup_plan(run_id="run=bad", discover=True, client=client, now=NOW)

        self.assertEqual(client.preflight_count, 0)
        self.assertEqual(client.list_count, 0)
        self.assertEqual(client.image_list_count, 0)

    def test_artifact_project_tag_is_not_cleanup_ownership(self) -> None:
        resource = linode_resource()
        resource["tags"] = [
            "project=customer-image-lab",
            "run_id=run-test",
            "mode=capture",
            "component=capture",
            "ttl=2026-01-01T00:00:00Z",
        ]
        client = FakeCleanupClient([resource])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "tag_mismatch")

    def test_custom_image_deliverable_project_tag_is_preserved(self) -> None:
        client = FakeCleanupClient([], images=[image_resource(project="customer-image-lab")])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.deleted_images, [])
        self.assertEqual(manifest["cleanup"]["preserved"][0]["resource_type"], "image")
        self.assertEqual(manifest["cleanup"]["preserved"][0]["reason"], "tag_mismatch")

    def test_provider_ids_are_redacted_in_serialized_manifest(self) -> None:
        client = FakeCleanupClient([linode_resource(linode_id=987)])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)
        exported = json.loads(serialize_manifest(manifest))

        self.assertEqual(exported["cleanup"]["deleted"][0]["linode_id"], "[REDACTED]")
        self.assertEqual(exported["resources"][0]["linode_id"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
