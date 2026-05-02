"""Linode API boundary for explicit execution commands."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .manifest import PROJECT, tags_to_dict

TOKEN_ENV_NAME = "LINODE_TOKEN"
DEFAULT_API_BASE_URL = "https://api.linode.com/v4"


class LinodeApiError(ValueError):
    """Raised when the Linode API boundary cannot complete a requested action."""


class LinodeTokenError(LinodeApiError):
    """Raised when execute mode lacks a usable Linode token."""


class LinodeClientProtocol(Protocol):
    def preflight(self) -> None: ...

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

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, command: str = "capture") -> "LinodeClient":
        values = env if env is not None else os.environ
        token = values.get(TOKEN_ENV_NAME)
        if not token:
            raise LinodeTokenError(f"{TOKEN_ENV_NAME} is required for {command} --execute")
        return cls(token=token)

    def preflight(self) -> None:
        self._request("GET", "/profile")
        self._request("GET", "/profile/grants", allow_empty=True)

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
        response = self._request("GET", f"/linode/instances/{linode_id}/disks")
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
            response = self._request("GET", f"/images/{escaped}")
            return self._image_resource(response)

        return self._wait_until(current, lambda resource: resource.get("status") == "available")

    def list_managed_linodes(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urlencode({"page": page, "page_size": 100})
            response = self._request("GET", f"/linode/instances?{query}")
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
        self._request("DELETE", f"/linode/instances/{linode_id}")
        return {"linode_id": linode_id, "deleted": True}

    def _wait_for_instance_status(self, linode_id: int, statuses: set[str]) -> dict[str, Any]:
        def current() -> dict[str, Any]:
            response = self._request("GET", f"/linode/instances/{linode_id}")
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
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
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
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise LinodeTokenError("LINODE_TOKEN was rejected by the Linode API") from exc
            raise LinodeApiError(f"Linode API request failed with status {exc.code}") from exc
        except OSError as exc:
            raise LinodeApiError("Linode API request failed") from exc

        if not text:
            return {} if allow_empty else {}
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise LinodeApiError("Linode API returned an unexpected response")

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
