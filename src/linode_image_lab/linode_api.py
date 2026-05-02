"""Linode API boundary for explicit execution commands."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .manifest import PROJECT, tags_to_dict

TOKEN_ENV_NAME = "LINODE_TOKEN"
DEFAULT_API_BASE_URL = "https://api.linode.com/v4"
DEFAULT_RETRY_BACKOFF_SECONDS = (0.5, 1.0)
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class LinodeApiError(ValueError):
    """Raised when the Linode API boundary cannot complete a requested action."""


class LinodeTokenError(LinodeApiError):
    """Raised when execute mode lacks a usable Linode token."""


class LinodePreflightError(LinodeApiError):
    """Raised when read-only provider input checks fail."""


class LinodeClientProtocol(Protocol):
    def preflight(self) -> None: ...

    def preflight_region(self, region: str) -> None: ...

    def preflight_instance_type(self, instance_type: str) -> None: ...

    def preflight_image(self, image_id: str) -> None: ...

    def create_instance(
        self,
        *,
        region: str,
        source_image: str,
        instance_type: str,
        label: str,
        tags: list[str],
        root_password: str,
    ) -> dict[str, Any]: ...

    def wait_instance_ready(self, linode_id: int) -> dict[str, Any]: ...

    def list_disks(self, linode_id: int) -> list[dict[str, Any]]: ...

    def shutdown_instance(self, linode_id: int) -> dict[str, Any]: ...

    def wait_instance_offline(self, linode_id: int) -> dict[str, Any]: ...

    def capture_image(
        self,
        *,
        disk_id: int,
        label: str,
        tags: list[str],
        description: str,
        cloud_init: bool,
    ) -> dict[str, Any]: ...

    def wait_image_available(self, image_id: str) -> dict[str, Any]: ...

    def list_managed_linodes(self) -> list[dict[str, Any]]: ...

    def delete_instance(self, linode_id: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LinodeClient:
    """Small HTTP client used only after the user opts into execution."""

    token: str = field(repr=False)
    api_base_url: str = DEFAULT_API_BASE_URL
    timeout_seconds: float = 30
    poll_interval_seconds: float = 5
    max_wait_seconds: float = 900
    max_retry_attempts: int = 3
    retry_backoff_seconds: tuple[float, ...] = DEFAULT_RETRY_BACKOFF_SECONDS
    retry_events: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        command: str = "capture",
        option: str = "--execute",
    ) -> "LinodeClient":
        values = env if env is not None else os.environ
        token = values.get(TOKEN_ENV_NAME)
        if not token:
            raise LinodeTokenError(f"{TOKEN_ENV_NAME} is required for {command} {option}")
        return cls(token=token)

    def preflight(self) -> None:
        self._request("GET", "/profile", retry=True, operation="preflight_profile")
        self._request("GET", "/profile/grants", allow_empty=True, retry=True, operation="preflight_grants")

    def preflight_region(self, region: str) -> None:
        escaped = quote(region, safe="")
        self._preflight_resource(f"/regions/{escaped}", "requested region is unavailable")

    def preflight_instance_type(self, instance_type: str) -> None:
        escaped = quote(instance_type, safe="")
        self._preflight_resource(f"/linode/types/{escaped}", "requested Linode type is unavailable")

    def preflight_image(self, image_id: str) -> None:
        escaped = quote(image_id, safe="")
        self._preflight_resource(f"/images/{escaped}", "requested image is unavailable")

    def create_instance(
        self,
        *,
        region: str,
        source_image: str,
        instance_type: str,
        label: str,
        tags: list[str],
        root_password: str,
    ) -> dict[str, Any]:
        payload = {
            "booted": True,
            "image": source_image,
            "label": label,
            "region": region,
            "root_pass": root_password,
            "tags": tags,
            "type": instance_type,
        }
        response = self._request("POST", "/linode/instances", payload)
        return self._instance_resource(response)

    def wait_instance_ready(self, linode_id: int) -> dict[str, Any]:
        return self._wait_for_instance_status(linode_id, {"running"})

    def list_disks(self, linode_id: int) -> list[dict[str, Any]]:
        response = self._request("GET", f"/linode/instances/{linode_id}/disks", retry=True, operation="list_disks")
        disks = response.get("data", []) if isinstance(response, dict) else []
        return [disk for disk in disks if isinstance(disk, dict)]

    def shutdown_instance(self, linode_id: int) -> dict[str, Any]:
        self._request("POST", f"/linode/instances/{linode_id}/shutdown", {})
        return {"linode_id": linode_id, "action": "shutdown"}

    def wait_instance_offline(self, linode_id: int) -> dict[str, Any]:
        return self._wait_for_instance_status(linode_id, {"offline"})

    def capture_image(
        self,
        *,
        disk_id: int,
        label: str,
        tags: list[str],
        description: str,
        cloud_init: bool,
    ) -> dict[str, Any]:
        payload = {
            "cloud_init": cloud_init,
            "description": description,
            "disk_id": disk_id,
            "label": label,
            "tags": tags,
        }
        response = self._request("POST", "/images", payload)
        return self._image_resource(response)

    def wait_image_available(self, image_id: str) -> dict[str, Any]:
        escaped = quote(image_id, safe="")

        def current() -> dict[str, Any]:
            response = self._request("GET", f"/images/{escaped}", retry=True, operation="poll_image")
            return self._image_resource(response)

        return self._wait_until(current, lambda resource: resource.get("status") == "available")

    def list_managed_linodes(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urlencode({"page": page, "page_size": 100})
            response = self._request("GET", f"/linode/instances?{query}", retry=True, operation="list_managed_linodes")
            data = response.get("data", []) if isinstance(response, dict) else []
            for item in data:
                if not isinstance(item, dict):
                    continue
                resource = self._instance_resource(item)
                tags = tags_to_dict(resource.get("tags", []))
                if tags.get("project") == PROJECT:
                    resources.append(resource)

            pages = response.get("pages", page) if isinstance(response, dict) else page
            if not isinstance(pages, int) or page >= pages:
                return resources
            page += 1

    def delete_instance(self, linode_id: int) -> dict[str, Any]:
        self._request("DELETE", f"/linode/instances/{linode_id}", retry=True, operation="delete_instance")
        return {"linode_id": linode_id, "deleted": True}

    def _preflight_resource(self, path: str, unavailable_message: str) -> None:
        try:
            self._request("GET", path, retry=True, operation="preflight_resource")
        except LinodeTokenError:
            raise
        except LinodeApiError as exc:
            raise LinodePreflightError(unavailable_message) from exc

    def _wait_for_instance_status(self, linode_id: int, statuses: set[str]) -> dict[str, Any]:
        def current() -> dict[str, Any]:
            response = self._request("GET", f"/linode/instances/{linode_id}", retry=True, operation="poll_instance")
            return self._instance_resource(response)

        return self._wait_until(current, lambda resource: str(resource.get("status")) in statuses)

    def _wait_until(
        self,
        current: Any,
        done: Any,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.max_wait_seconds
        resource = current()
        while not done(resource):
            if time.monotonic() >= deadline:
                raise LinodeApiError("timed out waiting for Linode resource readiness")
            time.sleep(self.poll_interval_seconds)
            resource = current()
        return resource

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_empty: bool = False,
        retry: bool = False,
        operation: str | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        attempts = self._retry_attempt_limit(retry)
        for attempt in range(1, attempts + 1):
            request = Request(
                f"{self.api_base_url}{path}",
                data=body,
                method=method,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )

            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    text = response.read().decode("utf-8")
                break
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise LinodeTokenError("LINODE_TOKEN was rejected by the Linode API") from exc
                if self._should_retry_status(exc.code, attempt=attempt, attempts=attempts):
                    delay, delay_source = self._retry_delay(exc, attempt)
                    self._record_retry_event(
                        method=method,
                        operation=operation,
                        attempt=attempt,
                        attempts=attempts,
                        reason=f"status_{exc.code}",
                        retry_delay_seconds=delay,
                        retry_delay_source=delay_source,
                    )
                    self._sleep_before_retry(delay)
                    continue
                raise LinodeApiError(self._failure_message(f"Linode API request failed with status {exc.code}", attempt)) from exc
            except OSError as exc:
                if attempt < attempts:
                    delay, delay_source = self._deterministic_retry_delay(attempt)
                    self._record_retry_event(
                        method=method,
                        operation=operation,
                        attempt=attempt,
                        attempts=attempts,
                        reason=exc.__class__.__name__,
                        retry_delay_seconds=delay,
                        retry_delay_source=delay_source,
                    )
                    self._sleep_before_retry(delay)
                    continue
                raise LinodeApiError(self._failure_message("Linode API request failed", attempt)) from exc

        if not text:
            return {} if allow_empty else {}
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise LinodeApiError("Linode API returned an unexpected response")

    def consume_retry_events(self) -> list[dict[str, Any]]:
        events = [dict(event) for event in self.retry_events]
        self.retry_events.clear()
        return events

    def _retry_attempt_limit(self, retry: bool) -> int:
        if not retry:
            return 1
        return max(1, self.max_retry_attempts)

    @staticmethod
    def _should_retry_status(status: int, *, attempt: int, attempts: int) -> bool:
        return status in RETRYABLE_STATUS_CODES and attempt < attempts

    def _retry_delay(self, exc: HTTPError, attempt: int) -> tuple[float | None, str | None]:
        if exc.code == 429:
            retry_after_delay = self._retry_after_delay(exc)
            if retry_after_delay is not None:
                return retry_after_delay, "retry_after"

            rate_limit_reset_delay = self._rate_limit_reset_delay(exc)
            if rate_limit_reset_delay is not None:
                return rate_limit_reset_delay, "x_ratelimit_reset"

        return self._deterministic_retry_delay(attempt)

    def _deterministic_retry_delay(self, attempt: int) -> tuple[float | None, str | None]:
        if not self.retry_backoff_seconds:
            return None, None
        delay = self.retry_backoff_seconds[min(attempt - 1, len(self.retry_backoff_seconds) - 1)]
        return delay, "deterministic_backoff"

    @classmethod
    def _retry_after_delay(cls, exc: HTTPError) -> float | None:
        value = cls._response_header(exc, "Retry-After")
        if value is None:
            return None

        text = str(value).strip()
        delay = cls._parse_non_negative_seconds(text)
        if delay is not None:
            return delay

        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        return cls._delay_until(retry_at.timestamp())

    @classmethod
    def _rate_limit_reset_delay(cls, exc: HTTPError) -> float | None:
        value = cls._response_header(exc, "X-RateLimit-Reset")
        if value is None:
            return None

        try:
            reset_at = float(str(value).strip())
        except ValueError:
            return None
        return cls._delay_until(reset_at)

    @staticmethod
    def _delay_until(timestamp: float) -> float | None:
        if not math.isfinite(timestamp):
            return None
        return max(0.0, timestamp - time.time())

    @staticmethod
    def _parse_non_negative_seconds(value: str) -> float | None:
        try:
            delay = float(value)
        except ValueError:
            return None
        if not math.isfinite(delay) or delay < 0:
            return None
        return delay

    @staticmethod
    def _response_header(exc: HTTPError, name: str) -> object | None:
        headers = exc.headers
        if not headers:
            return None
        get = getattr(headers, "get", None)
        if callable(get):
            value = get(name)
            if value is not None:
                return value
        items = getattr(headers, "items", None)
        if callable(items):
            for key, value in items():
                if str(key).lower() == name.lower():
                    return value
        return None

    @staticmethod
    def _sleep_before_retry(delay: float | None) -> None:
        if delay is not None and delay > 0:
            time.sleep(delay)

    def _record_retry_event(
        self,
        *,
        method: str,
        operation: str | None,
        attempt: int,
        attempts: int,
        reason: str,
        retry_delay_seconds: float | None = None,
        retry_delay_source: str | None = None,
    ) -> None:
        event = {
            "operation": operation or "linode_api_request",
            "method": method,
            "attempt": attempt,
            "next_attempt": attempt + 1,
            "max_attempts": attempts,
            "reason": reason,
        }
        if retry_delay_seconds is not None:
            event["retry_delay_seconds"] = retry_delay_seconds
        if retry_delay_source is not None:
            event["retry_delay_source"] = retry_delay_source
        self.retry_events.append(event)

    @staticmethod
    def _failure_message(message: str, attempt: int) -> str:
        if attempt <= 1:
            return message
        return f"{message} after {attempt} attempts"

    @staticmethod
    def _instance_resource(response: dict[str, Any]) -> dict[str, Any]:
        return {
            "linode_id": response.get("id"),
            "label": response.get("label"),
            "region": response.get("region"),
            "status": response.get("status"),
            "tags": response.get("tags", []),
        }

    @staticmethod
    def _image_resource(response: dict[str, Any]) -> dict[str, Any]:
        return {
            "image_id": response.get("id"),
            "label": response.get("label"),
            "status": response.get("status"),
            "tags": response.get("tags", []),
        }
