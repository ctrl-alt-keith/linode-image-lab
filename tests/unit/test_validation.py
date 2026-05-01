from __future__ import annotations

import subprocess
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

    def test_reports_execution_model_drift_terms_outside_boundary_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "README.md"
            phrase = "desired" + " state"
            path.write_text(f"Add {phrase} management later.\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(
            findings,
            ["README.md:1: out-of-scope infrastructure-management terminology detected"],
        )

    def test_allows_execution_model_drift_terms_inside_boundary_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "README.md"
            phrase = "state" + " file"
            path.write_text(
                "# Title\n\n"
                "## Execution Model Boundary\n\n"
                f"Do not add a {phrase}.\n\n"
                "## Commands\n\n"
                "capture remains explicit.\n",
                encoding="utf-8",
            )

            findings = scan_public_safety(root)

        self.assertEqual(findings, [])

    def test_reports_execution_model_drift_terms_after_boundary_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "README.md"
            phrase = "resource" + " graph"
            path.write_text(
                "# Title\n\n"
                "## Execution Model Boundary\n\n"
                "Boundary terms are documented here.\n\n"
                "## Commands\n\n"
                f"Build a {phrase} later.\n",
                encoding="utf-8",
            )

            findings = scan_public_safety(root)

        self.assertEqual(
            findings,
            ["README.md:9: out-of-scope infrastructure-management terminology detected"],
        )

    def test_allows_resource_state_status_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "docs" / "capture.md"
            path.parent.mkdir(parents=True)
            path.write_text("Validate provider resource state and running status.\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(findings, [])

    def test_skips_local_install_artifacts_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".venv" / "lib" / "dependency.py"
            path.parent.mkdir(parents=True)
            email_like = "person" + "@" + "example.com"
            path.write_text(f"maintainer = '{email_like}'\n", encoding="utf-8")

            findings = scan_public_safety(root)

        self.assertEqual(findings, [])

    def test_git_checkout_scans_tracked_files_not_ignored_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            email_like = "person" + "@" + "example.com"
            readme = root / "README.md"
            readme.write_text(f"contact {email_like}\n", encoding="utf-8")
            venv_file = root / ".venv" / "lib" / "dependency.py"
            venv_file.parent.mkdir(parents=True)
            private_url = "http://" + "127.0.0.1:8000"
            venv_file.write_text(f"url = '{private_url}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, stdout=subprocess.DEVNULL)

            findings = scan_public_safety(root)

        self.assertEqual(findings, ["README.md: email-like value detected"])


if __name__ == "__main__":
    unittest.main()
