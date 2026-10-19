"""Reject Debian package actions before CI builds or runs tests."""

import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import bazel_commands


class ActionAuditTest(unittest.TestCase):
    def audit(self, output_name, arguments, *, allow_deb=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = {
                "actions": [{"outputIds": [1], "arguments": arguments,
                             "environmentVariables": [{"key": "PRIVATE", "value": "private-value"}]}],
                "artifacts": [{"id": 1, "pathFragmentId": 1}],
                "pathFragments": [{"id": 1, "label": output_name}],
            }
            def query(command, *, cwd, stdout, check):
                self.assertEqual(command[1], "aquery")
                self.assertIn("--platforms=native", command)
                stdout.write(json.dumps(graph))
                stdout.flush()
            with mock.patch("subprocess.run", side_effect=query):
                try:
                    return bazel_commands.audit_targets(
                        "bazel", ["--platforms=native"], ["//:checked"],
                        workspace=root, output=root / "audit.json", allow_deb=allow_deb)
                finally:
                    summary = (root / "audit.json").read_text()
                    self.assertNotIn("private-value", summary)

    def test_accepts_tar_assembly_and_debian_extraction(self):
        result = self.audit("runtime.tar", ["dpkg-deb", "--fsys-tarfile", "input.deb"])
        self.assertEqual(result["targets"], ["//:checked"])
        self.assertEqual(result["action_count"], 1)

    def test_rejects_debian_output(self):
        with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
            self.audit("package.deb", ["packager"])

    def test_explicit_approval_allows_actual_deb_assembly(self):
        result = self.audit("package.deb", ["dpkg-deb", "--build", "tree", "package.deb"], allow_deb=True)
        self.assertTrue(result["deb_generation_authorized"])
        self.assertEqual(len(result["deb_packaging_actions"]), 1)
        self.assertEqual(result["packaging_wrappers"], [])

    def test_shell_comment_is_not_a_make_invocation(self):
        result = self.audit("package.deb", ["bash", "-c", "# make sure data is compressed\ntar -czf data.tar.gz tree"], allow_deb=True)
        self.assertEqual(result["packaging_wrappers"], [])
        with self.assertRaises(ValueError):
            self.audit("package.deb", ["bash", "-c", "# comment\nmake package"], allow_deb=True)

    def test_deb_approval_does_not_allow_make_or_dpkg_buildpackage(self):
        for command in (["sh", "-c", "make package"], ["dpkg-buildpackage", "-b"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.audit("package.deb", command, allow_deb=True)

    def test_rejects_wrapped_packaging_with_hidden_output(self):
        for command in (["dpkg-deb", "--build", "tree", "output"],
                        ["sh", "-c", "make package"], ["dpkg-buildpackage", "-b"]):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                    self.audit("output", command)


if __name__ == "__main__":
    unittest.main()
