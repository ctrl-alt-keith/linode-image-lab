from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

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

    def test_capture_execute_requires_options_before_mutation(self) -> None:
        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["capture", "--region", "us-east", "--execute", "--source-image", "linode/debian12"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--type", error.getvalue())

    def test_deploy_execute_requires_options_before_mutation(self) -> None:
        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--region", "us-east", "--execute", "--image-id", "private/789"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--type for the temporary deploy Linode", error.getvalue())

    def test_deploy_execute_requires_image_id_before_mutation(self) -> None:
        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--region", "us-east", "--execute", "--type", "g6-nanode-1"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--image-id for the custom image to deploy", error.getvalue())

    def test_capture_deploy_execute_requires_options_before_mutation(self) -> None:
        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["capture-deploy", "--region", "us-east", "--execute", "--source-image", "linode/debian12"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--type", error.getvalue())

    def test_missing_region_without_config_still_fails(self) -> None:
        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["capture"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("at least one non-empty --region is required", error.getvalue())

    def test_global_config_before_subcommand_fills_capture_deploy_defaults(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            ttl = "2030-01-01T00:00:00Z"

            [capture-deploy]
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["--config", config_path, "capture-deploy"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["regions"], ["us-east"])
        self.assertEqual(payload["ttl"], "2030-01-01T00:00:00Z")

    def test_command_local_config_after_subcommand_fills_capture_deploy_defaults(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            ttl = "2030-01-01T00:00:00Z"

            [capture-deploy]
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["capture-deploy", "--config", config_path])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["regions"], ["us-east"])
        self.assertEqual(payload["ttl"], "2030-01-01T00:00:00Z")

    def test_duplicate_global_and_command_local_config_fails_clearly(self) -> None:
        global_config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            """
        )
        command_config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-west"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["--config", global_config_path, "capture-deploy", "--config", command_config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("provide --config either before or after the command, not both", error.getvalue())

    def test_cli_values_override_config_defaults(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            ttl = "2030-01-01T00:00:00Z"

            [deploy]
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--config",
                    config_path,
                    "deploy",
                    "--region",
                    "us-west",
                    "--ttl",
                    "2031-01-01T00:00:00Z",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["regions"], ["us-west"])
        self.assertEqual(payload["ttl"], "2031-01-01T00:00:00Z")

    def test_unknown_config_key_fails_clearly(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            unexpected = "value"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["--config", config_path, "capture"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unknown config key in [defaults]: unexpected", error.getvalue())

    def test_secret_like_config_key_fails_clearly(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            LINODE_TOKEN = "not-used"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["--config", config_path, "capture"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must not contain secrets", error.getvalue())

    def test_execute_in_config_is_rejected(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture]
            region = "us-east"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            execute = "true"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["--config", config_path, "capture"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("key is not supported: execute", error.getvalue())

    def test_discover_in_config_is_rejected(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [cleanup]
            discover = "true"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["--config", config_path, "cleanup"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("key is not supported: discover", error.getvalue())

    def test_config_never_satisfies_linode_token(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            """
        )

        error = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                main(["--config", config_path, "deploy", "--execute"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("LINODE_TOKEN is required for deploy --execute", error.getvalue())

    def test_cleanup_execute_requires_linode_token(self) -> None:
        error = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                main(["cleanup", "--execute"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("LINODE_TOKEN is required for cleanup --execute", error.getvalue())

    def test_cleanup_discover_requires_linode_token(self) -> None:
        error = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                main(["cleanup", "--discover"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("LINODE_TOKEN is required for cleanup --discover", error.getvalue())

    def test_cleanup_plain_does_not_require_linode_token(self) -> None:
        output = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("linode_image_lab.cleanup.LinodeClient.from_env", side_effect=AssertionError("token lookup")),
            redirect_stdout(output),
        ):
            code = main(["cleanup"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["execution_mode"], "dry-run")
        self.assertEqual(payload["discovery"]["status"], "not_requested")

    def test_multi_region_config_is_accepted_for_dry_run(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture]
            regions = ["us-east", "us-west"]
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["--config", config_path, "capture"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["regions"], ["us-east", "us-west"])

    def test_multi_region_config_execute_fails_before_token_lookup(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture-deploy]
            regions = ["us-east", "us-west"]
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            """
        )

        error = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                main(["--config", config_path, "capture-deploy", "--execute"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("exactly one non-empty --region", error.getvalue())
        self.assertNotIn("LINODE_TOKEN", error.getvalue())

    def write_config(self, text: str) -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "lab.toml"
        path.write_text(text, encoding="utf-8")
        return str(path)


if __name__ == "__main__":
    unittest.main()
