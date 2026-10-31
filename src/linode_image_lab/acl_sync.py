"""Bounded trusted-registry synchronization for one LKE control-plane ACL."""

from __future__ import annotations

from ipaddress import ip_network
from typing import Any, Callable, Mapping, Protocol

from .linode_api import LinodeClient, LinodeClientProtocol, LinodePreflightError
from .manifest import PROJECT
from .openai_snapshot import CREATION_TIME, SHA256, SOURCE_URL, load_openai_snapshot
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


def build_acl_sync_manifest(
    *, cluster_id: int, registry: TrustedRegistry, current_acl: dict[str, Any],
    openai_cidrs: tuple[str, ...] = (),
) -> dict[str, Any]:
    if registry.cidr_count == 0:
        raise ACLSyncError("trusted registry contains no active CIDRs")
    intended = {
        "enabled": True,
        "addresses": {
            "ipv4": sorted(set(registry.ipv4_cidrs) | set(openai_cidrs)),
            "ipv6": list(registry.ipv6_cidrs),
        },
    }
    if openai_cidrs and sum(len(values) for values in intended["addresses"].values()) > 2048:
        raise ACLSyncError("OpenAI snapshot plan exceeds the local ACL entry safety bound")
    current_addresses = current_acl["addresses"]
    if openai_cidrs and any(
        set(current_addresses[family]) - set(intended["addresses"][family])
        for family in ("ipv4", "ipv6")
    ):
        raise ACLSyncError("OpenAI snapshot plan would remove existing ACL entries")
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
        "openai_snapshot": {
            "source_url": SOURCE_URL,
            "creation_time": CREATION_TIME,
            "sha256": SHA256,
            "cidr_count": len(openai_cidrs),
        } if openai_cidrs else None,
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
    openai_snapshot_cluster_id: int | None = None,
    allow_enable: bool = False,
    execute: bool = False,
    client: ACLClientProtocol | None = None,
    environ: Mapping[str, str] | None = None,
    plan_reporter: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    if type(cluster_id) is not int or cluster_id <= 0 or not isinstance(cluster_label, str) or not cluster_label.strip():
        raise ACLSyncError("positive --cluster-id and non-empty --cluster-label are required")
    if openai_snapshot_cluster_id is not None and (
        type(openai_snapshot_cluster_id) is not int or openai_snapshot_cluster_id != cluster_id
    ):
        raise ACLSyncError("OpenAI snapshot target must match the exact LKE cluster id")
    sync_client = client or LinodeClient.from_env(
        command="acl-sync", option="--execute" if execute else "dry-run",
    )
    try:
        registry = validate_registry(fetch_registry_from_object_storage(
            endpoint_url=registry_endpoint_url, bucket=registry_bucket,
            object_key=registry_object_key, region=registry_region, environ=environ,
        ))
        openai_cidrs = load_openai_snapshot() if openai_snapshot_cluster_id is not None else ()
        expected_cluster(sync_client, cluster_id, cluster_label)
        original_acl = normalize_acl(sync_client.get_lke_control_plane_acl(cluster_id))
        plan = build_acl_sync_manifest(
            cluster_id=cluster_id, registry=registry, current_acl=original_acl,
            openai_cidrs=openai_cidrs,
        )
    except (TrustedRegistryError, LinodePreflightError, ValueError) as exc:
        raise ACLSyncError(str(exc)) from exc
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
        if openai_cidrs and load_openai_snapshot() != openai_cidrs:
            raise ACLSyncError("OpenAI snapshot changed after planning; rerun acl-sync")
        expected_cluster(sync_client, cluster_id, cluster_label)
        if normalize_acl(sync_client.get_lke_control_plane_acl(cluster_id)) != original_acl:
            raise ACLSyncError("LKE control-plane ACL changed after planning; rerun acl-sync")
    except ACLSyncError as exc:
        raise ACLSyncError(str(exc), plan) from exc
    except (TrustedRegistryError, LinodePreflightError, ValueError) as exc:
        raise ACLSyncError("acl-sync pre-write verification failed", plan) from exc

    try:
        sync_client.update_lke_control_plane_acl(cluster_id, plan["provider_payload"])
    except ValueError as exc:
        plan["status"] = "uncertain"
        raise ACLSyncError("acl-sync update outcome is uncertain; inspect provider ACL before retry", plan) from exc
    plan["status"] = "submitted"
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


def routine_outcome(manifest: dict[str, Any] | None, *, execute: bool, failed: bool = False) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "outcome_version": 1,
        "command": "acl-sync",
        "status": "failed" if failed and (manifest is None or manifest["status"] != "uncertain") else (
            manifest["status"] if manifest is not None else "failed"
        ),
        "execution_mode": "execute" if execute else "dry-run",
    }
    if manifest is not None:
        outcome["planned_action"] = manifest["planned_action"]
        outcome["registry_generated_at"] = manifest["registry"]["generated_at"]
        outcome["registry_valid_until"] = manifest["registry"]["valid_until"]
        outcome["cidr_count"] = manifest["registry"]["cidr_count"]
        if manifest["openai_snapshot"] is not None:
            outcome["openai_snapshot_creation_time"] = manifest["openai_snapshot"]["creation_time"]
            outcome["openai_snapshot_cidr_count"] = manifest["openai_snapshot"]["cidr_count"]
        outcome["change_counts"] = {
            action: {family: len(manifest["diff"][action][family]) for family in ("ipv4", "ipv6")}
            for action in ("additions", "removals")
        }
        outcome["enforcement_verified"] = False
    return outcome
