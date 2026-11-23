from __future__ import annotations

import datetime as dt
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from linode_image_lab.acl_sync import (
    ACLPreviewError, ACLSyncError, acl_sync_plan, finish_private_error_file, open_private_error_file, routine_outcome,
)
from linode_image_lab.cli import main
from linode_image_lab.linode_api import LinodeApiError, LinodeClient
from linode_image_lab.registry_version_selection import version_proof
from linode_image_lab.trusted_registry import ACCESS_KEY_ENV, SECRET_KEY_ENV


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


class ACLPreservationPreviewTests(unittest.TestCase):
    CLI_ARGS = [
        "acl-sync", "--cluster-id", "12345", "--cluster-label", "example-lke",
        "--registry-endpoint-url", "https://us-east-1.linodeobjects.com",
        "--registry-bucket", "example-bucket", "--registry-object-key", "registry.json",
        "--output-format", "summary",
    ]

    def setUp(self) -> None:
        self.client = FakeACLClient()
        self.retained = ["192.0.2.1/32", "203.0.113.0/24"]
        self.previous = "198.51.100.1/32"
        self.baseline = {
            "schema_version": 1,
            "cluster_id": 12345,
            "cluster_label": "example-lke",
            "acl": {
                "enabled": True,
                "addresses": {"ipv4": [*self.retained, self.previous], "ipv6": ["2001:db8::1/128"]},
                "revision-id": "baseline",
            },
            "managed_ipv4": [self.previous],
            "max_managed_ipv4": 1,
        }
        self.client.acl = {"acl": json.loads(json.dumps(self.baseline["acl"]))}

    def preview(self, *cidrs: str, payload: dict | None = None, execute: bool = False) -> dict:
        with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage",
                   return_value=registry_payload(cidrs=cidrs) if payload is None else payload):
            result = acl_sync_plan(
                cluster_id=12345, cluster_label="example-lke", registry_endpoint_url="https://us-east-1.linodeobjects.com",
                registry_bucket="example-bucket", registry_object_key="registry.json",
                preservation_baseline=self.baseline, client=self.client, execute=execute,
            )
        return result

    def test_repeated_host_changes_preserve_other_routes_without_accumulation(self) -> None:
        for index, source in enumerate(("198.51.100.2/32", "198.51.100.3/32")):
            plan = self.preview(source, "203.0.113.2/32")
            self.assertEqual(plan["diff"]["additions"], {"ipv4": [source], "ipv6": []})
            self.assertEqual(plan["diff"]["removals"], {"ipv4": [self.previous], "ipv6": []})
            self.assertEqual(plan["provider_payload"], {
                "enabled": True,
                "addresses": {"ipv4": sorted([*self.retained, source]), "ipv6": ["2001:db8::1/128"]},
            })
            # Model promotion only after a new revision has independently been observed.
            new_acl = {**plan["provider_payload"], "revision-id": f"enforced-{index}"}
            self.baseline.update(acl=new_acl, managed_ipv4=[source])
            self.client.acl = {"acl": {**new_acl}}
            self.previous = source
        self.assertEqual(self.preview(self.previous)["planned_action"], "keep_acl")
        self.assertEqual(self.client.writes, [])

    def test_readback_requires_both_revision_and_full_policy(self) -> None:
        for changed in (
            {"revision-id": "other"},
            {"addresses": {"ipv4": self.retained, "ipv6": ["2001:db8::1/128"]}},
            {"enabled": False},
        ):
            with self.subTest(changed=changed):
                self.client.acl = {"acl": {**self.baseline["acl"], **changed}}
                with self.assertRaisesRegex(ACLPreviewError, "baseline_drift"):
                    self.preview("198.51.100.2/32")
        self.assertEqual(self.client.writes, [])

    def test_unclassified_current_route_stays_retained(self) -> None:
        self.baseline["managed_ipv4"] = []
        plan = self.preview("198.51.100.2/32")
        self.assertEqual(plan["diff"]["removals"], {"ipv4": [], "ipv6": []})
        self.assertIn(self.previous, plan["provider_payload"]["addresses"]["ipv4"])

    def test_refuses_ambiguous_ownership_and_bad_registry(self) -> None:
        for cidrs, reason in (
            ((), "registry_requires_ipv4_hosts"),
            (("198.51.100.0/24",), "registry_requires_ipv4_hosts"),
            (("198.51.100.2/32", "198.51.100.3/32"), "managed_route_limit_exceeded"),
            (("0.0.0.0/0",), "registry_invalid_or_unavailable"),
        ):
            with self.subTest(cidrs=cidrs), self.assertRaisesRegex(ACLPreviewError, reason):
                self.preview(*cidrs)
        stale = registry_payload(cidrs=("198.51.100.2/32",))
        stale["registry"].update(generated_at="2000-01-01T00:00:00Z", valid_until="2000-01-02T00:00:00Z")
        with self.assertRaisesRegex(ACLPreviewError, "registry_invalid_or_unavailable"):
            self.preview(payload=stale)
        self.baseline["managed_ipv4"] = ["198.51.100.20/32"]
        with self.assertRaisesRegex(ACLPreviewError, "invalid_managed_routes"):
            self.preview("198.51.100.2/32")
        self.assertEqual(self.client.writes, [])

    def test_cli_preview_blocks_execute_and_emits_only_summary(self) -> None:
        args = [*self.CLI_ARGS, "--preserve-from-stdin"]
        output = StringIO()
        with patch("linode_image_lab.acl_sync.LinodeClient.from_env", return_value=self.client) as factory, \
             patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage",
                   return_value=registry_payload(cidrs=("198.51.100.2/32",))), \
             patch("sys.stdin", StringIO(json.dumps(self.baseline))), redirect_stdout(output):
            self.assertEqual(main([*args, "--execute"]), 1)
            factory.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["preview_failure"], "preservation_preview_only")
        output = StringIO()
        with patch("linode_image_lab.acl_sync.LinodeClient.from_env", return_value=self.client), \
             patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage",
                   return_value=registry_payload(cidrs=("198.51.100.2/32",))), \
             patch("sys.stdin", StringIO(json.dumps(self.baseline))), redirect_stdout(output):
            self.assertEqual(main(args), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["preview_mode"], "preserve_routes")
        self.assertTrue(summary["baseline_verified"])
        self.assertEqual(summary["change_counts"]["removals"]["ipv4"], 1)
        self.assertNotIn("198.51.100.", output.getvalue())
        self.assertNotIn("12345", output.getvalue())
        self.assertEqual(self.client.writes, [])

    def test_version_selection_requires_preservation_preview_before_credentials(self) -> None:
        output = StringIO()
        with patch("linode_image_lab.cli.load_selection") as load, \
             patch("linode_image_lab.acl_sync.LinodeClient.from_env") as provider, redirect_stdout(output):
            self.assertEqual(main([*self.CLI_ARGS, "--registry-version-file", "/example/selection.json", "--execute"]), 1)
        load.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["preview_failure"], "registry_version_requires_preservation_preview")

    def test_bound_version_read_from_cli_requires_matching_header_and_keeps_all_baseline_routes(self) -> None:
        self.baseline["managed_ipv4"] = []
        version, proof_key = "synthetic+version/1", bytes(range(32))
        payload = registry_payload(cidrs=("198.51.100.2/32",))
        payload["entries"].append({"status": "active", "address_family": "ipv6", "cidr": "2001:db8::2/128"})
        with tempfile.TemporaryDirectory() as directory:
            selection = Path(directory) / "selection.json"
            selection.write_text(json.dumps({"version_id": version, "proof_key": proof_key.hex()}), encoding="utf-8")
            selection.chmod(0o600)
            args = [*self.CLI_ARGS, "--preserve-from-stdin", "--registry-version-file", str(selection)]
            for observed in (version, "other-version", None):
                with self.subTest(observed=observed):
                    output = StringIO()
                    response = MagicMock()
                    response.__enter__.return_value = response
                    response.headers = {"x-amz-version-id": observed}
                    response.read.return_value = json.dumps(payload).encode("utf-8")
                    with patch("linode_image_lab.acl_sync.LinodeClient.from_env", return_value=self.client), \
                         patch("linode_image_lab.trusted_registry.urlopen", return_value=response) as get, \
                         patch.dict(os.environ, {ACCESS_KEY_ENV: "test-access", SECRET_KEY_ENV: "test-value"}), \
                         patch("sys.stdin", StringIO(json.dumps(self.baseline))), redirect_stdout(output):
                        code = main(args)
                    get.assert_called_once()
                    self.assertTrue(get.call_args.args[0].full_url.endswith("?versionId=synthetic%2Bversion%2F1"))
                    summary = json.loads(output.getvalue())
                    if observed == version:
                        self.assertEqual(code, 0)
                        self.assertEqual(summary["version_proof"], version_proof(version, proof_key))
                        self.assertEqual(summary["change_counts"], {
                            "additions": {"ipv4": 1, "ipv6": 0}, "removals": {"ipv4": 0, "ipv6": 0},
                        })
                    else:
                        self.assertEqual(code, 1)
                        self.assertEqual(summary["preview_failure"], "registry_invalid_or_unavailable")
                        self.assertNotIn("version_proof", summary)
                    for private in (version, proof_key.hex(), "12345", "198.51.100", "2001:db8", str(selection)):
                        self.assertNotIn(private, output.getvalue())
        self.assertEqual(self.client.writes, [])


class ACLSyncTests(unittest.TestCase):
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
            (404, "http_response", "rejected", "target_not_found"),
            (409, "http_response", "uncertain", "http_response_uncertain"),
            (412, "http_response", "uncertain", "http_response_uncertain"),
            (413, "http_response", "uncertain", "http_response_uncertain"),
            (422, "http_response", "uncertain", "http_response_uncertain"),
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
        private_dir = tempfile.TemporaryDirectory()
        self.addCleanup(private_dir.cleanup)
        private_file = Path(private_dir.name) / "provider-error.json"

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
                "--provider-error-file", str(private_file),
            ])
        self.assertEqual(code, 1)
        self.assertTrue(private_file.is_file())
        self.assertEqual(json.loads(output.getvalue())["update_failure"], {
            "phase": "control_plane_acl_put", "category": "validation_rejected", "http_status": 400,
        })
        self.assertNotIn(private, output.getvalue() + errors.getvalue())
        self.assertNotIn("198.51.100.0/24", output.getvalue() + errors.getvalue())
        self.assertNotIn("verysecretvalue", output.getvalue() + errors.getvalue())

    def test_opt_in_provider_error_file_is_private_and_excludes_request_data(self) -> None:
        client = FakeACLClient()

        def reject(cluster_id: int, acl: dict) -> dict:
            raise LinodeApiError("Linode API request failed with status 400", status_code=400,
                                 failure_kind="http_response", provider_errors=[{
                                     "field": "acl.addresses.ipv4", "reason": "CIDR 198.51.100.0/24 token=private-value",
                                     "headers": "Bearer private-value", "request_payload": "private-payload",
                                 }])

        client.update_lke_control_plane_acl = reject  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider-error.json"
            with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
                with self.assertRaises(ACLSyncError) as caught:
                    acl_sync_plan(
                        cluster_id=12345, cluster_label="example-lke",
                        registry_endpoint_url="https://us-east-1.linodeobjects.com",
                        registry_bucket="example-bucket", registry_object_key="registry.json",
                        client=client, environ={}, execute=True, allow_enable=True,
                        provider_error_file=str(target),
                    )
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            record = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["provider_errors"][0]["field"], "acl.addresses.ipv4")
            self.assertIn("198.51.100.0/24", record["provider_errors"][0]["reason"])
            self.assertNotIn("private-value", target.read_text(encoding="utf-8"))
            self.assertNotIn("private-payload", target.read_text(encoding="utf-8"))
            summary = json.dumps(routine_outcome(caught.exception.manifest, execute=True, failed=True))
            self.assertEqual(caught.exception.manifest["private_error_artifact"], "written")
            self.assertNotIn(str(target), summary)
            self.assertNotIn("198.51.100.0/24", summary)

    def test_private_error_record_stays_bounded_with_large_provider_fields(self) -> None:
        client = FakeACLClient()

        def reject(cluster_id: int, acl: dict) -> dict:
            fields = {key: "\x00" * 4096 for key in ("field", "reason", "message")}
            raise LinodeApiError("provider rejected", status_code=400,
                                 failure_kind="http_response", provider_errors=[fields] * 16)

        client.update_lke_control_plane_acl = reject  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider-error.json"
            with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
                with self.assertRaises(ACLSyncError) as caught:
                    acl_sync_plan(
                        cluster_id=12345, cluster_label="example-lke",
                        registry_endpoint_url="https://us-east-1.linodeobjects.com",
                        registry_bucket="example-bucket", registry_object_key="registry.json",
                        client=client, environ={}, execute=True, allow_enable=True,
                        provider_error_file=str(target),
                    )
            self.assertEqual(caught.exception.manifest["private_error_artifact"], "written")
            self.assertLessEqual(target.stat().st_size, 16384)
            self.assertEqual(len(json.loads(target.read_text(encoding="utf-8"))["provider_errors"]), 8)

    def test_private_error_file_requires_execute_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider-error.json"
            with self.assertRaisesRegex(ACLSyncError, "requires --execute"):
                acl_sync_plan(
                    cluster_id=12345, cluster_label="example-lke",
                    registry_endpoint_url="https://us-east-1.linodeobjects.com",
                    registry_bucket="example-bucket", registry_object_key="registry.json",
                    client=FakeACLClient(), environ={}, provider_error_file=str(target),
                )
            self.assertFalse(target.exists())

    def test_private_error_file_is_not_created_without_a_put(self) -> None:
        client = FakeACLClient()
        client.acl["acl"] = {
            "enabled": True, "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider-error.json"
            with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
                result = acl_sync_plan(
                    cluster_id=12345, cluster_label="example-lke",
                    registry_endpoint_url="https://us-east-1.linodeobjects.com",
                    registry_bucket="example-bucket", registry_object_key="registry.json",
                    client=client, environ={}, execute=True, provider_error_file=str(target),
                )
            self.assertEqual(result["status"], "unchanged")
            self.assertFalse(target.exists())

    def test_private_record_failure_does_not_mask_put_outcome(self) -> None:
        plan = {"status": "uncertain"}
        with patch("linode_image_lab.acl_sync.write_private_error_file", side_effect=TypeError("private data")):
            finish_private_error_file(99, plan, None)
        self.assertEqual(plan, {"status": "uncertain", "private_error_artifact": "failed"})

    def test_private_record_write_failure_preserves_uncertain_put_status(self) -> None:
        client = FakeACLClient()

        def fail(cluster_id: int, acl: dict) -> dict:
            raise LinodeApiError("fixed failure", failure_kind="transport")

        client.update_lke_control_plane_acl = fail  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider-error.json"
            with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()), \
                 patch("linode_image_lab.acl_sync.os.fsync", side_effect=OSError("private detail")):
                with self.assertRaises(ACLSyncError) as caught:
                    acl_sync_plan(
                        cluster_id=12345, cluster_label="example-lke",
                        registry_endpoint_url="https://us-east-1.linodeobjects.com",
                        registry_bucket="example-bucket", registry_object_key="registry.json",
                        client=client, environ={}, execute=True, allow_enable=True,
                        provider_error_file=str(target),
                    )
            self.assertEqual(caught.exception.manifest["status"], "uncertain")
            self.assertEqual(caught.exception.manifest["private_error_artifact"], "failed")
            self.assertNotIn("private detail", str(caught.exception))

    def test_private_record_written_after_submitted_put(self) -> None:
        client = FakeACLClient()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider-error.json"
            with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
                result = acl_sync_plan(
                    cluster_id=12345, cluster_label="example-lke",
                    registry_endpoint_url="https://us-east-1.linodeobjects.com",
                    registry_bucket="example-bucket", registry_object_key="registry.json",
                    client=client, environ={}, execute=True, allow_enable=True,
                    provider_error_file=str(target),
                )
            self.assertEqual(result["status"], "submitted")
            self.assertEqual(result["private_error_artifact"], "written")
            record = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(record, {"schema_version": 1, "status": "submitted", "update_failure": None})

    def test_private_error_destination_rejects_symlinks_and_unsafe_parent(self) -> None:
        client = FakeACLClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.json"
            existing.write_text("retain", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(existing)
            linked_dir = root / "linked-dir"
            linked_dir.symlink_to(root, target_is_directory=True)
            options = dict(cluster_id=12345, cluster_label="example-lke",
                           registry_endpoint_url="https://us-east-1.linodeobjects.com",
                           registry_bucket="example-bucket", registry_object_key="registry.json",
                           client=client, environ={}, execute=True, allow_enable=True)
            with patch("linode_image_lab.acl_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
                for path in (existing, link, linked_dir / "new.json"):
                    with self.assertRaisesRegex(ACLSyncError, "private provider error file"):
                        acl_sync_plan(**options, provider_error_file=str(path))
                root.chmod(0o755)
                with self.assertRaisesRegex(ACLSyncError, "private provider error file"):
                    acl_sync_plan(**options, provider_error_file=str(root / "new.json"))
            self.assertEqual(existing.read_text(encoding="utf-8"), "retain")
            self.assertEqual(client.writes, [])

    def test_private_error_file_rejects_parent_swap_before_directory_pin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "protected"
            protected.mkdir(mode=0o700)
            replacement = root / "replacement"
            replacement.mkdir(mode=0o755)
            displaced = root / "displaced"
            real_open = os.open

            def swap_parent(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                if path == protected:
                    protected.rename(displaced)
                    replacement.rename(protected)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("linode_image_lab.acl_sync.os.open", side_effect=swap_parent):
                with self.assertRaisesRegex(ACLSyncError, "private provider error file"):
                    open_private_error_file(str(protected / "error.json"))
            self.assertFalse((protected / "error.json").exists())
            self.assertFalse((displaced / "error.json").exists())
