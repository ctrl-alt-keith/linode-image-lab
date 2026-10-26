from __future__ import annotations

import datetime as dt
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from linode_image_lab.acl_sync import ACLSyncError, acl_sync_plan, routine_outcome
from linode_image_lab.cli import main
from linode_image_lab.linode_api import LinodeClient


def registry_payload(*, cidrs: tuple[str, ...] = ("198.51.100.0/24",)) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "schema_version": 1,
        "registry": {
            "name": "trusted-network-registry",
            "generated_at": (now - dt.timedelta(minutes=2)).isoformat(),
            "valid_until": (now + dt.timedelta(minutes=30)).isoformat(),
            "publisher_version": "0.1.0",
        },
        "entries": [
            {"status": "active", "address_family": "ipv4", "cidr": cidr}
            for cidr in cidrs
        ],
    }


class FakeACLClient:
    def __init__(self) -> None:
        self.cluster = {"id": 12345, "label": "example-lke"}
        self.acl = {"acl": {"enabled": False, "addresses": {"ipv4": [], "ipv6": []}, "revision-id": ""}}
        self.writes: list[dict] = []

    def get_lke_cluster(self, cluster_id: int) -> dict:
        return self.cluster

    def get_lke_control_plane_acl(self, cluster_id: int) -> dict:
        return self.acl

    def update_lke_control_plane_acl(self, cluster_id: int, acl: dict) -> dict:
        self.writes.append(acl)
        return {"acl": acl}


def run_sync(client: FakeACLClient, *, execute: bool = False) -> dict:
    return acl_sync_plan(
        cluster_id=12345,
        cluster_label="example-lke",
        registry_endpoint_url="https://us-east-1.linodeobjects.com",
        registry_bucket="example-bucket",
        registry_object_key="registry.json",
        client=client,
        environ={},
        execute=execute,
    )


class ACLSyncTests(unittest.TestCase):
    def test_provider_client_uses_dedicated_acl_endpoint_and_minimal_payload(self) -> None:
        client = LinodeClient(token="x")
        with patch.object(LinodeClient, "_request", return_value={}) as request:
            client.update_lke_control_plane_acl(12345, {
                "enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []}
            })
        self.assertEqual(request.call_args.args, (
            "PUT", "/lke/clusters/12345/control_plane_acl",
            {"acl": {"enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []}}},
        ))
        self.assertFalse(request.call_args.kwargs["retry"])

    def test_cli_config_validate_accepts_acl_target_without_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acl.toml"
            path.write_text(
                'schema_version = 1\n[acl-sync]\ncluster_id = 12345\ncluster_label = "example-lke"\n'
                'registry_endpoint_url = "https://us-east-1.linodeobjects.com"\n'
                'registry_bucket = "example-bucket"\nregistry_object_key = "registry.json"\n',
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                result = main(["config", "validate", "--config", str(path), "--command", "acl-sync"])
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["target_command"], "acl-sync")
        self.assertEqual(payload["effective_defaults"]["cluster_id"], 12345)

    def test_dry_run_is_nonmutating_and_summary_omits_identity_and_cidrs(self) -> None:
        client = FakeACLClient()
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            plan = run_sync(client)
        self.assertEqual(plan["provider_payload"]["addresses"]["ipv4"], ["198.51.100.0/24"])
        self.assertEqual(client.writes, [])
        summary = str(routine_outcome(plan, execute=False))
        self.assertNotIn("198.51.100.0/24", summary)
        self.assertNotIn("12345", summary)

    def test_execute_rechecks_registry_and_acl_then_submits_only_enabled_allowlist(self) -> None:
        client = FakeACLClient()
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()) as fetch:
            result = run_sync(client, execute=True)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(client.writes, [
            {"enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []}}
        ])
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["safety"]["enforcement_verified"])

    def test_empty_or_universal_registry_fails_without_write(self) -> None:
        for cidrs in ((), ("0.0.0.0/0",)):
            client = FakeACLClient()
            with self.subTest(cidrs=cidrs), patch(
                "linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload(cidrs=cidrs)
            ), self.assertRaises(ACLSyncError):
                run_sync(client, execute=True)
            self.assertEqual(client.writes, [])

    def test_stale_registry_fails_without_write(self) -> None:
        client = FakeACLClient()
        stale = registry_payload()
        stale["registry"]["valid_until"] = "2000-01-01T00:00:00Z"
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=stale), self.assertRaises(ACLSyncError):
            run_sync(client, execute=True)
        self.assertEqual(client.writes, [])

    def test_changed_registry_before_write_fails_closed(self) -> None:
        client = FakeACLClient()
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", side_effect=[
            registry_payload(), registry_payload(cidrs=("203.0.113.0/24",))
        ]), self.assertRaisesRegex(ACLSyncError, "pre-write verification failed"):
            run_sync(client, execute=True)
        self.assertEqual(client.writes, [])

    def test_changed_acl_before_write_fails_closed(self) -> None:
        client = FakeACLClient()
        original = client.get_lke_control_plane_acl
        reads = 0

        def changing_acl(cluster_id: int) -> dict:
            nonlocal reads
            reads += 1
            if reads == 2:
                return {"acl": {"enabled": True, "addresses": {"ipv4": ["203.0.113.0/24"], "ipv6": []}}}
            return original(cluster_id)

        client.get_lke_control_plane_acl = changing_acl  # type: ignore[method-assign]
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()), self.assertRaises(ACLSyncError):
            run_sync(client, execute=True)
        self.assertEqual(client.writes, [])

    def test_cluster_identity_mismatch_fails_closed(self) -> None:
        client = FakeACLClient()
        client.cluster["label"] = "other-cluster"
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()), self.assertRaisesRegex(
            ACLSyncError, "identity"
        ):
            run_sync(client, execute=True)
        self.assertEqual(client.writes, [])

    def test_unknown_update_outcome_requires_inspection(self) -> None:
        client = FakeACLClient()

        def fail(cluster_id: int, acl: dict) -> dict:
            raise ValueError("possibly accepted")

        client.update_lke_control_plane_acl = fail  # type: ignore[method-assign]
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()), self.assertRaises(
            ACLSyncError
        ) as caught:
            run_sync(client, execute=True)
        self.assertEqual(caught.exception.manifest["status"], "uncertain")
        self.assertEqual(routine_outcome(caught.exception.manifest, execute=True, failed=True)["status"], "uncertain")
