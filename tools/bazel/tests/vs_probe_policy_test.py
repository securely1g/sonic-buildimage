"""Prove that VS service probes honor the build selection and independent owner bytes."""

import json
from pathlib import Path
import tempfile
import unittest

from tools.bazel.ci import vs_container_probe as probe
from tools.bazel.ci import vs_probe_policy as policy


class VSProbePolicyTest(unittest.TestCase):
    def test_disabled_debug_does_not_request_owner_symbols(self):
        for image in policy.FAMILIES:
            with self.subTest(image=image):
                targets = policy.preparation_targets(image, False)
                self.assertFalse([name for name in targets if "debug" in name or "symbols" in name])
        self.assertIn("p4rt.debug.tar", policy.preparation_targets("docker-sonic-p4rt", True))

    def test_runtime_only_bundle_never_runs_a_companion_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            variants = ("docker-mux", "docker-mux-dbg")
            bundle = {"archives": {"runtime": {"file": "docker-mux.gz"}}, "debug_enabled": False,
                      "supports_debug": True}
            (directory / "bundle.json").write_text(json.dumps(bundle))
            self.assertEqual(policy.selected_images(directory, variants), variants[:1])
            bundle["debug_enabled"] = True
            (directory / "bundle.json").write_text(json.dumps(bundle))
            with self.assertRaisesRegex(ValueError, "selected service variants"):
                policy.selected_images(directory, variants)

    def test_missing_native_source_or_requested_symbols_fails_before_docker(self):
        path = "usr/bin/service"
        owner = {"sha256": "abc", "mode": 0o755, "uid": 0, "gid": 0, "build_id": "aa11",
                 "has_debuglink": True, "needed": ["libc.so.6"]}
        installed = {path: {**owner, "elf_machine": 62}}
        evidence = {"native": {path: owner}, "owners": {}}
        bundle = {"fixtures": {}, "image": "docker-mux"}
        native, pairs = probe.native_expectations(Path("."), bundle, "runtime", installed, evidence)
        self.assertEqual(list(native), [path])
        self.assertEqual(pairs, [])
        with self.assertRaisesRegex(ValueError, "matching detached symbols"):
            probe.native_expectations(Path("."), bundle, "debug", installed, evidence)
        installed[path]["sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "source owner ELF"):
            probe.native_expectations(Path("."), bundle, "runtime", installed, evidence)


if __name__ == "__main__":
    unittest.main()
