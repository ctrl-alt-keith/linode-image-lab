from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from linode_image_lab.validation import scan_public_safety


class ValidationTests(unittest.TestCase):
    def test_allows_sanitized_fixture_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "tests" / "fixtures" / "sanitized" / "mock.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(findings, [])

    def test_reports_unsanitized_fixture_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "tests" / "fixtures" / "mock.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(
            findings,
            ["tests/fixtures/mock.json: fixture files must live under tests/fixtures/sanitized/"],
        )

    def test_reports_bidi_control_with_file_and_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sample.py"
            path.write_text("safe = True\nname = 'safe'" + chr(0x202E) + "\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(findings, ["sample.py:2: hidden Unicode bidi control detected"])

    def test_reports_legacy_workflow_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "README.md"
            legacy_term = "fr" + "eeze"
            path.write_text(f"{legacy_term}\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(findings, ["README.md:1: legacy image workflow terminology detected"])

    def test_allows_current_workflow_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "README.md"
            path.write_text("capture deploy capture-deploy custom image\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
