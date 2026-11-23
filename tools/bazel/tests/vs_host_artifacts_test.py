"""Exercise the VS handoff for host-only packages and the installed libteam consumer."""

from contextlib import chdir
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.ci import libteam_installed_test, vs_component_artifacts as components
from tools.bazel.ci.components import host


class VSHostArtifactsTest(unittest.TestCase):
    def test_host_inputs_follow_installer_debug_selection_without_an_image_owner(self):
        revision = "a" * 40
        families = {"docker-unit": {"members": {"runtime": ()}}}
        with tempfile.TemporaryDirectory() as temp, chdir(temp), patch.object(components, "components", return_value={"host": host}):
            root = Path("target/components")
            root.mkdir(parents=True)
            sources = {}
            for filename, label in {**host.RUNTIME, **host.DEBUG}.items():
                source = Path("products") / filename
                source.parent.mkdir(exist_ok=True)
                if filename.endswith(".deb"):
                    source.mkdir()
                    (source / filename).write_bytes(b"VS platform binary DEB")
                else:
                    source.write_bytes(b'{"package": "test"}' if filename.endswith(".json") else b"original payload")
                sources[label] = source
            families["docker-unit"]["members"]["debug"] = ()
            self.assertEqual(components.targets(families), set(sources))
            del families["docker-unit"]["members"]["debug"]
            self.assertEqual(components.targets(families), set(host.RUNTIME.values()))
            components.publish(root, families, sources, revision, "amd64", {"//tools/bazel/oci:image_version": "test"})
            package = root / "host"
            manifest = json.loads((package / "manifest.json").read_bytes())
            self.assertEqual(manifest["images"], [])
            self.assertFalse(manifest["debug"])
            self.assertFalse((package / "rsyslog-plugin.debug.tar").exists())
            self.assertEqual((package / "sonic-platform-vs_1.0_amd64.deb").read_bytes(), b"VS platform binary DEB")
            with patch.object(host, "verify"):
                flags = host.stage(package, revision, False)
            for selector, filename in host.TEST_INPUTS.items():
                self.assertIn(f"--{selector}=//{package}:{filename}", flags)
                self.assertIn(filename, (package / "BUILD.bazel").read_text())

    def test_libteam_runs_the_owner_binary_with_only_the_two_vs_payloads_substituted(self):
        xml = b'''<query><rule class="native_runtime_test" name="@@libteam+//:consumer_test">
          <label name="binary" value="@@libteam+//:consumer_binary"/>
          <list name="runtime_payloads">
            <label value="@@libteam+//:libteam_runtime"/><label value="@@libteam+//:libteamdctl_runtime"/>
            <label value="@@rules_distroless++apt+sysroot//libc6:data"/>
          </list></rule></query>'''
        with tempfile.TemporaryDirectory() as temp, chdir(temp):
            bundle, package = Path("swss"), Path("target/libteam")
            bundle.mkdir()
            package.parent.mkdir()
            for name in ("libteam.tar", "libteamdctl.tar"):
                (bundle / name).write_text(name)
            with patch.object(libteam_installed_test, "verify_inputs") as verify, patch.object(
                    libteam_installed_test.subprocess, "check_output", return_value=xml):
                label = libteam_installed_test.stage("bazel", bundle, "a" * 40, False, package)
            verify.assert_called_once_with("swss", bundle, "a" * 40, False)
            self.assertEqual(label, "//target/libteam:consumer_test")
            definition = (package / "BUILD.bazel").read_text()
            self.assertIn('binary="@@libteam+//:consumer_binary"', definition)
            self.assertIn('"@@rules_distroless++apt+sysroot//libc6:data"', definition)
            self.assertNotIn('@@libteam+//:libteam_runtime', definition)
            for name in ("libteam.tar", "libteamdctl.tar"):
                self.assertIn('":' + name + '"', definition)
                self.assertEqual((package / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
