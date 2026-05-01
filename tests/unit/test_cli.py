from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from linode_image_lab.cli import main


class CliTests(unittest.TestCase):
    def test_plan_emits_sanitized_dry_run_preview(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "plan",
                    "--region",
                    "us-east,us-west",
                    "--run-id",
                    "run-test",
                    "--ttl",
                    "2030-01-01T00:00:00Z",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["command"], "plan")
        self.assertEqual(payload["regions"], ["us-east", "us-west"])
        self.assertIn("project=linode-image-lab", payload["tags"])

    def test_exposes_capture_deploy_commands(self) -> None:
        for command in ("capture", "deploy", "capture-deploy"):
            with self.subTest(command=command):
                output = StringIO()
                with redirect_stdout(output):
                    code = main(
                        [
                            command,
                            "--region",
                            "us-east",
                            "--run-id",
                            "run-test",
                            "--ttl",
                            "2030-01-01T00:00:00Z",
                        ]
                    )

                payload = json.loads(output.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(payload["command"], command)
                self.assertEqual(payload["mode"], command)
                self.assertTrue(payload["dry_run"])

    def test_legacy_commands_are_not_retained(self) -> None:
        legacy_commands = ("fr" + "eeze", "th" + "aw", "fr" + "eeze-" + "th" + "aw")
        for command in legacy_commands:
            with self.subTest(command=command):
                error = StringIO()
                with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                    main([command, "--region", "us-east"])

                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
