"""Verify the prepared syncd Make manifests and their OCI labels."""

import argparse
import copy
import json
from pathlib import Path
import unittest

parser = argparse.ArgumentParser()
for name in ("runtime-manifest", "debug-manifest", "runtime-labels", "debug-labels"):
    parser.add_argument("--" + name, required=True, type=Path)
INPUTS, remaining = parser.parse_known_args()


class SyncdManifestLabelsTest(unittest.TestCase):
    def test_owner_metadata_and_labels_match_make(self):
        runtime = json.loads(INPUTS.runtime_manifest.read_bytes())
        debug = json.loads(INPUTS.debug_manifest.read_bytes())
        self.assertEqual(runtime["version"], "1.0.0")
        self.assertEqual(runtime["package"]["name"], "syncd")
        self.assertEqual(runtime["package"]["version"], "1.0.0")
        self.assertEqual(runtime["service"]["name"], "syncd")
        self.assertIs(runtime["service"]["asic-service"], True)
        self.assertIs(runtime["service"]["host-service"], False)
        expected_debug = copy.deepcopy(runtime)
        expected_debug["package"]["version"] += "+dbg"
        self.assertEqual(debug, expected_debug)
        for manifest, labels in ((runtime, INPUTS.runtime_labels), (debug, INPUTS.debug_labels)):
            key, value = labels.read_text().rstrip("\n").split("=", 1)
            self.assertEqual(key, "com.azure.sonic.manifest")
            self.assertEqual(json.loads(value), manifest)


if __name__ == "__main__":
    unittest.main(argv=[__file__, *remaining])
