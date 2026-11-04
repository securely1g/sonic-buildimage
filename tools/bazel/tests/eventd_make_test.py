"""Check Eventd's real Make selector and declared image-version input."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "eventd_version", ROOT / "dockers/docker-eventd/bazel/prepare_version.py")
VERSION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERSION)


class EventdMakeTest(unittest.TestCase):
    def evaluate(self, **overrides):
        settings = dict(BLDENV="trixie", CONFIGURED_PLATFORM="vs", CONFIGURED_ARCH="amd64",
                        INCLUDE_FIPS="y", BUILD_WITH_BAZEL_WHEN_AVAILABLE="y", DBG_IMAGE_MARK="dbg",
                        DOCKERS_PATH="dockers", TARGET_PATH="target", INCLUDE_SYSTEM_EVENTD="y",
                        SONIC_EVENTD="eventd.deb", SONIC_EVENTD_DBG="eventd-dbgsym.deb",
                        SONIC_UTILITIES_PY3="utilities.whl", PYTHON3_SWSSCOMMON="bindings.deb")
        settings.update(overrides)
        names = ["SONIC_BAZEL_DOCKER_IMAGES", "SONIC_BAZEL_DBG_DOCKER_IMAGES",
                 "$(DOCKER_EVENTD)_BAZEL_DEPENDS", "$(DOCKER_EVENTD_DBG)_BAZEL_DEPENDS",
                 "$(DOCKER_EVENTD)_DEPENDS", "$(DOCKER_EVENTD)_INSTALL_PYTHON_WHEELS",
                 "$(DOCKER_EVENTD)_INSTALL_DEBS", "$(DOCKER_EVENTD)_RUN_OPT"]
        with tempfile.TemporaryDirectory() as temporary:
            recipe = Path(temporary) / "Makefile"
            recipe.write_text("include rules/docker-eventd.mk\nresult:\n\t@printf '%s\\n' " +
                              " ".join("'$(" + name + ")'" for name in names) + "\n")
            return subprocess.check_output(
                ["make", "--no-print-directory", "-f", str(recipe), "result",
                 *[key + "=" + value for key, value in settings.items()]], cwd=ROOT, text=True).splitlines()

    def test_native_handoff_retains_legacy_and_builder_inputs(self):
        values = self.evaluate()
        self.assertEqual(values[:2], ["docker-eventd.gz", "docker-eventd-dbg.gz"])
        self.assertIn("target/bazel-manifests/docker-eventd/image.env", values[2])
        self.assertIn("target/bazel-manifests/docker-eventd/manifest.json", values[3])
        self.assertIn("target/bazel-manifests/docker-eventd-dbg/manifest.json", values[3])
        self.assertEqual(values[4:7], ["eventd.deb", "utilities.whl", "bindings.deb"])
        self.assertIn("/etc/sonic:/etc/sonic:ro", values[7])

    def test_other_profiles_keep_make_producers(self):
        for settings in ({"CONFIGURED_ARCH": "arm64"}, {"CONFIGURED_PLATFORM": "broadcom"},
                         {"BLDENV": "bookworm"}, {"INCLUDE_FIPS": "n"}, {"ENABLE_ASAN": "y"},
                         {"BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n"}, {"CROSS_BUILD_ENVIRON": "y"},
                         {"MULTIARCH_QEMU_ENVIRON": "y"}, {"DBG_IMAGE_MARK": "custom"}):
            with self.subTest(settings=settings):
                values = self.evaluate(**settings)
                self.assertEqual(values[:4], ["", "", "", ""])
                self.assertEqual(values[4:7], ["eventd.deb", "utilities.whl", "bindings.deb"])

    def test_version_change_noop_and_failed_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image.env"
            VERSION.publish(output, "master.1-test")
            original = output.stat().st_mtime_ns
            VERSION.publish(output, "master.1-test")
            self.assertEqual(output.stat().st_mtime_ns, original)
            self.assertEqual(output.read_text(), "DEBIAN_FRONTEND=noninteractive\nIMAGE_VERSION=master.1-test\n")
            VERSION.publish(output, "master.2-test")
            self.assertEqual(output.read_text(), "DEBIAN_FRONTEND=noninteractive\nIMAGE_VERSION=master.2-test\n")
            for bad in ("", "master\nEXTRA=value", "master\rvalue", "master\0value", "build-$PATH", "build-${HOME}"):
                with self.subTest(value=repr(bad)), self.assertRaises(ValueError):
                    VERSION.publish(output, bad)
                self.assertEqual(output.read_text(), "DEBIAN_FRONTEND=noninteractive\nIMAGE_VERSION=master.2-test\n")


if __name__ == "__main__":
    unittest.main()
