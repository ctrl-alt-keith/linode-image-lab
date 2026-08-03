from __future__ import annotations

import unittest

from linode_image_lab.redaction import REDACTION
from linode_image_lab.validation_results import (
    combined_validation,
    finish_validation,
    mark_validation_check_failed,
    mark_validation_check_succeeded,
    record_validation_check,
    start_validation,
)


class ValidationResultsTests(unittest.TestCase):
    def test_failed_check_records_sanitized_failure_reason(self) -> None:
        validation = start_validation((("api_check", "provider_resource"),))

        with self.assertRaises(ValueError):
            record_validation_check(
                validation,
                "api_check",
                lambda: raise_value_error("token=abcdefgh123456"),
            )

        self.assertEqual(validation["status"], "failed")
        self.assertEqual(
            validation["checks"][0],
            {
                "name": "api_check",
                "status": "failed",
                "target": "provider_resource",
                "failure_reason": f"token={REDACTION}",
            },
        )

    def test_combined_validation_prefixes_symbolic_targets(self) -> None:
        capture_validation = {
            "status": "succeeded",
            "checks": [{"name": "source_disk_found", "status": "succeeded", "target": "capture_source"}],
        }
        deploy_validation = {
            "status": "failed",
            "checks": [{"name": "required_tags_match", "status": "failed", "target": "deploy_instance"}],
        }

        validation = combined_validation(capture_validation=capture_validation, deploy_validation=deploy_validation)

        self.assertEqual(validation["status"], "failed")
        self.assertEqual(validation["checks"][0]["target"], "capture.capture_source")
        self.assertEqual(validation["checks"][1]["target"], "deploy.deploy_instance")

    def test_unknown_check_name_fails_closed(self) -> None:
        validation = start_validation((("api_check", "provider_resource"),))

        with self.assertRaisesRegex(ValueError, "must exist exactly once: typo"):
            mark_validation_check_succeeded(validation, "typo")

        self.assertEqual(validation["checks"][0]["status"], "pending")

    def test_failing_unknown_check_does_not_mutate_validation(self) -> None:
        validation = start_validation(
            (("api_check", "provider_resource"), ("disk_check", "source_disk"))
        )
        expected_checks = [dict(check) for check in validation["checks"]]

        with self.assertRaisesRegex(ValueError, "must exist exactly once: typo"):
            mark_validation_check_failed(validation, "typo", "provider failure")

        self.assertEqual(validation["status"], "running")
        self.assertEqual(validation["checks"], expected_checks)

    def test_duplicate_check_names_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "names must be unique"):
            start_validation((("api_check", "first"), ("api_check", "second")))

    def test_incomplete_validation_cannot_finish_successfully(self) -> None:
        validation = start_validation((("api_check", "provider_resource"),))

        with self.assertRaisesRegex(ValueError, "did not succeed: api_check"):
            finish_validation(validation)

        self.assertEqual(validation["status"], "failed")


def raise_value_error(message: str) -> None:
    raise ValueError(message)


if __name__ == "__main__":
    unittest.main()
