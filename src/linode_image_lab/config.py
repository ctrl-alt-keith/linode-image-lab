"""TOML config loading for CLI execution defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import tomllib


class ConfigError(ValueError):
    """Raised when config cannot safely provide execution defaults."""


SCHEMA_VERSION = 1

ROOT_KEYS = {
    "schema_version",
    "defaults",
    "capture",
    "deploy",
    "capture-deploy",
    "cleanup",
}
TABLE_FIELDS = {
    "defaults": {"region", "regions", "ttl"},
    "capture": {"region", "regions", "source_image", "type", "ttl"},
    "deploy": {"region", "regions", "image_id", "type", "ttl"},
    "capture-deploy": {"region", "regions", "source_image", "type", "ttl"},
    "cleanup": {"ttl"},
}
COMMAND_TABLES = {"capture", "deploy", "capture-deploy", "cleanup"}
PROHIBITED_KEYS = {
    "discover",
    "execute",
    "image-label",
    "image_label",
    "preserve-instance",
    "preserve-source",
    "preserve_instance",
    "preserve_source",
    "run-id",
    "run-id-prefix",
    "run_id",
    "run_id_prefix",
}
SECRET_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "ssh_key",
    "sshkey",
    "private_key",
    "privatekey",
    "root_password",
    "rootpassword",
    "cloud_init",
    "cloudinit",
    "user_data",
    "userdata",
    "authorized_keys",
    "authorizedkeys",
)


def load_config(path: str | None) -> dict[str, Any]:
    """Load and validate an optional TOML config file."""
    if path is None:
        return {}

    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            raw_config = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML config: {exc}") from exc

    validate_config(raw_config)
    return raw_config


def validate_config(config: dict[str, Any]) -> None:
    schema_version = config.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ConfigError("config schema_version must be 1")

    for key, value in config.items():
        validate_key_is_safe(key, location="root")
        if key not in ROOT_KEYS:
            raise ConfigError(f"unknown config key: {key}")
        if key == "schema_version":
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"config [{key}] must be a table")
        validate_table(key, value)


def validate_table(table: str, values: dict[str, Any]) -> None:
    allowed = TABLE_FIELDS[table]
    if "region" in values and "regions" in values:
        raise ConfigError(f"config [{table}] cannot set both region and regions")

    for key, value in values.items():
        validate_key_is_safe(key, location=f"[{table}]")
        if key not in allowed:
            raise ConfigError(f"unknown config key in [{table}]: {key}")
        validate_value(table, key, value)


def validate_key_is_safe(key: str, *, location: str) -> None:
    normalized = key.lower().replace("-", "_")
    if normalized in {value.replace("-", "_") for value in PROHIBITED_KEYS}:
        raise ConfigError(f"config {location} key is not supported: {key}")
    if any(fragment in normalized for fragment in SECRET_KEY_FRAGMENTS):
        raise ConfigError(f"config {location} key must not contain secrets: {key}")


def validate_value(table: str, key: str, value: Any) -> None:
    if key == "regions":
        if not isinstance(value, list) or not value:
            raise ConfigError(f"config [{table}].regions must be a non-empty list of strings")
        if not all(isinstance(region, str) and region.strip() for region in value):
            raise ConfigError(f"config [{table}].regions must be a non-empty list of strings")
        return

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config [{table}].{key} must be a non-empty string")


def command_defaults(config: dict[str, Any], command: str) -> dict[str, Any]:
    """Return defaults merged as [defaults] then the command-specific table."""
    if not config:
        return {}

    values: dict[str, Any] = dict(config.get("defaults", {}))
    if command in COMMAND_TABLES:
        values.update(config.get(command, {}))
    return values
