from __future__ import annotations

import unittest
from pathlib import Path


class RegionPolicyDriftWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "region-policy-drift-review.yml"
        ).read_text(encoding="utf-8")

    def test_schedule_dispatch_permissions_and_runtime_contract(self) -> None:
        self.assertIn('cron: "20 17 * * 1"', self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("timeout-minutes: 20", self.workflow)
        self.assertIn('python-version: "3.12"', self.workflow)
        self.assertIn("python -m pip install --disable-pip-version-check -e .", self.workflow)

    def test_exact_checkout_and_read_only_repository_contract(self) -> None:
        self.assertIn("ref: ${{ github.sha }}", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn('test "$tested_sha" = "$GITHUB_SHA"', self.workflow)
        self.assertIn("git status --porcelain=v1 --untracked-files=all", self.workflow)
        self.assertIn("git status --porcelain=v1 --untracked-files=all --ignored", self.workflow)
        self.assertIn("src/linode_image_lab.egg-info/", self.workflow)
        self.assertIn("regenerated policy and diff are outside the repository workspace", self.workflow)
        self.assertNotIn("contents: write", self.workflow)
        self.assertNotIn("pull-requests: write", self.workflow)
        self.assertNotIn("LINODE_TOKEN", self.workflow)

    def test_generation_validation_and_drift_evidence_contract(self) -> None:
        self.assertIn("https://api.linode.com/v4/regions", self.workflow)
        self.assertIn("linode-image-lab region-policy generate", self.workflow)
        self.assertIn("linode-image-lab region-policy validate", self.workflow)
        self.assertIn("make check", self.workflow)
        self.assertIn("git diff --no-index --no-ext-diff", self.workflow)
        self.assertIn("sha256sum", self.workflow)
        self.assertIn("actions/upload-artifact@v7", self.workflow)
        self.assertIn("ARTIFACT_DIGEST: ${{ steps.upload.outputs.artifact-digest }}", self.workflow)
        self.assertIn("retention-days: 14", self.workflow)
        self.assertIn("sed -n '1,120p'", self.workflow)

    def test_all_result_classes_and_failing_drift_signal_are_explicit(self) -> None:
        for result in ("Clean", "Drift detected", "Failed", "Unable to verify"):
            self.assertIn(result, self.workflow)
        self.assertIn('if [[ "$result" != "Clean" ]]', self.workflow)
        self.assertIn("always() && !cancelled()", self.workflow)


if __name__ == "__main__":
    unittest.main()
