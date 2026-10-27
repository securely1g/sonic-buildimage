"""Exercise the standalone image verifier's declared cache-download inputs."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import validate_syncd_image


class ImageInputsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd inputs ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = {
            label: self.root / "outputs" / key
            for key, label in validate_syncd_image.TARGETS.items()
        }
        self.missing = None
        self.commands = []
        self.unsafe = False

    def bazel(self, command, **kwargs):
        self.commands.append(command)
        action = command[1]
        if action == "aquery":
            # The audit must reject package producers before any build occurs.
            graph = {"actions": []}
            if self.unsafe:
                graph = {
                    "actions": [{"outputIds": [1]}],
                    "artifacts": [{"id": 1, "pathFragmentId": 1}],
                    "pathFragments": [{"id": 1, "label": "unexpected.deb"}],
                }
            json.dump(graph, kwargs["stdout"])
        elif action == "build":
            # Model a fresh output base with cache hits: only requested top-level
            # outputs are downloaded, even when the manifest's action needs a TAR.
            for label in command[2:]:
                path = self.paths[label]
                path.parent.mkdir(parents=True, exist_ok=True)
                if label != self.missing:
                    path.write_bytes(b"cached output")
        elif action == "cquery":
            labels = command[2].removeprefix("set(").removesuffix(")").split()
            return subprocess.CompletedProcess(
                command, 0, stdout="".join(
                    label + "\t" + repr([str(self.paths[label])]) + "\n" for label in labels))
        else:
            self.fail("Unexpected Bazel command: " + action)
        return subprocess.CompletedProcess(command, 0)

    def build(self):
        with mock.patch.object(validate_syncd_image.subprocess, "run", side_effect=self.bazel):
            return validate_syncd_image.build_inputs(["bazel"], [], self.root)

    def test_cached_build_materializes_source_runtime_symbols_and_receipt(self):
        inputs = self.build()
        for key in ("source_runtime", "source_debug", "source_receipt", "runtime_links"):
            self.assertEqual(inputs[key].read_bytes(), b"cached output")
        self.assertNotIn("runtime_handoff", inputs)
        self.assertNotIn("debug_handoff", inputs)
        self.assertEqual([command[1] for command in self.commands], ["aquery", "build", "cquery"])

    def test_missing_source_tar_is_rejected_before_image_probes(self):
        self.missing = validate_syncd_image.OWNER + ":source_runtime_payloads"
        with self.assertRaisesRegex(ValueError, "Bazel output is missing"):
            self.build()

    def test_deb_producer_stops_before_cache_download(self):
        self.unsafe = True
        with self.assertRaisesRegex(ValueError, "would produce a Debian package"):
            self.build()
        self.assertEqual([command[1] for command in self.commands], ["aquery"])


if __name__ == "__main__":
    unittest.main()
