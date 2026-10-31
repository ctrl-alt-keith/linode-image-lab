"""Pinned, reviewed 900-CIDR LKE candidate derived from the official OpenAI feed."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from importlib import resources
from ipaddress import IPv4Network

from .openai_snapshot import ADDRESS_COUNT, load_openai_snapshot

CANDIDATE_SHA256 = "6bf09fe2624bae1a3b794fbaaaaa2d03968aecd78ddbacb07373a5d6b938d539"
CANDIDATE_COUNT = 900
CANDIDATE_ADDRESS_COUNT = 109040
ADDITIONAL_ADDRESSES = 14944
NEW_PREFIX_FLOOR = 21
MAX_CANDIDATE_BYTES = 32768


def _addresses(networks: list[IPv4Network]) -> set[int]:
    return {
        address
        for network in networks
        for address in range(int(network.network_address), int(network.broadcast_address) + 1)
    }


def load_openai_acl_candidate() -> tuple[str, ...]:
    """Validate the committed derived list against its digest and pinned source."""
    try:
        raw = resources.files("linode_image_lab").joinpath("data/chatgpt-agents-lke-900.txt").read_bytes()
    except OSError as exc:
        raise ValueError("pinned OpenAI ACL candidate is unavailable") from exc
    if (len(raw) > MAX_CANDIDATE_BYTES or hashlib.sha256(raw).hexdigest() != CANDIDATE_SHA256
            or not raw.endswith(b"\n")):
        raise ValueError("pinned OpenAI ACL candidate digest does not match")
    try:
        values = raw.decode("ascii").splitlines()
        networks = [IPv4Network(value, strict=True) for value in values]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("pinned OpenAI ACL candidate contains an invalid CIDR") from exc
    source = [IPv4Network(value) for value in load_openai_snapshot()]
    if len(networks) != CANDIDATE_COUNT or len(set(networks)) != CANDIDATE_COUNT:
        raise ValueError("pinned OpenAI ACL candidate count is invalid")
    if values != [str(network) for network in sorted(networks, key=lambda net: (int(net.network_address), net.prefixlen))]:
        raise ValueError("pinned OpenAI ACL candidate order or spelling is invalid")
    if any(not network.is_global or (network.prefixlen < NEW_PREFIX_FLOOR and network not in source)
           for network in networks):
        raise ValueError("pinned OpenAI ACL candidate contains an unsafe supernet")
    original = _addresses(source)
    covered = _addresses(networks)
    if (len(original) != ADDRESS_COUNT or not original <= covered
            or len(covered) != CANDIDATE_ADDRESS_COUNT
            or len(covered) != sum(network.num_addresses for network in networks)
            or len(covered - original) != ADDITIONAL_ADDRESSES):
        raise ValueError("pinned OpenAI ACL candidate coverage is invalid")
    return tuple(values)


def generate_candidate() -> tuple[str, ...]:
    """Reproduce the fixed candidate by minimizing added addresses under /21."""
    source = [IPv4Network(value) for value in load_openai_snapshot()]
    fixed = [network for network in source if network.prefixlen < NEW_PREFIX_FLOOR]
    groups: dict[int, list[IPv4Network]] = defaultdict(list)
    shift = 32 - NEW_PREFIX_FLOOR
    group_size = 1 << shift
    for network in source:
        if network.prefixlen >= NEW_PREFIX_FLOOR:
            groups[int(network.network_address) >> shift].append(network)

    def group_choices(group_id: int, originals: list[IPv4Network]):
        base = group_id << shift
        bits = 0
        for network in originals:
            offset = int(network.network_address) - base
            bits |= ((1 << network.num_addresses) - 1) << offset

        def visit(offset: int, size: int):
            covered = ((bits >> offset) & ((1 << size) - 1)).bit_count()
            if covered == 0:
                return {0: (0, ())}
            network = IPv4Network((base + offset, 32 - (size.bit_length() - 1)))
            result = {1: (size - covered, (network,))}
            if size == 1 or covered == size:
                return result
            left, right = visit(offset, size // 2), visit(offset + size // 2, size // 2)
            for left_count, (left_cost, left_nets) in left.items():
                for right_count, (right_cost, right_nets) in right.items():
                    count, cost = left_count + right_count, left_cost + right_cost
                    if count not in result or cost < result[count][0]:
                        result[count] = (cost, left_nets + right_nets)
            return result

        return visit(0, group_size)

    choices = [(key, networks, group_choices(key, networks)) for key, networks in sorted(groups.items())]
    required_savings = len(source) - CANDIDATE_COUNT
    costs = [float("inf")] * (required_savings + 1)
    costs[0] = 0
    parents = []
    for _, originals, options in choices:
        next_costs = [float("inf")] * (required_savings + 1)
        parent = {}
        for prior_savings, prior_cost in enumerate(costs):
            if prior_cost == float("inf"):
                continue
            for count, (extra, _) in options.items():
                savings = prior_savings + len(originals) - count
                if savings <= required_savings and prior_cost + extra < next_costs[savings]:
                    next_costs[savings] = prior_cost + extra
                    parent[savings] = (prior_savings, count)
        costs = next_costs
        parents.append(parent)
    if costs[required_savings] != ADDITIONAL_ADDRESSES:
        raise ValueError("fixed OpenAI ACL optimization result changed")

    selected = list(fixed)
    savings = required_savings
    for index in range(len(choices) - 1, -1, -1):
        prior, count = parents[index][savings]
        selected.extend(choices[index][2][count][1])
        savings = prior
    return tuple(str(network) for network in sorted(selected, key=lambda net: (int(net.network_address), net.prefixlen)))


if __name__ == "__main__":
    print("\n".join(generate_candidate()))
