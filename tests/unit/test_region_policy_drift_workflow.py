from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "region-policy-drift-review.yml"
)


def _indented_blocks(lines: list[str], marker: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped != marker:
            continue
        indent = len(line) - len(stripped)
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indent:
                break
            end += 1
        blocks.append(lines[index:end])
    return blocks


def _step_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    for sequence in _indented_blocks(lines, "steps:"):
        candidates = [
            (index, len(line) - len(line.lstrip()))
            for index, line in enumerate(sequence[1:], start=1)
            if re.match(r"^\s*-\s+[A-Za-z0-9_-]+:", line)
        ]
        if not candidates:
            continue
        item_indent = min(indent for _, indent in candidates)
        starts = [index for index, indent in candidates if indent == item_indent]
        blocks.extend(
            sequence[start : starts[index + 1] if index + 1 < len(starts) else len(sequence)]
            for index, start in enumerate(starts)
        )
    return blocks


def _mapping(block: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    base_indent = len(block[0]) - len(block[0].lstrip())
    entry_indents = [
        len(line) - len(line.lstrip())
        for line in block[1:]
        if line.strip() and not line.lstrip().startswith("#") and ":" in line
    ]
    if not entry_indents:
        return values
    entry_indent = min(indent for indent in entry_indents if indent > base_indent)
    for line in block[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent != entry_indent or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        values[key] = value.strip()
    return values


def _step_id(block: list[str]) -> str | None:
    for line in block:
        match = re.match(r"^\s+id:\s*([A-Za-z0-9_-]+)\s*$", line)
        if match:
            return match.group(1)
    return None


class RegionPolicyDriftWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.lines = cls.workflow.splitlines()
        cls.steps = _step_blocks(cls.lines)

    def test_hosted_review_remains_read_only_and_fail_closed(self) -> None:
        permission_blocks = _indented_blocks(self.lines, "permissions:")
        self.assertTrue(permission_blocks, "workflow must declare repository permissions")
        permission_declarations = [
            line for line in self.lines if re.match(r"^\s*permissions\s*:", line)
        ]
        self.assertEqual(len(permission_declarations), len(permission_blocks))
        for block in permission_blocks:
            permissions = _mapping(block)
            self.assertEqual(permissions, {"contents": "read"})

        checkout = next(
            block
            for block in self.steps
            if any("uses: actions/checkout@" in line for line in block)
        )
        checkout_options = _mapping(_indented_blocks(checkout, "with:")[0])
        self.assertEqual(checkout_options.get("ref"), "${{ github.sha }}")
        self.assertEqual(checkout_options.get("persist-credentials"), "false")

        self.assertNotIn("LINODE_TOKEN", self.workflow)
        self.assertNotRegex(self.workflow, r"\bsecrets\s*(?:\.|\[)")

        sha_verifier = next(
            block
            for block in self.steps
            if "git rev-parse HEAD" in "\n".join(block) and "GITHUB_SHA" in "\n".join(block)
        )
        self.assertRegex("\n".join(sha_verifier), r"test\s+.*tested_sha.*GITHUB_SHA")

        repository_guard = next(
            block
            for block in self.steps
            if "git status --porcelain" in "\n".join(block)
            and re.search(r"\[\[\s+-n\s+\"\$status\"\s+\]\]", "\n".join(block))
        )
        self.assertIn("exit 1", "\n".join(repository_guard))
        repository_guard_id = _step_id(repository_guard)
        self.assertIsNotNone(repository_guard_id)

        classifier = next(
            block
            for block in self.steps
            if 'result="Drift detected"' in "\n".join(block)
        )
        self.assertIn('result="Failed"', "\n".join(classifier))
        self.assertIn(f"steps.{repository_guard_id}.outcome", "\n".join(classifier))

        conclusion = next(
            block
            for block in self.steps
            if "classification.txt" in "\n".join(block) and 'result" != "Clean' in "\n".join(block)
        )
        self.assertIn("exit 1", "\n".join(conclusion))


if __name__ == "__main__":
    unittest.main()
