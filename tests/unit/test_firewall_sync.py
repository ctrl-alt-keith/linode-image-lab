from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from linode_image_lab.cli import main
from linode_image_lab.firewall_sync import (
    MANAGED_RULE_DESCRIPTION,
    FirewallSyncError,
    firewall_sync_plan,
)


class FakeFirewallClient:
    def __init__(self, rules: dict[str, object]) -> None:
        self.rules = rules
        self.updates: list[dict[str, object]] = []

    def get_firewall_rules(self, firewall_id: int) -> dict[str, object]:
        return self.rules

    def update_firewall_rules(self, firewall_id: int, rules: dict[str, object]) -> dict[str, object]:
        self.updates.append(rules)
        return {}


def registry_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "registry": {
            "name": "trusted-network-registry",
            "generated_at": "2029-12-31T23:00:00Z",
            "valid_until": "2030-01-01T00:00:00Z",
            "publisher_version": "0.1.0",
        },
        "entries": [
            {
                "id": "admin-ipv4",
                "cidr": "198.51.100.0/24",
                "address_family": "ipv4",
                "kind": "static",
                "source_type": "config",
                "source_ref": "static-admin",
                "status": "active",
            },
            {
                "id": "admin-ipv6",
                "cidr": "2001:db8:100::/64",
                "address_family": "ipv6",
                "kind": "static",
                "source_type": "config",
                "source_ref": "static-admin-ipv6",
                "status": "active",
            },
        ],
        "summary": {
            "entry_count": 2,
            "static_count": 2,
            "discovered_count": 0,
        },
    }


def firewall_rules(*, inbound: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "inbound": inbound or [],
        "outbound": [
            {
                "label": "preserve-outbound",
                "description": "operator-owned",
                "action": "ACCEPT",
                "protocol": "TCP",
                "addresses": {"ipv4": ["198.51.100.10/32"], "ipv6": []},
                "ports": "443",
            }
        ],
        "inbound_policy": "DROP",
        "outbound_policy": "ACCEPT",
    }


def managed_rule(*, ipv4: list[str] | None = None, ipv6: list[str] | None = None) -> dict[str, object]:
    return {
        "label": "tnr-allowlist",
        "description": MANAGED_RULE_DESCRIPTION,
        "action": "ACCEPT",
        "protocol": "TCP",
        "ports": "22",
        "addresses": {
            "ipv4": ipv4 or [],
            "ipv6": ipv6 or [],
        },
    }


class FirewallSyncTests(unittest.TestCase):
    def test_dry_run_diff_adds_registry_cidrs_without_mutating(self) -> None:
        client = FakeFirewallClient(firewall_rules())

        with patch("linode_image_lab.firewall_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            manifest = firewall_sync_plan(
                firewall_id=12345,
                registry_endpoint_url="https://us-east-1.linodeobjects.com",
                registry_bucket="example-bucket",
                registry_object_key="registry.json",
                ports="22",
                client=client,
                environ={},
            )

        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["planned_action"], "add_managed_rule")
        self.assertEqual(manifest["diff"]["additions"]["ipv4"], ["198.51.100.0/24"])
        self.assertEqual(manifest["diff"]["additions"]["ipv6"], ["2001:db8:100::/64"])
        self.assertEqual(client.updates, [])

    def test_execute_requires_explicit_flag(self) -> None:
        client = FakeFirewallClient(firewall_rules())

        with patch("linode_image_lab.firewall_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            manifest = firewall_sync_plan(
                firewall_id=12345,
                registry_endpoint_url="https://us-east-1.linodeobjects.com",
                registry_bucket="example-bucket",
                registry_object_key="registry.json",
                ports="22",
                client=client,
                environ={},
            )

        self.assertEqual(manifest["execution_mode"], "dry-run")
        self.assertEqual(client.updates, [])

    def test_execute_updates_only_managed_rule_and_preserves_unrelated_rules(self) -> None:
        unrelated = {
            "label": "operator-ssh",
            "description": "operator-owned",
            "action": "ACCEPT",
            "protocol": "TCP",
            "ports": "2222",
            "addresses": {"ipv4": ["203.0.113.0/24"], "ipv6": []},
        }
        client = FakeFirewallClient(
            firewall_rules(inbound=[unrelated, managed_rule(ipv4=["198.51.100.1/32"])])
        )

        with patch("linode_image_lab.firewall_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            manifest = firewall_sync_plan(
                firewall_id=12345,
                registry_endpoint_url="https://us-east-1.linodeobjects.com",
                registry_bucket="example-bucket",
                registry_object_key="registry.json",
                ports="22",
                execute=True,
                client=client,
                environ={},
            )

        self.assertFalse(manifest["dry_run"])
        self.assertEqual(manifest["status"], "applied")
        self.assertEqual(len(client.updates), 1)
        updated_inbound = client.updates[0]["inbound"]
        self.assertEqual(updated_inbound[0], unrelated)
        self.assertEqual(updated_inbound[1]["label"], "tnr-allowlist")
        self.assertEqual(updated_inbound[1]["addresses"]["ipv4"], ["198.51.100.0/24"])
        self.assertEqual(client.updates[0]["outbound"], firewall_rules()["outbound"])

    def test_execute_skips_update_when_managed_rule_is_unchanged(self) -> None:
        client = FakeFirewallClient(
            firewall_rules(
                inbound=[
                    managed_rule(
                        ipv4=["198.51.100.0/24"],
                        ipv6=["2001:db8:100::/64"],
                    )
                ]
            )
        )

        with patch("linode_image_lab.firewall_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            manifest = firewall_sync_plan(
                firewall_id=12345,
                registry_endpoint_url="https://us-east-1.linodeobjects.com",
                registry_bucket="example-bucket",
                registry_object_key="registry.json",
                ports="22",
                execute=True,
                client=client,
                environ={},
            )

        self.assertEqual(manifest["status"], "unchanged")
        self.assertFalse(manifest["applied"])
        self.assertEqual(client.updates, [])

    def test_ambiguous_managed_label_fails_closed(self) -> None:
        client = FakeFirewallClient(
            firewall_rules(
                inbound=[
                    {
                        "label": "tnr-allowlist",
                        "description": "operator-owned",
                        "action": "ACCEPT",
                        "protocol": "TCP",
                        "ports": "22",
                        "addresses": {"ipv4": ["198.51.100.0/24"], "ipv6": []},
                    }
                ]
            )
        )

        with patch("linode_image_lab.firewall_sync.fetch_registry_from_object_storage", return_value=registry_payload()):
            with self.assertRaisesRegex(FirewallSyncError, "managed label"):
                firewall_sync_plan(
                    firewall_id=12345,
                    registry_endpoint_url="https://us-east-1.linodeobjects.com",
                    registry_bucket="example-bucket",
                    registry_object_key="registry.json",
                    ports="22",
                    execute=True,
                    client=client,
                    environ={},
                )

        self.assertEqual(client.updates, [])

    def test_cli_dry_run_uses_config_and_env_only_secrets(self) -> None:
        config_text = """
schema_version = 1

[firewall-sync]
firewall_id = 12345
registry_endpoint_url = "https://us-east-1.linodeobjects.com"
registry_bucket = "example-bucket"
registry_object_key = "registry.json"
ports = "22"
"""
        import tempfile

        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write(config_text)
            handle.flush()
            output = StringIO()
            client = FakeFirewallClient(firewall_rules())
            with (
                patch("linode_image_lab.firewall_sync.LinodeClient.from_env", return_value=client),
                patch("linode_image_lab.firewall_sync.fetch_registry_from_object_storage", return_value=registry_payload()),
                redirect_stdout(output),
            ):
                code = main(["--config", handle.name, "firewall-sync"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["command"], "firewall-sync")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(client.updates, [])


if __name__ == "__main__":
    unittest.main()
