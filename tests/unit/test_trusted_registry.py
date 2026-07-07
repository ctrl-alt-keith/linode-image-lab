from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from linode_image_lab.trusted_registry import (
    ACCESS_KEY_ENV,
    SECRET_KEY_ENV,
    RegistryFetchError,
    RegistryValidationError,
    fetch_registry_from_object_storage,
    validate_registry,
)


ROOT = Path(__file__).resolve().parents[2]
PRODUCER_REGISTRY_FIXTURE = ROOT / "tests/fixtures/sanitized/trusted-network-registry.v1.example.json"


class FakeHTTPResponse:
    def __init__(self, body: dict[str, object] | bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.body, bytes):
            return self.body
        return json.dumps(self.body).encode("utf-8")


def registry_payload(*, valid_until: str = "2030-01-01T00:00:00Z", cidr: str = "198.51.100.0/24") -> dict[str, object]:
    return {
        "schema_version": 1,
        "registry": {
            "name": "trusted-network-registry",
            "generated_at": "2029-12-31T23:00:00Z",
            "valid_until": valid_until,
            "publisher_version": "0.1.0",
        },
        "entries": [
            {
                "id": "admin-static-example",
                "cidr": cidr,
                "address_family": "ipv6" if ":" in cidr else "ipv4",
                "kind": "static",
                "source_type": "config",
                "source_ref": "static-admin",
                "status": "active",
            },
            {
                "id": "admin-static-ipv6-example",
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


class TrustedRegistryTests(unittest.TestCase):
    def test_registry_fetch_success_uses_object_storage_credentials(self) -> None:
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            self.assertEqual(timeout, 30)
            return FakeHTTPResponse(registry_payload())

        with patch("linode_image_lab.trusted_registry.urlopen", side_effect=fake_urlopen):
            payload = fetch_registry_from_object_storage(
                endpoint_url="https://us-east-1.linodeobjects.com",
                bucket="example-bucket",
                object_key="registry.json",
                environ={ACCESS_KEY_ENV: "test-access", SECRET_KEY_ENV: "test-secret"},
            )

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url, "https://us-east-1.linodeobjects.com/example-bucket/registry.json")
        self.assertIn("AWS4-HMAC-SHA256", requests[0].get_header("Authorization"))

    def test_https_endpoint_is_accepted(self) -> None:
        with patch("linode_image_lab.trusted_registry.urlopen", return_value=FakeHTTPResponse(registry_payload())):
            payload = fetch_registry_from_object_storage(
                endpoint_url="https://us-east-1.linodeobjects.com/",
                bucket="example-bucket",
                object_key="registry.json",
                environ={ACCESS_KEY_ENV: "test-access", SECRET_KEY_ENV: "test-secret"},
            )

        self.assertEqual(payload["schema_version"], 1)

    def test_http_endpoint_is_rejected_before_fetch(self) -> None:
        with patch("linode_image_lab.trusted_registry.urlopen") as fetch:
            with self.assertRaisesRegex(RegistryFetchError, "must use https"):
                fetch_registry_from_object_storage(
                    endpoint_url="http://us-east-1.linodeobjects.com",
                    bucket="example-bucket",
                    object_key="registry.json",
                    environ={ACCESS_KEY_ENV: "test-access", SECRET_KEY_ENV: "test-secret"},
                )

        fetch.assert_not_called()

    def test_endpoint_with_path_is_rejected_before_fetch(self) -> None:
        with patch("linode_image_lab.trusted_registry.urlopen") as fetch:
            with self.assertRaisesRegex(RegistryFetchError, "endpoint URL is invalid"):
                fetch_registry_from_object_storage(
                    endpoint_url="https://us-east-1.linodeobjects.com/prefix",
                    bucket="example-bucket",
                    object_key="registry.json",
                    environ={ACCESS_KEY_ENV: "test-access", SECRET_KEY_ENV: "test-secret"},
                )

        fetch.assert_not_called()

    def test_registry_fetch_failure_fails_closed(self) -> None:
        error = HTTPError("https://example.invalid", 403, "forbidden", {}, None)
        self.addCleanup(error.close)
        with patch("linode_image_lab.trusted_registry.urlopen", side_effect=error):
            with self.assertRaisesRegex(RegistryFetchError, "trusted registry fetch failed"):
                fetch_registry_from_object_storage(
                    endpoint_url="https://us-east-1.linodeobjects.com",
                    bucket="example-bucket",
                    object_key="registry.json",
                    environ={ACCESS_KEY_ENV: "test-access", SECRET_KEY_ENV: "test-secret"},
                )

    def test_stale_registry_is_rejected(self) -> None:
        with self.assertRaisesRegex(RegistryValidationError, "valid_until is stale"):
            validate_registry(
                registry_payload(valid_until="2029-12-31T00:00:00Z"),
                now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
            )

    def test_invalid_cidr_is_rejected(self) -> None:
        with self.assertRaisesRegex(RegistryValidationError, "invalid CIDR"):
            validate_registry(registry_payload(cidr="198.51.100.1/24"))

    def test_universal_allow_cidr_is_rejected(self) -> None:
        with self.assertRaisesRegex(RegistryValidationError, "universal allow"):
            validate_registry(registry_payload(cidr="0.0.0.0/0"))

    def test_ipv4_and_ipv6_cidrs_are_supported(self) -> None:
        registry = validate_registry(registry_payload())

        self.assertEqual(registry.ipv4_cidrs, ("198.51.100.0/24",))
        self.assertEqual(registry.ipv6_cidrs, ("2001:db8:100::/64",))

    def test_accepts_vendored_producer_registry_v1_fixture(self) -> None:
        payload = json.loads(PRODUCER_REGISTRY_FIXTURE.read_text(encoding="utf-8"))

        registry = validate_registry(
            payload,
            now=dt.datetime(2026, 5, 17, 0, 30, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(registry.name, "trusted-network-registry")
        self.assertEqual(registry.ipv4_cidrs, ("198.51.100.0/24", "203.0.113.10/32"))
        self.assertEqual(registry.ipv6_cidrs, ("2001:db8::10/128", "2001:db8:100::/64"))

    def test_rejects_unsupported_registry_schema_version(self) -> None:
        payload = json.loads(PRODUCER_REGISTRY_FIXTURE.read_text(encoding="utf-8"))
        payload["schema_version"] = 2

        with self.assertRaisesRegex(RegistryValidationError, "schema_version is not supported"):
            validate_registry(
                payload,
                now=dt.datetime(2026, 5, 17, 0, 30, tzinfo=dt.timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
