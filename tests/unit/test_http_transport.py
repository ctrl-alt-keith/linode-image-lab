"""Offline redirect checks for both credentialed HTTP clients."""

from __future__ import annotations

from http.client import HTTPMessage
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, Request, build_opener

from linode_image_lab import linode_api, trusted_registry
from linode_image_lab import http_transport

TOKEN_VALUE = "test-" + "token-value"


class RedirectResponse:
    def __init__(self, code: int, destination: str) -> None:
        self.code = code
        self.msg = "redirect"
        self.headers = HTTPMessage()
        self.headers["Location"] = destination

    def info(self) -> HTTPMessage:
        return self.headers

    def __enter__(self) -> "RedirectResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass


class RedirectTests(unittest.TestCase):
    def test_both_credentialed_clients_use_redirect_refusal(self) -> None:
        self.assertIs(linode_api.urlopen, http_transport.open_without_redirect)
        self.assertIs(trusted_registry.urlopen, http_transport.open_without_redirect)

    def test_redirect_never_opens_destination_or_forwards_header(self) -> None:
        original = "https://api.example.test/v4/networking/firewalls/123/rules"
        credential = f"Bearer {TOKEN_VALUE}"
        for code, destination in (
            (301, "https://other.example.test/rules"),
            (302, "http://other.example.test/rules"),
            (303, "https://api.example.test/elsewhere"),
            (307, "https://other.example.test/rules"),
            (308, "https://other.example.test/rules"),
        ):
            with self.subTest(code=code, destination=destination):
                opened: list[Request] = []

                class FakeHTTPSHandler(HTTPSHandler):
                    def https_open(self, request: Request) -> RedirectResponse:
                        opened.append(request)
                        return RedirectResponse(code, destination)

                def fake_build_opener(handler: http_transport.RejectRedirects):
                    self.assertIsInstance(handler, http_transport.RejectRedirects)
                    return build_opener(handler, FakeHTTPSHandler())

                request = Request(original, method="GET", headers={"Authorization": credential})
                with patch.object(http_transport, "build_opener", side_effect=fake_build_opener):
                    with self.assertRaises(HTTPError) as raised:
                        http_transport.open_without_redirect(request, timeout=3)

                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.msg, "redirect refused")
                self.assertEqual([item.full_url for item in opened], [original])
                self.assertEqual(opened[0].get_header("Authorization"), credential)
                self.assertNotIn(credential, str(raised.exception))
                raised.exception.close()

    def test_both_callers_fail_closed_on_redirect(self) -> None:
        for caller in ("registry", "firewall"):
            with self.subTest(caller=caller):
                opened: list[Request] = []

                class FakeHTTPSHandler(HTTPSHandler):
                    def https_open(self, request: Request) -> RedirectResponse:
                        opened.append(request)
                        return RedirectResponse(302, "https://other.example.test/collect")

                def fake_build_opener(handler: http_transport.RejectRedirects):
                    return build_opener(handler, FakeHTTPSHandler())

                with patch.object(http_transport, "build_opener", side_effect=fake_build_opener):
                    if caller == "registry":
                        with self.assertRaisesRegex(trusted_registry.RegistryFetchError,
                                                    "trusted registry fetch failed"):
                            trusted_registry.fetch_registry_from_object_storage(
                                endpoint_url="https://us-east-1.linodeobjects.com",
                                bucket="example-bucket", object_key="registry.json",
                                environ={"LINODE_OBJ_ACCESS_KEY": "synthetic-access",
                                         "LINODE_OBJ_SECRET_KEY": "synthetic-secret"},
                            )
                        self.assertIn("AWS4-HMAC-SHA256", opened[0].get_header("Authorization"))
                    else:
                        client = linode_api.LinodeClient(token=TOKEN_VALUE)
                        with self.assertRaisesRegex(linode_api.LinodeApiError,
                                                    "Linode API request failed with status 302") as raised:
                            client.get_firewall_rules(123)
                        self.assertNotIn(TOKEN_VALUE, str(raised.exception))
                        self.assertEqual(opened[0].get_header("Authorization"), f"Bearer {TOKEN_VALUE}")
                self.assertEqual(len(opened), 1)

    def test_stock_handler_would_forward_the_same_request(self) -> None:
        original = "https://api.example.test/v4/networking/firewalls/123/rules"
        destination = "https://other.example.test/collect"
        opened: list[Request] = []

        class FakeHTTPSHandler(HTTPSHandler):
            def https_open(self, request: Request) -> RedirectResponse:
                opened.append(request)
                return RedirectResponse(302 if len(opened) == 1 else 200, destination)

        request = Request(original, method="GET", headers={"Authorization": f"Bearer {TOKEN_VALUE}"})
        with build_opener(FakeHTTPSHandler()).open(request, timeout=3):
            pass

        self.assertEqual([item.full_url for item in opened], [original, destination])
        self.assertEqual(opened[1].get_header("Authorization"), f"Bearer {TOKEN_VALUE}")


if __name__ == "__main__":
    unittest.main()
