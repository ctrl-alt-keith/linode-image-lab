from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from linode_image_lab.validation import scan_public_safety


class ValidationTests(unittest.TestCase):
    def test_reports_bidi_control_with_file_and_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sample.py"
            path.write_text("safe = True\nname = 'safe'" + chr(0x202E) + "\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(findings, ["sample.py:2: hidden Unicode bidi control detected"])


if __name__ == "__main__":
    unittest.main()
