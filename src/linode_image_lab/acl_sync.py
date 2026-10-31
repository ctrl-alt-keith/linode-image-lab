"""Bounded trusted-registry synchronization for one LKE control-plane ACL."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from ipaddress import ip_network
from pathlib import Path
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


BASELINE_CLUSTER_ID = 664899


def acl_state(acl: dict[str, Any]) -> dict[str, Any]:
    return {"enabled": acl["enabled"], "addresses": acl["addresses"]}


def acl_digest(acl: dict[str, Any]) -> str:
    encoded = json.dumps(acl, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_protected_baseline(path: str, digest: str, cluster_id: int, cluster_label: str) -> dict[str, Any]:
    if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(digest, str) or len(digest) != 64:
        raise ACLSyncError("protected ACL baseline path or digest is invalid")
    try:
        int(digest, 16)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536:
                raise ACLSyncError("protected ACL baseline file must be owned, regular, and mode 0600")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(65537)
        finally:
            os.close(descriptor)
        document = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ACLSyncError("protected ACL baseline could not be read or parsed") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "cluster_id", "cluster_label", "acl"}:
        raise ACLSyncError("protected ACL baseline schema is invalid")
    if document["schema_version"] != 1 or document["cluster_id"] != cluster_id or document["cluster_label"] != cluster_label:
        raise ACLSyncError("protected ACL baseline target identity does not match")
    baseline = normalize_acl({"acl": document["acl"]})
    if not baseline["enabled"] or acl_digest(baseline) != digest.lower():
        raise ACLSyncError("protected ACL baseline state or digest does not match")
    return baseline


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


def baseline_acl_plan(
    *, cluster_id: int, cluster_label: str, baseline_acl_file: str,
    baseline_acl_sha256: str, baseline_rollback: bool, execute: bool,
    client: ACLClientProtocol, plan_reporter: Callable[[str], object] | None,
) -> dict[str, Any]:
    if cluster_id != BASELINE_CLUSTER_ID:
        raise ACLSyncError("protected baseline mode is limited to its reviewed LKE cluster")
    try:
        baseline = load_protected_baseline(baseline_acl_file, baseline_acl_sha256, cluster_id, cluster_label)
        openai_cidrs = load_openai_snapshot()
        expected_cluster(client, cluster_id, cluster_label)
        original_acl = normalize_acl(client.get_lke_control_plane_acl(cluster_id))
    except (LinodePreflightError, ValueError) as exc:
        raise ACLSyncError(str(exc)) from exc
    expanded = {
        "enabled": True,
        "addresses": {
            "ipv4": sorted(set(baseline["addresses"]["ipv4"]) | set(openai_cidrs)),
            "ipv6": list(baseline["addresses"]["ipv6"]),
        },
    }
    if sum(len(values) for values in expanded["addresses"].values()) > 2048:
        raise ACLSyncError("OpenAI snapshot plan exceeds the local ACL entry safety bound")
    if baseline_rollback:
        if acl_state(original_acl) != expanded:
            raise ACLSyncError("live ACL is not the exact approved snapshot expansion; rollback aborted")
        intended = acl_state(baseline)
    else:
        if original_acl != baseline:
            raise ACLSyncError("live ACL differs from the protected baseline; addition aborted")
        intended = expanded
    diff = {
        action: {
            family: sorted(set(addresses[family]) - set(other[family]))
            for family in ("ipv4", "ipv6")
        }
        for action, addresses, other in (
            ("additions", intended["addresses"], original_acl["addresses"]),
            ("removals", original_acl["addresses"], intended["addresses"]),
        )
    }
    if not baseline_rollback and any(diff["removals"].values()):
        raise ACLSyncError("OpenAI snapshot plan would remove existing ACL entries")
    plan = {
        "schema_version": 1, "project": PROJECT, "command": "acl-sync", "status": "planned",
        "valid": True, "dry_run": True, "execution_mode": "dry-run",
        "target": {"cluster_id": cluster_id}, "registry": None,
        "baseline": {"sha256": acl_digest(baseline), "rollback": baseline_rollback},
        "openai_snapshot": {"source_url": SOURCE_URL, "creation_time": CREATION_TIME,
                            "sha256": SHA256, "cidr_count": len(openai_cidrs)},
        "planned_action": "rollback_acl" if baseline_rollback else "replace_acl",
        "diff": diff, "provider_payload": intended,
        "safety": {"mutates": False, "requires_execute_for_mutation": True,
                   "stale_registry_fallback": "disabled", "enforcement_verified": False},
    }
    if not execute:
        return plan
    if plan_reporter is not None:
        plan_reporter(execute_plan_summary(plan))
    plan["execution_mode"] = "execute"
    plan["dry_run"] = False
    try:
        if load_protected_baseline(baseline_acl_file, baseline_acl_sha256, cluster_id, cluster_label) != baseline:
            raise ACLSyncError("protected ACL baseline changed after planning")
        if load_openai_snapshot() != openai_cidrs:
            raise ACLSyncError("OpenAI snapshot changed after planning")
        expected_cluster(client, cluster_id, cluster_label)
        if normalize_acl(client.get_lke_control_plane_acl(cluster_id)) != original_acl:
            raise ACLSyncError("LKE control-plane ACL changed after planning")
    except (ACLSyncError, LinodePreflightError, ValueError) as exc:
        raise ACLSyncError("baseline ACL pre-write verification failed", plan) from exc
    try:
        client.update_lke_control_plane_acl(cluster_id, intended)
    except ValueError as exc:
        plan["status"] = "uncertain"
        raise ACLSyncError("acl-sync update outcome is uncertain; inspect provider ACL before retry", plan) from exc
    plan["status"] = "submitted"
    plan["applied"] = True
    plan["safety"]["mutates"] = True
    return plan


def acl_sync_plan(
    *,
    cluster_id: int,
    cluster_label: str,
    registry_endpoint_url: str,
    registry_bucket: str,
    registry_object_key: str,
    registry_region: str | None = None,
    openai_snapshot_cluster_id: int | None = None,
    baseline_acl_file: str | None = None,
    baseline_acl_sha256: str | None = None,
    baseline_rollback: bool = False,
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
    baseline_mode = baseline_acl_file is not None or baseline_acl_sha256 is not None or baseline_rollback
    if baseline_mode and cluster_id != BASELINE_CLUSTER_ID:
        raise ACLSyncError("protected baseline mode is limited to its reviewed LKE cluster")
    if baseline_mode and (
        not baseline_acl_file or not baseline_acl_sha256
        or openai_snapshot_cluster_id != BASELINE_CLUSTER_ID
    ):
        raise ACLSyncError("protected baseline mode requires its file, digest, and exact OpenAI snapshot target")
    sync_client = client or LinodeClient.from_env(
        command="acl-sync", option="--execute" if execute else "dry-run",
    )
    if baseline_mode:
        return baseline_acl_plan(
            cluster_id=cluster_id, cluster_label=cluster_label,
            baseline_acl_file=baseline_acl_file, baseline_acl_sha256=baseline_acl_sha256,
            baseline_rollback=baseline_rollback, execute=execute,
            client=sync_client, plan_reporter=plan_reporter,
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
        if manifest["registry"] is not None:
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
