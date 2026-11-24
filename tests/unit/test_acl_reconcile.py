from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
from contextlib import redirect_stdout
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from linode_image_lab.acl_reconcile import (
    ACLReconcileError, WanObservation, WanSource, bind_wan_observations, host_route, reconcile_acl,
)
from linode_image_lab.cli import main
from linode_image_lab.linode_api import LinodeApiError, LinodeClient
from linode_image_lab.registry_version_selection import RegistryVersionSelection
from linode_image_lab.trusted_registry import RegistryFetchError


RETAINED = {
    "ipv4": ["192.0.2.9", "192.0.2.1/32"],
    "ipv6": ["2001:0DB8:abcd::1/128", "2001:db8::2"],
}
OBSERVED = {
    "synthetic-a": {"ipv4": "198.51.100.2/32"},
    "synthetic-b": {"ipv4": "203.0.113.2/32"},
}
SLOTS = {"synthetic-a": "wan1", "synthetic-b": "wan2"}
SELECTION = RegistryVersionSelection("example-registry-version", bytes(range(32)))
BASELINE = {"enabled": True, "addresses": RETAINED, "revision-id": "baseline"}
REGISTRY_ARGS = {
    "endpoint_url": "https://us-east-1.linodeobjects.com",
    "bucket": "example-bucket",
    "object_key": "registry.json",
}
REGISTRY_ENV = {"LINODE_OBJ_ACCESS_KEY": "synthetic-access", "LINODE_OBJ_SECRET_KEY": "synthetic-secret"}


def registry_payload(observed: dict) -> dict:
    now = datetime.now(timezone.utc)
    start, stop = (now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=15)).isoformat()
    return {
        "schema_version": 1,
        "registry": {
            "name": "synthetic-registry", "generated_at": start, "valid_until": stop, "publisher_version": "1",
        },
        "entries": [
            {
                "cidr": route, "address_family": family, "status": "active", "kind": "discovered",
                "source_type": "meraki_uplink_addresses", "source_ref": SLOTS[slot],
                "observed_at": start, "expires_at": stop,
            }
            for slot, routes in observed.items() for family, route in routes.items()
        ],
    }


def registry_response(payload, version=SELECTION.version_id):
    response = BytesIO(json.dumps(payload).encode())
    response.headers = {} if version is None else {"x-amz-version-id": version}
    return response


class FakeACL:
    def __init__(self, directory: Path) -> None:
        self.acl = deepcopy(BASELINE)
        self.directory = directory
        self.requests = []
        self.reads = 0
        self.fail_put = False
        self.accept_put = True
        self.drift_after_put = False
        self.stale_after_put = False
        self.drift_at_read = None
        self.stale = None

    def get_lke_cluster(self, cluster_id) -> dict:
        return {"id": 12345, "label": "synthetic-cluster"}

    def get_lke_control_plane_acl(self, cluster_id) -> dict:
        self.reads += 1
        if self.reads == self.drift_at_read:
            return {"acl": {**self.acl, "revision-id": "changed-after-plan"}}
        if self.stale is not None:
            stale, self.stale = self.stale, None
            return {"acl": deepcopy(stale)}
        return {"acl": deepcopy(self.acl)}

    def update_lke_control_plane_acl(self, cluster_id, acl) -> dict:
        receipts = [json.loads(path.read_text().splitlines()[0]) for path in self.directory.glob("*.jsonl")]
        if not any(receipt["after"] == acl for receipt in receipts):
            raise AssertionError("PUT preceded durable intent")
        self.requests.append(deepcopy(acl))
        if self.stale_after_put:
            self.stale = self.acl
        if self.accept_put:
            self.acl = deepcopy(acl)
        if self.drift_after_put:
            self.acl["addresses"]["ipv6"] = []
        if self.fail_put:
            raise LinodeApiError("synthetic lost response")
        return {"acl": deepcopy(acl)}


class GuardedACLTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        self.client = FakeACL(self.directory)
        # Permit RFC documentation addresses in simulated provider responses.
        # Production public-address validation is checked separately below.
        self.enterContext(patch.object(IPv4Address, "is_global", new_callable=PropertyMock, return_value=True))
        self.enterContext(patch("linode_image_lab.acl_reconcile.time.sleep"))
        self.document = {
            "schema_version": 1, "cluster_id": 12345, "cluster_label": "synthetic-cluster",
            "acl": deepcopy(BASELINE), "retained": deepcopy(RETAINED), "slots": SLOTS, "checkpoint": None,
            "observed": deepcopy(OBSERVED), "registry_version_proof": SELECTION.proof(),
            "source_observations_valid_until": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    def run_acl(self, *, document=None, execute=True, rollback=False, effect=None):
        document = self.document if document is None else document
        payload = {} if rollback else registry_payload(document["observed"])
        with patch("linode_image_lab.acl_reconcile.fetch_registry_from_object_storage",
                   return_value=payload, side_effect=effect) as fetch:
            result = reconcile_acl(
                document=document, cluster_id=12345, cluster_label="synthetic-cluster", rollback=rollback,
                execute=execute, receipt_dir=str(self.directory) if execute else None,
                selection=None if rollback else SELECTION, registry_args={}, client=self.client,
            )
        self.reads = [call.kwargs for call in fetch.call_args_list]
        return result

    def receipts(self) -> list:
        return [[json.loads(line) for line in path.read_text().splitlines()]
                for path in self.directory.glob("*.jsonl")]

    def test_forward_execute_requires_observation_deadline_but_legacy_preview_can_plan(self) -> None:
        legacy = self.document.copy()
        legacy.pop("source_observations_valid_until")
        with self.assertRaisesRegex(ACLReconcileError, "^source_observation_deadline_required$"):
            self.run_acl(document=legacy)
        for invalid in (None, "2030-01-01", "bad-deadline"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ACLReconcileError, "^invalid_source_observation_deadline$",
            ):
                self.run_acl(document={**legacy, "source_observations_valid_until": invalid})
        self.assertEqual(self.client.reads, 0)
        self.assertEqual(self.run_acl(document=legacy, execute=False)["status"], "planned")
        self.assertEqual(self.client.requests, [])
        self.assertEqual(self.receipts(), [])

    def test_guarded_preview_and_successive_ipv4_updates_without_ipv6_observations(self) -> None:
        plan = self.run_acl(execute=False)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["change_counts"]["additions"], {"ipv4": 2, "ipv6": 0})
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.client.requests, [])
        self.client.stale_after_put = True
        result = self.run_acl()
        self.assertEqual(self.reads, [
            {"exact_version_id": SELECTION.version_id}, {"expected_current_version_id": SELECTION.version_id},
        ])
        self.assertTrue(result["enforcement_verified"])
        [[intent, outcome]] = self.receipts()
        self.assertEqual(outcome, {"status": "enforced"})
        self.assertEqual(intent["managed_before"], {})
        self.assertEqual(intent["managed_after"], OBSERVED)
        self.assertTrue(intent["after"]["enabled"])
        self.assertEqual(intent["after"]["addresses"]["ipv4"][:2], RETAINED["ipv4"])
        self.assertEqual(intent["after"]["addresses"]["ipv6"], RETAINED["ipv6"])
        self.assertNotEqual(intent["before"]["revision-id"], intent["after"]["revision-id"])

        self.document.update(acl=intent["after"], checkpoint={"intent": intent, "outcome": outcome})
        self.document["observed"]["synthetic-a"] = {"ipv4": "198.51.100.3/32"}
        result = self.run_acl()
        self.assertEqual(result["change_counts"], {
            "additions": {"ipv4": 1, "ipv6": 0}, "removals": {"ipv4": 1, "ipv6": 0},
        })
        self.assertEqual(self.client.acl["addresses"]["ipv6"], RETAINED["ipv6"])
        self.assertEqual(self.client.acl["addresses"]["ipv4"][:2], RETAINED["ipv4"])
        self.assertEqual(set(self.client.acl["addresses"]["ipv4"]), {
            *RETAINED["ipv4"], *(routes["ipv4"] for routes in self.document["observed"].values()),
        })

    def test_present_registry_ipv6_never_enters_guarded_policy(self) -> None:
        payload = registry_payload(OBSERVED)
        payload["entries"].append({
            **payload["entries"][0], "cidr": "2001:db8:1::2/128", "address_family": "ipv6",
        })
        result = self.run_acl(effect=[payload, payload])
        self.assertEqual(result["change_counts"]["additions"]["ipv6"], 0)
        self.assertEqual(self.client.requests[0]["addresses"]["ipv6"], RETAINED["ipv6"])

    def test_preexisting_matches_are_never_enrolled_by_equality(self) -> None:
        for family, route in OBSERVED["synthetic-a"].items():
            self.document["retained"][family].append(route)
        self.document["acl"]["addresses"] = deepcopy(self.document["retained"])
        self.client.acl = deepcopy(self.document["acl"])
        self.run_acl()
        [[intent, _]] = self.receipts()
        self.assertNotIn("synthetic-a", intent["managed_after"])
        self.assertEqual(intent["retained"], self.document["retained"])

    def test_incomplete_ambiguous_or_externally_unproven_input_fails_closed(self) -> None:
        for reason, mutate in (
            ("incomplete_or_unapproved_sources", lambda d: d["observed"]["synthetic-b"].pop("ipv4")),
            ("incomplete_or_unapproved_sources", lambda d: d["observed"]["synthetic-b"].update(ipv6="2001:db8::9/128")),
            ("incomplete_or_unapproved_sources", lambda d: d["observed"].pop("synthetic-b")),
            ("ambiguous_source_observations", lambda d: d["observed"]["synthetic-b"].update(OBSERVED["synthetic-a"])),
            ("source_version_proof_mismatch", lambda d: d.update(registry_version_proof="0" * 64)),
            ("unverified_ownership_checkpoint", lambda d: d.update(checkpoint={"intent": {}, "outcome": {}})),
            ("unclassified_baseline_routes", lambda d: d["retained"]["ipv4"].clear()),
            ("ipv6_policy_changed", lambda d: d["retained"]["ipv6"].reverse()),
        ):
            document = deepcopy(self.document)
            mutate(document)
            with self.subTest(reason=reason), self.assertRaisesRegex(ACLReconcileError, reason):
                self.run_acl(document=document)
        for kind in ("static", "stale"):
            payload = registry_payload(OBSERVED)
            entry = payload["entries"][1]
            if kind == "static":
                entry.update(kind="static", source_type="config")
            else:
                entry["expires_at"] = "2000-01-01T00:00:00Z"
            with self.subTest(kind=kind), self.assertRaisesRegex(ACLReconcileError, "does_not_match_registry"):
                self.run_acl(effect=[payload])
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.client.requests, [])

    def test_registry_and_acl_are_rechecked_before_write(self) -> None:
        payload = registry_payload(OBSERVED)
        for changed in (RegistryFetchError("version mismatch"), {**payload, "unexpected": True}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ACLReconcileError, "registry_changed"):
                self.run_acl(effect=[payload, changed])
        self.assertEqual(self.receipts(), [])
        self.client.drift_at_read = self.client.reads + 2
        with self.assertRaisesRegex(ACLReconcileError, "baseline_drift"):
            self.run_acl()
        self.assertEqual(len(self.receipts()[0]), 1)
        self.assertEqual(self.client.requests, [])

    def test_response_loss_prevents_retry_from_same_baseline(self) -> None:
        self.client.fail_put = True
        self.client.accept_put = False
        with self.assertRaisesRegex(ACLReconcileError, "provider_result_unverified") as failure:
            self.run_acl()
        self.assertEqual(failure.exception.status, "uncertain")
        self.assertEqual(len(self.receipts()[0]), 1)
        with self.assertRaisesRegex(ACLReconcileError, "already_exists"):
            self.run_acl()
        self.assertEqual(len(self.client.requests), 1)

    def test_matching_revision_without_exact_addresses_does_not_verify(self) -> None:
        self.client.drift_after_put = True
        with self.assertRaisesRegex(ACLReconcileError, "unexpected_readback") as failure:
            self.run_acl()
        self.assertEqual(failure.exception.status, "uncertain")
        self.assertEqual(len(self.receipts()[0]), 1)

    def test_guarded_rollback_refuses_a_receipt_with_ipv6_changes(self) -> None:
        self.run_acl()
        [[intent, _]] = self.receipts()
        intent["after"]["addresses"]["ipv6"].reverse()
        with self.assertRaisesRegex(ACLReconcileError, "ipv6_policy_changed"):
            self.run_acl(document=intent, rollback=True)
        self.assertEqual(len(self.client.requests), 1)

    def test_timeout_keeps_pending_receipt_without_a_second_put(self) -> None:
        self.client.accept_put = False
        with self.assertRaisesRegex(ACLReconcileError, "enforcement_timeout") as failure:
            self.run_acl()
        self.assertEqual(failure.exception.status, "uncertain")
        self.assertEqual(len(self.receipts()[0]), 1)
        self.assertEqual(len(self.client.requests), 1)

    def test_rollback_recovers_lost_ack_only_if_forward_is_fully_enforced(self) -> None:
        self.client.fail_put = True
        with self.assertRaises(ACLReconcileError):
            self.run_acl()
        [[intent]] = self.receipts()
        self.client.fail_put = False
        self.client.acl["revision-id"] = "different-revision"
        with self.assertRaisesRegex(ACLReconcileError, "baseline_drift"):
            self.run_acl(document=intent, rollback=True)
        self.client.acl = deepcopy(intent["after"])
        result = self.run_acl(document=intent, rollback=True)
        self.assertEqual(result["status"], "enforced")
        self.assertEqual(self.client.acl["addresses"], BASELINE["addresses"])
        self.assertNotIn(self.client.acl["revision-id"], (BASELINE["revision-id"], intent["after"]["revision-id"]))
        self.assertEqual(self.reads, [])

    def test_cli_rollback_needs_only_the_provider_and_protected_receipt(self) -> None:
        self.run_acl()
        [[intent, _]] = self.receipts()
        output = StringIO()
        with patch("sys.stdin", StringIO(json.dumps(intent))), redirect_stdout(output), patch(
            "linode_image_lab.acl_reconcile.LinodeClient.from_env", return_value=LinodeClient()
        ), patch("linode_image_lab.acl_reconcile.LinodeClient._request",
                 side_effect=[self.client.get_lke_cluster(12345), self.client.get_lke_control_plane_acl(12345)]):
            self.assertEqual(main([
                "acl-sync", "--cluster-id", "12345", "--cluster-label", "synthetic-cluster",
                "--output-format", "summary", "--rollback-from-stdin",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["operation"], "rollback")
        for private in ("synthetic-cluster", "198.51.100", intent["after"]["revision-id"]):
            self.assertNotIn(private, output.getvalue())

    def test_cli_guarded_execute_uses_exact_provider_resource_and_summary(self) -> None:
        selection = self.directory / "selection.json"
        selection.write_text(json.dumps({"version_id": SELECTION.version_id, "proof_key": SELECTION.proof_key.hex()}))
        selection.chmod(0o600)

        def provider_request(_client, method, path, payload=None, **options):
            if path == "/lke/clusters/12345" and method == "GET":
                return self.client.get_lke_cluster(12345)
            self.assertEqual(path, "/lke/clusters/12345/control_plane_acl")
            if method == "GET":
                return self.client.get_lke_control_plane_acl(12345)
            self.assertEqual(method, "PUT")
            self.assertFalse(options["retry"])
            return self.client.update_lke_control_plane_acl(12345, payload["acl"])

        output = StringIO()
        with patch("sys.stdin", StringIO(json.dumps(self.document))), redirect_stdout(output), patch(
            "linode_image_lab.acl_reconcile.LinodeClient.from_env", return_value=LinodeClient()
        ), patch.object(LinodeClient, "_request", autospec=True, side_effect=provider_request), patch(
            "linode_image_lab.acl_reconcile.fetch_registry_from_object_storage", return_value=registry_payload(OBSERVED)
        ):
            self.assertEqual(main([
                "acl-sync", "--cluster-id", "12345", "--cluster-label", "synthetic-cluster",
                "--registry-endpoint-url", "https://us-east-1.linodeobjects.com",
                "--registry-bucket", "example-bucket", "--registry-object-key", "registry.json",
                "--output-format", "summary", "--reconcile-from-stdin", "--registry-version-file", str(selection),
                "--execute", "--acl-receipt-dir", str(self.directory),
            ]), 0)
        self.assertEqual(len(self.client.requests), 1)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], "enforced")
        self.assertTrue(summary["enforcement_verified"])
        for private in (SELECTION.version_id, "12345", "198.51.100", "2001:db8", str(self.directory)):
            self.assertNotIn(private, output.getvalue())


class PublicHostValidationTest(unittest.TestCase):
    def test_rejects_nonpublic_or_nonhost_routes(self) -> None:
        for route in (
            "2001:db8::1/128", "::ffff:192.0.2.1/128", "10.0.0.1/32", "192.0.2.0/24",
        ):
            with self.subTest(route=route), self.assertRaisesRegex(ACLReconcileError, "invalid_source_host"):
                host_route(route)


class WanObservationBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.approved = {slot: WanSource("synthetic-device", interface) for slot, interface in SLOTS.items()}
        self.now = datetime.now(timezone.utc)
        self.observations = [
            WanObservation(source, OBSERVED[slot]["ipv4"], self.now) for slot, source in self.approved.items()
        ]
        self.registry = registry_payload(OBSERVED)
        self.enterContext(patch.object(IPv4Address, "is_global", new_callable=PropertyMock, return_value=True))
        self.enterContext(patch.dict(os.environ, REGISTRY_ENV))

    def bind(self, **overrides):
        args = {
            "approved_slots": self.approved,
            "observations": self.observations,
            "max_age": timedelta(minutes=5),
            "selection": SELECTION,
            "registry_args": REGISTRY_ARGS,
        }
        return bind_wan_observations(**(args | overrides))

    def test_binding_feeds_summary_preview_using_same_exact_registry_version(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            selection = Path(root).resolve() / "selection.json"
            selection.write_text(json.dumps({"version_id": SELECTION.version_id, "proof_key": SELECTION.proof_key.hex()}))
            selection.chmod(0o600)
            client = FakeACL(selection.parent)
            output = StringIO()
            with patch("linode_image_lab.trusted_registry.urlopen", side_effect=[
                registry_response(self.registry), registry_response(self.registry),
            ]) as reader, patch(
                "linode_image_lab.acl_reconcile.LinodeClient.from_env", return_value=LinodeClient(),
            ), patch.object(LinodeClient, "_request", autospec=True, side_effect=[
                client.get_lke_cluster(12345), client.get_lke_control_plane_acl(12345),
            ]) as management:
                binding = self.bind(observations=self.observations[::-1])
                self.assertEqual(binding, {
                    "slots": SLOTS, "observed": OBSERVED, "registry_version_proof": SELECTION.proof(),
                    "source_observations_valid_until": (self.now + timedelta(minutes=5)).isoformat(),
                })
                protected = {
                    "schema_version": 1, "cluster_id": 12345, "cluster_label": "synthetic-cluster",
                    "acl": BASELINE, "retained": RETAINED, "checkpoint": None, **binding,
                }
                with patch("sys.stdin", StringIO(json.dumps(protected))), redirect_stdout(output):
                    self.assertEqual(main([
                        "acl-sync", "--cluster-id", "12345", "--cluster-label", "synthetic-cluster",
                        "--registry-endpoint-url", REGISTRY_ARGS["endpoint_url"],
                        "--registry-bucket", REGISTRY_ARGS["bucket"], "--registry-object-key", REGISTRY_ARGS["object_key"],
                        "--output-format", "summary", "--reconcile-from-stdin", "--registry-version-file", str(selection),
                    ]), 0)
            self.assertEqual([call.args[1] for call in management.call_args_list], ["GET", "GET"])
            self.assertEqual(list(selection.parent.glob("*.jsonl")), [])

        exact_url = (
            f'{REGISTRY_ARGS["endpoint_url"]}/{REGISTRY_ARGS["bucket"]}/{REGISTRY_ARGS["object_key"]}'
            f"?versionId={SELECTION.version_id}"
        )
        self.assertEqual([call.args[0].full_url for call in reader.call_args_list], [exact_url, exact_url])
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], "planned")
        self.assertEqual(summary["change_counts"], {
            "additions": {"ipv4": 2, "ipv6": 0}, "removals": {"ipv4": 0, "ipv6": 0},
        })
        for private in ("synthetic-device", SELECTION.version_id, "198.51.100", "2001:db8"):
            self.assertNotIn(private, output.getvalue())

    def test_missing_duplicate_or_wrong_device_observations_stop_before_registry_read(self) -> None:
        first, second = self.observations
        wrong_device = replace(second, source=WanSource("other-device", second.source.interface))
        for observations in ([first], [first, first], [first, wrong_device], [first, second, wrong_device]):
            with self.subTest(observations=observations), patch("linode_image_lab.trusted_registry.urlopen") as reader:
                with self.assertRaisesRegex(ACLReconcileError, "^incomplete_or_unapproved_sources$"):
                    self.bind(observations=observations)
                reader.assert_not_called()

    def test_independent_observations_must_be_fresh(self) -> None:
        first, second = self.observations
        for timestamp in (self.now - timedelta(minutes=5), self.now + timedelta(minutes=1),
                          self.now.replace(tzinfo=None)):
            with self.subTest(timestamp=timestamp), patch(
                "linode_image_lab.trusted_registry.urlopen", return_value=registry_response(self.registry),
            ), self.assertRaisesRegex(ACLReconcileError, "^source_observation_not_fresh$"):
                self.bind(observations=[replace(first, observed_at=timestamp), second])

    def test_fresh_matching_registry_cannot_supply_unverified_source_time(self) -> None:
        first, second = self.observations
        with patch("linode_image_lab.trusted_registry.urlopen", return_value=registry_response(self.registry)) as read:
            with self.assertRaisesRegex(ACLReconcileError, "^source_observation_time_unverified$"):
                self.bind(observations=[first, replace(second, observed_at=None)])
            read.assert_not_called()

    def test_in_memory_preview_matches_retained_routes_without_writes_or_files(self) -> None:
        retained = {**RETAINED, "ipv4": [*RETAINED["ipv4"], *(entry["ipv4"] for entry in OBSERVED.values())]}
        baseline = {**BASELINE, "addresses": retained}
        client = FakeACL(Path.cwd())
        client.acl = deepcopy(baseline)
        with patch("linode_image_lab.trusted_registry.urlopen", side_effect=[
            registry_response(self.registry), registry_response(self.registry),
        ]) as storage, patch.object(client, "update_lke_control_plane_acl") as put, patch(
            "linode_image_lab.acl_reconcile.open_private_error_file",
        ) as receipt, patch("linode_image_lab.registry_version_selection.os.open") as selection_file:
            document = {
                "schema_version": 1, "cluster_id": 12345, "cluster_label": "synthetic-cluster",
                "acl": baseline, "retained": retained, "checkpoint": None, **self.bind(),
            }
            result = reconcile_acl(
                document=document, cluster_id=12345, cluster_label="synthetic-cluster",
                rollback=False, execute=False, receipt_dir=None, selection=SELECTION,
                registry_args=REGISTRY_ARGS, client=client,
            )
            put.assert_not_called()
            receipt.assert_not_called()
            selection_file.assert_not_called()
        self.assertEqual([call.args[0].get_method() for call in storage.call_args_list], ["GET", "GET"])
        self.assertEqual(client.reads, 1)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["execution_mode"], "dry-run")
        self.assertEqual(result["version_proof"], SELECTION.proof())
        summary = json.dumps(result)
        for private in (SELECTION.version_id, "synthetic-device", "198.51.100", "2001:db8", '"baseline"'):
            self.assertNotIn(private, summary)
        self.assertEqual(client.acl, baseline)

    def test_expired_binding_cannot_reach_provider_write(self) -> None:
        first, second = self.observations
        observations = [first, replace(second, observed_at=self.now - timedelta(minutes=1))]
        deadline = self.now + timedelta(minutes=4)
        for entry in self.registry["entries"]:
            self.assertLess(datetime.fromisoformat(entry["observed_at"]), deadline)
            self.assertGreater(datetime.fromisoformat(entry["expires_at"]), deadline)

        for during_final_read in (False, True):
            with self.subTest(during_final_read=during_final_read), tempfile.TemporaryDirectory() as root, patch(
                "linode_image_lab.acl_reconcile.datetime", wraps=datetime,
            ) as clock, patch("linode_image_lab.trusted_registry.urlopen", side_effect=[
                registry_response(self.registry) for _ in range(3)
            ]) as storage:
                instant = self.now
                clock.now.side_effect = lambda zone: instant.astimezone(zone)
                directory = Path(root).resolve()
                client = FakeACL(directory)
                get_acl = client.get_lke_control_plane_acl

                def read_acl_at_boundary(cluster_id):
                    nonlocal instant
                    value = get_acl(cluster_id)
                    if during_final_read and client.reads == 2:
                        instant = deadline
                    return value

                document = {
                    "schema_version": 1, "cluster_id": 12345, "cluster_label": "synthetic-cluster",
                    "acl": BASELINE, "retained": RETAINED, "checkpoint": None,
                    **self.bind(observations=observations),
                }
                self.assertEqual(document["source_observations_valid_until"], deadline.isoformat())
                if not during_final_read:
                    instant = deadline

                failure = None
                with patch.object(client, "get_lke_control_plane_acl", side_effect=read_acl_at_boundary):
                    try:
                        reconcile_acl(
                            document=document, cluster_id=12345, cluster_label="synthetic-cluster",
                            rollback=False, execute=True, receipt_dir=str(directory), selection=SELECTION,
                            registry_args=REGISTRY_ARGS, client=client,
                        )
                    except ACLReconcileError as error:
                        failure = error

                self.assertEqual(len(client.requests), 0, "expired observations reached the provider PUT")
                self.assertEqual(client.acl, BASELINE)
                self.assertIsNotNone(failure)
                self.assertEqual((failure.status, str(failure)), ("failed", "source_observation_not_fresh"))
                receipts = list(directory.glob("*.jsonl"))
                if during_final_read:
                    self.assertEqual(client.reads, 2)
                    exact_key = (
                        f'{REGISTRY_ARGS["endpoint_url"]}/{REGISTRY_ARGS["bucket"]}/{REGISTRY_ARGS["object_key"]}'
                    )
                    exact_version = f"{exact_key}?versionId={SELECTION.version_id}"
                    self.assertEqual([call.args[0].full_url for call in storage.call_args_list],
                                     [exact_version, exact_version, exact_key])
                    receipt, = receipts
                    intent, = receipt.read_text().splitlines()
                    self.assertEqual(json.loads(intent)["before"], BASELINE)
                else:
                    self.assertEqual(client.reads, 0)
                    self.assertEqual(receipts, [])

    def test_missing_or_wrong_registry_response_version_does_not_fall_back_to_latest(self) -> None:
        for version in (None, "other-version"):
            with self.subTest(version=version), patch(
                "linode_image_lab.trusted_registry.urlopen", return_value=registry_response(self.registry, version),
            ) as reader:
                with self.assertRaisesRegex(ACLReconcileError, "^registry_invalid_or_unavailable$"):
                    self.bind()
                reader.assert_called_once()

    def test_registry_must_corroborate_each_interface_with_fresh_discovery(self) -> None:
        candidates = (
            {"source_ref": "wan2"},
            {"cidr": "198.51.100.42/32"},
            {"expires_at": (self.now - timedelta(seconds=1)).isoformat()},
            {"kind": "static", "source_type": "config"},
        )
        for candidate in candidates:
            payload = deepcopy(self.registry)
            payload["entries"][0].update(candidate)
            with self.subTest(candidate=candidate), patch(
                "linode_image_lab.trusted_registry.urlopen", return_value=registry_response(payload),
            ), self.assertRaisesRegex(ACLReconcileError, "^source_observation_does_not_match_registry$"):
                self.bind()
