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
    def audit(self, output_name, arguments, mnemonic="Spawn"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = {
                "actions": [{"outputIds": [1], "arguments": arguments, "mnemonic": mnemonic,
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
                        workspace=root, output=root / "audit.json")
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

    def test_accepts_linking_a_native_make_tool(self):
        """The GNU Make executable is a compiler output, not a packaging command."""
        result = self.audit("bin/make", ["toolchain/x86_64-linux-g++", "main.o", "-o", "bin/make"], "CppLink")
        self.assertEqual(result["packaging_wrappers"], [])

    def test_accepts_stripping_a_native_make_tool(self):
        """A strip input named make must not prevent building the tool itself."""
        result = self.audit("bin/make.stripped", ["toolchain/aarch64-linux-strip", "-S", "-o", "bin/make.stripped", "bin/make"], "CcStrip")
        self.assertEqual(result["packaging_wrappers"], [])

    def test_link_mnemonic_does_not_hide_a_packaging_wrapper(self):
        """Check the actual executable and shell text even for link-labelled actions."""
        for command in (["make", "package"], ["sh", "-c", "make package"],
                        ["sh", "-c", "dpkg-deb --build tree output"]):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                    self.audit("output", command, "CppLink")

    def test_native_link_output_still_cannot_be_a_debian_package(self):
        """The filename check remains independent of native-tool recognition."""
        with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
            self.audit("package.deb", ["gcc", "main.o", "-o", "package.deb"], "CppLink")

    def test_rejects_wrapped_packaging_with_hidden_output(self):
        for command in (["dpkg-deb", "--build", "tree", "output"],
                        ["sh", "-c", "make package"], ["dpkg-buildpackage", "-b"]):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                    self.audit("output", command)


if __name__ == "__main__":
    unittest.main()
