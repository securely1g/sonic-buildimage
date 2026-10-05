#!/usr/bin/env python3
"""Regression coverage for the SWSS service manifest and startup rendering."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import container_config
import render


INPUT_PATHS = [Path(value) for value in sys.argv[1:4]]
METADATA, MANIFEST, INIT = [path.read_text() for path in INPUT_PATHS]
del sys.argv[1:4]


class RenderTest(unittest.TestCase):
    def context(self):
        return render.manifest_context(METADATA)

    def test_legacy_manifest_contract(self):
        manifest = container_config.render_manifest(MANIFEST, self.context())
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
        runtime = container_config.render_manifest(MANIFEST, self.context())
        debug = container_config.render_manifest(MANIFEST, self.context(), version_suffix="dbg")
        self.assertEqual(debug["package"]["version"], "1.0.0+dbg")
        debug["package"]["version"] = runtime["package"]["version"]
        self.assertEqual(debug, runtime)

    def test_debug_appends_existing_version_metadata(self):
        context = self.context()
        context["version"] = "1.0.0+custom"
        manifest = container_config.render_manifest(MANIFEST, context, version_suffix="dbg")
        self.assertEqual(manifest["package"]["version"], "1.0.0+custom.dbg")

    def test_make_metadata_changes_flow_into_manifest(self):
        metadata = METADATA + "\n$(DOCKER_ORCHAGENT)_SERVICE_AFTER += database telemetry\n"
        context = render.manifest_context(metadata)
        manifest = container_config.render_manifest(MANIFEST, context)
        self.assertEqual(manifest["service"]["after"], ["database", "telemetry"])
        self.assertEqual(json.loads(json.dumps(manifest, separators=(",", ":"))), manifest)

    def test_computed_or_conditional_metadata_fails(self):
        for extra in (
            "$(DOCKER_ORCHAGENT)_VERSION = $(SONIC_VERSION)",
            "ifeq ($(PLATFORM),vs)\n$(DOCKER_ORCHAGENT)_VERSION = 2.0.0\nendif",
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                render.manifest_context(METADATA + "\n" + extra)

    def test_cli_writes_matching_manifests_labels_and_startup_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = root / "swss.service.j2"
            service.write_text("# SWSS per-namespace service\n")
            arguments = [
                "render.py", "--make-metadata", str(INPUT_PATHS[0]),
                "--manifest-template", str(INPUT_PATHS[1]),
                "--docker-init-template", str(INPUT_PATHS[2]),
                "--asic-service-template", str(service),
                "--init-output", str(root / "init.sh"),
                "--manifest-output", str(root / "manifest.json"),
                "--debug-manifest-output", str(root / "debug.json"),
                "--labels-output", str(root / "labels"),
                "--debug-labels-output", str(root / "debug.labels"),
            ]
            with mock.patch.object(sys, "argv", arguments):
                render.main()
            for manifest_name, label_name, version in (
                ("manifest.json", "labels", "1.0.0"),
                ("debug.json", "debug.labels", "1.0.0+dbg"),
            ):
                manifest = json.loads((root / manifest_name).read_text())
                self.assertEqual(manifest["package"]["version"], version)
                key, value = (root / label_name).read_text().strip().split("=", 1)
                self.assertEqual(key, "com.azure.sonic.manifest")
                self.assertEqual(json.loads(value), manifest)
            self.assertEqual((root / "init.sh").read_text(), render.render_init(INIT))

    def test_non_asan_init_retains_runtime_configuration(self):
        result = render.render_init(INIT)
        self.assertNotIn("ENABLE_ASAN", result)
        self.assertNotIn("{%", result)
        self.assertIn('"${ASIC_VENDOR:-unknown}', result)
        self.assertIn("/etc/sonic/constants.yml", result)
        self.assertIn("/etc/swss/config.d/switch.json", result)
        self.assertIn("exec /usr/local/bin/supervisord", result)
        self.assertTrue(result.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
