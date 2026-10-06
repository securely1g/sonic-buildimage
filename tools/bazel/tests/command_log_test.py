"""Check command evidence and artifact-path separation."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel.ci import command_log


class CommandEvidenceTest(unittest.TestCase):
    """Keep command results separate from their retained diagnostic evidence."""

    def test_diagnostics_cannot_become_an_artifact_path(self):
        """Return only stdout for path lookup while retaining stderr and success status."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            result = command_log.execute(
                [sys.executable, "-c", "import sys; print('runtime.tar'); print('INFO: query', file=sys.stderr)"],
                directory, receipt, "query", cwd=directory)
            self.assertEqual(result.splitlines(), ["runtime.tar"])
            self.assertIn("INFO: query", (directory / "query.log").read_text())
            self.assertEqual(receipt["commands"][0]["returncode"], 0)

    def test_failed_build_retains_its_diagnostics(self):
        """Record a failing command's message and exit status before propagating failure."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            with self.assertRaises(subprocess.CalledProcessError):
                command_log.execute([sys.executable, "-c", "raise SystemExit('build failed')"],
                           directory, receipt, "build", cwd=directory)
            self.assertIn("build failed", (directory / "build.log").read_text())
            self.assertEqual(receipt["commands"][0]["returncode"], 1)


if __name__ == "__main__":
    unittest.main()
