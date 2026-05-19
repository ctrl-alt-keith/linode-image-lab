"""Trusted Network Registry ingestion and validation."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import ParseResult, quote, urlparse
from urllib.request import Request, urlopen

ACCESS_KEY_ENV = "LINODE_OBJ_ACCESS_KEY"
SECRET_KEY_ENV = "LINODE_OBJ_SECRET_KEY"
SUPPORTED_SCHEMA_VERSION = 1
UNIVERSAL_CIDRS = {"0.0.0.0/0", "::/0"}


class TrustedRegistryError(ValueError):
    """Raised when the trusted registry cannot be safely used."""


class RegistryFetchError(TrustedRegistryError):
    """Raised when Object Storage registry fetch fails closed."""


class RegistryValidationError(TrustedRegistryError):
    """Raised when a fetched registry payload is not safe to consume."""


@dataclass(frozen=True)
class TrustedRegistry:
    name: str
    generated_at: str
    valid_until: str
    publisher_version: str
    ipv4_cidrs: tuple[str, ...]
    ipv6_cidrs: tuple[str, ...]

    @property
    def cidr_count(self) -> int:
        return len(self.ipv4_cidrs) + len(self.ipv6_cidrs)


def fetch_registry_from_object_storage(
    *,
    endpoint_url: str,
    bucket: str,
    object_key: str,
    region: str | None = None,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Fetch registry JSON from Linode Object Storage using S3 SigV4."""

    values = environ if environ is not None else os.environ
    access_key = _required_env_value(values, ACCESS_KEY_ENV)
    secret_key = _required_env_value(values, SECRET_KEY_ENV)
    endpoint = _parse_endpoint(endpoint_url)
    signing_region = region or _region_from_endpoint(endpoint.hostname or "")
    if not signing_region:
        raise RegistryFetchError("registry Object Storage region is required")

    path = f"/{quote(bucket, safe='')}/{quote(object_key, safe='/')}"
    request_url = f"{endpoint.scheme}://{endpoint.netloc}{path}"
    now = dt.datetime.now(dt.timezone.utc)
    headers = _signed_get_headers(
        access_key=access_key,
        secret_key=secret_key,
        region=signing_region,
        host=endpoint.netloc,
        path=path,
        now=now,
    )
    request = Request(request_url, method="GET", headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except HTTPError as exc:
        raise RegistryFetchError("trusted registry fetch failed") from exc
    except OSError as exc:
        raise RegistryFetchError("trusted registry fetch failed") from exc

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryValidationError("trusted registry JSON could not be parsed") from exc
    if not isinstance(parsed, dict):
        raise RegistryValidationError("trusted registry JSON must be an object")
    return parsed


def validate_registry(payload: dict[str, Any], *, now: dt.datetime | None = None) -> TrustedRegistry:
    """Validate and normalize a Trusted Network Registry v1 payload."""

    current_time = now or dt.datetime.now(dt.timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=dt.timezone.utc)

    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise RegistryValidationError("trusted registry schema_version is not supported")

    registry = _required_dict(payload, "registry")
    name = _required_string(registry, "name", "registry.name")
    generated_at = _required_string(registry, "generated_at", "registry.generated_at")
    valid_until = _required_string(registry, "valid_until", "registry.valid_until")
    publisher_version = _required_string(registry, "publisher_version", "registry.publisher_version")
    valid_until_time = _parse_datetime(valid_until, "registry.valid_until")
    if valid_until_time <= current_time:
        raise RegistryValidationError("trusted registry valid_until is stale")

    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RegistryValidationError("trusted registry entries must be a list")

    ipv4: set[str] = set()
    ipv6: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RegistryValidationError(f"trusted registry entries[{index}] must be an object")
        if entry.get("status") != "active":
            raise RegistryValidationError("trusted registry contains inactive entries")
        address_family = entry.get("address_family")
        if address_family not in {"ipv4", "ipv6"}:
            raise RegistryValidationError("trusted registry entry address_family is not supported")
        cidr = _required_string(entry, "cidr", f"entries[{index}].cidr")
        canonical = _canonical_cidr(cidr)
        if canonical in UNIVERSAL_CIDRS:
            raise RegistryValidationError("trusted registry contains a universal allow CIDR")
        if (address_family == "ipv4" and ":" in canonical) or (address_family == "ipv6" and ":" not in canonical):
            raise RegistryValidationError("trusted registry entry address_family does not match cidr")
        if canonical != cidr:
            raise RegistryValidationError("trusted registry CIDRs must be canonical")
        if address_family == "ipv4":
            ipv4.add(canonical)
        else:
            ipv6.add(canonical)

    return TrustedRegistry(
        name=name,
        generated_at=generated_at,
        valid_until=valid_until,
        publisher_version=publisher_version,
        ipv4_cidrs=tuple(sorted(ipv4, key=_network_sort_key)),
        ipv6_cidrs=tuple(sorted(ipv6, key=_network_sort_key)),
    )


def _required_env_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise RegistryFetchError(f"missing required env var: {name}")
    return value


def _parse_endpoint(endpoint_url: str) -> ParseResult:
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RegistryFetchError("registry Object Storage endpoint URL is invalid")
    return parsed


def _region_from_endpoint(host: str) -> str | None:
    host = host.lower()
    suffix = ".linodeobjects.com"
    if host.endswith(suffix):
        region = host[: -len(suffix)]
        return region or None
    return None


def _signed_get_headers(
    *,
    access_key: str,
    secret_key: str,
    region: str,
    host: str,
    path: str,
    now: dt.datetime,
) -> dict[str, str]:
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [
            "GET",
            path,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Accept": "application/json",
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "Host": host,
        "X-Amz-Content-SHA256": payload_hash,
        "X-Amz-Date": amz_date,
    }


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(f"AWS4{secret_key}".encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RegistryValidationError(f"trusted registry {key} must be an object")
    return value


def _required_string(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryValidationError(f"trusted registry {label} must be a non-empty string")
    return value


def _parse_datetime(value: str, label: str) -> dt.datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RegistryValidationError(f"trusted registry {label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise RegistryValidationError(f"trusted registry {label} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _canonical_cidr(value: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise RegistryValidationError("trusted registry contains an invalid CIDR") from exc
    return str(network)


def _network_sort_key(value: str) -> tuple[int, int, int]:
    network = ipaddress.ip_network(value)
    return (network.version, int(network.network_address), network.prefixlen)
