"""Supply protected runtime inputs for one exact WAN reconciliation."""

from __future__ import annotations

from http.client import HTTPException
import json
import os
import re
import secrets
from urllib.parse import quote, urlencode
from urllib.request import Request

from .acl_reconcile import ACLReconcileError, WanObservation, WanSource, host_route, reconcile_exact_wan_acl
from .config import load_config, normalize_firewall_id
from .http_transport import open_without_redirect
from .linode_api import LinodeClient, LinodeTokenError
from .registry_version_selection import RegistryVersionSelection
from .trusted_registry import read_current_registry_version

COMMAND = "acl-reconcile-wans"
MAX_WAN_RESPONSE = 64 * 1024
WAN_INTERFACES = ("wan1", "wan2")


def reconcile_configured_wans(config_path: str | None) -> dict:
    try:
        config = load_config(config_path)["acl-sync"]
        cluster_id = normalize_firewall_id(config["cluster_id"], "cluster_id")
        cluster_label = config["cluster_label"]
        registry_args = dict(endpoint_url=config["registry_endpoint_url"], bucket=config["registry_bucket"],
                             object_key=config["registry_object_key"], region=config.get("registry_region"))
    except (OSError, KeyError, ValueError):
        raise ACLReconcileError("invalid_wan_acl_config") from None

    organization = os.environ.get("MERAKI_ORGANIZATION_ID", "")
    serial = os.environ.get("MERAKI_DEVICE_SERIAL", "")
    api_key = os.environ.get("MERAKI_DASHBOARD_API_KEY", "")
    if not re.fullmatch(r"[0-9]{1,64}", organization) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", serial):
        raise ACLReconcileError("missing_or_invalid_meraki_selector")
    if not api_key or any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        raise ACLReconcileError("missing_or_invalid_meraki_credential")
    try:
        client = LinodeClient.from_env(command=COMMAND)
    except LinodeTokenError:
        raise ACLReconcileError("missing_linode_credential") from None

    try:
        version = read_current_registry_version(**registry_args)
    except ValueError:
        raise ACLReconcileError("registry_version_unavailable") from None
    return reconcile_exact_wan_acl(
        approved_slots={interface: WanSource(serial, interface) for interface in WAN_INTERFACES},
        read_dashboard=lambda: _read_dashboard_wans(organization, serial, api_key),
        cluster_id=cluster_id, cluster_label=cluster_label, registry_args=registry_args,
        selection=RegistryVersionSelection(version, secrets.token_bytes(32)), client=client,
    )


def _read_dashboard_wans(organization: str, serial: str, api_key: str) -> list[WanObservation]:
    query = urlencode({"serials[]": serial})
    url = (f"https://api.meraki.com/api/v1/organizations/{quote(organization, safe='')}"
           f"/devices/uplinks/addresses/byDevice?{query}")
    request = Request(url, method="GET", headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"})
    try:
        with open_without_redirect(request, timeout=30) as response:
            if re.search(r'\brel\s*=\s*(?:"[^"]*\bnext\b[^"]*"|next\b)', response.headers.get("Link", ""), re.I):
                raise ACLReconcileError("meraki_response_incomplete")
            raw = response.read(MAX_WAN_RESPONSE + 1)
            if len(raw) > MAX_WAN_RESPONSE:
                raise ACLReconcileError("meraki_response_incomplete")
            devices = json.loads(raw)
    except ACLReconcileError:
        raise
    except (OSError, HTTPException, ValueError):
        raise ACLReconcileError("meraki_read_failed") from None

    try:
        if not isinstance(devices, list) or len(devices) != 1 or devices[0]["serial"] != serial:
            raise ValueError
        uplinks = devices[0]["uplinks"]
        if not isinstance(uplinks, list):
            raise ValueError
        observations = []
        for uplink in uplinks:
            interface = uplink["interface"]
            if interface not in WAN_INTERFACES:
                continue
            addresses = uplink["addresses"]
            if not isinstance(addresses, list):
                raise ValueError
            ipv4 = [address for address in addresses if address["protocol"] == "ipv4"]
            if len(ipv4) != 1:
                raise ValueError
            route = host_route(f'{ipv4[0]["public"]["address"]}/32')
            observations.append(WanObservation(WanSource(serial, interface), route, observed_at=None))
        return observations
    except (KeyError, TypeError, ValueError):
        raise ACLReconcileError("invalid_meraki_wan_observation") from None
