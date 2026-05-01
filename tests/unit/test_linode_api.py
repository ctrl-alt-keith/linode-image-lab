from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from linode_image_lab.linode_api import LinodeApiError, LinodeClient, LinodeTokenError

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
    def http_error(self, status: int, message: str) -> HTTPError:
        error = HTTPError(f"{API_BASE_URL}/profile", status, message, {}, None)
        self.addCleanup(error.close)
        return error

    def test_from_env_missing_token_raises_token_error(self) -> None:
        with self.assertRaisesRegex(LinodeTokenError, "LINODE_TOKEN"):
            LinodeClient.from_env({})

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
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)

        with patch(
            "linode_image_lab.linode_api.urlopen",
            side_effect=self.http_error(500, "server failed"),
        ):
            with self.assertRaises(LinodeApiError) as raised:
                client.preflight()

        self.assertNotIsInstance(raised.exception, LinodeTokenError)
        self.assertNotIn(TOKEN_VALUE, str(raised.exception))
        self.assertEqual(str(raised.exception), "Linode API request failed with status 500")

    def test_network_failure_maps_to_api_error_without_token_value(self) -> None:
        client = LinodeClient(token=TOKEN_VALUE, api_base_url=API_BASE_URL)

        with patch("linode_image_lab.linode_api.urlopen", side_effect=OSError("network unavailable")):
            with self.assertRaises(LinodeApiError) as raised:
                client.preflight()

        self.assertNotIn(TOKEN_VALUE, str(raised.exception))
        self.assertEqual(str(raised.exception), "Linode API request failed")


if __name__ == "__main__":
    unittest.main()
