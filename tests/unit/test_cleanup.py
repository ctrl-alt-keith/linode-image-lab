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
        refreshed_resources: dict[int, dict[str, object]] | None = None,
        refetch_failure: bool = False,
        delete_failures: set[int] | None = None,
    ) -> None:
        self.resources = resources
        self.refreshed_resources = refreshed_resources or {}
        self.refetch_failure = refetch_failure
        self.delete_failures = delete_failures or set()
        self.preflight_count = 0
        self.list_count = 0
        self.get_count = 0
        self.deleted: list[int] = []

    def preflight(self) -> None:
        self.preflight_count += 1

    def list_managed_linodes(self) -> list[dict[str, object]]:
        self.list_count += 1
        return list(self.resources)

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

    def delete_instance(self, linode_id: int) -> dict[str, object]:
        self.deleted.append(linode_id)
        if linode_id in self.delete_failures:
            raise ValueError("provider response included private details")
        return {"linode_id": linode_id, "deleted": True}


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

    def test_discover_lists_but_does_not_delete(self) -> None:
        client = FakeCleanupClient([linode_resource()])

        manifest = cleanup_plan(discover=True, client=client, now=NOW)

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["execution_mode"], "discover")
        self.assertEqual(manifest["cleanup"]["status"], "previewed")
        self.assertEqual(len(manifest["cleanup_candidates"]), 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.preflight_count, 1)
        self.assertEqual(client.list_count, 1)

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

    def test_execute_preserves_candidate_that_becomes_unexpired_before_delete(self) -> None:
        initial = linode_resource(linode_id=456, ttl="2026-01-01T00:00:00Z")
        refreshed = linode_resource(linode_id=456, ttl="2030-01-01T00:00:00Z")
        client = FakeCleanupClient([initial], refreshed_resources={456: refreshed})

        manifest = cleanup_plan(execute=True, client=client, now=NOW)

        self.assertEqual(client.get_count, 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(manifest["cleanup"]["deleted"], [])
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

    def test_provider_ids_are_redacted_in_serialized_manifest(self) -> None:
        client = FakeCleanupClient([linode_resource(linode_id=987)])

        manifest = cleanup_plan(execute=True, client=client, now=NOW)
        exported = json.loads(serialize_manifest(manifest))

        self.assertEqual(exported["cleanup"]["deleted"][0]["linode_id"], "[REDACTED]")
        self.assertEqual(exported["resources"][0]["linode_id"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
