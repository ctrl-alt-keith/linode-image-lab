from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linode_image_lab.cli import build_parser, main
from linode_image_lab.config import AUTHORIZED_KEYS_FILE_MAX_BYTES
from linode_image_lab.linode_api import LinodeTokenError
from linode_image_lab.user_data import USER_DATA_FILE_MAX_BYTES

PUBLIC_KEY_ONE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA one@example"
PUBLIC_KEY_TWO = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA two@example"
USER_DATA = "#cloud-config\nfinal_message: sensitive value\n"
USER_DATA_OVERRIDE = "#!/bin/bash\necho override\n"


class CliTests(unittest.TestCase):
    def test_version_prints_package_version_and_exits(self) -> None:
        output = StringIO()
        with patch("linode_image_lab.cli.version", return_value="9.8.7") as package_version:
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "9.8.7\n")
        package_version.assert_called_once_with("linode-image-lab")

    def test_version_ignores_following_subcommand(self) -> None:
        output = StringIO()
        with patch("linode_image_lab.cli.version", return_value="9.8.7"):
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main(["--version", "plan"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "9.8.7\n")

    def test_version_after_subcommand_prints_package_version_and_exits(self) -> None:
        for command in ("plan", "capture", "deploy", "capture-deploy", "cleanup"):
            with self.subTest(command=command):
                output = StringIO()
                with patch("linode_image_lab.cli.version", return_value="9.8.7"):
                    with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                        main([command, "--version"])

                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(output.getvalue(), "9.8.7\n")

    def test_help_output_includes_version_flag(self) -> None:
        with patch("linode_image_lab.cli.version", return_value="9.8.7"):
            help_output = build_parser().format_help()

        self.assertIn("--version", help_output)

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

    def test_manifest_file_matches_stdout_for_supported_commands(self) -> None:
        cases = [
            [
                "capture",
                "--region",
                "us-east",
                "--run-id",
                "run-test",
                "--ttl",
                "2030-01-01T00:00:00Z",
            ],
            [
                "deploy",
                "--region",
                "us-east",
                "--run-id",
                "run-test",
                "--ttl",
                "2030-01-01T00:00:00Z",
                "--image-id",
                "private/789",
                "--type",
                "g6-nanode-1",
            ],
            [
                "capture-deploy",
                "--region",
                "us-east",
                "--run-id",
                "run-test",
                "--ttl",
                "2030-01-01T00:00:00Z",
                "--source-image",
                "linode/debian12",
                "--type",
                "g6-nanode-1",
            ],
            [
                "cleanup",
                "--run-id",
                "run-test",
                "--ttl",
                "2030-01-01T00:00:00Z",
            ],
        ]

        for argv in cases:
            with self.subTest(command=argv[0]):
                directory = tempfile.TemporaryDirectory()
                self.addCleanup(directory.cleanup)
                manifest_path = Path(directory.name) / "manifest.json"
                output = StringIO()

                with redirect_stdout(output):
                    code = main([*argv, "--manifest-file", str(manifest_path)])

                self.assertEqual(code, 0)
                self.assertEqual(manifest_path.read_text(encoding="utf-8"), output.getvalue())
                self.assertEqual(json.loads(output.getvalue())["command"], argv[0])

    def test_manifest_file_dash_uses_stdout_only(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            code = main(["cleanup", "--manifest-file", "-"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["command"], "cleanup")

    def test_manifest_file_missing_parent_fails_before_mutation(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manifest_path = Path(directory.name) / "missing" / "manifest.json"
        output = StringIO()
        error = StringIO()

        with (
            patch("linode_image_lab.capture.LinodeClient.from_env", side_effect=AssertionError("token lookup")) as token,
            redirect_stdout(output),
            redirect_stderr(error),
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "capture",
                    "--execute",
                    "--region",
                    "us-east",
                    "--source-image",
                    "linode/debian12",
                    "--type",
                    "g6-nanode-1",
                    "--manifest-file",
                    str(manifest_path),
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("--manifest-file parent directory does not exist", error.getvalue())
        token.assert_not_called()

    def test_manifest_file_persists_partial_failure_manifest(self) -> None:
        class InvalidTokenClient:
            def preflight(self) -> None:
                raise LinodeTokenError("LINODE_TOKEN is invalid, expired, or rejected by the Linode API")

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manifest_path = Path(directory.name) / "failed-manifest.json"
        output = StringIO()
        error = StringIO()

        with (
            patch("linode_image_lab.capture.LinodeClient.from_env", return_value=InvalidTokenClient()),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            code = main(
                [
                    "capture",
                    "--execute",
                    "--region",
                    "us-east",
                    "--run-id",
                    "run-test",
                    "--ttl",
                    "2030-01-01T00:00:00Z",
                    "--source-image",
                    "linode/debian12",
                    "--type",
                    "g6-nanode-1",
                    "--manifest-file",
                    str(manifest_path),
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), output.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["steps"][0]["name"], "preflight_api_access")
        self.assertIn("capture --execute failed", error.getvalue())

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

    def test_deploy_config_fills_firewall_and_instance_type_alias_for_dry_run(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            firewall_id = 12345
            """
        )

        output = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("linode_image_lab.linode_api.LinodeClient.from_env", side_effect=AssertionError("token lookup")),
            redirect_stdout(output),
        ):
            code = main(["deploy", "--config", config_path])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["regions"], ["us-east"])
        self.assertEqual(payload["deploy_config"]["firewall"], {"enabled": True, "firewall_id": "[REDACTED]"})

    def test_deploy_config_loads_authorized_keys_and_file_for_dry_run_metadata(self) -> None:
        keys_path = self.write_file(f"{PUBLIC_KEY_TWO}\n")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            authorized_keys = ["{PUBLIC_KEY_ONE}"]
            authorized_keys_file = "{keys_path}"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["deploy", "--config", config_path])

        payload_text = output.getvalue()
        payload = json.loads(payload_text)
        self.assertEqual(code, 0)
        self.assertNotIn(PUBLIC_KEY_ONE, payload_text)
        self.assertNotIn(PUBLIC_KEY_TWO, payload_text)
        self.assertEqual(
            payload["deploy_config"]["authorized_keys"],
            {"authorized_key_count": 2, "enabled": True},
        )

    def test_cli_authorized_keys_merge_with_config_and_dedupe(self) -> None:
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            authorized_keys = ["{PUBLIC_KEY_ONE}", "{PUBLIC_KEY_TWO}"]
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "deploy",
                    "--config",
                    config_path,
                    "--authorized-key",
                    PUBLIC_KEY_ONE,
                    "--authorized-key",
                    PUBLIC_KEY_TWO,
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["deploy_config"]["authorized_keys"],
            {"authorized_key_count": 2, "enabled": True},
        )

    def test_capture_deploy_uses_deploy_scoped_authorized_key_config(self) -> None:
        config_path = self.write_config(
            f"""
            schema_version = 1

            [capture-deploy]
            region = "us-east"
            source_image = "linode/alpine3.23"
            instance_type = "g6-nanode-1"

            [deploy]
            authorized_keys = ["{PUBLIC_KEY_ONE}"]
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["capture-deploy", "--config", config_path])

        payload_text = output.getvalue()
        payload = json.loads(payload_text)
        self.assertEqual(code, 0)
        self.assertNotIn(PUBLIC_KEY_ONE, payload_text)
        self.assertEqual(
            payload["deploy_config"]["authorized_keys"],
            {"authorized_key_count": 1, "enabled": True},
        )

    def test_deploy_config_loads_user_data_file_for_dry_run_metadata(self) -> None:
        user_data_path = self.write_file(USER_DATA, name="cloud-init.yaml")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            user_data_file = "{user_data_path}"
            """
        )

        output = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("linode_image_lab.linode_api.LinodeClient.from_env", side_effect=AssertionError("token lookup")),
            redirect_stdout(output),
        ):
            code = main(["deploy", "--config", config_path])

        payload_text = output.getvalue()
        payload = json.loads(payload_text)
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertNotIn(USER_DATA, payload_text)
        self.assertNotIn("I2Nsb3VkLWNvbmZpZw", payload_text)
        self.assertEqual(
            payload["deploy_config"]["user_data"],
            {"enabled": True, "source": "file", "byte_count": len(USER_DATA.encode("utf-8"))},
        )

    def test_deploy_user_data_config_path_is_relative_to_config_file(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "cloud-init.yaml").write_text(USER_DATA, encoding="utf-8")
        config_path = root / "lab.toml"
        config_path.write_text(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            user_data_file = "cloud-init.yaml"
            """,
            encoding="utf-8",
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["deploy", "--config", str(config_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["deploy_config"]["user_data"],
            {"enabled": True, "source": "file", "byte_count": len(USER_DATA.encode("utf-8"))},
        )

    def test_capture_deploy_uses_deploy_scoped_user_data_config(self) -> None:
        user_data_path = self.write_file(USER_DATA, name="cloud-init.yaml")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [capture-deploy]
            region = "us-east"
            source_image = "linode/alpine3.23"
            instance_type = "g6-nanode-1"

            [deploy]
            user_data_file = "{user_data_path}"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["capture-deploy", "--config", config_path])

        payload_text = output.getvalue()
        payload = json.loads(payload_text)
        self.assertEqual(code, 0)
        self.assertNotIn(USER_DATA, payload_text)
        self.assertEqual(
            payload["deploy_config"]["user_data"],
            {"enabled": True, "source": "file", "byte_count": len(USER_DATA.encode("utf-8"))},
        )

    def test_cli_user_data_file_overrides_config_user_data_file(self) -> None:
        config_user_data_path = self.write_file(USER_DATA, name="config-cloud-init.yaml")
        cli_user_data_path = self.write_file(USER_DATA_OVERRIDE, name="cli-cloud-init.sh")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            user_data_file = "{config_user_data_path}"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["deploy", "--config", config_path, "--user-data-file", cli_user_data_path])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["deploy_config"]["user_data"],
            {"enabled": True, "source": "file", "byte_count": len(USER_DATA_OVERRIDE.encode("utf-8"))},
        )

    def test_user_data_file_missing_fails_without_leaking_path_contents(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            user_data_file = "missing-cloud-init.yaml"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--config", config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("[deploy].user_data_file file not found", error.getvalue())

    def test_user_data_file_unreadable_and_empty_inputs_fail_safely(self) -> None:
        directory_path = tempfile.TemporaryDirectory()
        self.addCleanup(directory_path.cleanup)
        empty_path = self.write_file("", name="empty-cloud-init.yaml")

        cases = [
            (directory_path.name, "file could not be read"),
            (empty_path, "must not be empty"),
        ]
        for path, message in cases:
            with self.subTest(message=message):
                error = StringIO()
                with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                    main(
                        [
                            "deploy",
                            "--region",
                            "us-east",
                            "--user-data-file",
                            path,
                        ]
                    )

                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, error.getvalue())

    def test_binary_user_data_file_fails_safely(self) -> None:
        binary_path = self.write_bytes(b"#cloud-config\n\x00secret\n", name="binary-cloud-init.yaml")

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--region", "us-east", "--user-data-file", binary_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must be text user data, not binary data", error.getvalue())
        self.assertNotIn("secret", error.getvalue())

    def test_user_data_file_too_large_fails_before_loading_contents(self) -> None:
        user_data_path = self.write_bytes(b"x" * (USER_DATA_FILE_MAX_BYTES + 1), name="large-cloud-init.yaml")

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--region", "us-east", "--user-data-file", user_data_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(f"file is too large; limit is {USER_DATA_FILE_MAX_BYTES} bytes", error.getvalue())

    def test_config_validate_shows_firewall_cli_override_precedence(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            firewall_id = 12345
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "deploy",
                    "--firewall-id",
                    "45678",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["effective_defaults"]["firewall_id"], "[REDACTED]")
        self.assertIn({"field": "firewall_id", "source": "cli --firewall-id"}, payload["sources"])

    def test_config_validate_reports_authorized_key_metadata_without_raw_keys(self) -> None:
        keys_path = self.write_file(f"{PUBLIC_KEY_TWO}\n")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            authorized_keys = ["{PUBLIC_KEY_ONE}"]
            authorized_keys_file = "{keys_path}"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "deploy",
                    "--authorized-key",
                    PUBLIC_KEY_ONE,
                ]
            )

        payload_text = output.getvalue()
        payload = json.loads(payload_text)
        self.assertEqual(code, 0)
        self.assertNotIn(PUBLIC_KEY_ONE, payload_text)
        self.assertNotIn(PUBLIC_KEY_TWO, payload_text)
        self.assertEqual(
            payload["effective_defaults"]["authorized_keys"],
            {"authorized_key_count": 2, "enabled": True},
        )
        self.assertIn({"field": "authorized_keys", "source": "[deploy].authorized_keys"}, payload["sources"])
        self.assertIn({"field": "authorized_keys", "source": "[deploy].authorized_keys_file"}, payload["sources"])
        self.assertIn({"field": "authorized_keys", "source": "cli --authorized-key"}, payload["sources"])

    def test_config_validate_reports_user_data_metadata_without_raw_contents(self) -> None:
        user_data_path = self.write_file(USER_DATA, name="cloud-init.yaml")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            user_data_file = "{user_data_path}"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["config", "validate", "--config", config_path, "--command", "deploy"])

        payload_text = output.getvalue()
        payload = json.loads(payload_text)
        self.assertEqual(code, 0)
        self.assertNotIn(USER_DATA, payload_text)
        self.assertEqual(
            payload["effective_defaults"]["user_data"],
            {"enabled": True, "source": "file", "byte_count": len(USER_DATA.encode("utf-8"))},
        )
        self.assertIn({"field": "user_data", "source": "[deploy].user_data_file"}, payload["sources"])

    def test_config_validate_reports_user_data_cli_override_precedence(self) -> None:
        config_user_data_path = self.write_file(USER_DATA, name="config-cloud-init.yaml")
        cli_user_data_path = self.write_file(USER_DATA_OVERRIDE, name="cli-cloud-init.sh")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            instance_type = "g6-nanode-1"
            user_data_file = "{config_user_data_path}"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "deploy",
                    "--user-data-file",
                    cli_user_data_path,
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["effective_defaults"]["user_data"],
            {"enabled": True, "source": "file", "byte_count": len(USER_DATA_OVERRIDE.encode("utf-8"))},
        )
        self.assertIn({"field": "user_data", "source": "cli --user-data-file"}, payload["sources"])

    def test_config_validate_shows_effective_defaults_and_precedence(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            ttl = "2030-01-01T00:00:00Z"

            [capture]
            regions = ["us-west"]
            ttl = "2031-01-01T00:00:00Z"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            image_project_tag = "customer-image-lab"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "capture",
                    "--region",
                    "eu-central",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["valid"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["safety"], {"auth_lookup": "not_attempted", "mutates": False})
        self.assertEqual(payload["precedence"], ["cli", "[capture]", "[defaults]"])
        self.assertEqual(
            payload["effective_defaults"],
            {
                "regions": ["eu-central"],
                "source_image": "linode/alpine3.23",
                "ttl": "2031-01-01T00:00:00Z",
                "type": "g6-nanode-1",
                "image_project_tag": "customer-image-lab",
            },
        )
        self.assertEqual(
            payload["sources"],
            [
                {"field": "regions", "source": "cli --region"},
                {"field": "ttl", "source": "[capture].ttl"},
                {"field": "source_image", "source": "[capture].source_image"},
                {"field": "type", "source": "[capture].type"},
                {"field": "image_project_tag", "source": "[capture].image_project_tag"},
            ],
        )

    def test_capture_config_image_project_tag_only_changes_artifact_tags(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture]
            region = "us-east"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            image_project_tag = "customer-image-lab"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["--config", config_path, "capture"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["artifact_tags"][0], "project=customer-image-lab")
        self.assertIn("run_id=", "\n".join(payload["artifact_tags"]))
        self.assertIn("mode=capture", payload["artifact_tags"])
        self.assertIn("component=capture", payload["artifact_tags"])
        self.assertIn("ttl=", "\n".join(payload["artifact_tags"]))
        self.assertIn("project=linode-image-lab", payload["lifecycle_tags"])
        self.assertIn("run_id=", "\n".join(payload["lifecycle_tags"]))

    def test_capture_deploy_config_accepts_image_project_tag(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture-deploy]
            region = "us-east"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            image_project_tag = "customer-image-lab"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["--config", config_path, "capture-deploy"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["artifact_tags"][0], "project=customer-image-lab")
        self.assertIn("run_id=", "\n".join(payload["artifact_tags"]))
        self.assertIn("mode=capture-deploy", payload["artifact_tags"])
        self.assertIn("component=capture", payload["artifact_tags"])
        self.assertIn("ttl=", "\n".join(payload["artifact_tags"]))
        self.assertIn("project=linode-image-lab", payload["component_tags"]["capture"])
        self.assertIn("project=linode-image-lab", payload["component_tags"]["deploy"])

    def test_config_rejects_image_project_tag_lifecycle_override(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture]
            region = "us-east"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            image_project_tag = "project=other"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["--config", config_path, "capture"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must not configure internal lifecycle tag key: project", error.getvalue())

    def test_config_validate_never_reads_linode_token(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            """
        )

        output = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("linode_image_lab.linode_api.LinodeClient.from_env", side_effect=AssertionError("token lookup")),
            redirect_stdout(output),
        ):
            code = main(["config", "validate", "--config", config_path, "--command", "deploy"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["effective_defaults"]["image_id"], "[REDACTED]")
        self.assertIn({"field": "image_id", "source": "[deploy].image_id"}, payload["sources"])

    def test_invalid_firewall_config_fails_clearly(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            firewall_id = "not-an-id"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--config", config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("config [deploy].firewall_id must be a positive integer", error.getvalue())

    def test_type_and_instance_type_config_aliases_are_mutually_exclusive(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            instance_type = "g6-standard-1"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--config", config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("cannot set both type and instance_type", error.getvalue())

    def test_config_validate_rejects_invalid_config_without_command_execution(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [defaults]
            region = "us-east"
            LINODE_TOKEN = "not-used"
            """
        )

        error = StringIO()
        with (
            patch("linode_image_lab.cli.capture_plan", side_effect=AssertionError("command execution")),
            redirect_stderr(error),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["config", "validate", "--config", config_path, "--command", "capture"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must not contain secrets", error.getvalue())

    def test_config_validate_rejects_unsupported_cli_overrides_by_command(self) -> None:
        config_path = self.write_config("schema_version = 1\n")
        user_data_path = self.write_file(USER_DATA, name="cloud-init.yaml")
        keys_path = self.write_file(f"{PUBLIC_KEY_ONE}\n")

        cases = [
            ("plan", ["--source-image", "linode/alpine3.23"], "--source-image"),
            ("plan", ["--image-id", "private/example-custom-image"], "--image-id"),
            ("plan", ["--type", "g6-nanode-1"], "--type"),
            ("plan", ["--firewall-id", "123"], "--firewall-id"),
            ("plan", ["--authorized-key", PUBLIC_KEY_ONE], "--authorized-key"),
            ("plan", ["--authorized-keys-file", keys_path], "--authorized-keys-file"),
            ("plan", ["--user-data-file", user_data_path], "--user-data-file"),
            ("capture", ["--image-id", "private/example-custom-image"], "--image-id"),
            ("capture", ["--firewall-id", "123"], "--firewall-id"),
            ("capture", ["--authorized-key", PUBLIC_KEY_ONE], "--authorized-key"),
            ("capture", ["--authorized-keys-file", keys_path], "--authorized-keys-file"),
            ("capture", ["--user-data-file", user_data_path], "--user-data-file"),
            ("deploy", ["--source-image", "linode/alpine3.23"], "--source-image"),
            ("capture-deploy", ["--image-id", "private/example-custom-image"], "--image-id"),
            ("cleanup", ["--region", "us-east"], "--region"),
            ("cleanup", ["--source-image", "linode/alpine3.23"], "--source-image"),
            ("cleanup", ["--image-id", "private/example-custom-image"], "--image-id"),
            ("cleanup", ["--type", "g6-nanode-1"], "--type"),
            ("cleanup", ["--firewall-id", "123"], "--firewall-id"),
            ("cleanup", ["--authorized-key", PUBLIC_KEY_ONE], "--authorized-key"),
            ("cleanup", ["--authorized-keys-file", keys_path], "--authorized-keys-file"),
            ("cleanup", ["--user-data-file", user_data_path], "--user-data-file"),
        ]
        for command, args, option in cases:
            with self.subTest(command=command, option=option):
                error = StringIO()
                with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                    main(["config", "validate", "--config", config_path, "--command", command, *args])

                self.assertEqual(raised.exception.code, 2)
                self.assertIn(f"{option} is not supported for {command} config defaults", error.getvalue())

    def test_config_validate_rejects_unsupported_cli_override_for_command(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [cleanup]
            ttl = "2030-01-01T00:00:00Z"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "cleanup",
                    "--region",
                    "us-east",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--region is not supported for cleanup config defaults", error.getvalue())

    def test_config_validate_rejects_unsupported_firewall_override_for_capture(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture]
            region = "us-east"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "capture",
                    "--firewall-id",
                    "123",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--firewall-id is not supported for capture config defaults", error.getvalue())

    def test_config_validate_rejects_unsupported_authorized_key_override_for_capture(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [capture]
            region = "us-east"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "capture",
                    "--authorized-key",
                    PUBLIC_KEY_ONE,
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--authorized-key is not supported for capture config defaults", error.getvalue())

    def test_config_validate_rejects_unsupported_user_data_override_for_capture(self) -> None:
        user_data_path = self.write_file(USER_DATA, name="cloud-init.yaml")
        config_path = self.write_config(
            """
            schema_version = 1

            [capture]
            region = "us-east"
            source_image = "linode/alpine3.23"
            type = "g6-nanode-1"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "config",
                    "validate",
                    "--config",
                    config_path,
                    "--command",
                    "capture",
                    "--user-data-file",
                    user_data_path,
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--user-data-file is not supported for capture config defaults", error.getvalue())

    def test_inline_user_data_config_is_rejected(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            user_data = "#cloud-config"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--config", config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must not contain secrets", error.getvalue())

    def test_invalid_authorized_key_config_fails_without_leaking_key(self) -> None:
        private_key_marker = "-----BEGIN OPENSSH PRIVATE KEY-----"
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            authorized_keys = ["{private_key_marker}"]
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--config", config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must be a public SSH key", error.getvalue())
        self.assertNotIn(private_key_marker, error.getvalue())

    def test_invalid_authorized_keys_file_fails_without_leaking_key(self) -> None:
        keys_path = self.write_file("not-a-public-key\n")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            authorized_keys_file = "{keys_path}"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--config", config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must be a valid OpenSSH public key", error.getvalue())
        self.assertNotIn("not-a-public-key", error.getvalue())

    def test_authorized_keys_file_too_large_fails_before_parsing_contents(self) -> None:
        keys_path = self.write_bytes(b"x" * (AUTHORIZED_KEYS_FILE_MAX_BYTES + 1), name="authorized_keys")
        config_path = self.write_config(
            f"""
            schema_version = 1

            [deploy]
            region = "us-east"
            image_id = "private/example-custom-image"
            type = "g6-nanode-1"
            authorized_keys_file = "{keys_path}"
            """
        )

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["deploy", "--config", config_path])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(f"file is too large; limit is {AUTHORIZED_KEYS_FILE_MAX_BYTES} bytes", error.getvalue())

    def test_config_validate_preserves_ttl_without_semantic_parsing(self) -> None:
        config_path = self.write_config(
            """
            schema_version = 1

            [cleanup]
            ttl = "not-a-timestamp"
            """
        )

        output = StringIO()
        with redirect_stdout(output):
            code = main(["config", "validate", "--config", config_path, "--command", "cleanup"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["effective_defaults"]["ttl"], "not-a-timestamp")

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

    def test_multi_region_config_execute_requires_token(self) -> None:
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
        self.assertIn("LINODE_TOKEN", error.getvalue())

    def write_config(self, text: str) -> str:
        return self.write_file(text, name="lab.toml")

    def write_file(self, text: str, *, name: str = "authorized_keys") -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def write_bytes(self, data: bytes, *, name: str) -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / name
        path.write_bytes(data)
        return str(path)


if __name__ == "__main__":
    unittest.main()
