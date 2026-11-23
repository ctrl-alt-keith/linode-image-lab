"""Bounded trusted-registry synchronization for one LKE control-plane ACL."""

from __future__ import annotations

import json
import os
import stat
from ipaddress import ip_network
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TextIO

from .linode_api import LinodeApiError, LinodeClient, LinodeClientProtocol, LinodePreflightError
from .manifest import PROJECT
from .trusted_registry import (
    TrustedRegistry,
    TrustedRegistryError,
    fetch_registry_from_object_storage,
    validate_registry,
)


class ACLSyncError(ValueError):
    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


class ACLPreviewError(ACLSyncError):
    """A preview stop with a fixed, safe reason for observer output."""


def read_preservation_baseline(stream: TextIO) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate key")
        return result

    limit = 256 * 1024
    try:
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("oversize baseline")
        baseline = json.loads(payload, object_pairs_hook=unique)
        if not isinstance(baseline, dict):
            raise ValueError("invalid baseline")
    except (OSError, ValueError, RecursionError):
        raise ACLPreviewError("invalid_baseline_input") from None
    return baseline


DEFINITE_HTTP_REJECTIONS = {
    400: "validation_rejected", 401: "authentication_rejected",
    403: "permission_rejected", 404: "target_not_found",
}


def raise_acl_update_failure(plan: dict[str, Any], exc: Exception, private_file: int | None = None) -> None:
    status = exc.status_code if isinstance(exc, LinodeApiError) else None
    status = status if type(status) is int and 100 <= status <= 599 else None
    kind = exc.failure_kind if isinstance(exc, LinodeApiError) else None
    category = "unknown"
    if kind == "http_response" and status in DEFINITE_HTTP_REJECTIONS:
        category = DEFINITE_HTTP_REJECTIONS[status]
        plan["status"] = "rejected"
    else:
        plan["status"] = "uncertain"
        if kind == "transport":
            category = "transport_uncertain"
        elif kind == "response_parse":
            category = "response_parse_uncertain"
        elif kind == "http_response":
            category = "http_response_uncertain"
    plan["update_failure"] = {"phase": "control_plane_acl_put", "category": category}
    if status is not None:
        plan["update_failure"]["http_status"] = status
    finish_private_error_file(private_file, plan, exc)
    if plan["status"] == "rejected":
        raise ACLSyncError("acl-sync update received an HTTP rejection", plan) from None
    raise ACLSyncError("acl-sync update outcome is uncertain; inspect provider ACL before retry", plan) from None


def open_private_error_file(path: str) -> int:
    candidate = Path(path)
    parent = candidate.parent
    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        if not candidate.is_absolute() or parent.resolve(strict=True) != parent:
            raise OSError
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened_parent = os.fstat(parent_fd)
        if (opened_parent.st_dev, opened_parent.st_ino) != (info.st_dev, info.st_ino) or (
            not stat.S_ISDIR(opened_parent.st_mode) or opened_parent.st_uid != os.getuid()
            or stat.S_IMODE(opened_parent.st_mode) != 0o700
        ):
            raise OSError
        descriptor = os.open(candidate.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent_fd)
        os.fchmod(descriptor, 0o600)
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode) or created.st_uid != os.getuid() or stat.S_IMODE(created.st_mode) != 0o600:
            raise OSError
        return descriptor
    except (OSError, RuntimeError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ACLSyncError("private provider error file must be fresh under an owned mode-0700 directory") from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def write_private_error_file(descriptor: int, plan: dict[str, Any], exc: Exception | None) -> None:
    try:
        record: dict[str, Any] = {
            "schema_version": 1,
            "status": plan["status"],
            "update_failure": plan.get("update_failure"),
        }
        if isinstance(exc, LinodeApiError):
            errors = []
            for item in exc.provider_errors[:8]:
                entry = {}
                for key in ("field", "reason", "message"):
                    value = item.get(key)
                    if isinstance(value, str):
                        entry[key] = value[:96]
                if entry:
                    errors.append(entry)
            record["provider_errors"] = errors
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > 16384:
            raise OSError("private provider error record exceeds limit")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def finish_private_error_file(descriptor: int | None, plan: dict[str, Any], exc: Exception | None) -> None:
    if descriptor is None:
        return
    try:
        write_private_error_file(descriptor, plan, exc)
    except Exception:
        plan["private_error_artifact"] = "failed"
    else:
        plan["private_error_artifact"] = "written"


class ACLClientProtocol(LinodeClientProtocol, Protocol):
    def get_lke_cluster(self, cluster_id: int) -> dict[str, Any]: ...

    def get_lke_control_plane_acl(self, cluster_id: int) -> dict[str, Any]: ...

    def update_lke_control_plane_acl(self, cluster_id: int, acl: dict[str, Any]) -> dict[str, Any]: ...


def normalize_acl(response: dict[str, Any]) -> dict[str, Any]:
    acl = response.get("acl")
    if not isinstance(acl, dict) or type(acl.get("enabled")) is not bool:
        raise ACLSyncError("LKE control-plane ACL response is invalid")
    addresses = acl.get("addresses")
    if not isinstance(addresses, dict):
        raise ACLSyncError("LKE control-plane ACL addresses response is invalid")
    normalized: dict[str, Any] = {"enabled": acl["enabled"], "addresses": {}}
    for family in ("ipv4", "ipv6"):
        cidrs = addresses.get(family)
        if not isinstance(cidrs, list) or not all(isinstance(item, str) for item in cidrs):
            raise ACLSyncError("LKE control-plane ACL addresses response is invalid")
        try:
            networks = [ip_network(item, strict=True) for item in cidrs]
        except ValueError as exc:
            raise ACLSyncError("LKE control-plane ACL addresses response is invalid") from exc
        if any(network.version != (4 if family == "ipv4" else 6) for network in networks):
            raise ACLSyncError("LKE control-plane ACL address family is invalid")
        normalized["addresses"][family] = sorted(str(network) for network in networks)
    revision = acl.get("revision-id")
    if revision is not None and not isinstance(revision, str):
        raise ACLSyncError("LKE control-plane ACL revision response is invalid")
    normalized["revision-id"] = revision
    return normalized


def expected_cluster(client: ACLClientProtocol, cluster_id: int, cluster_label: str) -> None:
    cluster = client.get_lke_cluster(cluster_id)
    if cluster.get("id") != cluster_id or cluster.get("label") != cluster_label:
        raise ACLSyncError("LKE cluster identity does not match the expected id and label")


def preservation_payload(
    baseline: dict[str, Any], *, cluster_id: int, cluster_label: str,
    current_acl: dict[str, Any], registry: TrustedRegistry,
) -> dict[str, Any]:
    keys = {"schema_version", "cluster_id", "cluster_label", "acl", "managed_ipv4", "max_managed_ipv4"}
    if (
        set(baseline) != keys or type(baseline.get("schema_version")) is not int or baseline["schema_version"] != 1
        or type(baseline.get("cluster_id")) is not int or baseline["cluster_id"] != cluster_id
        or baseline.get("cluster_label") != cluster_label
    ):
        raise ACLPreviewError("invalid_baseline_identity")
    saved_acl = baseline["acl"]
    if (
        not isinstance(saved_acl, dict) or set(saved_acl) != {"enabled", "addresses", "revision-id"}
        or not isinstance(saved_acl["addresses"], dict) or set(saved_acl["addresses"]) != {"ipv4", "ipv6"}
    ):
        raise ACLPreviewError("invalid_baseline_policy")
    try:
        expected = normalize_acl(baseline)
    except ACLSyncError:
        raise ACLPreviewError("invalid_baseline_policy") from None
    if not expected["enabled"] or not expected["revision-id"]:
        raise ACLPreviewError("invalid_baseline_policy")
    if expected != current_acl:
        raise ACLPreviewError("baseline_drift")

    addresses = expected["addresses"]
    if any(
        len(set(routes)) != len(routes) or any(ip_network(route).prefixlen == 0 for route in routes)
        for routes in addresses.values()
    ):
        raise ACLPreviewError("invalid_baseline_policy")
    managed, limit = baseline["managed_ipv4"], baseline["max_managed_ipv4"]
    if (
        type(limit) is not int or limit < 1 or not isinstance(managed, list)
        or not all(isinstance(route, str) and route in addresses["ipv4"] and ip_network(route).prefixlen == 32
                   for route in managed)
        or len(set(managed)) != len(managed) or len(managed) > limit
    ):
        raise ACLPreviewError("invalid_managed_routes")
    if not registry.ipv4_cidrs or any(ip_network(route).prefixlen != 32 for route in registry.ipv4_cidrs):
        raise ACLPreviewError("registry_requires_ipv4_hosts")

    retained = set(addresses["ipv4"]) - set(managed)
    retained_networks = [ip_network(route) for route in retained]
    next_managed = set()
    for route in registry.ipv4_cidrs:
        host = ip_network(route)
        if not any(host.subnet_of(network) for network in retained_networks):
            next_managed.add(route)
    if len(next_managed) > limit:
        raise ACLPreviewError("managed_route_limit_exceeded")
    return {
        "enabled": True,
        "addresses": {"ipv4": sorted(retained | next_managed), "ipv6": addresses["ipv6"]},
    }


def build_acl_sync_manifest(
    *, cluster_id: int, registry: TrustedRegistry, current_acl: dict[str, Any],
    intended: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if registry.cidr_count == 0:
        raise ACLSyncError("trusted registry contains no active CIDRs")
    if intended is None:
        intended = {
            "enabled": True,
            "addresses": {
                "ipv4": list(registry.ipv4_cidrs),
                "ipv6": list(registry.ipv6_cidrs),
            },
        }
    current_addresses = current_acl["addresses"]
    diff = {
        action: {
            family: sorted(values)
            for family, values in families.items()
        }
        for action, families in {
            "additions": {
                family: set(intended["addresses"][family]) - set(current_addresses[family])
                for family in ("ipv4", "ipv6")
            },
            "removals": {
                family: set(current_addresses[family]) - set(intended["addresses"][family])
                for family in ("ipv4", "ipv6")
            },
        }.items()
    }
    unchanged = current_acl["enabled"] and not any(diff[action][family] for action in diff for family in ("ipv4", "ipv6"))
    planned_action = "keep_acl" if unchanged else ("enable_acl" if not current_acl["enabled"] else "replace_acl")
    return {
        "schema_version": 1,
        "project": PROJECT,
        "command": "acl-sync",
        "status": "planned",
        "valid": True,
        "dry_run": True,
        "execution_mode": "dry-run",
        "target": {"cluster_id": cluster_id},
        "registry": {
            "name": registry.name,
            "generated_at": registry.generated_at,
            "valid_until": registry.valid_until,
            "cidr_count": registry.cidr_count,
            "ipv4_count": len(registry.ipv4_cidrs),
            "ipv6_count": len(registry.ipv6_cidrs),
        },
        "planned_action": planned_action,
        "diff": diff,
        "provider_payload": intended,
        "safety": {
            "mutates": False,
            "requires_execute_for_mutation": True,
            "stale_registry_fallback": "disabled",
            "enforcement_verified": False,
        },
    }


def acl_sync_plan(
    *,
    cluster_id: int,
    cluster_label: str,
    registry_endpoint_url: str,
    registry_bucket: str,
    registry_object_key: str,
    registry_region: str | None = None,
    provider_error_file: str | None = None,
    preservation_baseline: dict[str, Any] | None = None,
    allow_enable: bool = False,
    execute: bool = False,
    client: ACLClientProtocol | None = None,
    environ: Mapping[str, str] | None = None,
    plan_reporter: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    if type(cluster_id) is not int or cluster_id <= 0 or not isinstance(cluster_label, str) or not cluster_label.strip():
        raise ACLSyncError("positive --cluster-id and non-empty --cluster-label are required")
    if preservation_baseline is not None and (execute or allow_enable or provider_error_file is not None):
        raise ACLPreviewError("preservation_preview_only")
    if provider_error_file is not None and not execute:
        raise ACLSyncError("private provider error file requires --execute")
    sync_client = client or LinodeClient.from_env(
        command="acl-sync", option="--execute" if execute else "dry-run",
    )
    try:
        registry = validate_registry(fetch_registry_from_object_storage(
            endpoint_url=registry_endpoint_url, bucket=registry_bucket,
            object_key=registry_object_key, region=registry_region, environ=environ,
        ))
        expected_cluster(sync_client, cluster_id, cluster_label)
        original_acl = normalize_acl(sync_client.get_lke_control_plane_acl(cluster_id))
    except (TrustedRegistryError, LinodePreflightError, ValueError) as exc:
        if preservation_baseline is not None:
            reason = "registry_invalid_or_unavailable" if isinstance(exc, TrustedRegistryError) else "target_read_failed"
            raise ACLPreviewError(reason) from None
        raise ACLSyncError(str(exc)) from exc
    intended = None
    if preservation_baseline is not None:
        intended = preservation_payload(
            preservation_baseline, cluster_id=cluster_id, cluster_label=cluster_label,
            current_acl=original_acl, registry=registry,
        )
    plan = build_acl_sync_manifest(cluster_id=cluster_id, registry=registry, current_acl=original_acl, intended=intended)
    if preservation_baseline is not None:
        plan["preview_mode"] = "preserve_routes"
    if not execute:
        return plan

    if plan["planned_action"] == "enable_acl" and not allow_enable:
        raise ACLSyncError("first ACL enablement requires explicit --allow-enable", plan)

    if plan_reporter is not None:
        plan_reporter(execute_plan_summary(plan))
    plan["execution_mode"] = "execute"
    plan["dry_run"] = False
    if plan["planned_action"] == "keep_acl":
        plan["status"] = "unchanged"
        plan["applied"] = False
        return plan

    try:
        # Re-fetch both independently owned inputs immediately before the write.
        refreshed_registry = validate_registry(fetch_registry_from_object_storage(
            endpoint_url=registry_endpoint_url, bucket=registry_bucket,
            object_key=registry_object_key, region=registry_region, environ=environ,
        ))
        if refreshed_registry != registry:
            raise ACLSyncError("trusted registry changed after planning; rerun acl-sync")
        expected_cluster(sync_client, cluster_id, cluster_label)
        if normalize_acl(sync_client.get_lke_control_plane_acl(cluster_id)) != original_acl:
            raise ACLSyncError("LKE control-plane ACL changed after planning; rerun acl-sync")
    except ACLSyncError as exc:
        raise ACLSyncError(str(exc), plan) from exc
    except (TrustedRegistryError, LinodePreflightError, ValueError) as exc:
        raise ACLSyncError("acl-sync pre-write verification failed", plan) from exc

    private_file = open_private_error_file(provider_error_file) if provider_error_file is not None else None
    try:
        sync_client.update_lke_control_plane_acl(cluster_id, plan["provider_payload"])
    except Exception as exc:
        raise_acl_update_failure(plan, exc, private_file)
    except BaseException:
        if private_file is not None:
            os.close(private_file)
        raise
    plan["status"] = "submitted"
    finish_private_error_file(private_file, plan, None)
    plan["applied"] = True
    plan["safety"]["mutates"] = True
    return plan


def execute_plan_summary(plan: dict[str, Any]) -> str:
    diff = plan["diff"]
    return (
        "acl-sync planned change before execute:\n"
        f"  action: {plan['planned_action']}\n"
        f"  current ACL enabled: {plan['planned_action'] != 'enable_acl'}\n"
        f"  add ipv4: {len(diff['additions']['ipv4'])}, ipv6: {len(diff['additions']['ipv6'])}\n"
        f"  remove ipv4: {len(diff['removals']['ipv4'])}, ipv6: {len(diff['removals']['ipv6'])}\n"
    )


def routine_outcome(
    manifest: dict[str, Any] | None, *, execute: bool, failed: bool = False, preview_failure: str | None = None,
) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "outcome_version": 1,
        "command": "acl-sync",
        "status": "failed" if failed and (manifest is None or manifest["status"] not in {"uncertain", "rejected"}) else (
            manifest["status"] if manifest is not None else "failed"
        ),
        "execution_mode": "execute" if execute else "dry-run",
    }
    if preview_failure is not None:
        outcome.update(preview_mode="preserve_routes", preview_failure=preview_failure)
    if manifest is not None:
        outcome["planned_action"] = manifest["planned_action"]
        outcome["registry_generated_at"] = manifest["registry"]["generated_at"]
        outcome["registry_valid_until"] = manifest["registry"]["valid_until"]
        outcome["cidr_count"] = manifest["registry"]["cidr_count"]
        outcome["change_counts"] = {
            action: {family: len(manifest["diff"][action][family]) for family in ("ipv4", "ipv6")}
            for action in ("additions", "removals")
        }
        outcome["enforcement_verified"] = False
        if manifest.get("preview_mode") == "preserve_routes":
            outcome["preview_mode"] = "preserve_routes"
            outcome["baseline_verified"] = True
        if "update_failure" in manifest:
            outcome["update_failure"] = manifest["update_failure"]
        if "private_error_artifact" in manifest:
            outcome["private_error_artifact"] = manifest["private_error_artifact"]
    return outcome
