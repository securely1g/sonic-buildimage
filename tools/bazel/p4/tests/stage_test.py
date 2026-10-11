"""Invalid source selections must fail before Bazel or output publication."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

STAGE = Path(__file__).resolve().parents[1] / "stage.py"


class SourceSelectionTest(unittest.TestCase):
    def test_explicit_empty_and_wrong_revisions_fail_before_bazel(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "packages"
            for revision in ("", "0" * 40):
                with self.subTest(revision=revision):
                    result = subprocess.run(
                        [sys.executable, str(STAGE), "--output-directory", str(output),
                         "--dash-sai-commit=" + revision,
                         "--bazel", str(Path(temporary) / "must-not-run")],
                        text=True, capture_output=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("DASH source revision changed", result.stderr)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
