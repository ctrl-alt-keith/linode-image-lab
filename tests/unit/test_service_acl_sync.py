from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from linode_image_lab.cli import main
from linode_image_lab.service_acl_sync import (
    ACL_ANNOTATION, ACL_PATH, FIREWALL_ID_ANNOTATION, ServiceACLSyncError,
    KubernetesServiceClient, service_acl_sync,
)


def registry(*, cidr: str = "198.51.100.0/24", stale: bool = False) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    return {"schema_version": 1, "registry": {
        "name": "trusted-network-registry", "publisher_version": "0.1.0",
        "generated_at": (now - dt.timedelta(minutes=2)).isoformat(),
        "valid_until": (now - dt.timedelta(minutes=1) if stale else now + dt.timedelta(minutes=30)).isoformat(),
    }, "entries": [{"status": "active", "address_family": "ipv4", "cidr": cidr}]}


def service(*, acl: str | None = None) -> dict:
    return {"metadata": {"namespace": "traefik", "name": "traefik", "uid": "uid-example",
                         "resourceVersion": "123", "annotations": {
                             ACL_ANNOTATION: acl or json.dumps({"allowList": {"ipv4": ["203.0.113.0/24"], "ipv6": []}})
                         }}, "spec": {"type": "LoadBalancer", "ports": [{"port": 443, "protocol": "TCP"}]}}


class FakeServiceClient:
    def __init__(self) -> None:
        self.value = service()
        self.patches: list[list[dict]] = []
        self.after_patch = None

    def get_service(self, namespace: str, name: str) -> dict:
        assert (namespace, name) == ("traefik", "traefik")
        return self.value

    def patch_service(self, namespace: str, name: str, body: list[dict]) -> None:
        self.patches.append(body)
        if self.after_patch:
            self.after_patch()
        else:
            self.value = service(acl=body[-1]["value"])


def sync(client: FakeServiceClient, *, execute: bool = True) -> dict:
    return service_acl_sync(namespace="traefik", name="traefik", uid="uid-example",
                            registry_endpoint_url="https://us-east-1.linodeobjects.com",
                            registry_bucket="example-bucket", registry_object_key="registry.json",
                            execute=execute, client=client, environ={})


class ServiceACLSyncTests(unittest.TestCase):
    def test_kubernetes_transport_uses_exact_api_path_and_json_patch_content_type(self) -> None:
        client = KubernetesServiceClient.__new__(KubernetesServiceClient)
        client.base_url = "https://kubernetes.example:443"
        client.token = "x"
        client.context = object()
        captured = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b"{}"

        class Opener:
            def open(self, request, timeout):
                captured.append((request, timeout))
                return Response()

        with patch("linode_image_lab.service_acl_sync.build_opener", return_value=Opener()):
            client.patch_service("traefik", "traefik", [{"op": "test", "path": "/metadata/uid", "value": "uid-example"}])
        request, timeout = captured[0]
        self.assertEqual(request.full_url, "https://kubernetes.example:443/api/v1/namespaces/traefik/services/traefik")
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.get_header("Content-type"), "application/json-patch+json")
        self.assertEqual(timeout, 15)

    def test_kubernetes_transport_classifies_denial_conflict_and_malformed_response(self) -> None:
        client = KubernetesServiceClient.__new__(KubernetesServiceClient)
        client.base_url = "https://kubernetes.example:443"
        client.token = "x"
        client.context = object()

        class Opener:
            def __init__(self, error):
                self.error = error

            def open(self, request, timeout):
                raise self.error

        for code, reason in ((403, "access denied"), (409, "concurrently"), (422, "precondition")):
            error = HTTPError("https://kubernetes.example", code, "private-token 198.51.100.0/24", {}, None)
            with self.subTest(code=code), patch("linode_image_lab.service_acl_sync.build_opener", return_value=Opener(error)):
                with self.assertRaises(ServiceACLSyncError) as caught:
                    client.get_service("traefik", "traefik")
            self.assertIn(reason, str(caught.exception))
            self.assertNotIn("private-token", str(caught.exception))
            self.assertNotIn("198.51.100.0/24", str(caught.exception))

        class BadResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b"not JSON 198.51.100.0/24"

        class BadOpener:
            def open(self, request, timeout):
                return BadResponse()

        with patch("linode_image_lab.service_acl_sync.build_opener", return_value=BadOpener()), self.assertRaises(
            ServiceACLSyncError
        ) as caught:
            client.get_service("traefik", "traefik")
        self.assertEqual(str(caught.exception), "Kubernetes API response is invalid")

    def test_cli_config_defaults_redact_service_identity_and_registry_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "service.toml"
            config.write_text(
                'schema_version = 1\n[service-acl-sync]\nservice_namespace = "traefik"\n'
                'service_name = "traefik"\nservice_uid = "uid-example"\n'
                'registry_endpoint_url = "https://us-east-1.linodeobjects.com"\n'
                'registry_bucket = "example-bucket"\nregistry_object_key = "registry.json"\n',
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                code = main(["config", "validate", "--config", str(config), "--command", "service-acl-sync"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["target_command"], "service-acl-sync")
        for private_value in ("uid-example", "traefik", "example-bucket", "registry.json"):
            self.assertNotIn(private_value, output.getvalue())

    def test_replaces_only_current_annotation_with_conflict_guards_and_readback(self) -> None:
        client = FakeServiceClient()
        with patch("linode_image_lab.service_acl_sync.fetch_registry_from_object_storage", return_value=registry()) as fetch:
            result = sync(client)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual((result["status"], result["applied"]), ("verified", True))
        body = client.patches[0]
        self.assertEqual(body[:3], [
            {"op": "test", "path": "/metadata/uid", "value": "uid-example"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "123"},
            {"op": "test", "path": ACL_PATH, "value": service()["metadata"]["annotations"][ACL_ANNOTATION]},
        ])
        self.assertEqual(body[3]["op"], "replace")
        self.assertEqual(body[3]["path"], ACL_PATH)
        self.assertEqual(json.loads(body[3]["value"]), {"allowList": {"ipv4": ["198.51.100.0/24"], "ipv6": []}})
        self.assertNotIn("198.51.100.0/24", str(result))

    def test_dry_run_and_canonical_noop_do_not_patch(self) -> None:
        client = FakeServiceClient()
        with patch("linode_image_lab.service_acl_sync.fetch_registry_from_object_storage", return_value=registry()):
            self.assertEqual(sync(client, execute=False)["status"], "planned")
            client.value = service(acl='{"allowList":{"ipv6":[],"ipv4":["198.51.100.0/24"]}}')
            self.assertEqual(sync(client)["status"], "unchanged")
        self.assertEqual(client.patches, [])

    def test_stale_and_changed_registry_preserve_existing_acl(self) -> None:
        for payloads in ([registry(stale=True)], [registry(), registry(cidr="192.0.2.0/24")]):
            client = FakeServiceClient()
            with self.subTest(payloads=payloads), patch(
                "linode_image_lab.service_acl_sync.fetch_registry_from_object_storage", side_effect=payloads
            ), self.assertRaises(ServiceACLSyncError):
                sync(client)
            self.assertEqual(client.patches, [])

    def test_identity_shape_and_annotation_guards_preserve_existing_acl(self) -> None:
        mutations = (
            lambda s: s["metadata"].update(uid="replacement"),
            lambda s: s["metadata"].update(resourceVersion=""),
            lambda s: s["spec"].update(type="ClusterIP"),
            lambda s: s["spec"].update(ports=[{"port": 80, "protocol": "TCP"}]),
            lambda s: s["spec"].update(ports=[{"port": 443, "protocol": "UDP"}]),
            lambda s: s["spec"]["ports"].append({"port": 80, "protocol": "TCP"}),
            lambda s: s["metadata"]["annotations"].update({FIREWALL_ID_ANNOTATION: "123"}),
            lambda s: s["metadata"]["annotations"].pop(ACL_ANNOTATION),
            lambda s: s["metadata"]["annotations"].update({ACL_ANNOTATION: "malformed"}),
        )
        for mutate in mutations:
            client = FakeServiceClient()
            mutate(client.value)
            with self.subTest(mutate=mutate), patch(
                "linode_image_lab.service_acl_sync.fetch_registry_from_object_storage", return_value=registry()
            ), self.assertRaises(ServiceACLSyncError):
                sync(client)
            self.assertEqual(client.patches, [])

    def test_denied_conflict_network_and_readback_failure_are_sanitized(self) -> None:
        for stage in ("get", "patch", "readback"):
            client = FakeServiceClient()
            calls = 0

            def get(namespace: str, name: str) -> dict:
                nonlocal calls
                calls += 1
                if stage == "get" or (stage == "readback" and calls == 2):
                    raise OSError("secret=abc CIDR 198.51.100.0/24")
                return client.value

            client.get_service = get  # type: ignore[method-assign]
            if stage == "patch":
                def fail(namespace: str, name: str, body: list[dict]) -> None:
                    raise ValueError("secret=abc CIDR 198.51.100.0/24")
                client.patch_service = fail  # type: ignore[method-assign]
            with self.subTest(stage=stage), patch(
                "linode_image_lab.service_acl_sync.fetch_registry_from_object_storage", return_value=registry()
            ), self.assertRaises(ServiceACLSyncError) as caught:
                sync(client)
            self.assertNotIn("secret=abc", str(caught.exception))
            self.assertNotIn("198.51.100.0/24", str(caught.exception))

    def test_replacement_after_patch_fails_readback(self) -> None:
        client = FakeServiceClient()
        client.after_patch = lambda: client.value["metadata"].update(uid="replacement")
        with patch("linode_image_lab.service_acl_sync.fetch_registry_from_object_storage", return_value=registry()), self.assertRaisesRegex(
            ServiceACLSyncError, "identity"
        ):
            sync(client)
