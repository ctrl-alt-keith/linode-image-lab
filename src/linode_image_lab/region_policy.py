"""Region policy artifact generation and validation."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .linode_api import LinodeClient, LinodeClientProtocol, region_capabilities
from .redaction import redact

SCHEMA_VERSION = 1
DEFAULT_REGION_POLICY_PATH = Path("policy/region-policy.toml")
SUPPORTED_PROVIDER_REGION_KEYS = frozenset({"capabilities"})
SUPPORTED_GROUP_KEYS = frozenset({"regions"})
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
COUNTRY_CODE_RE = re.compile(r"^[a-z]{2}$")


class RegionPolicyError(ValueError):
    """Raised when a region policy artifact cannot be generated or validated."""


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "target": self.target,
        }


@dataclass(frozen=True)
class ResolvedRegionPolicyGroups:
    path: Path
    requested_groups: list[str]
    regions: list[str]
    group_sources: list[dict[str, Any]]
    validation_report: dict[str, Any]


class RegionPolicyGroupResolutionError(RegionPolicyError):
    """Raised when requested policy groups cannot be resolved safely."""

    def __init__(self, message: str, validation_report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.validation_report = validation_report


def generate_region_policy_artifact(
    *,
    client: LinodeClientProtocol | None = None,
    existing_policy_path: Path | None = None,
    replace_groups: bool = False,
) -> str:
    provider_regions = current_provider_region_facts(client=client)
    generated_groups = generated_region_groups(provider_regions)
    groups: dict[str, list[str]] = {}
    if existing_policy_path is not None and existing_policy_path.exists() and not replace_groups:
        groups = load_operator_groups(existing_policy_path)
    return render_region_policy_toml(
        provider_regions=provider_regions,
        generated_groups=generated_groups,
        groups=groups,
    )


def write_region_policy_artifact(
    *,
    path: Path,
    content: str,
) -> None:
    parent = path.parent if path.parent != Path("") else Path(".")
    parent.mkdir(parents=True, exist_ok=True)

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                Path(temp_path).unlink()
            except FileNotFoundError:
                pass


def validate_region_policy_artifact(
    *,
    path: Path,
    client: LinodeClientProtocol | None = None,
) -> dict[str, Any]:
    issues: list[ValidationIssue] = []
    try:
        policy = load_policy(path)
    except RegionPolicyError as exc:
        issues.append(ValidationIssue("malformed_policy", str(exc), str(path)))
        return validation_report(path=path, issues=issues)

    provider_regions = current_provider_region_facts(client=client)
    provider_by_region = {region["region"]: region for region in provider_regions}
    issues.extend(validate_policy_schema(policy))
    if not issues:
        issues.extend(validate_provider_regions_current(policy, provider_by_region))
        issues.extend(validate_generated_groups(policy, provider_regions, provider_by_region))
        issues.extend(validate_region_groups(policy, "groups", provider_by_region))

    return validation_report(
        path=path,
        issues=issues,
        provider_region_count=len(provider_by_region),
        policy_provider_region_count=len(policy.get("provider_regions", {})),
        generated_group_count=len(policy.get("generated_groups", {})),
        group_count=len(policy.get("groups", {})),
    )


def validation_report(
    *,
    path: Path,
    issues: list[ValidationIssue],
    provider_region_count: int | None = None,
    policy_provider_region_count: int | None = None,
    generated_group_count: int | None = None,
    group_count: int | None = None,
) -> dict[str, Any]:
    valid = not issues
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "command": "region-policy",
        "action": "validate",
        "path": str(path),
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "safety": {
            "mutates": False,
            "auth_lookup": "not_attempted",
            "account_data": "not_read",
        },
        "errors": [issue.to_dict() for issue in issues],
    }
    if provider_region_count is not None:
        report["provider_region_count"] = provider_region_count
    if policy_provider_region_count is not None:
        report["policy_provider_region_count"] = policy_provider_region_count
    if generated_group_count is not None:
        report["generated_group_count"] = generated_group_count
    if group_count is not None:
        report["group_count"] = group_count
    return report


def serialize_validation_report(report: dict[str, Any]) -> str:
    return json.dumps(redact(report), indent=2, sort_keys=True) + "\n"


def resolve_region_policy_groups(
    *,
    path: Path,
    group_names: list[str],
    client: LinodeClientProtocol | None = None,
) -> ResolvedRegionPolicyGroups:
    requested_groups = unique_names(group_names)
    report = validate_region_policy_artifact(path=path, client=client)
    if not report["valid"]:
        first_error = report["errors"][0] if report["errors"] else {}
        code = first_error.get("code", "invalid_policy")
        target = first_error.get("target", str(path))
        raise RegionPolicyGroupResolutionError(
            f"region policy validation failed before resolving replication groups: {code} at {target}",
            report,
        )

    policy = load_policy(path)
    operator_groups = policy.get("groups", {})
    generated_groups = policy.get("generated_groups", {})
    if not isinstance(operator_groups, dict):
        operator_groups = {}
    if not isinstance(generated_groups, dict):
        generated_groups = {}

    regions: list[str] = []
    sources: list[dict[str, Any]] = []
    for group_name in requested_groups:
        source_namespace = ""
        group: Any = None
        if group_name in operator_groups:
            source_namespace = "groups"
            group = operator_groups[group_name]
        elif group_name in generated_groups:
            source_namespace = "generated_groups"
            group = generated_groups[group_name]
        else:
            raise RegionPolicyGroupResolutionError(
                f"unknown replication group in region policy: {group_name}",
                report,
            )

        if not isinstance(group, dict) or not string_list(group.get("regions")) or not group.get("regions"):
            raise RegionPolicyGroupResolutionError(
                f"malformed replication group in region policy: {source_namespace}.{group_name}",
                report,
            )

        group_regions = [str(region).strip().lower() for region in group["regions"]]
        regions.extend(group_regions)
        sources.append(
            {
                "group": group_name,
                "source": source_namespace,
                "regions": unique_names(group_regions),
            }
        )

    return ResolvedRegionPolicyGroups(
        path=path,
        requested_groups=requested_groups,
        regions=unique_names(regions),
        group_sources=sources,
        validation_report=report,
    )


def unique_names(values: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def current_provider_region_facts(
    *,
    client: LinodeClientProtocol | None = None,
) -> list[dict[str, Any]]:
    linode = client or LinodeClient()
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in linode.list_regions():
        region = item.get("region")
        if not isinstance(region, str) or not region.strip():
            continue
        region_id = region.strip()
        if region_id in seen:
            continue
        seen.add(region_id)
        normalized.append(
            {
                "region": region_id,
                "capabilities": normalize_capabilities(item.get("capabilities", [])),
                "country": normalize_country(item.get("country")),
            }
        )
    return sorted(normalized, key=lambda entry: entry["region"])


def normalize_capabilities(value: object) -> list[str]:
    if isinstance(value, dict):
        return normalize_capabilities(region_capabilities(value))
    if not isinstance(value, list):
        return []
    capabilities: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        capability = item.strip()
        if capability in seen:
            continue
        seen.add(capability)
        capabilities.append(capability)
    return sorted(capabilities)


def normalize_country(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    country = value.strip().lower()
    if COUNTRY_CODE_RE.fullmatch(country) is None:
        return None
    return country


def generated_region_groups(provider_regions: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    capability_groups: dict[str, set[str]] = {}
    country_groups: dict[str, set[str]] = {}
    country_capability_groups: dict[str, set[str]] = {}
    for entry in provider_regions:
        region = entry.get("region")
        if not isinstance(region, str) or not region.strip():
            continue
        region_id = region.strip()
        country = normalize_country(entry.get("country"))
        for capability in normalize_capabilities(entry.get("capabilities", [])):
            group_name = generated_capability_group_name(capability)
            if group_name is not None:
                capability_groups.setdefault(group_name, set()).add(region_id)
            country_group_name = generated_country_capability_group_name(country, capability)
            if country_group_name is not None:
                country_capability_groups.setdefault(country_group_name, set()).add(region_id)
        if country is not None:
            country_groups.setdefault(f"country_{country}", set()).add(region_id)

    for name, regions in sorted(capability_groups.items()):
        groups[name] = sorted(regions)
    for name, regions in sorted(country_groups.items()):
        groups[name] = sorted(regions)
    for name, regions in sorted(country_capability_groups.items()):
        groups[name] = sorted(regions)
    return groups


def generated_capability_group_name(capability: str) -> str | None:
    slug = generated_capability_slug(capability)
    if slug is None:
        return None
    return f"capability_{slug}"


def generated_country_capability_group_name(country: str | None, capability: str) -> str | None:
    if country is None:
        return None
    slug = generated_capability_slug(capability)
    if slug is None:
        return None
    return f"country_{country}_{slug}"


def generated_capability_slug(capability: str) -> str | None:
    normalized = capability.strip().lower()
    if not normalized:
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not slug:
        return None
    return slug


def load_operator_groups(path: Path) -> dict[str, list[str]]:
    policy = load_policy(path)
    issues: list[ValidationIssue] = []
    if policy.get("schema_version") != SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                "unsupported_schema_version",
                "schema_version must be 1",
                "schema_version",
            )
        )
    groups = policy.get("groups", {})
    if groups is not None and not isinstance(groups, dict):
        issues.append(ValidationIssue("malformed_groups", "groups must be a table", "groups"))
    elif isinstance(groups, dict):
        issues.extend(group_schema_issues(groups, "groups"))
    if issues:
        first = issues[0]
        raise RegionPolicyError(f"{first.target}: {first.message}")
    return {
        str(name): list(group["regions"])
        for name, group in sorted(groups.items())
        if isinstance(group, dict)
    }


def load_policy(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RegionPolicyError(f"policy file does not exist: {path}") from exc
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise RegionPolicyError(f"policy file is not valid TOML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RegionPolicyError("policy file must contain a TOML document")
    return parsed


def validate_policy_schema(policy: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    allowed_top_level = {"schema_version", "provider_regions", "generated_groups", "groups"}
    for key in sorted(policy):
        if key not in allowed_top_level:
            issues.append(
                ValidationIssue(
                    "unknown_top_level_key",
                    "policy contains an unsupported top-level key",
                    str(key),
                )
            )

    if policy.get("schema_version") != SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                "unsupported_schema_version",
                "schema_version must be 1",
                "schema_version",
            )
        )

    provider_regions = policy.get("provider_regions")
    if not isinstance(provider_regions, dict) or not provider_regions:
        issues.append(
            ValidationIssue(
                "missing_provider_regions",
                "provider_regions must be a non-empty table",
                "provider_regions",
            )
        )
    else:
        issues.extend(provider_region_schema_issues(provider_regions))

    for namespace in ("generated_groups", "groups"):
        groups = policy.get(namespace, {})
        if groups is None:
            continue
        if not isinstance(groups, dict):
            issues.append(ValidationIssue(f"malformed_{namespace}", f"{namespace} must be a table", namespace))
        else:
            issues.extend(group_schema_issues(groups, namespace))
    return issues


def provider_region_schema_issues(provider_regions: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for region, details in sorted(provider_regions.items()):
        target = f"provider_regions.{region}"
        if not isinstance(details, dict):
            issues.append(
                ValidationIssue(
                    "malformed_provider_region",
                    "provider region entry must be a table",
                    target,
                )
            )
            continue
        for key in sorted(details):
            if key not in SUPPORTED_PROVIDER_REGION_KEYS:
                issues.append(
                    ValidationIssue(
                        "unknown_provider_region_key",
                        "provider region entry contains an unsupported key",
                        f"{target}.{key}",
                    )
                )
        capabilities = details.get("capabilities")
        if not string_list(capabilities):
            issues.append(
                ValidationIssue(
                    "malformed_provider_capabilities",
                    "provider region capabilities must be a list of non-empty strings",
                    f"{target}.capabilities",
                )
            )
    return issues


def group_schema_issues(groups: dict[str, Any], namespace: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for group_name, group in sorted(groups.items()):
        target = f"{namespace}.{group_name}"
        if not isinstance(group, dict):
            issues.append(ValidationIssue(f"malformed_{namespace}_entry", "group entry must be a table", target))
            continue
        for key in sorted(group):
            if key not in SUPPORTED_GROUP_KEYS:
                issues.append(
                    ValidationIssue(
                        f"unknown_{namespace}_key",
                        "group entry contains an unsupported key",
                        f"{target}.{key}",
                    )
                )
        regions = group.get("regions")
        if not string_list(regions):
            issues.append(
                ValidationIssue(
                    f"malformed_{namespace}_regions",
                    "group regions must be a list of non-empty strings",
                    f"{target}.regions",
                )
            )
    return issues


def validate_provider_regions_current(
    policy: dict[str, Any],
    provider_by_region: dict[str, dict[str, Any]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    provider_regions = policy.get("provider_regions", {})
    if not isinstance(provider_regions, dict):
        return issues
    for region, details in sorted(provider_regions.items()):
        target = f"provider_regions.{region}"
        current = provider_by_region.get(str(region))
        if current is None:
            issues.append(
                ValidationIssue(
                    "unknown_provider_region",
                    "provider region no longer exists in current provider metadata",
                    target,
                )
            )
            continue
        current_capabilities = normalize_capabilities(current.get("capabilities", []))
        configured_capabilities = normalize_capabilities(details.get("capabilities", []))
        if configured_capabilities != current_capabilities:
            issues.append(
                ValidationIssue(
                    "stale_provider_capabilities",
                    "provider region capabilities differ from current provider metadata",
                    f"{target}.capabilities",
                )
            )

    for region in sorted(provider_by_region):
        if region not in provider_regions:
            issues.append(
                ValidationIssue(
                    "missing_provider_region",
                    "current provider region is missing from the artifact",
                    f"provider_regions.{region}",
                )
            )
    return issues


def validate_generated_groups(
    policy: dict[str, Any],
    provider_regions: list[dict[str, Any]],
    provider_by_region: dict[str, dict[str, Any]],
) -> list[ValidationIssue]:
    issues = validate_region_groups(policy, "generated_groups", provider_by_region)
    generated_groups = policy.get("generated_groups", {})
    if not isinstance(generated_groups, dict):
        return issues

    expected = generated_region_groups(provider_regions)
    actual = normalized_group_region_map(generated_groups)
    if actual != expected:
        issues.append(
            ValidationIssue(
                "stale_generated_groups",
                "generated groups differ from current provider-derived helper groups",
                "generated_groups",
            )
        )
    return issues


def validate_region_groups(
    policy: dict[str, Any],
    namespace: str,
    provider_by_region: dict[str, dict[str, Any]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    groups = policy.get(namespace, {})
    if not isinstance(groups, dict):
        return issues
    for group_name, group in sorted(groups.items()):
        if not isinstance(group, dict) or not isinstance(group.get("regions"), list):
            continue
        for region in group["regions"]:
            if region not in provider_by_region:
                issues.append(
                    ValidationIssue(
                        f"unknown_{namespace}_region",
                        "group references a region missing from current provider metadata",
                        f"{namespace}.{group_name}.regions",
                    )
                )
    return issues


def normalized_group_region_map(groups: dict[str, Any]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for name, group in groups.items():
        if not isinstance(group, dict) or not isinstance(group.get("regions"), list):
            continue
        regions = [region for region in group["regions"] if isinstance(region, str) and region.strip()]
        normalized[str(name)] = sorted(regions)
    return dict(sorted(normalized.items()))


def string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and bool(item.strip()) for item in value)


def render_region_policy_toml(
    *,
    provider_regions: list[dict[str, Any]],
    generated_groups: dict[str, list[str]] | None = None,
    groups: dict[str, list[str]] | None = None,
) -> str:
    lines = [
        "# Generated by linode-image-lab region-policy generate.",
        "# Version this provider policy snapshot and review diffs for provider drift.",
        "# provider_regions.* and generated_groups.* are generated; groups.* is operator-owned intent.",
        f"schema_version = {SCHEMA_VERSION}",
        "",
    ]
    for region in provider_regions:
        region_id = str(region["region"])
        lines.append(f"[provider_regions.{toml_key(region_id)}]")
        lines.append(f"capabilities = {toml_string_list(normalize_capabilities(region.get('capabilities', [])))}")
        lines.append("")

    for group_name, regions in sorted((generated_groups or {}).items()):
        lines.append(f"[generated_groups.{toml_key(str(group_name))}]")
        lines.append(f"regions = {toml_string_list(list(regions))}")
        lines.append("")

    for group_name, regions in sorted((groups or {}).items()):
        lines.append(f"[groups.{toml_key(str(group_name))}]")
        lines.append(f"regions = {toml_string_list(list(regions))}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def toml_string_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def toml_key(value: str) -> str:
    return value if BARE_KEY_RE.fullmatch(value) else json.dumps(value)
