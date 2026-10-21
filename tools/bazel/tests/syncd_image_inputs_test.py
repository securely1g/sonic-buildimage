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
        owner = validate_syncd_image.OWNER
        for variant in ("runtime", "debug"):
            directory = self.root / "outputs" / (variant + "_debs")
            self.paths[owner + ":" + variant + "_import_manifest"] = directory / "manifest.json"
            self.paths[owner + ":" + variant + "_debs"] = directory / "payload.tar"
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
                if label.endswith(":debug_debs"):
                    path.symlink_to(self.paths[validate_syncd_image.OWNER + ":runtime_debs"])
                else:
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

    def test_cached_manifests_materialize_both_payloads(self):
        inputs = self.build()
        for variant in ("runtime", "debug"):
            manifest = inputs[variant + "_handoff"]
            self.assertTrue(manifest.is_file())
            self.assertEqual(manifest.with_name("payload.tar").read_bytes(), b"cached output")
        self.assertTrue(inputs["debug_payload"].is_symlink())
        self.assertEqual([command[1] for command in self.commands], ["aquery", "build", "cquery"])

    def test_payload_target_must_match_the_manifest_sibling(self):
        self.paths[validate_syncd_image.OWNER + ":runtime_debs"] = self.root / "unrelated/payload.tar"
        with self.assertRaisesRegex(ValueError, "not beside its manifest: runtime"):
            self.build()

    def test_deb_producer_stops_before_cache_download(self):
        self.unsafe = True
        with self.assertRaisesRegex(ValueError, "would produce a Debian package"):
            self.build()
        self.assertEqual([command[1] for command in self.commands], ["aquery"])


if __name__ == "__main__":
    unittest.main()
