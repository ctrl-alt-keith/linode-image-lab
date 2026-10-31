"""Pinned public Codex Cloud egress snapshot for an opt-in LKE ACL target."""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from ipaddress import IPv4Network, collapse_addresses, ip_network


SOURCE_URL = "https://openai.com/chatgpt-agents.json"
SHA256 = "d5e2852903834fc805b3a482da2e6a2c275c24752a35d8302a0b8aa346795dae"
CREATION_TIME = "2026-10-29T17:36:49.756588"
PREFIX_COUNT = 1786
ADDRESS_COUNT = 94096
MAX_BYTES = 128 * 1024
MAX_PREFIXES = 2048


def load_openai_snapshot() -> tuple[str, ...]:
    """Return exact-coverage CIDRs after checking the pinned source bytes and shape."""
    try:
        raw = resources.files("linode_image_lab").joinpath("data/chatgpt-agents.json").read_bytes()
    except OSError as exc:
        raise ValueError("pinned OpenAI snapshot is unavailable") from exc
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("pinned OpenAI snapshot size or digest does not match")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pinned OpenAI snapshot is invalid JSON") from exc
    if not isinstance(document, dict) or set(document) != {"creationTime", "prefixes"}:
        raise ValueError("pinned OpenAI snapshot schema is invalid")
    prefixes = document["prefixes"]
    if document["creationTime"] != CREATION_TIME or not isinstance(prefixes, list):
        raise ValueError("pinned OpenAI snapshot date or prefixes are invalid")
    if len(prefixes) != PREFIX_COUNT or len(prefixes) > MAX_PREFIXES:
        raise ValueError("pinned OpenAI snapshot prefix count is invalid")
    networks: list[IPv4Network] = []
    for entry in prefixes:
        if (not isinstance(entry, dict) or set(entry) != {"ipv4Prefix"}
                or not isinstance(entry["ipv4Prefix"], str)):
            raise ValueError("pinned OpenAI snapshot contains an unsupported prefix")
        try:
            network = ip_network(entry["ipv4Prefix"], strict=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("pinned OpenAI snapshot contains an invalid CIDR") from exc
        if not isinstance(network, IPv4Network) or not network.is_global or network.prefixlen < 16:
            raise ValueError("pinned OpenAI snapshot contains a disallowed CIDR")
        networks.append(network)
    if len(set(networks)) != PREFIX_COUNT:
        raise ValueError("pinned OpenAI snapshot contains duplicate CIDRs")
    collapsed = tuple(collapse_addresses(networks))
    if sum(network.num_addresses for network in collapsed) != ADDRESS_COUNT:
        raise ValueError("pinned OpenAI snapshot address count is invalid")
    return tuple(str(network) for network in collapsed)
