"""Exercise MACsec's real Make selector without executing image/package recipes."""

from pathlib import Path
import json
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.bazel.tests.docker_test import ArchiveRecipeFixture


class MacsecMakeTest(unittest.TestCase):
    def evaluate(self, **changes):
        values = dict(BLDENV="trixie", CONFIGURED_ARCH="amd64", CONFIGURED_PLATFORM="vs",
                      INCLUDE_FIPS="y", INCLUDE_MACSEC="y", INSTALL_DEBUG_TOOLS="n", DBG_IMAGE_MARK="dbg",
                      BUILD_WITH_BAZEL_WHEN_AVAILABLE="y", DOCKERS_PATH="dockers", TARGET_PATH="target",
                      WPASUPPLICANT="wpasupplicant.deb", SONIC_UTILITIES_PY3="sonic_utilities.whl",
                      PYTHON3_SWSSCOMMON="python3-swsscommon.deb", LIBYANG3_PY3="python3-yang.deb",
                      DOCKER_SWSS_LAYER_TRIXIE="docker-swss-layer-trixie.gz")
        values.update(changes)
        variables = ["SONIC_BAZEL_DOCKER_IMAGES", "SONIC_BAZEL_DBG_DOCKER_IMAGES",
                     "SONIC_BAZEL_SWITCHABLE_IMAGES", "docker-macsec.gz_BAZEL_TARGET",
                     "docker-macsec-$(DBG_IMAGE_MARK).gz_BAZEL_TARGET",
                     "docker-macsec-$(DBG_IMAGE_MARK).gz_BAZEL_DEPENDS",
                     "docker-macsec.gz_DEPENDS", "docker-macsec.gz_LOAD_DOCKERS",
                     "docker-macsec.gz_INSTALL_PYTHON_WHEELS", "docker-macsec.gz_INSTALL_DEBS",
                     "docker-macsec.gz_SERVICE_REQUIRES", "docker-macsec.gz_SERVICE_AFTER",
                     "docker-macsec.gz_CONTAINER_PRIVILEGED", "docker-macsec.gz_CONTAINER_VOLUMES",
                     "docker-macsec.gz_CLI_CONFIG_PLUGIN", "docker-macsec.gz_CLI_SHOW_PLUGIN",
                     "docker-macsec.gz_CLI_CLEAR_PLUGIN", "SONIC_PACKAGES_LOCAL"]
        recipe = "include rules/docker-macsec.mk\ninclude tools/bazel/docker.mk\n.PHONY: result\nresult:\n\t@printf '%s\\n' "
        recipe += " ".join("'$(" + variable + ")'" for variable in variables) + "\n"
        return subprocess.check_output(["make", "--no-print-directory", "-f", "-", "result",
                                        *[key + "=" + value for key, value in values.items()]],
                                       input=recipe, cwd=ROOT, text=True).splitlines()

    def test_selected_runtime_and_debug_use_manifest_prerequisites(self):
        result = self.evaluate()
        self.assertEqual(result[:2], ["docker-macsec.gz", "docker-macsec-dbg.gz"])
        self.assertEqual(set(result[2].split()), set(result[:2]))
        self.assertEqual(result[3:5], ["//dockers/docker-macsec:docker-macsec.gz",
                                     "//dockers/docker-macsec:docker-macsec-dbg.gz"])
        self.assertEqual(set(result[5].split()), {
            "target/bazel-manifests/" + name + "/manifest.json" for name in
            ("docker-base-trixie", "docker-config-engine-trixie", "docker-swss-layer-trixie",
             "docker-macsec", "docker-macsec-dbg")})
        self.assertEqual(result[6:13], ["wpasupplicant.deb", "docker-swss-layer-trixie.gz",
                                       "sonic_utilities.whl", "python3-swsscommon.deb python3-yang.deb",
                                       "config-setup", "swss syncd", "false"])
        self.assertEqual(result[13].split(), ["/etc/sonic:/etc/sonic:ro", "/etc/localtime:/etc/localtime:ro"])
        self.assertEqual(result[14:17], ["/cli/config/plugins/macsec.py", "/cli/show/plugins/show_macsec.py",
                                        "/cli/clear/plugins/clear_macsec_counter.py"])
        self.assertEqual(result[17], "docker-macsec.gz")

    def test_excluded_profiles_preserve_legacy_inputs_and_service_metadata(self):
        baseline = self.evaluate()
        for settings in ({"BLDENV": "bookworm"}, {"CONFIGURED_ARCH": "arm64"},
                         {"CONFIGURED_ARCH": "armhf"}, {"CONFIGURED_PLATFORM": "broadcom"},
                         {"BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n"}, {"INCLUDE_FIPS": "n"},
                         {"DBG_IMAGE_MARK": "debug"}, {"ENABLE_ASAN": "y"},
                         {"CROSS_BUILD_ENVIRON": "y"}, {"MULTIARCH_QEMU_ENVIRON": "y"}):
            with self.subTest(settings=settings):
                result = self.evaluate(**settings)
                self.assertEqual(result[:2], ["", ""])
                self.assertEqual(result[3:6], ["", "", ""])
                self.assertEqual(result[6:], baseline[6:])
                if settings.get("BLDENV", "trixie") == "trixie":
                    self.assertIn("docker-macsec.gz", result[2])

    def test_existing_install_flags_select_local_image_variant(self):
        self.assertEqual(self.evaluate(INCLUDE_MACSEC="n")[17], "")
        self.assertEqual(self.evaluate(INSTALL_DEBUG_TOOLS="y")[17], "docker-macsec-dbg.gz")


class MacsecArchiveRecipeTest(ArchiveRecipeFixture, unittest.TestCase):
    """Run actual Make manifests/export; replace only the Bazel executable."""

    archives = ("docker-macsec.gz", "docker-macsec-dbg.gz")
    labels = {name: "//dockers/docker-macsec:" + name for name in archives}

    def setUp(self):
        super().setUp()
        for directory in ("dockers", "rules", "files"):
            (self.root / directory).symlink_to(ROOT / directory, target_is_directory=True)
        (self.root / "scripts/j2cli").symlink_to(ROOT / "scripts/j2cli", target_is_directory=True)
        generator = (ROOT / "rules/functions").read_text().split("define generate_manifest\n", 1)[1].split("endef", 1)[0]
        self.makefile += "\ndefine generate_manifest\n" + generator + "endef\n"
        parents = ("docker-base-trixie", "docker-config-engine-trixie", "docker-swss-layer-trixie", "docker-macsec")
        self.prerequisites = {name: ["target/bazel-manifests/" + parent + "/manifest.json" for parent in parents]
                              for name in self.archives}
        self.prerequisites[self.archives[1]].append("target/bazel-manifests/docker-macsec-dbg/manifest.json")
        self.makefile += """
BUILD_WITH_BAZEL_WHEN_AVAILABLE = y
BLDENV = trixie
CONFIGURED_PLATFORM = vs
CONFIGURED_ARCH = amd64
INCLUDE_FIPS = y
INCLUDE_MACSEC = y
INSTALL_DEBUG_TOOLS = n
DBG_IMAGE_MARK = dbg
DOCKERS_PATH = dockers
include rules/docker-base-trixie.mk
include rules/docker-config-engine-trixie.mk
include rules/docker-swss-layer-trixie.mk
include rules/docker-macsec.mk
include tools/bazel/docker.mk
"""
        # Use the installer's actual export expression, including owner/state fields.
        slave = (ROOT / "slave.mk").read_text()
        local_export = slave.split('\texport sonic_local_packages="', 1)[1].split('"\n', 1)[0]
        self.makefile += '\n.PHONY: local-package\nlocal-package:\n\texport sonic_local_packages="' + local_export + '"\n'
        self.makefile += '\t@printf "local=%s\\n" "$$sonic_local_packages"\n'

    def test_both_local_variants_export_with_real_manifests(self):
        original = self.makefile
        for debug, archive in zip(("n", "y"), self.archives):
            with self.subTest(debug=debug):
                self.makefile = original.replace("INSTALL_DEBUG_TOOLS = n", "INSTALL_DEBUG_TOOLS = " + debug)
                selection = self.run_make(goals=["local-package"])
                self.assert_success(selection)
                # Keep the original local-package metadata, including its debug defaults.
                state = "y" if debug == "n" else ""
                selected = next(line.removeprefix("local=").strip() for line in selection.stdout.splitlines()
                                if line.startswith("local="))
                self.assertEqual(selected, archive + "|target/" + archive + "||" + state)
                before = len(self.recorded_calls())
                self.assert_success(self.run_make(archive))
                calls = self.recorded_calls()[before:]
                self.assertEqual([call["arguments"][0] for call in calls], ["build", "cquery"])
                self.assertTrue(all(call["prerequisites_ready"] for call in calls))
                self.assertTrue(all(call["arguments"][-1] == self.labels[archive] for call in calls))
                self.assertEqual((self.target / archive).read_bytes(), ("new " + archive).encode())
                manifest = json.loads((self.target / "bazel-manifests" / archive.removesuffix(".gz") / "manifest.json").read_text())
                self.assertEqual(manifest["package"]["name"], "macsec")
                self.assertEqual(manifest["package"]["version"], "1.0.0" + ("+dbg" if debug == "y" else ""))
                self.assertEqual(manifest["service"]["name"], "macsec")
                self.assertEqual(manifest["service"]["requires"], ["config-setup"])
                self.assertEqual(manifest["service"]["after"], ["swss", "syncd"])
                self.assertFalse(manifest["container"]["privileged"])
                self.assertEqual(manifest["cli"]["config"], "/cli/config/plugins/macsec.py")

    def test_missing_parent_manifest_still_stops_before_bazel(self):
        self.makefile += "\ndocker-swss-layer-trixie_MANIFEST_IMAGE :=\n"
        result = self.run_make(self.archives[1])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("docker-swss-layer-trixie_MANIFEST_IMAGE is required", result.stderr)
        self.assertEqual(self.recorded_calls(), [])
        for archive in self.archives:
            self.assertEqual((self.target / archive).read_bytes(), b"previous image")


class MacsecLocalInstallerTest(unittest.TestCase):

    def test_existing_local_installer_handles_latest_and_version_tags(self):
        """Execute only the real rendered local-package block with a command recorder."""
        import jinja2

        template = (ROOT / "files/build_templates/sonic_debian_extension.j2").read_text()
        options = "{% macro get_install_options" + template.split("{% macro get_install_options", 1)[1].split("{% endmacro -%}", 1)[0] + "{% endmacro -%}"
        local = "{% for package in sonic_local_packages" + template.split("{% for package in sonic_local_packages", 1)[1].split("{% endfor -%}", 1)[0] + "{% endfor -%}"
        recorder = r'''
set -eu
sudo() {
    case "$*" in
        *" sonic-package-manager install --from-tarball "*) printf 'install %s\n' "$*" ;;
        *" docker images --format "*) printf '%s\n' "$RECORDED_TAG" ;;
        *" docker tag "*) printf 'tag %s\n' "$*" ;;
        *) printf 'unexpected command\n' >&2; return 1 ;;
    esac
}
FILESYSTEM_ROOT=/fixture
DOCKER_HOST=unix:///fixture/docker.sock
SONIC_IMAGE_VERSION=20261104.1
'''
        for image in ("docker-macsec", "docker-macsec-dbg"):
            for existing in ("latest", "20261104.1"):
                with self.subTest(image=image, existing=existing):
                    rendered = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(options + local).render(
                        sonic_local_packages=f"{image}.gz|target/{image}.gz|kube|y")
                    result = subprocess.run(["bash", "-c", recorder + "\nRECORDED_TAG=" + existing + "\n" + rendered],
                                            capture_output=True, text=True, check=True)
                    self.assertIn(f"sonic-package-manager install --from-tarball target/{image}.gz -y -v DEBUG --set-owner kube --enable", result.stdout)
                    source, target = ("latest", "20261104.1") if existing == "latest" else ("20261104.1", "latest")
                    self.assertIn(f"docker tag {image}:{source} {image}:{target}", result.stdout)


if __name__ == "__main__":
    unittest.main()
