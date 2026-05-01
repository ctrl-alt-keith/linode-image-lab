"""Redaction helpers for public-safe CLI output."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTION = "[REDACTED]"
SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|api[_-]?key|credential)", re.I)
TOKEN_TEXT_RE = re.compile(
    r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}|\b(token|secret|password)=([^\s]+)"
)
PROVIDER_IDENTIFIER_KEYS = {
    "account_id",
    "disk_id",
    "image_id",
    "linode_id",
    "provider_id",
    "resource_id",
    "user_id",
}


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key))


def is_provider_identifier_key(key: str) -> bool:
    return key in PROVIDER_IDENTIFIER_KEYS


def redact_text(value: str) -> str:
    """Redact token-like text while preserving environment variable names."""
    if value == "LINODE_TOKEN":
        return value

    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            return f"{match.group(1)}{REDACTION}"
        if match.group(2):
            return f"{match.group(2)}={REDACTION}"
        return REDACTION

    return TOKEN_TEXT_RE.sub(replace, value)


def redact(value: Any) -> Any:
    """Recursively redact mappings and sequences."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text) or is_provider_identifier_key(key_text):
                redacted[key_text] = REDACTION
            else:
                redacted[key_text] = redact(item)
        return redacted

    if isinstance(value, str):
        return redact_text(value)

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item) for item in value]

    return value
