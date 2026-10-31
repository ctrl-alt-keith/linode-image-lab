from __future__ import annotations

import hashlib
import unittest
from importlib import resources

from linode_image_lab.openai_acl_candidate import (
    ADDITIONAL_ADDRESSES,
    CANDIDATE_ADDRESS_COUNT,
    CANDIDATE_COUNT,
    CANDIDATE_SHA256,
    generate_candidate,
    load_openai_acl_candidate,
)


class OpenAIACLCandidateTests(unittest.TestCase):
    def test_reviewed_candidate_reproduces_exact_pinned_bytes(self) -> None:
        raw = resources.files("linode_image_lab").joinpath("data/chatgpt-agents-lke-900.txt").read_bytes()
        candidate = load_openai_acl_candidate()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CANDIDATE_SHA256)
        self.assertEqual(tuple(raw.decode("ascii").splitlines()), candidate)
        self.assertEqual(generate_candidate(), candidate)
        self.assertEqual((len(candidate), CANDIDATE_COUNT), (900, 900))
        self.assertEqual(CANDIDATE_ADDRESS_COUNT - ADDITIONAL_ADDRESSES, 94096)
