from __future__ import annotations

import unittest

from linode_image_lab.redaction import REDACTION
from linode_image_lab.validation_results import (
    combined_validation,
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


def raise_value_error(message: str) -> None:
    raise ValueError(message)


if __name__ == "__main__":
    unittest.main()
