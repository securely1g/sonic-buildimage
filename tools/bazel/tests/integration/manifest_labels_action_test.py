"""Exercise the production label action and command with prepared JSON."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


parser = argparse.ArgumentParser()
for argument in ("tool", "runtime-manifest", "debug-manifest", "runtime-labels", "debug-labels"):
    parser.add_argument("--" + argument, type=Path, required=True)
INPUTS, remaining = parser.parse_known_args()


class ManifestLabelsActionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "manifest.labels"

    def run_tool(self, source, output=None):
        return subprocess.run([
            str(INPUTS.tool.resolve()),
            "--manifest", str(source),
            "--output", str(output or self.output),
        ], capture_output=True, text=True, timeout=30)

    def test_bazel_actions_use_each_supplied_manifest_unchanged(self):
        """Check that both Bazel actions preserve their supplied Make manifests."""
        for manifest_path, labels_path in (
            (INPUTS.runtime_manifest, INPUTS.runtime_labels),
            (INPUTS.debug_manifest, INPUTS.debug_labels),
        ):
            with self.subTest(manifest=manifest_path):
                key, value = labels_path.read_text().rstrip("\n").split("=", 1)
                self.assertEqual(key, "com.azure.sonic.manifest")
                self.assertEqual(json.loads(value), json.loads(manifest_path.read_text()))

    def test_replacing_one_input_changes_only_requested_output(self):
        """Keep the debug label intact when converting a changed runtime manifest."""
        changed = self.root / "changed.json"
        manifest = json.loads(INPUTS.runtime_manifest.read_text())
        manifest["service"]["after"] = ["database", "telemetry"]
        changed.write_text(json.dumps(manifest))
        debug_output = self.root / "debug.labels"
        result = self.run_tool(INPUTS.debug_manifest, debug_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = debug_output.read_bytes()
        result = self.run_tool(changed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text().split("=", 1)[1]), manifest)
        self.assertEqual(debug_output.read_bytes(), before)

    def test_invalid_input_preserves_existing_output(self):
        """Preserve the last valid label when manifest validation fails."""
        changed = self.root / "invalid.json"
        for source in ('{"invalid": }', '[]', '{"invalid": NaN}'):
            with self.subTest(source=source):
                changed.write_text(source)
                self.output.write_text("previous output\n")
                result = self.run_tool(changed)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.output.read_text(), "previous output\n")

    def test_missing_input_fails_without_output(self):
        """Fail visibly when Make has not prepared the declared manifest."""
        result = self.run_tool(self.root / "missing.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_unwritable_output_path_is_an_error(self):
        """Report failed publication instead of claiming a label was produced."""
        result = self.run_tool(INPUTS.runtime_manifest, self.root / "missing" / "labels")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main(argv=[__file__, *remaining])
