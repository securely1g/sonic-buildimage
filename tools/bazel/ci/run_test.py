#!/usr/bin/env python3
"""Cold Bazelisk diagnostics must not contaminate parsed command stdout."""

from pathlib import Path
import tempfile
import unittest

import run as ci


class BazelVersionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receipt = {"commands": []}

    def bazelisk(self, version="8.5.1", status=0):
        executable = self.root / "bazelisk"
        executable.write_text(
            "#!/bin/sh\n"
            "printf 'Downloading Bazel from releases.bazel.build\\n' >&2\n"
            f"printf 'bazel {version}\\n'\n"
            f"exit {status}\n")
        executable.chmod(0o755)
        return str(executable)

    def test_cold_download_diagnostic_is_logged_but_version_uses_stdout(self):
        actual = ci.check_bazel_version(self.bazelisk(), "bazel 8.5.1", self.root, self.receipt)
        self.assertEqual(actual, "bazel 8.5.1")
        log = (self.root / "bazel-version.log").read_text()
        self.assertIn("Downloading Bazel", log)
        self.assertIn("bazel 8.5.1", log)
        self.assertEqual(self.receipt["commands"][0]["returncode"], 0)
        self.assertGreaterEqual(self.receipt["commands"][0]["elapsed_seconds"], 0)

    def test_incorrect_version_still_fails(self):
        with self.assertRaisesRegex(ValueError, "Expected bazel 8.5.1, got bazel 8.4.0"):
            ci.check_bazel_version(self.bazelisk("8.4.0"), "bazel 8.5.1", self.root, self.receipt)

    def test_nonzero_command_preserves_failure_and_diagnostics(self):
        with self.assertRaisesRegex(RuntimeError, "failed with exit 7"):
            ci.check_bazel_version(self.bazelisk(status=7), "bazel 8.5.1", self.root, self.receipt)
        self.assertEqual(self.receipt["commands"][0]["returncode"], 7)
        self.assertIn("Downloading Bazel", (self.root / "bazel-version.log").read_text())


if __name__ == "__main__":
    unittest.main()
