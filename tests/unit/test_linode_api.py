from __future__ import annotations

import json
import unittest
from unittest.mock import call, patch
from urllib.error import HTTPError

from linode_image_lab.linode_api import LinodeApiError, LinodeClient, LinodePreflightError, LinodeTokenError

TOKEN_VALUE = "test-" + "token-value"
API_BASE_URL = "https://api.example/v4"


class FakeHTTPResponse:
    def __init__(self, body: dict[str, object] | bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.body, bytes):
            return self.body
        return json.dumps(self.body).encode("utf-8")


def request_payload(request: object) -> dict[str, object]:
    data = getattr(request, "data")
    if not isinstance(data, bytes):
        raise AssertionError("request did not include a JSON body")
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError("request body was not a JSON object")
    return parsed


class LinodeClientTests(unittest.TestCase):
    def http_error(self, status: int, message: str, headers: dict[str, str] | None = None) -> HTTPError:
        error = HTTPError(f"{API_BASE_URL}/profile", status, message, headers or {}, None)
        self.addCleanup(error.close)
        return error

    def test_from_env_missing_token_raises_token_error(self) -> None:
        with self.assertRaisesRegex(LinodeTokenError, "LINODE_TOKEN"):
            LinodeClient.from_env({})

    def test_from_env_missing_token_names_requested_option(self) -> None:
        with self.assertRaisesRegex(LinodeTokenError, "cleanup --discover"):
            LinodeClient.from_env({}, command="cleanup", option="--discover")

    def test_preflight_issues_profile_and_grants_requests(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL, timeout_seconds=7)
        responses = [FakeHTTPResponse({}), FakeHTTPResponse(b"")]
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            self.assertEqual(timeout, 7)
            return responses.pop(0)

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            client.preflight()

        self.assertEqual([request.get_method() for request in requests], ["GET", "GET"])
        self.assertEqual(
            [request.full_url for request in requests],
            [f"{API_BASE_URL}/profile", f"{API_BASE_URL}/profile/grants"],
        )
        self.assertEqual([request.get_header("Authorization") for request in requests], [f"Bearer {TOKEN_VALUE}"] * 2)
        self.assertEqual(responses, [])

    def test_provider_preflight_helpers_use_read_only_resource_paths(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            return FakeHTTPResponse({})

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            client.preflight_region("us-east")
            client.preflight_instance_type("g6-nanode-1")
            client.preflight_image("linode/debian12")

        self.assertEqual([request.get_method() for request in requests], ["GET", "GET", "GET"])
        self.assertEqual(
            [request.full_url for request in requests],
            [
                f"{API_BASE_URL}/regions/us-east",
                f"{API_BASE_URL}/linode/types/g6-nanode-1",
                f"{API_BASE_URL}/images/linode%2Fdebian12",
            ],
        )

    def test_provider_preflight_failure_uses_sanitized_message(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL, retry_backoff_seconds=())

        with patch(
            "linode_image_lab.linode_api.urlopen",
            side_effect=self.http_error(404, "missing private/789"),
        ):
            with self.assertRaises(LinodePreflightError) as raised:
                client.preflight_image("private/789")

        self.assertEqual(str(raised.exception), "requested image is unavailable")
        self.assertNotIn("private/789", str(raised.exception))
        self.assertNotIn(TOKEN_VALUE, str(raised.exception))

    def test_read_retries_transient_http_failures_with_deterministic_backoff(self) -> None:
        client = LinodeClient(
            token=TOKEN_VALUE,
            api_base_url=API_BASE_URL,
            max_retry_attempts=3,
            retry_backoff_seconds=(1.0, 2.0),
        )
        responses: list[FakeHTTPResponse | HTTPError] = [
            self.http_error(429, "rate limited"),
            self.http_error(500, "server failed"),
            FakeHTTPResponse({"data": [{"id": 456, "label": "root"}]}),
        ]
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            response = responses.pop(0)
            if isinstance(response, HTTPError):
                raise response
            return response

        with (
            patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen),
            patch("linode_image_lab.linode_api.time.sleep") as sleep,
        ):
            disks = client.list_disks(123)

        self.assertEqual([request.get_method() for request in requests], ["GET", "GET", "GET"])
        self.assertEqual([request.full_url for request in requests], [f"{API_BASE_URL}/linode/instances/123/disks"] * 3)
        self.assertEqual(disks, [{"id": 456, "label": "root"}])
        self.assertEqual(sleep.call_args_list, [call(1.0), call(2.0)])
        self.assertEqual(
            client.consume_retry_events(),
            [
                {
                    "operation": "list_disks",
                    "method": "GET",
                    "attempt": 1,
                    "next_attempt": 2,
                    "max_attempts": 3,
                    "reason": "status_429",
                    "retry_delay_seconds": 1.0,
                    "retry_delay_source": "deterministic_backoff",
                },
                {
                    "operation": "list_disks",
                    "method": "GET",
                    "attempt": 2,
                    "next_attempt": 3,
                    "max_attempts": 3,
                    "reason": "status_500",
                    "retry_delay_seconds": 2.0,
                    "retry_delay_source": "deterministic_backoff",
                },
            ],
        )

    def test_rate_limit_retry_honors_retry_after_header(self) -> None:
        client = LinodeClient(
            token=TOKEN_VALUE,
            api_base_url=API_BASE_URL,
            max_retry_attempts=2,
            retry_backoff_seconds=(1.0,),
        )
        responses: list[FakeHTTPResponse | HTTPError] = [
            self.http_error(429, "rate limited", {"Retry-After": "7"}),
            FakeHTTPResponse({"data": [{"id": 456, "label": "root"}]}),
        ]

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            response = responses.pop(0)
            if isinstance(response, HTTPError):
                raise response
            return response

        with (
            patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen),
            patch("linode_image_lab.linode_api.time.sleep") as sleep,
        ):
            disks = client.list_disks(123)

        self.assertEqual(disks, [{"id": 456, "label": "root"}])
        self.assertEqual(sleep.call_args_list, [call(7.0)])
        self.assertEqual(
            client.consume_retry_events(),
            [
                {
                    "operation": "list_disks",
                    "method": "GET",
                    "attempt": 1,
                    "next_attempt": 2,
                    "max_attempts": 2,
                    "reason": "status_429",
                    "retry_delay_seconds": 7.0,
                    "retry_delay_source": "retry_after",
                }
            ],
        )

    def test_rate_limit_retry_uses_reset_header_when_retry_after_is_invalid(self) -> None:
        client = LinodeClient(
            token=TOKEN_VALUE,
            api_base_url=API_BASE_URL,
            max_retry_attempts=2,
            retry_backoff_seconds=(1.0,),
        )
        responses: list[FakeHTTPResponse | HTTPError] = [
            self.http_error(
                429,
                "rate limited",
                {"Retry-After": "soon", "X-RateLimit-Reset": "115"},
            ),
            FakeHTTPResponse({"data": [{"id": 456, "label": "root"}]}),
        ]

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            response = responses.pop(0)
            if isinstance(response, HTTPError):
                raise response
            return response

        with (
            patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen),
            patch("linode_image_lab.linode_api.time.time", return_value=100.0),
            patch("linode_image_lab.linode_api.time.sleep") as sleep,
        ):
            disks = client.list_disks(123)

        self.assertEqual(disks, [{"id": 456, "label": "root"}])
        self.assertEqual(sleep.call_args_list, [call(15.0)])
        self.assertEqual(
            client.consume_retry_events(),
            [
                {
                    "operation": "list_disks",
                    "method": "GET",
                    "attempt": 1,
                    "next_attempt": 2,
                    "max_attempts": 2,
                    "reason": "status_429",
                    "retry_delay_seconds": 15.0,
                    "retry_delay_source": "x_ratelimit_reset",
                }
            ],
        )

    def test_delete_instance_retries_transient_network_failure(self) -> None:
        client = LinodeClient(
            token=TOKEN_VALUE,
            api_base_url=API_BASE_URL,
            max_retry_attempts=2,
            retry_backoff_seconds=(),
        )
        responses: list[FakeHTTPResponse | OSError] = [OSError("network unavailable"), FakeHTTPResponse({})]
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            response = responses.pop(0)
            if isinstance(response, OSError):
                raise response
            return response

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            resource = client.delete_instance(123)

        self.assertEqual([request.get_method() for request in requests], ["DELETE", "DELETE"])
        self.assertEqual([request.full_url for request in requests], [f"{API_BASE_URL}/linode/instances/123"] * 2)
        self.assertEqual(resource, {"linode_id": 123, "deleted": True})
        self.assertEqual(
            client.consume_retry_events(),
            [
                {
                    "operation": "delete_instance",
                    "method": "DELETE",
                    "attempt": 1,
                    "next_attempt": 2,
                    "max_attempts": 2,
                    "reason": "OSError",
                }
            ],
        )

    def test_create_instance_sends_expected_payload_and_maps_response(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)
        root_value = "generated-" + "root-pass"
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            return FakeHTTPResponse(
                {
                    "id": 123,
                    "label": "lil-run-source",
                    "region": "us-east",
                    "status": "provisioning",
                    "tags": ["project=linode-image-lab"],
                }
            )

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            resource = client.create_instance(
                region="us-east",
                source_image="linode/debian12",
                instance_type="g6-nanode-1",
                label="lil-run-source",
                tags=["project=linode-image-lab"],
                root_password=root_value,
            )

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, f"{API_BASE_URL}/linode/instances")
        self.assertEqual(
            request_payload(request),
            {
                "booted": True,
                "image": "linode/debian12",
                "label": "lil-run-source",
                "region": "us-east",
                "root_pass": root_value,
                "tags": ["project=linode-image-lab"],
                "type": "g6-nanode-1",
            },
        )
        self.assertEqual(
            resource,
            {
                "linode_id": 123,
                "label": "lil-run-source",
                "region": "us-east",
                "status": "provisioning",
                "tags": ["project=linode-image-lab"],
            },
        )

    def test_create_instance_does_not_retry_transient_failure(self) -> None:
        client = LinodeClient(
            token=TOKEN_VALUE,
            api_base_url=API_BASE_URL,
            max_retry_attempts=3,
            retry_backoff_seconds=(),
        )
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            raise self.http_error(500, "server failed")

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(LinodeApiError) as raised:
                client.create_instance(
                    region="us-east",
                    source_image="linode/debian12",
                    instance_type="g6-nanode-1",
                    label="lil-run-source",
                    tags=["project=linode-image-lab"],
                    root_password="generated-" + "root-pass",
                )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(str(raised.exception), "Linode API request failed with status 500")
        self.assertEqual(client.consume_retry_events(), [])

    def test_capture_image_sends_expected_payload_and_maps_response(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            return FakeHTTPResponse(
                {
                    "id": "private/789",
                    "label": "lil-run-image",
                    "status": "creating",
                    "tags": ["project=linode-image-lab"],
                }
            )

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            resource = client.capture_image(
                disk_id=456,
                label="lil-run-image",
                tags=["project=linode-image-lab"],
                description="linode-image-lab capture run run-test",
                cloud_init=True,
            )

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, f"{API_BASE_URL}/images")
        self.assertEqual(
            request_payload(request),
            {
                "cloud_init": True,
                "description": "linode-image-lab capture run run-test",
                "disk_id": 456,
                "label": "lil-run-image",
                "tags": ["project=linode-image-lab"],
            },
        )
        self.assertEqual(
            resource,
            {
                "image_id": "private/789",
                "label": "lil-run-image",
                "status": "creating",
                "tags": ["project=linode-image-lab"],
            },
        )

    def test_shutdown_instance_uses_expected_path(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            return FakeHTTPResponse({})

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            resource = client.shutdown_instance(123)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(requests[0].full_url, f"{API_BASE_URL}/linode/instances/123/shutdown")
        self.assertEqual(request_payload(requests[0]), {})
        self.assertEqual(resource, {"linode_id": 123, "action": "shutdown"})

    def test_delete_instance_uses_expected_path(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            return FakeHTTPResponse({})

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            resource = client.delete_instance(123)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "DELETE")
        self.assertEqual(requests[0].full_url, f"{API_BASE_URL}/linode/instances/123")
        self.assertIsNone(getattr(requests[0], "data"))
        self.assertEqual(resource, {"linode_id": 123, "deleted": True})

    def test_list_managed_linodes_maps_project_tagged_instances(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)
        responses = [
            FakeHTTPResponse(
                {
                    "page": 1,
                    "pages": 2,
                    "data": [
                        {
                            "id": 123,
                            "label": "lil-run-source",
                            "region": "us-east",
                            "status": "running",
                            "tags": ["project=linode-image-lab"],
                        },
                        {
                            "id": 999,
                            "label": "unmanaged",
                            "region": "us-east",
                            "status": "running",
                            "tags": ["project=other"],
                        },
                    ],
                }
            ),
            FakeHTTPResponse(
                {
                    "page": 2,
                    "pages": 2,
                    "data": [
                        {
                            "id": 456,
                            "label": "lil-run-deploy",
                            "region": "us-west",
                            "status": "offline",
                            "tags": ["project=linode-image-lab"],
                        }
                    ],
                }
            ),
        ]
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            return responses.pop(0)

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            resources = client.list_managed_linodes()

        self.assertEqual([request.get_method() for request in requests], ["GET", "GET"])
        self.assertEqual(
            [request.full_url for request in requests],
            [
                f"{API_BASE_URL}/linode/instances?page=1&page_size=100",
                f"{API_BASE_URL}/linode/instances?page=2&page_size=100",
            ],
        )
        self.assertEqual([resource["linode_id"] for resource in resources], [123, 456])

    def test_wait_instance_ready_polls_until_running(self) -> None:
        client = LinodeClient(
            token=TOKEN_VALUE,
            api_base_url=API_BASE_URL,
            poll_interval_seconds=0,
            max_wait_seconds=1,
        )
        responses = [
            FakeHTTPResponse(
                {
                    "id": 123,
                    "label": "lil-run-deploy",
                    "region": "us-east",
                    "status": "provisioning",
                    "tags": ["project=linode-image-lab"],
                }
            ),
            FakeHTTPResponse(
                {
                    "id": 123,
                    "label": "lil-run-deploy",
                    "region": "us-east",
                    "status": "running",
                    "tags": ["project=linode-image-lab"],
                }
            ),
        ]
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> FakeHTTPResponse:
            requests.append(request)
            return responses.pop(0)

        with patch("linode_image_lab.linode_api.urlopen", side_effect=fake_urlopen):
            resource = client.wait_instance_ready(123)

        self.assertEqual([request.get_method() for request in requests], ["GET", "GET"])
        self.assertEqual(
            [request.full_url for request in requests],
            [f"{API_BASE_URL}/linode/instances/123", f"{API_BASE_URL}/linode/instances/123"],
        )
        self.assertEqual(resource["status"], "running")
        self.assertEqual(responses, [])

    def test_auth_failures_map_to_token_error(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)

        for status in (401, 403):
            with self.subTest(status=status):
                error = self.http_error(status, "auth failed")
                with patch(
                    "linode_image_lab.linode_api.urlopen",
                    side_effect=error,
                ):
                    with self.assertRaises(LinodeTokenError):
                        client.preflight()

    def test_non_auth_failure_maps_to_api_error_without_token_value(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL, retry_backoff_seconds=())

        with patch(
            "linode_image_lab.linode_api.urlopen",
            side_effect=self.http_error(500, "server failed"),
        ):
            with self.assertRaises(LinodeApiError) as raised:
                client.preflight()

        self.assertNotIsInstance(raised.exception, LinodeTokenError)
        self.assertNotIn(TOKEN_VALUE, str(raised.exception))
        self.assertEqual(str(raised.exception), "Linode API request failed with status 500 after 3 attempts")

    def test_network_failure_maps_to_api_error_without_token_value(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL, retry_backoff_seconds=())

        with patch("linode_image_lab.linode_api.urlopen", side_effect=OSError("network unavailable")):
            with self.assertRaises(LinodeApiError) as raised:
                client.preflight()

        self.assertNotIn(TOKEN_VALUE, str(raised.exception))
        self.assertEqual(str(raised.exception), "Linode API request failed after 3 attempts")


if __name__ == "__main__":
    unittest.main()
