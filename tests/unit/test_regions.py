from __future__ import annotations

import unittest

from linode_image_lab.regions import parse_regions


class RegionParsingTests(unittest.TestCase):
    def test_parses_comma_separated_regions(self) -> None:
        self.assertEqual(parse_regions("us-east, us-west"), ["us-east", "us-west"])

    def test_parses_repeated_regions_and_deduplicates(self) -> None:
        self.assertEqual(parse_regions(["us-east", "us-west,us-east"]), ["us-east", "us-west"])

    def test_ignores_empty_values(self) -> None:
        self.assertEqual(parse_regions(["", " us-east ,, "]), ["us-east"])


if __name__ == "__main__":
    unittest.main()
