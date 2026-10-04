#!/usr/bin/env python3
"""Regression coverage for the SWSS service manifest and startup rendering."""

import json
from pathlib import Path
import sys
import unittest

import render


METADATA, MANIFEST, INIT = [Path(value).read_text() for value in sys.argv[1:4]]
del sys.argv[1:4]


class RenderTest(unittest.TestCase):
    def context(self):
        return dict(render.manifest_metadata(METADATA), asic_service="true", host_service="false")

    def test_legacy_manifest_contract(self):
        manifest = render.render_manifest(MANIFEST, self.context())
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
        runtime = render.render_manifest(MANIFEST, self.context())
        debug = render.render_manifest(MANIFEST, self.context(), debug=True)
        self.assertEqual(debug["package"]["version"], "1.0.0+dbg")
        debug["package"]["version"] = runtime["package"]["version"]
        self.assertEqual(debug, runtime)

    def test_debug_appends_existing_version_metadata(self):
        context = self.context()
        context["version"] = "1.0.0+custom"
        manifest = render.render_manifest(MANIFEST, context, debug=True)
        self.assertEqual(manifest["package"]["version"], "1.0.0+custom.dbg")

    def test_make_metadata_changes_flow_into_manifest(self):
        metadata = METADATA + "\n$(DOCKER_ORCHAGENT)_SERVICE_AFTER += database telemetry\n"
        context = dict(render.manifest_metadata(metadata), asic_service="true", host_service="false")
        manifest = render.render_manifest(MANIFEST, context)
        self.assertEqual(manifest["service"]["after"], ["database", "telemetry"])
        self.assertEqual(json.loads(json.dumps(manifest, separators=(",", ":"))), manifest)

    def test_computed_or_conditional_metadata_fails(self):
        for extra in (
            "$(DOCKER_ORCHAGENT)_VERSION = $(SONIC_VERSION)",
            "ifeq ($(PLATFORM),vs)\n$(DOCKER_ORCHAGENT)_VERSION = 2.0.0\nendif",
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                render.manifest_metadata(METADATA + "\n" + extra)

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
