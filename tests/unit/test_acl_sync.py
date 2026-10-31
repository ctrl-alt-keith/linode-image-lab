from __future__ import annotations

import datetime as dt
import hashlib
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from linode_image_lab.acl_sync import ACLSyncError, acl_sync_plan, routine_outcome
from linode_image_lab.cli import build_parser, main, resolve_config_defaults
from linode_image_lab.linode_api import LinodeApiError, LinodeClient
from linode_image_lab.openai_snapshot import load_openai_snapshot


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
        allow_enable=execute,
        execute=execute,
    )


class ACLSyncTests(unittest.TestCase):
    def test_baseline_cli_does_not_require_registry_source(self) -> None:
        args = build_parser().parse_args([
            "acl-sync", "--cluster-id", "664899", "--cluster-label", "example-lke",
            "--openai-snapshot-cluster-id", "664899", "--baseline-acl-file", "/protected/acl.json",
            "--baseline-acl-sha256", "a" * 64,
        ])
        resolve_config_defaults(args)
        self.assertIsNone(args.registry_endpoint_url)

    def test_protected_baseline_addition_and_exact_rollback(self) -> None:
        client = FakeACLClient()
        client.cluster = {"id": 664899, "label": "example-lke"}
        baseline = {"enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []},
                    "revision-id": "baseline-revision"}
        client.acl = {"acl": baseline}
        canonical = json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(json.dumps({"schema_version": 1, "cluster_id": 664899,
                                        "cluster_label": "example-lke", "acl": baseline}), encoding="utf-8")
            path.chmod(0o600)
            options = dict(cluster_id=664899, cluster_label="example-lke",
                           registry_endpoint_url="", registry_bucket="", registry_object_key="",
                           openai_snapshot_cluster_id=664899, baseline_acl_file=str(path),
                           baseline_acl_sha256=digest, client=client, environ={})
            with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage") as fetch:
                plan = acl_sync_plan(**options)
                self.assertEqual(plan["diff"]["removals"], {"ipv4": [], "ipv6": []})
                self.assertIn("198.51.100.0/24", plan["provider_payload"]["addresses"]["ipv4"])
                self.assertEqual(acl_sync_plan(**options, execute=True)["status"], "submitted")
                fetch.assert_not_called()
            self.assertEqual(client.writes, [plan["provider_payload"]])
            client.acl = {"acl": {**plan["provider_payload"], "revision-id": "after-addition"}}
            expected_live = client.acl
            client.acl = {"acl": {**plan["provider_payload"],
                                  "addresses": {"ipv4": ["203.0.113.0/24"], "ipv6": []}}}
            with self.assertRaisesRegex(ACLSyncError, "rollback aborted"):
                acl_sync_plan(**options, baseline_rollback=True, execute=True)
            self.assertEqual(len(client.writes), 1)
            client.acl = expected_live
            rollback = acl_sync_plan(**options, baseline_rollback=True)
            self.assertEqual(rollback["provider_payload"], {"enabled": True, "addresses": baseline["addresses"]})
            self.assertEqual(rollback["diff"]["additions"], {"ipv4": [], "ipv6": []})
            with patch.object(client, "get_lke_control_plane_acl",
                              side_effect=[expected_live, {"acl": baseline}]):
                with self.assertRaisesRegex(ACLSyncError, "pre-write verification failed"):
                    acl_sync_plan(**options, baseline_rollback=True, execute=True)
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(acl_sync_plan(**options, baseline_rollback=True, execute=True)["status"], "submitted")
            self.assertEqual(client.writes[-1], rollback["provider_payload"])

            changed = {**baseline, "addresses": {"ipv4": ["203.0.113.0/24"], "ipv6": []}}
            path.write_text(json.dumps({"schema_version": 1, "cluster_id": 664899,
                                        "cluster_label": "example-lke", "acl": changed}), encoding="utf-8")
            with self.assertRaisesRegex(ACLSyncError, "digest does not match"):
                acl_sync_plan(**options, execute=True)
            path.write_text(json.dumps({"schema_version": 1, "cluster_id": 664899,
                                        "cluster_label": "example-lke", "acl": baseline}), encoding="utf-8")
            with patch.object(client, "get_lke_control_plane_acl",
                              side_effect=[{"acl": baseline}, {"acl": changed}]):
                with self.assertRaisesRegex(ACLSyncError, "pre-write verification failed"):
                    acl_sync_plan(**options, execute=True)
            self.assertEqual(len(client.writes), 2)

            client.cluster["id"] = 12345
            with self.assertRaisesRegex(ACLSyncError, "reviewed LKE cluster"):
                acl_sync_plan(**{**options, "cluster_id": 12345, "openai_snapshot_cluster_id": 12345})
            client.cluster["id"] = 664899
            client.acl = {"acl": {**baseline, "addresses": {"ipv4": ["203.0.113.0/24"], "ipv6": []}}}
            with self.assertRaisesRegex(ACLSyncError, "differs from the protected baseline"):
                acl_sync_plan(**options, execute=True)
            self.assertEqual(len(client.writes), 2)

    def test_pinned_openai_snapshot_is_exact_and_cluster_scoped(self) -> None:
        cidrs = load_openai_snapshot()
        self.assertEqual(len(cidrs), 1304)
        client = FakeACLClient()
        client.acl["acl"] = {
            "enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []},
        }
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            plan = acl_sync_plan(
                cluster_id=12345, cluster_label="example-lke",
                registry_endpoint_url="https://us-east-1.linodeobjects.com",
                registry_bucket="example-bucket", registry_object_key="registry.json",
                openai_snapshot_cluster_id=12345, client=client, environ={},
            )
            with self.assertRaisesRegex(ACLSyncError, "exact LKE cluster id"):
                acl_sync_plan(
                    cluster_id=12345, cluster_label="example-lke",
                    registry_endpoint_url="https://us-east-1.linodeobjects.com",
                    registry_bucket="example-bucket", registry_object_key="registry.json",
                    openai_snapshot_cluster_id=67890, client=client, environ={},
                )
        self.assertEqual(len(plan["diff"]["additions"]["ipv4"]), 1304)
        self.assertEqual(plan["diff"]["removals"], {"ipv4": [], "ipv6": []})
        self.assertIn("198.51.100.0/24", plan["provider_payload"]["addresses"]["ipv4"])
        self.assertEqual(client.writes, [])
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            submitted = acl_sync_plan(
                cluster_id=12345, cluster_label="example-lke",
                registry_endpoint_url="https://us-east-1.linodeobjects.com",
                registry_bucket="example-bucket", registry_object_key="registry.json",
                openai_snapshot_cluster_id=12345, client=client, environ={}, execute=True,
            )
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(client.writes, [plan["provider_payload"]])

    def test_openai_snapshot_refuses_to_remove_existing_acl_entry(self) -> None:
        client = FakeACLClient()
        client.acl["acl"] = {
            "enabled": True,
            "addresses": {"ipv4": ["198.51.100.0/24", "203.0.113.0/24"], "ipv6": []},
        }
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            with self.assertRaisesRegex(ACLSyncError, "remove existing ACL entries"):
                acl_sync_plan(
                    cluster_id=12345, cluster_label="example-lke",
                    registry_endpoint_url="https://us-east-1.linodeobjects.com",
                    registry_bucket="example-bucket", registry_object_key="registry.json",
                    openai_snapshot_cluster_id=12345, client=client, environ={}, execute=True,
                )
        self.assertEqual(client.writes, [])

    def test_first_enable_requires_explicit_flag_and_separate_action(self) -> None:
        client = FakeACLClient()
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            self.assertEqual(run_sync(client)["planned_action"], "enable_acl")
            with self.assertRaisesRegex(ACLSyncError, "--allow-enable"):
                acl_sync_plan(
                    cluster_id=12345, cluster_label="example-lke",
                    registry_endpoint_url="https://us-east-1.linodeobjects.com",
                    registry_bucket="example-bucket", registry_object_key="registry.json",
                    client=client, environ={}, execute=True,
                )
        self.assertEqual(client.writes, [])

    def test_enabled_acl_with_same_addresses_is_unchanged(self) -> None:
        client = FakeACLClient()
        client.acl["acl"] = {
            "enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []},
            "revision-id": "synthetic-revision",
        }
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            result = run_sync(client, execute=True)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(client.writes, [])

    def test_provider_addresses_are_validated_and_canonicalized(self) -> None:
        client = FakeACLClient()
        client.acl["acl"] = {"enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []}}
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            self.assertEqual(run_sync(client)["planned_action"], "keep_acl")
            client.acl["acl"]["addresses"]["ipv4"] = ["2001:db8::/64"]
            with self.assertRaisesRegex(ACLSyncError, "address family"):
                run_sync(client)
            client.acl["acl"]["addresses"]["ipv4"] = ["invalid"]
            with self.assertRaisesRegex(ACLSyncError, "addresses response"):
                run_sync(client)

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
                'openai_snapshot_cluster_id = 12345\n'
                'baseline_acl_file = "/protected/acl-baseline.json"\n'
                'baseline_acl_sha256 = "' + 'a' * 64 + '"\n'
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
        self.assertEqual(payload["effective_defaults"]["openai_snapshot_cluster_id"], 12345)
        self.assertEqual(payload["effective_defaults"]["baseline_acl_sha256"], 'a' * 64)

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
        ]), self.assertRaisesRegex(ACLSyncError, "trusted registry changed after planning"):
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

    def test_update_diagnostics_are_fixed_and_do_not_echo_provider_data(self) -> None:
        private = "private-cidr=198.51.100.0/24 token=verysecretvalue"
        cases = (
            (400, "http_response", "rejected", "validation_rejected"),
            (401, "http_response", "rejected", "authentication_rejected"),
            (403, "http_response", "rejected", "permission_rejected"),
            (422, "http_response", "rejected", "validation_rejected"),
            (429, "http_response", "uncertain", "http_response_uncertain"),
            (500, "http_response", "uncertain", "http_response_uncertain"),
            (None, "transport", "uncertain", "transport_uncertain"),
            (None, "response_parse", "uncertain", "response_parse_uncertain"),
        )
        for status, kind, expected_status, category in cases:
            with self.subTest(status=status, kind=kind):
                client = FakeACLClient()

                def fail(cluster_id: int, acl: dict) -> dict:
                    raise LinodeApiError(private, status_code=status,
                                         failure_kind=kind, provider_errors=[{"reason": private}])

                client.update_lke_control_plane_acl = fail  # type: ignore[method-assign]
                with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
                    with self.assertRaises(ACLSyncError) as caught:
                        run_sync(client, execute=True)
                manifest = caught.exception.manifest
                self.assertEqual(manifest["status"], expected_status)
                expected = {"phase": "control_plane_acl_put", "category": category}
                if status is not None:
                    expected["http_status"] = status
                self.assertEqual(manifest["update_failure"], expected)
                outcome = routine_outcome(manifest, execute=True, failed=True)
                self.assertEqual(outcome["status"], expected_status)
                public_text = json.dumps(outcome) + str(caught.exception)
                self.assertNotIn(private, public_text)
                self.assertNotIn("verysecretvalue", public_text)
                self.assertNotIn("198.51.100.0/24", public_text)

    def test_cli_summary_keeps_rejected_put_body_private(self) -> None:
        client = FakeACLClient()
        private = "198.51.100.0/24 token=verysecretvalue"

        def reject(cluster_id: int, acl: dict) -> dict:
            raise LinodeApiError(private, status_code=400, failure_kind="http_response",
                                 provider_errors=[{"reason": private}])

        client.update_lke_control_plane_acl = reject  # type: ignore[method-assign]
        output, errors = StringIO(), StringIO()
        with patch("linode_image_lab.acl_sync.LinodeClient.from_env", return_value=client), \
             patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()), \
             redirect_stdout(output), redirect_stderr(errors):
            code = main([
                "acl-sync", "--cluster-id", "12345", "--cluster-label", "example-lke",
                "--registry-endpoint-url", "https://us-east-1.linodeobjects.com",
                "--registry-bucket", "example-bucket", "--registry-object-key", "registry.json",
                "--output-format", "summary", "--execute", "--allow-enable",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["update_failure"], {
            "phase": "control_plane_acl_put", "category": "validation_rejected", "http_status": 400,
        })
        self.assertNotIn(private, output.getvalue() + errors.getvalue())
        self.assertNotIn("198.51.100.0/24", output.getvalue() + errors.getvalue())
        self.assertNotIn("verysecretvalue", output.getvalue() + errors.getvalue())
