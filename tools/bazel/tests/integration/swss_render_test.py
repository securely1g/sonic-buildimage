#!/usr/bin/env python3
"""Check the selected SWSS manifests and their generated OCI labels."""

import json
from pathlib import Path
import sys
import unittest

from tools.bazel.tests.integration.manifest_label_assertions import assert_manifest_labels

INPUT_PATHS = [Path(value) for value in sys.argv[1:5]]
RUNTIME, DEBUG = [json.loads(path.read_text()) for path in INPUT_PATHS[:2]]
del sys.argv[1:5]


class RenderTest(unittest.TestCase):
    def test_legacy_manifest_contract(self):
        """Preserve SWSS package identity, ASIC scope, shutdown ordering and container/CLI settings."""
        manifest = RUNTIME
        self.assertEqual(manifest["package"], {"version": "1.0.0", "name": "swss", "depends": []})
        service = manifest["service"]
        self.assertEqual(service["name"], "swss")
        self.assertTrue(service["asic-service"])
        self.assertFalse(service["host-service"])
        self.assertEqual(service["warm-shutdown"], {"after": [], "before": ["syncd"]})
        self.assertEqual(service["fast-shutdown"], {"after": [], "before": ["syncd"]})
        self.assertEqual(service["syslog"], {"support-rate-limit": True})
        self.assertEqual(manifest["container"], {"privileged": False, "volumes": [], "tmpfs": []})
        self.assertEqual(manifest["cli"], {"config": "", "show": "", "clear": ""})

    def test_debug_differs_only_in_package_version(self):
        """Keep generated debug metadata identical to runtime metadata except for the version suffix."""
        runtime = RUNTIME
        debug = json.loads(json.dumps(DEBUG))
        self.assertEqual(debug["package"]["version"], "1.0.0+dbg")
        debug["package"]["version"] = runtime["package"]["version"]
        self.assertEqual(debug, runtime)

    def test_generated_manifests_and_labels_match(self):
        """Require each emitted OCI label to encode its corresponding generated manifest and version."""
        for manifest, labels, version in (
            (RUNTIME, INPUT_PATHS[2], "1.0.0"),
            (DEBUG, INPUT_PATHS[3], "1.0.0+dbg"),
        ):
            self.assertEqual(manifest["package"]["version"], version)
            assert_manifest_labels(self, labels, manifest)


if __name__ == "__main__":
    unittest.main()
