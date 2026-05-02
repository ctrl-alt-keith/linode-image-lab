"""Structured validation result helpers for execute manifests."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .redaction import redact_text

CheckSpec = tuple[str, str]


def start_validation(checks: Iterable[CheckSpec]) -> dict[str, Any]:
    return {"status": "running", "checks": [check_result(name, target, "pending") for name, target in checks]}


def check_result(name: str, target: str, status: str) -> dict[str, str]:
    return {"name": name, "status": status, "target": target}


def record_validation_check(validation: dict[str, Any], name: str, check: Callable[[], None]) -> None:
    try:
        check()
    except Exception as exc:
        mark_validation_check_failed(validation, name, safe_failure_reason(exc))
        raise
    mark_validation_check_succeeded(validation, name)


def mark_validation_check_succeeded(validation: dict[str, Any], name: str) -> None:
    for check in validation.get("checks", []):
        if check.get("name") == name:
            check["status"] = "succeeded"
            return


def mark_validation_check_failed(validation: dict[str, Any], name: str, failure_reason: str) -> None:
    validation["status"] = "failed"
    for check in validation.get("checks", []):
        if check.get("name") == name:
            check["status"] = "failed"
            check["failure_reason"] = failure_reason
            return


def finish_validation(validation: dict[str, Any]) -> None:
    if validation.get("status") == "failed":
        return
    validation["status"] = "succeeded"


def combined_validation(
    *,
    capture_validation: dict[str, Any] | None,
    deploy_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    validations = [
        validation
        for validation in (
            prefixed_validation(capture_validation, "capture"),
            prefixed_validation(deploy_validation, "deploy"),
        )
        if validation is not None
    ]
    if not validations:
        return {"status": "not_started", "checks": []}

    checks: list[dict[str, Any]] = []
    statuses: list[str] = []
    for validation in validations:
        statuses.append(str(validation.get("status", "not_started")))
        checks.extend(validation.get("checks", []))

    if any(status == "failed" for status in statuses):
        status = "failed"
    elif all(status == "succeeded" for status in statuses):
        status = "succeeded"
    elif any(status == "running" for status in statuses):
        status = "running"
    else:
        status = "not_started"
    return {"status": status, "checks": checks}


def prefixed_validation(validation: dict[str, Any] | None, prefix: str) -> dict[str, Any] | None:
    if validation is None:
        return None
    checks = []
    for check in validation.get("checks", []):
        copied = dict(check)
        target = str(copied.get("target", "validation"))
        copied["target"] = f"{prefix}.{target}"
        checks.append(copied)
    return {"status": validation.get("status", "not_started"), "checks": checks}


def safe_failure_reason(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return redact_text(str(exc))
    return exc.__class__.__name__
