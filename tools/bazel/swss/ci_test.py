"""Keep Bazel diagnostics separate from queried artifact paths and retain failures."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ci


class CommandEvidenceTest(unittest.TestCase):
    def test_diagnostics_cannot_become_an_artifact_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            result = ci.execute(
                [sys.executable, "-c", "import sys; print('runtime.tar'); print('INFO: query', file=sys.stderr)"],
                directory, receipt, "query")
            self.assertEqual(result.splitlines(), ["runtime.tar"])
            self.assertIn("INFO: query", (directory / "query.log").read_text())
            self.assertEqual(receipt["commands"][0]["returncode"], 0)

    def test_failed_build_retains_its_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            with self.assertRaises(subprocess.CalledProcessError):
                ci.execute([sys.executable, "-c", "raise SystemExit('build failed')"],
                           directory, receipt, "build")
            self.assertIn("build failed", (directory / "build.log").read_text())
            self.assertEqual(receipt["commands"][0]["returncode"], 1)

    def test_external_source_lookup_needs_no_configured_execution_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_base = Path(temporary)
            source = output_base / "external/sonic-swss+"
            (source / "dist").mkdir(parents=True)
            (source / "dist/BUILD.bazel").write_text("# Source install declarations\n")

            def execute(command, *args):
                if command[1] == "info":
                    # This is the only info key that works without configuring
                    # a platform whose repository has a root-module alias.
                    self.assertEqual(command[2:], ["output_base"])
                    return str(output_base) + "\n"
                self.assertIn("--starlark:expr=target.label.workspace_root", command)
                return "external/sonic-swss+\n"

            with mock.patch.object(ci, "execute", side_effect=execute):
                self.assertEqual(ci.source_directory("bazel", [], output_base, {}), source)


if __name__ == "__main__":
    unittest.main()
