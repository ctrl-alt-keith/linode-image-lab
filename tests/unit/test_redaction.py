from __future__ import annotations

import unittest

from linode_image_lab.redaction import REDACTION, redact, redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_mapping_keys(self) -> None:
        payload = {"token": "not-a-real-value", "root_pass": "generated-root-pass", "env": "LINODE_TOKEN"}

        self.assertEqual(redact(payload), {"token": REDACTION, "root_pass": REDACTION, "env": "LINODE_TOKEN"})

    def test_redacts_token_like_text(self) -> None:
        self.assertEqual(redact_text("Bearer abcdefgh123456"), f"Bearer {REDACTION}")
        self.assertEqual(redact_text("token=abcdefgh123456"), f"token={REDACTION}")

    def test_redacts_public_ssh_key_text(self) -> None:
        public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA user@example"

        self.assertEqual(redact_text(f"key={public_key}"), f"key={REDACTION}")

    def test_redacts_provider_identifiers_but_keeps_run_id(self) -> None:
        payload = {"linode_id": 123, "image_id": "private/123", "run_id": "run-test"}

        self.assertEqual(
            redact(payload),
            {"linode_id": REDACTION, "image_id": REDACTION, "run_id": "run-test"},
        )


if __name__ == "__main__":
    unittest.main()
