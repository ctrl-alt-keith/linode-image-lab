"""One guarded ACL update or restoration from operator-owned evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from ipaddress import ip_network
from pathlib import Path
from typing import Any

from .acl_sync import (
    MAX_PROTECTED_ACL_INPUT, ACLClientProtocol, ACLSyncError, expected_cluster, normalize_acl, open_private_error_file,
)
from .linode_api import LinodeClient
from .registry_version_selection import RegistryVersionSelection
from .trusted_registry import fetch_registry_from_object_storage, validate_registry

FAMILIES = ("ipv4", "ipv6")
IDENTITY_FIELDS = {"schema_version", "cluster_id", "cluster_label"}
RECEIPT_FIELDS = IDENTITY_FIELDS | {
    "operation", "before", "after", "retained", "slots", "managed_before", "managed_after",
}


class ACLReconcileError(ACLSyncError):
    def __init__(self, reason: str, *, status: str = "failed") -> None:
        super().__init__(reason)
        self.status = status


@dataclass(frozen=True)
class WanSource:
    device_id: str
    interface: str


@dataclass(frozen=True)
class WanObservation:
    source: WanSource
    ipv4: str
    observed_at: datetime | None


def policy(acl: Any) -> dict[str, Any]:
    if not isinstance(acl, dict) or set(acl) != {"enabled", "addresses", "revision-id"}:
        raise ACLReconcileError("invalid_acl_policy")
    try:
        result = normalize_acl({"acl": acl})
    except ACLSyncError:
        raise ACLReconcileError("invalid_acl_policy") from None
    addresses = acl["addresses"]
    if (
        not result["enabled"] or not result["revision-id"] or set(addresses) != set(FAMILIES)
        or any(len(set(routes)) != len(routes) or any(ip_network(route).prefixlen == 0 for route in routes)
               for routes in result["addresses"].values())
    ):
        raise ACLReconcileError("invalid_acl_policy")
    # Normalize for validation only. The provider's literal address lists are
    # needed for drift checks, passthrough, and restoration.
    return {**result, "addresses": {family: list(addresses[family]) for family in FAMILIES}}


def host_route(route: Any) -> str:
    try:
        if not isinstance(route, str):
            raise ValueError
        network = ip_network(route, strict=True)
        address = network.network_address
        if (
            network.version != 4 or network.prefixlen != 32 or route != str(network)
            or not address.is_global or address.is_multicast
        ):
            raise ValueError
    except ValueError:
        raise ACLReconcileError("invalid_source_host") from None
    return route


def verify_identity(document: dict[str, Any], cluster_id: int, cluster_label: str) -> None:
    if (
        type(document.get("schema_version")) is not int or document["schema_version"] != 1
        or type(document.get("cluster_id")) is not int or document["cluster_id"] != cluster_id
        or document.get("cluster_label") != cluster_label
    ):
        raise ACLReconcileError("invalid_attempt_identity")


def validate_slots(slots: Any) -> None:
    if (
        not isinstance(slots, dict) or len(slots) != 2
        or not all(re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", key) for key in slots)
        or sorted(str(interface) for interface in slots.values()) != ["wan1", "wan2"]
    ):
        raise ACLReconcileError("incomplete_or_unapproved_sources")


def validate_ownership(slots: dict, retained: dict, managed: Any, addresses: dict) -> None:
    if not isinstance(managed, dict) or not managed.keys() <= slots.keys():
        raise ACLReconcileError("invalid_managed_ownership")
    if retained["ipv6"] != addresses["ipv6"]:
        raise ACLReconcileError("ipv6_policy_changed")
    previous = set(retained["ipv4"])
    for routes in managed.values():
        if not isinstance(routes, dict) or routes.keys() != {"ipv4"}:
            raise ACLReconcileError("invalid_managed_ownership")
        previous.add(host_route(routes["ipv4"]))
    if previous != set(addresses["ipv4"]):
        raise ACLReconcileError("unclassified_baseline_routes")


def observed_entry_is_fresh(entry: dict, now: datetime) -> bool:
    try:
        observed, expires = (datetime.fromisoformat(entry[key]) for key in ("observed_at", "expires_at"))
        return observed <= now < expires
    except (KeyError, TypeError, ValueError):
        return False


def corroborate_observations(slots: dict, observed: Any, registry: dict, now: datetime) -> dict[str, str]:
    if not isinstance(observed, dict) or observed.keys() != slots.keys():
        raise ACLReconcileError("incomplete_or_unapproved_sources")

    # The protected observation binds the intended device and slot. Registry v1
    # supplies corroboration by interface and IPv4 host, but omits device IDs.
    discovered = {
        (entry["source_ref"], entry["cidr"])
        for entry in registry["entries"]
        if entry.get("kind") == "discovered" and entry.get("source_type") == "meraki_uplink_addresses"
        and entry.get("address_family") == "ipv4"
        and isinstance(entry.get("source_ref"), str)
        and observed_entry_is_fresh(entry, now)
    }
    seen = set()
    corroborated = {}
    for slot, interface in slots.items():
        routes = observed[slot]
        if not isinstance(routes, dict) or routes.keys() != {"ipv4"}:
            raise ACLReconcileError("incomplete_or_unapproved_sources")
        route = host_route(routes["ipv4"])
        if route in seen:
            raise ACLReconcileError("ambiguous_source_observations")
        seen.add(route)
        if (interface, route) not in discovered:
            raise ACLReconcileError("source_observation_does_not_match_registry")
        corroborated[slot] = route
    return corroborated


def _match_wan_observations(
    approved_slots: dict[str, WanSource],
    observations: list[WanObservation],
) -> tuple[dict[str, str], dict[WanSource, WanObservation]]:
    slots = {slot: source.interface for slot, source in approved_slots.items()}
    validate_slots(slots)
    by_source = {observation.source: observation for observation in observations}
    expected_sources = set(approved_slots.values())
    if (
        len(observations) != len(expected_sources) or by_source.keys() != expected_sources
        or any(not source.device_id or source.device_id != source.device_id.strip() for source in expected_sources)
    ):
        raise ACLReconcileError("incomplete_or_unapproved_sources")
    return slots, by_source


def bind_wan_observations(
    *,
    approved_slots: dict[str, WanSource],
    observations: list[WanObservation],
    max_age: timedelta,
    selection: RegistryVersionSelection,
    registry_args: dict[str, Any],
) -> dict[str, Any]:
    """Prepare guarded input fields from independently collected WAN observations."""
    slots, by_source = _match_wan_observations(approved_slots, observations)
    if max_age <= timedelta(0):
        raise ACLReconcileError("invalid_source_observation_age")
    if not selection.version_id:
        raise ACLReconcileError("invalid_registry_version_selection")

    observed = {}
    observation_times = []
    try:
        for slot, source in approved_slots.items():
            observation = by_source[source]
            timestamp = observation.observed_at
            if timestamp is None:
                raise ACLReconcileError("source_observation_time_unverified")
            if timestamp.utcoffset() is None:
                raise ACLReconcileError("source_observation_not_fresh")
            observation_times.append(timestamp.astimezone(timezone.utc))
            observed[slot] = {"ipv4": observation.ipv4}
        valid_until = min(observation_times) + max_age
    except OverflowError:
        raise ACLReconcileError("invalid_source_observation_age") from None

    try:
        registry = fetch_registry_from_object_storage(**registry_args, exact_version_id=selection.version_id)
        now = datetime.now(timezone.utc)
        validate_registry(registry, now=now)
    except ValueError:
        raise ACLReconcileError("registry_invalid_or_unavailable") from None

    if max(observation_times) > now or now >= valid_until:
        raise ACLReconcileError("source_observation_not_fresh")
    corroborate_observations(slots, observed, registry, now)
    return {
        "slots": slots, "observed": observed, "registry_version_proof": selection.proof(),
        "source_observations_valid_until": valid_until.isoformat(),
    }


def source_observation_deadline(value: Any) -> datetime:
    try:
        deadline = datetime.fromisoformat(value)
        if deadline.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        raise ACLReconcileError("invalid_source_observation_deadline") from None
    return deadline


def require_fresh_observations(deadline: datetime | None) -> None:
    if deadline is not None and datetime.now(timezone.utc) >= deadline:
        raise ACLReconcileError("source_observation_not_fresh")


def planned_addresses(document: dict, before: dict, retained: dict, registry: dict) -> tuple[dict, dict]:
    routes = corroborate_observations(
        document["slots"], document["observed"], registry, datetime.now(timezone.utc),
    )
    coverage = [ip_network(route) for route in retained["ipv4"]]
    next_managed = {
        slot: {"ipv4": route}
        for slot, route in routes.items()
        if not any(ip_network(route).subnet_of(network) for network in coverage)
    }
    current = before["addresses"]["ipv4"]
    managed = {routes["ipv4"] for routes in next_managed.values()}
    wanted = set(retained["ipv4"]) | managed
    ipv4 = [route for route in current if route in wanted] + sorted(managed - set(current))
    return {"ipv4": ipv4, "ipv6": list(before["addresses"]["ipv6"])}, next_managed


def read_acl(client: ACLClientProtocol, cluster_id: int) -> dict[str, Any]:
    return policy(client.get_lke_control_plane_acl(cluster_id)["acl"])


def receipt_policies(document: Any, cluster_id: int, cluster_label: str) -> tuple[dict, dict]:
    if not isinstance(document, dict) or set(document) != RECEIPT_FIELDS:
        raise ACLReconcileError("invalid_attempt_receipt")
    verify_identity(document, cluster_id, cluster_label)
    if document["operation"] not in ("reconcile", "rollback"):
        raise ACLReconcileError("invalid_attempt_receipt")
    before, after = policy(document["before"]), policy(document["after"])
    if before["revision-id"] == after["revision-id"] or before["addresses"] == after["addresses"]:
        raise ACLReconcileError("invalid_attempt_receipt")
    slots = document["slots"]
    validate_slots(slots)
    retained = policy({**before, "addresses": document["retained"]})["addresses"]
    validate_ownership(slots, retained, document["managed_before"], before["addresses"])
    validate_ownership(slots, retained, document["managed_after"], after["addresses"])
    return before, after


def record_line(stream, value: dict[str, Any]) -> None:
    line = json.dumps(value, sort_keys=True) + "\n"
    if len(line) > MAX_PROTECTED_ACL_INPUT:
        raise ACLReconcileError("attempt_receipt_too_large")
    stream.write(line)
    stream.flush()
    os.fsync(stream.fileno())


def receipt_path(directory: str, cluster_id: int, revision: str) -> Path:
    key = hashlib.sha256(f"{cluster_id}\0{revision}".encode()).hexdigest()
    return Path(directory) / f"acl-{key}.jsonl"


def apply_attempt(
    client: ACLClientProtocol, receipt: dict[str, Any], directory: str, *,
    observation_deadline: datetime | None = None,
) -> None:
    cluster_id, before, after = (receipt[key] for key in ("cluster_id", "before", "after"))
    require_fresh_observations(observation_deadline)
    try:
        descriptor = open_private_error_file(
            str(receipt_path(directory, cluster_id, before["revision-id"])), sync_parent=True,
        )
    except ACLSyncError:
        raise ACLReconcileError("attempt_receipt_unavailable_or_already_exists") from None

    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        try:
            record_line(stream, receipt)
            if read_acl(client, cluster_id) != before:
                raise ACLReconcileError("baseline_drift")
            require_fresh_observations(observation_deadline)
        except ACLReconcileError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise ACLReconcileError("final_preflight_failed") from None
        try:
            client.update_lke_control_plane_acl(cluster_id, after)
            for attempt in range(12):
                observed = read_acl(client, cluster_id)
                if observed == after:
                    record_line(stream, {"status": "enforced"})
                    return
                if observed["revision-id"] != before["revision-id"] or observed["addresses"] not in (
                    before["addresses"], after["addresses"],
                ):
                    raise ACLReconcileError("unexpected_readback", status="uncertain")
                if attempt < 11:
                    time.sleep(2)
        except ACLReconcileError as exc:
            raise ACLReconcileError(str(exc), status="uncertain") from None
        except (OSError, ValueError, KeyError, TypeError):
            # A post-PUT GET error is also uncertain. Avoid retrying either write.
            raise ACLReconcileError("provider_result_unverified", status="uncertain") from None
        raise ACLReconcileError("enforcement_timeout", status="uncertain")


def reconcile_acl(
    *, document: dict[str, Any], cluster_id: int, cluster_label: str, rollback: bool,
    execute: bool, receipt_dir: str | None, selection: RegistryVersionSelection | None,
    registry_args: dict[str, Any], client: ACLClientProtocol | None = None,
) -> dict[str, Any]:
    verify_identity(document, cluster_id, cluster_label)
    if execute != (receipt_dir is not None):
        raise ACLReconcileError("execute_requires_receipt_directory")
    if rollback == (selection is not None):
        raise ACLReconcileError("forward_requires_exact_version_and_rollback_uses_receipt")

    observation_deadline = None
    if rollback:
        original, before = receipt_policies(document, cluster_id, cluster_label)
        if document["operation"] != "reconcile":
            raise ACLReconcileError("invalid_forward_receipt")
        addresses = original["addresses"]
        managed_before, managed_after = document["managed_after"], document["managed_before"]
    else:
        fields = IDENTITY_FIELDS | {
            "acl", "retained", "slots", "checkpoint", "observed", "registry_version_proof",
        }
        deadline_field = "source_observations_valid_until"
        if not fields <= document.keys() <= fields | {deadline_field}:
            raise ACLReconcileError("invalid_reconciliation_input")
        if deadline_field in document:
            observation_deadline = source_observation_deadline(document[deadline_field])
            require_fresh_observations(observation_deadline)
        elif execute:
            raise ACLReconcileError("source_observation_deadline_required")
        before = policy(document["acl"])
        slots = document["slots"]
        validate_slots(slots)
        retained = policy({**before, "addresses": document["retained"]})["addresses"]
        checkpoint = document["checkpoint"]
        managed_before = {}
        if checkpoint is not None:
            if not isinstance(checkpoint, dict) or set(checkpoint) != {"intent", "outcome"} or (
                checkpoint["outcome"] != {"status": "enforced"}
            ):
                raise ACLReconcileError("unverified_ownership_checkpoint")
            intent = checkpoint["intent"]
            _, accepted = receipt_policies(intent, cluster_id, cluster_label)
            if accepted != before or intent["slots"] != slots or intent["retained"] != retained:
                raise ACLReconcileError("ownership_checkpoint_drift")
            managed_before = intent["managed_after"]
        validate_ownership(slots, retained, managed_before, before["addresses"])
        proof = document["registry_version_proof"]
        if selection is None or not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof) or (
            not hmac.compare_digest(proof, selection.proof())
        ):
            raise ACLReconcileError("source_version_proof_mismatch")
        try:
            registry = fetch_registry_from_object_storage(**registry_args, exact_version_id=selection.version_id)
            validate_registry(registry)
        except ValueError:
            raise ACLReconcileError("registry_invalid_or_unavailable") from None
        addresses, managed_after = planned_addresses(document, before, retained, registry)

    try:
        if client is None:
            client = replace(LinodeClient.from_env(command="acl-sync"), timeout_seconds=8, max_retry_attempts=1)
        expected_cluster(client, cluster_id, cluster_label)
        if read_acl(client, cluster_id) != before:
            raise ACLReconcileError("baseline_drift")
    except ACLReconcileError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ACLReconcileError("target_read_failed") from None

    require_fresh_observations(observation_deadline)
    operation = "rollback" if rollback else "reconcile"
    added = {family: len(set(addresses[family]) - set(before["addresses"][family])) for family in FAMILIES}
    removed = {family: len(set(before["addresses"][family]) - set(addresses[family])) for family in FAMILIES}
    status = "unchanged" if not any(added.values()) and not any(removed.values()) else "planned"
    if status == "unchanged" and managed_before != managed_after:
        raise ACLReconcileError("ownership_changed_without_policy")
    if execute and status == "unchanged" and receipt_dir is not None and os.path.lexists(
        receipt_path(receipt_dir, cluster_id, before["revision-id"])
    ):
        raise ACLReconcileError("attempt_receipt_unavailable_or_already_exists")
    if execute and status != "unchanged":
        if selection is not None:
            try:
                latest = fetch_registry_from_object_storage(**registry_args, expected_current_version_id=selection.version_id)
                validate_registry(latest)
                if latest != registry:
                    raise ValueError
                planned_addresses(document, before, retained, latest)
            except ValueError:
                raise ACLReconcileError("registry_changed_or_unavailable") from None
        receipt = {
            "schema_version": 1, "cluster_id": cluster_id, "cluster_label": cluster_label,
            "operation": operation, "before": before,
            "after": {"enabled": True, "addresses": addresses, "revision-id": uuid.uuid4().hex},
            "retained": policy({**before, "addresses": document["retained"]})["addresses"],
            "slots": document["slots"],
            "managed_before": managed_before, "managed_after": managed_after,
        }
        assert receipt_dir is not None
        apply_attempt(client, receipt, receipt_dir, observation_deadline=observation_deadline)
        status = "enforced"
    return {
        "outcome_version": 1, "command": "acl-sync", "operation": operation, "status": status,
        "execution_mode": "execute" if execute else "dry-run", "baseline_verified": True,
        "enforcement_verified": status in {"unchanged", "enforced"},
        "change_counts": {"additions": added, "removals": removed},
        **({"version_proof": selection.proof()} if selection is not None else {}),
    }


def preview_dashboard_wans(
    *,
    approved_slots: dict[str, WanSource],
    read_dashboard: Callable[[], list[WanObservation]],
    baseline: dict[str, Any],
    cluster_id: int,
    cluster_label: str,
    selection: RegistryVersionSelection,
    registry_args: dict[str, Any],
    client: ACLClientProtocol | None = None,
) -> dict[str, Any]:
    """Compare one direct dashboard read to the selected registry and retained ACL."""
    if not selection.version_id:
        raise ACLReconcileError("invalid_registry_version_selection")
    before = policy(baseline)
    observations = read_dashboard()
    fetched_at = datetime.now(timezone.utc).isoformat()
    slots, by_source = _match_wan_observations(approved_slots, observations)
    document = {
        "schema_version": 1, "cluster_id": cluster_id, "cluster_label": cluster_label,
        "acl": before, "retained": before["addresses"], "checkpoint": None,
        "slots": slots,
        "observed": {slot: {"ipv4": by_source[source].ipv4} for slot, source in approved_slots.items()},
        "registry_version_proof": selection.proof(),
    }
    summary = reconcile_acl(
        document=document, cluster_id=cluster_id, cluster_label=cluster_label,
        rollback=False, execute=False, receipt_dir=None, selection=selection,
        registry_args=registry_args, client=client,
    )
    return {**summary, "observation_basis": "dashboard_at_retrieval", "dashboard_fetched_at": fetched_at}
