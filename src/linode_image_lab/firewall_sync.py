"""Trusted registry to Linode Cloud Firewall sync planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .linode_api import LinodeClient, LinodeClientProtocol, LinodePreflightError
from .manifest import PROJECT
from .trusted_registry import (
    TrustedRegistry,
    TrustedRegistryError,
    fetch_registry_from_object_storage,
    validate_registry,
)

MANAGED_RULE_DESCRIPTION = "Managed by linode-image-lab trusted-network-registry sync."
MAX_FIREWALL_RULE_ADDRESSES = 255


class FirewallSyncError(ValueError):
    """Raised when firewall sync cannot safely continue."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest


class FirewallRulesClientProtocol(LinodeClientProtocol, Protocol):
    def get_firewall_rules(self, firewall_id: int) -> dict[str, Any]: ...

    def update_firewall_rules(self, firewall_id: int, rules: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FirewallSyncOptions:
    firewall_id: int
    registry_endpoint_url: str
    registry_bucket: str
    registry_object_key: str
    registry_region: str | None = None
    protocol: str = "TCP"
    ports: str | None = None
    managed_label: str = "tnr-allowlist"
    execute: bool = False


def firewall_sync_plan(
    *,
    firewall_id: int,
    registry_endpoint_url: str,
    registry_bucket: str,
    registry_object_key: str,
    registry_region: str | None = None,
    protocol: str = "TCP",
    ports: str | None = None,
    managed_label: str = "tnr-allowlist",
    execute: bool = False,
    client: FirewallRulesClientProtocol | None = None,
    environ: Mapping[str, str] | None = None,
    plan_reporter: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    options = FirewallSyncOptions(
        firewall_id=firewall_id,
        registry_endpoint_url=registry_endpoint_url,
        registry_bucket=registry_bucket,
        registry_object_key=registry_object_key,
        registry_region=registry_region,
        protocol=normalize_protocol(protocol),
        ports=normalize_ports(ports),
        managed_label=normalize_managed_label(managed_label),
        execute=execute,
    )
    validate_rule_shape(options)
    sync_client = client or LinodeClient.from_env(
        command="firewall-sync",
        option="--execute" if execute else "dry-run",
    )
    try:
        payload = fetch_registry_from_object_storage(
            endpoint_url=options.registry_endpoint_url,
            bucket=options.registry_bucket,
            object_key=options.registry_object_key,
            region=options.registry_region,
            environ=environ,
        )
        registry = validate_registry(payload)
        current_rules = sync_client.get_firewall_rules(options.firewall_id)
        plan = build_firewall_sync_manifest(
            options=options,
            registry=registry,
            current_rules=current_rules,
        )
    except (TrustedRegistryError, LinodePreflightError, ValueError) as exc:
        raise FirewallSyncError(str(exc)) from exc

    if not execute:
        return plan

    if plan_reporter is not None:
        plan_reporter(execute_plan_summary(plan))
    if plan["planned_action"] == "keep_managed_rule":
        plan["execution_mode"] = "execute"
        plan["dry_run"] = False
        plan["status"] = "unchanged"
        plan["message"] = "trusted registry firewall sync found no managed rule changes"
        plan["applied"] = False
        return plan
    plan["execution_mode"] = "execute"
    plan["dry_run"] = False
    plan["safety"]["mutates"] = True
    try:
        sync_client.update_firewall_rules(options.firewall_id, plan["provider_payload"])
    except ValueError as exc:
        raise FirewallSyncError("firewall-sync --execute failed", plan) from exc

    plan["status"] = "applied"
    plan["message"] = "trusted registry firewall sync applied managed rule changes"
    plan["applied"] = True
    return plan


def build_firewall_sync_manifest(
    *,
    options: FirewallSyncOptions,
    registry: TrustedRegistry,
    current_rules: dict[str, Any],
) -> dict[str, Any]:
    rules = normalize_firewall_rules(current_rules)
    intended_rule = intended_managed_rule(options, registry)
    managed_index = managed_rule_index(rules["inbound"], options.managed_label)
    current_managed = rules["inbound"][managed_index] if managed_index is not None else None

    current_cidrs = managed_rule_cidrs(current_managed) if current_managed is not None else {"ipv4": [], "ipv6": []}
    intended_cidrs = {"ipv4": list(registry.ipv4_cidrs), "ipv6": list(registry.ipv6_cidrs)}
    additions = cidr_difference(intended_cidrs, current_cidrs)
    removals = cidr_difference(current_cidrs, intended_cidrs)
    kept = cidr_intersection(intended_cidrs, current_cidrs)

    inbound = list(rules["inbound"])
    if managed_index is None:
        inbound.append(intended_rule)
        action = "add_managed_rule"
    else:
        inbound[managed_index] = intended_rule
        action = "replace_managed_rule" if rule_changed(current_managed, intended_rule) else "keep_managed_rule"

    provider_payload = {
        "inbound": inbound,
        "outbound": rules["outbound"],
        "inbound_policy": rules["inbound_policy"],
        "outbound_policy": rules["outbound_policy"],
    }

    return {
        "schema_version": 1,
        "project": PROJECT,
        "command": "firewall-sync",
        "status": "planned",
        "valid": True,
        "dry_run": True,
        "execution_mode": "dry-run",
        "message": "firewall-sync is non-mutating unless --execute is provided",
        "safety": {
            "mutates": False,
            "requires_execute_for_mutation": True,
            "stale_registry_fallback": "disabled",
            "managed_rule_ownership": "exact label and description",
        },
        "target": {
            "firewall_id": options.firewall_id,
            "managed_label": options.managed_label,
        },
        "registry": {
            "name": registry.name,
            "generated_at": registry.generated_at,
            "valid_until": registry.valid_until,
            "publisher_version": registry.publisher_version,
            "cidr_count": registry.cidr_count,
            "ipv4_count": len(registry.ipv4_cidrs),
            "ipv6_count": len(registry.ipv6_cidrs),
        },
        "managed_rule": public_rule_summary(intended_rule),
        "planned_action": action,
        "diff": {
            "additions": additions,
            "removals": removals,
            "kept": kept,
        },
        "unmanaged_rule_count": len(rules["inbound"]) - (1 if managed_index is not None else 0),
        "provider_payload": provider_payload,
    }


def normalize_firewall_rules(rules: dict[str, Any]) -> dict[str, Any]:
    inbound = rules.get("inbound")
    outbound = rules.get("outbound")
    inbound_policy = rules.get("inbound_policy")
    outbound_policy = rules.get("outbound_policy")
    if not isinstance(inbound, list) or not all(isinstance(rule, dict) for rule in inbound):
        raise FirewallSyncError("Linode firewall inbound rules response is invalid")
    if not isinstance(outbound, list) or not all(isinstance(rule, dict) for rule in outbound):
        raise FirewallSyncError("Linode firewall outbound rules response is invalid")
    if inbound_policy not in {"ACCEPT", "DROP"} or outbound_policy not in {"ACCEPT", "DROP"}:
        raise FirewallSyncError("Linode firewall rules policy response is invalid")
    return {
        "inbound": [dict(rule) for rule in inbound],
        "outbound": [dict(rule) for rule in outbound],
        "inbound_policy": inbound_policy,
        "outbound_policy": outbound_policy,
    }


def intended_managed_rule(options: FirewallSyncOptions, registry: TrustedRegistry) -> dict[str, Any]:
    cidr_count = registry.cidr_count
    if cidr_count == 0:
        raise FirewallSyncError("trusted registry contains no active CIDRs")
    if cidr_count > MAX_FIREWALL_RULE_ADDRESSES:
        raise FirewallSyncError("trusted registry exceeds Linode firewall address limit for one rule")

    rule = {
        "label": options.managed_label,
        "description": MANAGED_RULE_DESCRIPTION,
        "action": "ACCEPT",
        "protocol": options.protocol,
        "addresses": {
            "ipv4": list(registry.ipv4_cidrs),
            "ipv6": list(registry.ipv6_cidrs),
        },
    }
    if options.ports is not None:
        rule["ports"] = options.ports
    return rule


def managed_rule_index(inbound_rules: list[dict[str, Any]], managed_label: str) -> int | None:
    matches = [index for index, rule in enumerate(inbound_rules) if rule.get("label") == managed_label]
    if not matches:
        return None
    if len(matches) > 1:
        raise FirewallSyncError("multiple firewall rules use the managed label; ownership is ambiguous")
    index = matches[0]
    if inbound_rules[index].get("description") != MANAGED_RULE_DESCRIPTION:
        raise FirewallSyncError("firewall rule uses the managed label without the managed description")
    return index


def managed_rule_cidrs(rule: dict[str, Any] | None) -> dict[str, list[str]]:
    if rule is None:
        return {"ipv4": [], "ipv6": []}
    addresses = rule.get("addresses")
    if not isinstance(addresses, dict):
        return {"ipv4": [], "ipv6": []}
    return {
        "ipv4": sorted(str(value) for value in addresses.get("ipv4", []) if isinstance(value, str)),
        "ipv6": sorted(str(value) for value in addresses.get("ipv6", []) if isinstance(value, str)),
    }


def cidr_difference(left: dict[str, list[str]], right: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        "ipv4": sorted(set(left["ipv4"]) - set(right["ipv4"])),
        "ipv6": sorted(set(left["ipv6"]) - set(right["ipv6"])),
    }


def cidr_intersection(left: dict[str, list[str]], right: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        "ipv4": sorted(set(left["ipv4"]) & set(right["ipv4"])),
        "ipv6": sorted(set(left["ipv6"]) & set(right["ipv6"])),
    }


def rule_changed(current_rule: dict[str, Any] | None, intended_rule: dict[str, Any]) -> bool:
    if current_rule is None:
        return True
    return public_rule_summary(current_rule) != public_rule_summary(intended_rule)


def public_rule_summary(rule: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "label": rule.get("label"),
        "description": rule.get("description"),
        "action": rule.get("action"),
        "protocol": rule.get("protocol"),
        "addresses": managed_rule_cidrs(rule),
    }
    if "ports" in rule:
        summary["ports"] = rule["ports"]
    return summary


def normalize_protocol(value: str) -> str:
    protocol = value.strip().upper() if isinstance(value, str) else ""
    if protocol not in {"TCP", "UDP", "ICMP", "IPENCAP"}:
        raise FirewallSyncError("--protocol must be one of TCP, UDP, ICMP, or IPENCAP")
    return protocol


def normalize_ports(value: str | None) -> str | None:
    if value is None:
        return None
    ports = value.strip()
    if not ports:
        raise FirewallSyncError("--ports must be non-empty when provided")
    return ports


def normalize_managed_label(value: str) -> str:
    label = value.strip() if isinstance(value, str) else ""
    if not 3 <= len(label) <= 32:
        raise FirewallSyncError("--managed-label must be between 3 and 32 characters")
    return label


def validate_rule_shape(options: FirewallSyncOptions) -> None:
    if options.protocol in {"TCP", "UDP"} and options.ports is None:
        raise FirewallSyncError(f"--ports is required for {options.protocol} firewall rules")
    if options.protocol in {"ICMP", "IPENCAP"} and options.ports is not None:
        raise FirewallSyncError(f"--ports is not allowed for {options.protocol} firewall rules")


def execute_plan_summary(plan: dict[str, Any]) -> str:
    diff = plan["diff"]
    return (
        "firewall-sync planned changes before execute:\n"
        f"  action: {plan['planned_action']}\n"
        f"  add ipv4: {len(diff['additions']['ipv4'])}, ipv6: {len(diff['additions']['ipv6'])}\n"
        f"  remove ipv4: {len(diff['removals']['ipv4'])}, ipv6: {len(diff['removals']['ipv6'])}\n"
        f"  keep ipv4: {len(diff['kept']['ipv4'])}, ipv6: {len(diff['kept']['ipv6'])}\n"
    )
