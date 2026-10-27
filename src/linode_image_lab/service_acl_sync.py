"""Refresh one CCM-owned LoadBalancer firewall ACL through its Service annotation."""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPSHandler, Request, build_opener

from .http_transport import RejectRedirects
from .trusted_registry import TrustedRegistryError, fetch_registry_from_object_storage, validate_registry

ACL_ANNOTATION = "service.beta.kubernetes.io/linode-loadbalancer-firewall-acl"
FIREWALL_ID_ANNOTATION = "service.beta.kubernetes.io/linode-loadbalancer-firewall-id"
ACL_PATH = "/metadata/annotations/" + ACL_ANNOTATION.replace("~", "~0").replace("/", "~1")
TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


class ServiceACLSyncError(ValueError):
    """A safe, CIDR-free failure for the single-Service refresh."""


class ServiceClient(Protocol):
    def get_service(self, namespace: str, name: str) -> dict[str, Any]: ...
    def patch_service(self, namespace: str, name: str, patch: list[dict[str, str]]) -> None: ...


class KubernetesServiceClient:
    """In-cluster Kubernetes API transport using only the pod ServiceAccount."""

    def __init__(self, *, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        host = env.get("KUBERNETES_SERVICE_HOST", "")
        port = env.get("KUBERNETES_SERVICE_PORT", "")
        if not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ServiceACLSyncError("Kubernetes API connection is not configured")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ServiceACLSyncError("Kubernetes API connection is not configured") from exc
        try:
            self.token = TOKEN_PATH.read_text(encoding="utf-8").strip()
            self.context = ssl.create_default_context(cafile=str(CA_PATH))
        except (OSError, ValueError) as exc:
            raise ServiceACLSyncError("Kubernetes ServiceAccount credentials are unavailable") from exc
        if not self.token:
            raise ServiceACLSyncError("Kubernetes ServiceAccount credentials are unavailable")
        url_host = f"[{host}]" if address.version == 6 else host
        self.base_url = f"https://{url_host}:{port}"

    def _request(self, method: str, namespace: str, name: str, payload: Any = None) -> dict[str, Any]:
        path = f"/api/v1/namespaces/{quote(namespace, safe='')}/services/{quote(name, safe='')}"
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json-patch+json"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with build_opener(RejectRedirects(), HTTPSHandler(context=self.context)).open(request, timeout=15) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                reason = "Kubernetes Service access denied"
            elif exc.code == 404:
                reason = "Kubernetes Service was not found"
            elif exc.code == 409:
                reason = "Kubernetes Service changed concurrently"
            elif exc.code == 422:
                reason = "Kubernetes Service patch precondition failed"
            else:
                reason = "Kubernetes API request failed"
            raise ServiceACLSyncError(reason) from exc
        except (OSError, URLError) as exc:
            raise ServiceACLSyncError("Kubernetes API request failed") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ServiceACLSyncError("Kubernetes API response is invalid") from exc
        if not isinstance(parsed, dict):
            raise ServiceACLSyncError("Kubernetes API response is invalid")
        return parsed

    def get_service(self, namespace: str, name: str) -> dict[str, Any]:
        return self._request("GET", namespace, name)

    def patch_service(self, namespace: str, name: str, patch: list[dict[str, str]]) -> None:
        self._request("PATCH", namespace, name, patch)


def _acl_sources(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        payload = json.loads(value)
        sources = payload["allowList"]
        if not isinstance(payload, dict) or not isinstance(sources, dict) or set(sources) != {"ipv4", "ipv6"}:
            raise ValueError
        result = []
        for family, version in (("ipv4", 4), ("ipv6", 6)):
            cidrs = sources[family]
            if not isinstance(cidrs, list) or not all(isinstance(item, str) for item in cidrs):
                raise ValueError
            networks = [ipaddress.ip_network(item, strict=True) for item in cidrs]
            if any(network.version != version or str(network) != cidr or network.prefixlen == 0
                   for network, cidr in zip(networks, cidrs)):
                raise ValueError
            result.append(tuple(sorted(set(cidrs), key=lambda item: (
                int(ipaddress.ip_network(item).network_address), ipaddress.ip_network(item).prefixlen))))
        if not any(result):
            raise ValueError
        return result[0], result[1]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ServiceACLSyncError("current Service ACL annotation is invalid") from exc


def _service_state(service: dict[str, Any], namespace: str, name: str, uid: str) -> tuple[str, str]:
    if not isinstance(service, dict):
        raise ServiceACLSyncError("Kubernetes Service response is invalid")
    metadata = service.get("metadata")
    spec = service.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ServiceACLSyncError("Kubernetes Service response is invalid")
    if (metadata.get("namespace"), metadata.get("name"), metadata.get("uid")) != (namespace, name, uid):
        raise ServiceACLSyncError("Kubernetes Service identity changed")
    resource_version = metadata.get("resourceVersion")
    annotations = metadata.get("annotations")
    if not isinstance(resource_version, str) or not resource_version or not isinstance(annotations, dict):
        raise ServiceACLSyncError("Kubernetes Service response is invalid")
    if FIREWALL_ID_ANNOTATION in annotations:
        raise ServiceACLSyncError("external firewall-id annotation is present")
    ports = spec.get("ports")
    if (spec.get("type") != "LoadBalancer" or not isinstance(ports, list) or len(ports) != 1
        or not isinstance(ports[0], dict) or type(ports[0].get("port")) is not int
        or ports[0]["port"] != 443 or ports[0].get("protocol", "TCP") != "TCP"):
        raise ServiceACLSyncError("Kubernetes Service must expose only TCP 443")
    current = annotations.get(ACL_ANNOTATION)
    if not isinstance(current, str):
        raise ServiceACLSyncError("current CCM ACL annotation is required")
    _acl_sources(current)
    return resource_version, current


def service_acl_sync(
    *, namespace: str, name: str, uid: str, registry_endpoint_url: str,
    registry_bucket: str, registry_object_key: str, registry_region: str | None = None,
    execute: bool = False, client: ServiceClient | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Plan or execute one guarded annotation replacement; never alter provider firewalls directly."""
    if not all(isinstance(value, str) and value.strip() for value in (namespace, name, uid)):
        raise ServiceACLSyncError("exact namespace, name, and UID are required")
    try:
        registry = validate_registry(fetch_registry_from_object_storage(
            endpoint_url=registry_endpoint_url, bucket=registry_bucket,
            object_key=registry_object_key, region=registry_region, environ=environ,
        ))
    except TrustedRegistryError as exc:
        raise ServiceACLSyncError("trusted registry is unavailable or invalid") from exc
    if registry.cidr_count == 0:
        raise ServiceACLSyncError("trusted registry contains no active CIDRs")
    service_client = client or KubernetesServiceClient(environ=environ)
    try:
        rv, current = _service_state(service_client.get_service(namespace, name), namespace, name, uid)
        desired_sources = (registry.ipv4_cidrs, registry.ipv6_cidrs)
        unchanged = _acl_sources(current) == desired_sources
        result = {"schema_version": 1, "command": "service-acl-sync", "status": "unchanged" if unchanged else "planned",
                  "dry_run": not execute, "applied": False, "cidr_count": registry.cidr_count}
        if unchanged or not execute:
            return result
        # A fresh registry check precedes the write; JSON Patch tests guard intervening Service changes.
        refreshed = validate_registry(fetch_registry_from_object_storage(
            endpoint_url=registry_endpoint_url, bucket=registry_bucket,
            object_key=registry_object_key, region=registry_region, environ=environ,
        ))
        if refreshed != registry:
            raise ServiceACLSyncError("trusted registry changed before patch")
        desired = json.dumps({"allowList": {"ipv4": list(registry.ipv4_cidrs),
                                             "ipv6": list(registry.ipv6_cidrs)}}, separators=(",", ":"))
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": rv},
            {"op": "test", "path": ACL_PATH, "value": current},
            {"op": "replace", "path": ACL_PATH, "value": desired},
        ]
        service_client.patch_service(namespace, name, patch)
        _, observed = _service_state(service_client.get_service(namespace, name), namespace, name, uid)
        if observed != desired:
            raise ServiceACLSyncError("Service ACL readback differs from submitted annotation")
        result.update(status="verified", dry_run=False, applied=True)
        return result
    except TrustedRegistryError as exc:
        raise ServiceACLSyncError("trusted registry is unavailable or invalid") from exc
    except ServiceACLSyncError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ServiceACLSyncError("Kubernetes Service refresh failed") from exc
