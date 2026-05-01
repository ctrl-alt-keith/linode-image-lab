from __future__ import annotations

import unittest

from linode_image_lab.redaction import REDACTION, redact, redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_mapping_keys(self) -> None:
        payload = {"token": "not-a-real-value", "env": "LINODE_TOKEN"}

        self.assertEqual(redact(payload), {"token": REDACTION, "env": "LINODE_TOKEN"})

    def test_redacts_token_like_text(self) -> None:
        self.assertEqual(redact_text("Bearer abcdefgh123456"), f"Bearer {REDACTION}")
        self.assertEqual(redact_text("token=abcdefgh123456"), f"token={REDACTION}")


if __name__ == "__main__":
    unittest.main()
