"""TOML config loading for CLI execution defaults."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
import tomllib

from .regions import parse_regions
from .user_data import DeployUserData, UserDataError, load_user_data_file


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
    "defaults": {"region", "regions", "ttl", "image_id", "type", "instance_type", "firewall_id"},
    "capture": {"region", "regions", "source_image", "type", "instance_type", "ttl"},
    "deploy": {
        "region",
        "regions",
        "image_id",
        "type",
        "instance_type",
        "firewall_id",
        "ttl",
        "authorized_keys",
        "authorized_keys_file",
        "user_data_file",
    },
    "capture-deploy": {
        "region",
        "regions",
        "source_image",
        "type",
        "instance_type",
        "firewall_id",
        "ttl",
        "authorized_keys",
        "authorized_keys_file",
    },
    "cleanup": {"ttl"},
}
COMMAND_TABLES = {"capture", "deploy", "capture-deploy", "cleanup"}
COMMAND_DEFAULT_FIELDS = {
    "plan": ("regions", "ttl"),
    "capture": ("regions", "ttl", "source_image", "type"),
    "deploy": ("regions", "ttl", "image_id", "type", "firewall_id", "authorized_keys", "user_data"),
    "capture-deploy": ("regions", "ttl", "source_image", "type", "firewall_id", "authorized_keys", "user_data"),
    "cleanup": ("ttl",),
}
CLI_SOURCE_LABELS = {
    "regions": "cli --region",
    "ttl": "cli --ttl",
    "source_image": "cli --source-image",
    "image_id": "cli --image-id",
    "type": "cli --type",
    "firewall_id": "cli --firewall-id",
    "authorized_keys": "cli authorized key inputs",
    "user_data": "cli --user-data-file",
}
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
)
SECRET_POINTER_KEYS = {
    "user_data_file",
}
AUTHORIZED_KEY_TYPES = (
    "ssh-rsa",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519" + "@openssh.com",
    "sk-ecdsa-sha2-nistp256" + "@openssh.com",
)
AUTHORIZED_KEY_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(key_type) for key_type in AUTHORIZED_KEY_TYPES)
    + r") [A-Za-z0-9+/=]+(?: .*)?$"
)
PRIVATE_KEY_MARKERS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "PuTTY-User-Key-File-",
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
    return normalize_config(raw_config, base_dir=config_path.parent)


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
    if "type" in values and "instance_type" in values:
        raise ConfigError(f"config [{table}] cannot set both type and instance_type")

    for key, value in values.items():
        validate_key_is_safe(key, location=f"[{table}]")
        if key not in allowed:
            raise ConfigError(f"unknown config key in [{table}]: {key}")
        validate_value(table, key, value)


def validate_key_is_safe(key: str, *, location: str) -> None:
    normalized = key.lower().replace("-", "_")
    if normalized in SECRET_POINTER_KEYS:
        return
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

    if key == "firewall_id":
        normalize_firewall_id(value, f"config [{table}].firewall_id")
        return

    if key == "authorized_keys":
        if not isinstance(value, list) or not value:
            raise ConfigError(f"config [{table}].authorized_keys must be a non-empty list of public SSH keys")
        for index, item in enumerate(value):
            normalize_authorized_key(item, f"config [{table}].authorized_keys[{index}]")
        return

    if key in {"authorized_keys_file", "user_data_file"}:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"config [{table}].{key} must be a non-empty string")
        return

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config [{table}].{key} must be a non-empty string")


def normalize_config(config: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in config.items():
        if not isinstance(value, dict):
            normalized[key] = value
            continue
        table = dict(value)
        if "authorized_keys" in table:
            table["authorized_keys"] = [
                normalize_authorized_key(item, f"config [{key}].authorized_keys[{index}]")
                for index, item in enumerate(table["authorized_keys"])
            ]
        if "authorized_keys_file" in table:
            table["authorized_keys_file"] = str(resolve_config_path(table["authorized_keys_file"], base_dir=base_dir))
        if "user_data_file" in table:
            table["user_data_file"] = str(resolve_config_path(table["user_data_file"], base_dir=base_dir))
        normalized[key] = table
    return normalized


def resolve_config_path(value: str, *, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def command_defaults(config: dict[str, Any], command: str) -> dict[str, Any]:
    """Return defaults merged as [defaults] then the command-specific table."""
    if not config:
        return {}

    values: dict[str, Any] = dict(config.get("defaults", {}))
    if command in COMMAND_TABLES:
        values.update(config.get(command, {}))
    keys = config_authorized_keys(config, command)
    values.pop("authorized_keys_file", None)
    if keys:
        values["authorized_keys"] = keys
    else:
        values.pop("authorized_keys", None)
    user_data = config_user_data(config, command)
    values.pop("user_data_file", None)
    if user_data is not None:
        values["user_data"] = user_data
    return values


def effective_command_defaults(
    config: dict[str, Any],
    command: str,
    *,
    cli_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return safe, source-labeled defaults for a command.

    The result is suitable for non-mutating config inspection. Values are
    resolved with the same precedence used by command execution:
    CLI-provided values, then command tables, then [defaults].
    """
    if command not in COMMAND_DEFAULT_FIELDS:
        raise ConfigError(f"unsupported command for config validation: {command}")

    cli_values = cli_defaults or {}
    effective_defaults: dict[str, Any] = {}
    sources: list[dict[str, str]] = []

    for field in COMMAND_DEFAULT_FIELDS[command]:
        if field == "authorized_keys":
            keys, key_sources = resolve_authorized_key_defaults(config, command, cli_values)
            if not keys:
                continue
            effective_defaults["authorized_keys"] = {
                "enabled": True,
                "authorized_key_count": len(keys),
            }
            for source in key_sources or [CLI_SOURCE_LABELS[field]]:
                sources.append({"field": field, "source": source})
            continue
        if field == "user_data":
            user_data, user_data_source = resolve_user_data_defaults(config, command, cli_values)
            if user_data is None:
                continue
            effective_defaults["user_data"] = user_data_metadata(user_data)
            sources.append({"field": field, "source": user_data_source})
            continue
        resolved = resolve_default_field(config, command, field, cli_values)
        if resolved is None:
            continue
        value, source = resolved
        effective_defaults[field] = value
        sources.append({"field": field, "source": source})

    return {
        "precedence": precedence_labels(command),
        "effective_defaults": effective_defaults,
        "sources": sources,
    }


def precedence_labels(command: str) -> list[str]:
    labels = ["cli"]
    if command == "capture-deploy":
        labels.append("[deploy]")
    if command in COMMAND_TABLES:
        labels.append(f"[{command}]")
    labels.append("[defaults]")
    return labels


def resolve_default_field(
    config: dict[str, Any],
    command: str,
    field: str,
    cli_values: dict[str, Any],
) -> tuple[Any, str] | None:
    if field in cli_values:
        return normalize_default_value(field, cli_values[field]), CLI_SOURCE_LABELS[field]

    command_values = config.get(command, {}) if command in COMMAND_TABLES else {}
    resolved = resolve_table_field(command_values, field, f"[{command}]")
    if resolved is not None:
        return resolved

    return resolve_table_field(config.get("defaults", {}), field, "[defaults]")


def resolve_authorized_key_defaults(
    config: dict[str, Any],
    command: str,
    cli_values: dict[str, Any],
) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    sources: list[str] = []
    for table_name in authorized_key_table_order(command):
        table = config.get(table_name, {})
        table_keys = table_authorized_keys(table, f"[{table_name}]")
        if table_keys:
            keys.extend(table_keys)
            if "authorized_keys" in table:
                sources.append(f"[{table_name}].authorized_keys")
            if "authorized_keys_file" in table:
                sources.append(f"[{table_name}].authorized_keys_file")

    cli_authorized = cli_values.get("authorized_keys")
    if isinstance(cli_authorized, dict):
        cli_keys = list(cli_authorized.get("keys") or [])
        cli_file = cli_authorized.get("file")
        if cli_keys:
            keys.extend(normalize_authorized_keys(cli_keys, "--authorized-key"))
            sources.append("cli --authorized-key")
        if cli_file is not None:
            keys.extend(load_authorized_keys_file(str(cli_file), "--authorized-keys-file"))
            sources.append("cli --authorized-keys-file")

    return dedupe_authorized_keys(keys), sources


def resolve_user_data_defaults(
    config: dict[str, Any],
    command: str,
    cli_values: dict[str, Any],
) -> tuple[DeployUserData | None, str]:
    if "user_data" in cli_values:
        return load_user_data(str(cli_values["user_data"]), "cli --user-data-file"), CLI_SOURCE_LABELS["user_data"]

    if command not in {"deploy", "capture-deploy"}:
        return None, ""

    deploy_table = config.get("deploy", {})
    if "user_data_file" not in deploy_table:
        return None, ""
    return load_user_data(str(deploy_table["user_data_file"]), "[deploy].user_data_file"), "[deploy].user_data_file"


def user_data_metadata(user_data: DeployUserData) -> dict[str, Any]:
    return {
        "enabled": True,
        "source": user_data.source,
        "byte_count": user_data.byte_count,
    }


def config_authorized_keys(config: dict[str, Any], command: str) -> list[str]:
    keys: list[str] = []
    for table_name in authorized_key_table_order(command):
        keys.extend(table_authorized_keys(config.get(table_name, {}), f"[{table_name}]"))
    return dedupe_authorized_keys(keys)


def authorized_key_table_order(command: str) -> tuple[str, ...]:
    if command == "deploy":
        return ("deploy",)
    if command == "capture-deploy":
        return ("deploy", "capture-deploy")
    return ()


def config_user_data(config: dict[str, Any], command: str) -> DeployUserData | None:
    if command not in {"deploy", "capture-deploy"}:
        return None
    deploy_table = config.get("deploy", {})
    if "user_data_file" not in deploy_table:
        return None
    return load_user_data(str(deploy_table["user_data_file"]), "[deploy].user_data_file")


def table_authorized_keys(table: dict[str, Any], label: str) -> list[str]:
    keys: list[str] = []
    if "authorized_keys" in table:
        keys.extend(normalize_authorized_keys(table["authorized_keys"], f"{label}.authorized_keys"))
    if "authorized_keys_file" in table:
        keys.extend(load_authorized_keys_file(str(table["authorized_keys_file"]), f"{label}.authorized_keys_file"))
    return keys


def normalize_authorized_keys(values: list[Any], label: str) -> list[str]:
    return [normalize_authorized_key(value, f"{label}[{index}]") for index, value in enumerate(values)]


def load_authorized_keys_file(path: str, label: str) -> list[str]:
    key_path = Path(path).expanduser()
    try:
        text = key_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} file not found") from exc
    except OSError as exc:
        raise ConfigError(f"{label} file could not be read") from exc

    if not text:
        raise ConfigError(f"{label} must contain at least one public SSH key")
    return [normalize_authorized_key(line, f"{label} line {index}") for index, line in enumerate(text.splitlines(), 1)]


def load_user_data(path: str, label: str) -> DeployUserData:
    try:
        return load_user_data_file(path, label)
    except UserDataError as exc:
        raise ConfigError(str(exc)) from exc


def normalize_authorized_key(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty public SSH key")
    key = value.strip()
    if any(marker in key for marker in PRIVATE_KEY_MARKERS) or "PRIVATE KEY" in key:
        raise ConfigError(f"{label} must be a public SSH key, not private key material")
    if "\n" in key or "\r" in key or not AUTHORIZED_KEY_RE.match(key):
        raise ConfigError(f"{label} must be a valid OpenSSH public key")
    return key


def dedupe_authorized_keys(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def resolve_table_field(table: dict[str, Any], field: str, label: str) -> tuple[Any, str] | None:
    if field == "regions":
        if "regions" in table:
            return normalize_default_value(field, table["regions"]), f"{label}.regions"
        if "region" in table:
            return normalize_default_value(field, [table["region"]]), f"{label}.region"
        return None

    if field == "type":
        if "instance_type" in table:
            return table["instance_type"], f"{label}.instance_type"
        if "type" in table:
            return table["type"], f"{label}.type"
        return None

    if field in table:
        return normalize_default_value(field, table[field]), f"{label}.{field}"
    return None


def normalize_default_value(field: str, value: Any) -> Any:
    if field == "regions":
        regions = parse_regions(value)
        if not regions:
            raise ConfigError(
                "config validate requires at least one non-empty --region when --region is provided"
            )
        return regions
    if field == "firewall_id":
        return normalize_firewall_id(value, "firewall_id")
    return value


def normalize_firewall_id(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a positive integer")
    if isinstance(value, int):
        firewall_id = value
    elif isinstance(value, str) and value.strip():
        try:
            firewall_id = int(value.strip(), 10)
        except ValueError as exc:
            raise ConfigError(f"{label} must be a positive integer") from exc
    else:
        raise ConfigError(f"{label} must be a positive integer")

    if firewall_id <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return firewall_id
