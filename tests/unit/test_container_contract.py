from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ContainerContractTests(unittest.TestCase):
    def test_container_entrypoint_resolves_current_source_cli(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run(
            [sys.executable, "-m", "linode_image_lab.cli", "firewall-sync", "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output-format {manifest,summary}", result.stdout)

    def test_consumer_image_uses_pinned_amd64_base_and_one_shot_cli(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?m)^FROM python:3\.12\.15-slim-bookworm@sha256:[0-9a-f]{64}$",
        )
        self.assertIn('USER 65532:65532', dockerfile)
        self.assertIn('ENTRYPOINT ["linode-image-lab"]', dockerfile)
        self.assertNotIn("--execute", dockerfile)
        self.assertNotIn("RUN pip", dockerfile)

    def test_build_context_excludes_private_or_local_material(self) -> None:
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(ignore[0], "**")
        self.assertEqual(
            set(ignore[1:]),
            {
                "!src/", "src/*", "!src/linode_image_lab/",
                "src/linode_image_lab/*", "!src/linode_image_lab/*.py",
                "!pyproject.toml", "!policy/", "policy/*", "!policy/region-policy.toml",
            },
        )


if __name__ == "__main__":
    unittest.main()
