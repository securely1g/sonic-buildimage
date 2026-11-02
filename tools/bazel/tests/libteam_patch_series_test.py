#!/usr/bin/env python3
"""Keep the Bazel libteam patch list identical to Make's StGit series."""

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
LIBTEAM = ROOT / "src/libteam"


def make_series():
    lines = (LIBTEAM / "patch/series").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def bazel_patches():
    tree = ast.parse((LIBTEAM / "MODULE.bazel").read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "http_archive"):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        if ast.literal_eval(keywords["name"]) != "libteam_source":
            continue
        labels = ast.literal_eval(keywords["patches"])
        prefix = "//:patch/"
        if not all(label.startswith(prefix) for label in labels):
            raise AssertionError("libteam patches must be local //:patch/ labels")
        return [label[len(prefix):] for label in labels]
    raise AssertionError("libteam_source http_archive is missing")


class LibteamPatchSeriesTest(unittest.TestCase):
    def test_bazel_applies_make_series_in_order(self):
        self.assertEqual(bazel_patches(), make_series())

    def test_every_series_patch_exists(self):
        for name in make_series():
            self.assertTrue((LIBTEAM / "patch" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
