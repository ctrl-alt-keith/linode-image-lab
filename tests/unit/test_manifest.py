from __future__ import annotations

import json
import unittest

from linode_image_lab.manifest import create_manifest, generate_tags, serialize_manifest


class ManifestTests(unittest.TestCase):
    def test_generates_required_tags(self) -> None:
        tags = generate_tags(
            run_id="run-test",
            mode="freeze-thaw",
            component="builder",
            ttl="2030-01-01T00:00:00Z",
        )

        self.assertEqual(
            tags,
            [
                "project=linode-image-lab",
                "run_id=run-test",
                "mode=freeze-thaw",
                "component=builder",
                "ttl=2030-01-01T00:00:00Z",
            ],
        )

    def test_creates_serializable_manifest(self) -> None:
        manifest = create_manifest(
            command="plan",
            mode="freeze",
            regions=["us-east"],
            run_id="run-test",
            ttl="2030-01-01T00:00:00Z",
        )

        serialized = serialize_manifest(manifest)
        parsed = json.loads(serialized)

        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["project"], "linode-image-lab")
        self.assertEqual(parsed["planned_actions"][0]["region"], "us-east")
        self.assertFalse(parsed["planned_actions"][0]["mutates"])


if __name__ == "__main__":
    unittest.main()
