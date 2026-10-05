#!/usr/bin/env python3
"""Cold Bazelisk diagnostics must not contaminate parsed command stdout."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

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


class RustGraphTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serde = "@@shared+//:serde (abc)\n@@shared+//:serde_core (abc)\n"
        self.common = "@@sonic-swss-common+//crates/swss-common:swss_common (abc)\n"

    def test_shared_provider_graph_is_recorded(self):
        with mock.patch.object(ci, "capture", side_effect=[self.serde + self.common, self.serde]):
            report = ci.verify_rust_dependencies(self.root, {"commands": []}, "bazel")
        self.assertEqual(report["common_library"], self.common.split()[0])
        self.assertEqual(json.loads((self.root / "rust-dependencies.json").read_text()), report)

    def test_duplicate_serde_provider_is_rejected(self):
        graph = self.serde + self.common + "@@second+//:serde (def)\n"
        with mock.patch.object(ci, "capture", side_effect=[graph, self.serde]):
            with self.assertRaisesRegex(ValueError, "one shared serde"):
                ci.verify_rust_dependencies(self.root, {"commands": []}, "bazel")

    def test_missing_common_library_is_rejected(self):
        with mock.patch.object(ci, "capture", side_effect=[self.serde, self.serde]):
            with self.assertRaisesRegex(ValueError, "public Rust library"):
                ci.verify_rust_dependencies(self.root, {"commands": []}, "bazel")

    def test_external_test_alias_matches_bep_canonical_name(self):
        path = self.root / "bep.json"
        path.write_text(json.dumps({
            "id": {"testSummary": {"label": "@@sonic-swss+//crates/countersyncd:common_rust_test"}},
            "testSummary": {"overallStatus": "PASSED"},
        }) + "\n")
        target = "@sonic_swss//crates/countersyncd:common_rust_test"
        self.assertEqual(ci.verify_tests(path, [target]), {target: "PASSED"})


if __name__ == "__main__":
    unittest.main()
