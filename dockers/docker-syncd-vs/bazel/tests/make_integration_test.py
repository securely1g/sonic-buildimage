#!/usr/bin/env python3
"""Check syncd-vs selection and package prerequisites without running Bazel."""

import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[4]


class MakeIntegrationTest(unittest.TestCase):
    def makefile(self):
        expansion = next(line for line in (ROOT / "rules/functions").read_text().splitlines()
                         if line.startswith("expand ="))
        return expansion + "\n" + """
DBG_IMAGE_MARK = dbg
PLATFORM_PATH = platform/vs
TARGET_PATH = target
DEBS_PATH = target/debs/trixie
DOCKER_CONFIG_ENGINE_TRIXIE = docker-config-engine-trixie.gz
docker-config-engine-trixie.gz_DBG_DEPENDS = base-dbgsym.deb $(FIPS_OPENSSH_CLIENT)
docker-config-engine-trixie.gz_DBG_IMAGE_PACKAGES = gdb gdbserver vim sshpass strace
SYNCD_VS = syncd-vs.deb
SYNCD_VS_DBG = syncd-vs-dbgsym.deb
LIBNL3_DEV = libnl-3-dev.deb
LIBNL_ROUTE3_DEV = libnl-route-3-dev.deb
LIBNL3 = libnl-3-200.deb
LIBSWSSCOMMON_DBG = libswsscommon-dbgsym.deb
LIBSAIMETADATA_DBG = libsaimetadata-dbgsym.deb
LIBSAIREDIS_DBG = libsairedis-dbgsym.deb
LIBSAIVS_DBG = libsaivs-dbgsym.deb
FIPS_OPENSSH_CLIENT = openssh-client_10.0p1-7+fips_amd64.deb
syncd-vs.deb_RDEPENDS = libsairedis.deb libsaimetadata.deb libsaivs.deb libsai.deb
libsairedis.deb_RDEPENDS = libswsscommon.deb
libsai.deb_RDEPENDS = p4lang-pi.deb p4lang-bmv2.deb p4lang-p4c.deb
syncd-vs-dbgsym.deb_RDEPENDS = syncd-vs.deb
SONIC_DOCKER_IMAGES = docker-other.gz
include platform/vs/docker-syncd-vs.mk
.PHONY: selected
selected:
\t@echo bazel=$(SONIC_BAZEL_DOCKER_IMAGES)
\t@echo bazel_debug=$(SONIC_BAZEL_DBG_DOCKER_IMAGES)
\t@echo legacy=$(filter-out $(SONIC_BAZEL_DOCKER_IMAGES),$(SONIC_DOCKER_IMAGES))
\t@echo debug=$(filter-out $(SONIC_BAZEL_DBG_DOCKER_IMAGES),$(SONIC_DOCKER_DBG_IMAGES))
\t@echo install=$(SONIC_INSTALL_DOCKER_IMAGES)
\t@echo install_debug=$(SONIC_INSTALL_DOCKER_DBG_IMAGES)
\t@echo switchable=$(SONIC_BAZEL_SWITCHABLE_IMAGES)
\t@echo oci_bases=$(SONIC_BAZEL_OCI_BASES)
\t@echo oci_archive=$(docker-config-engine-trixie.oci_OCI_ARCHIVE)
\t@echo oci_platform=$(docker-config-engine-trixie.oci_OCI_PLATFORM)
\t@echo manifests=$(SONIC_BAZEL_MANIFESTS)
\t@echo runtime_label=$($(DOCKER_SYNCD_BASE)_BAZEL_TARGET)
\t@echo debug_label=$($(DOCKER_SYNCD_BASE_DBG)_BAZEL_TARGET)
\t@echo runtime_oci=$($(DOCKER_SYNCD_BASE)_BAZEL_OCI_TARGET)
\t@echo debug_oci=$($(DOCKER_SYNCD_BASE_DBG)_BAZEL_OCI_TARGET)
\t@echo runtime_inputs=$($(DOCKER_SYNCD_BASE)_BAZEL_DEPENDS)
\t@echo debug_inputs=$($(DOCKER_SYNCD_BASE_DBG)_BAZEL_DEPENDS)
\t@echo runtime_debs=$(SYNCD_VS_BAZEL_RUNTIME_DEBS)
\t@echo debug_debs=$(SYNCD_VS_BAZEL_DEBUG_DEBS)
\t@echo runtime_required=$(SYNCD_VS_BAZEL_RUNTIME_REQUIRED)
\t@echo debug_required=$(SYNCD_VS_BAZEL_DEBUG_REQUIRED)
\t@echo runtime_packages=$($(DOCKER_SYNCD_BASE)_DEPENDS)
\t@echo runtime_path=$($(DOCKER_SYNCD_BASE)_PATH)
\t@echo debug_path=$($(DOCKER_SYNCD_BASE_DBG)_PATH)
\t@echo run_opt=$($(DOCKER_SYNCD_BASE)_RUN_OPT)
target/debs/trixie/%.deb:
\t@echo fixture-deb $@
"""

    def run_make(self, target="selected", *, dry_run=False, **overrides):
        settings = {
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n", "BLDENV": "trixie",
            "CONFIGURED_PLATFORM": "vs", "CONFIGURED_ARCH": "amd64", "DBG_IMAGE_MARK": "dbg",
            "INCLUDE_VS_DASH_SAI": "y", "INCLUDE_FIPS": "y", "ENABLE_ASAN": "n", "ENABLE_SYNCD_RPC": "n",
        }
        settings.update(overrides)
        arguments = ["make", "--no-print-directory", "-f", "-"]
        if dry_run:
            arguments.append("--dry-run")
        return subprocess.run(arguments + [target] + [f"{key}={value}" for key, value in settings.items()],
                              input=self.makefile(), text=True, cwd=ROOT, capture_output=True, check=False)

    def values(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_opt_in_uses_oci_and_keeps_the_installer_and_runtime_metadata(self):
        values = self.values(self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y"))
        self.assertEqual(values["bazel"], "docker-syncd-vs.gz")
        self.assertEqual(values["bazel_debug"], "docker-syncd-vs-dbg.gz")
        self.assertEqual(values["runtime_label"], "//dockers/docker-syncd-vs:docker-syncd-vs.gz")
        self.assertEqual(values["debug_label"], "//dockers/docker-syncd-vs:docker-syncd-vs-dbg.gz")
        self.assertEqual(values["runtime_oci"], "//dockers/docker-syncd-vs:docker-syncd-vs")
        self.assertEqual(values["debug_oci"], "//dockers/docker-syncd-vs:docker-syncd-vs-dbg")
        self.assertEqual(values["runtime_inputs"].split(), [
            "target/docker-config-engine-trixie.oci",
            "target/bazel-inputs/docker-syncd-vs/runtime/manifest.json",
            "target/bazel-inputs/docker-syncd-vs/runtime/payload.tar",
            "target/bazel-manifests/docker-syncd-vs/manifest.json"])
        self.assertEqual(values["debug_inputs"].split(), values["runtime_inputs"].split() + [
            "target/bazel-inputs/docker-syncd-vs/debug/manifest.json",
            "target/bazel-inputs/docker-syncd-vs/debug/payload.tar",
            "target/bazel-manifests/docker-syncd-vs-dbg/manifest.json"])
        self.assertEqual(values["manifests"].split(), ["docker-syncd-vs", "docker-syncd-vs-dbg"])
        self.assertEqual(values["oci_bases"], "docker-config-engine-trixie.oci")
        self.assertEqual(values["oci_archive"], "target/docker-config-engine-trixie.gz")
        self.assertEqual(values["oci_platform"], "linux/amd64")
        self.assertEqual(values["legacy"], "docker-other.gz")
        self.assertEqual(values["debug"], "")
        self.assertEqual(values["install"], "docker-syncd-vs.gz")
        self.assertEqual(values["install_debug"], "docker-syncd-vs-dbg.gz")
        self.assertEqual(values["runtime_packages"].split(), ["syncd-vs.deb", "libnl-3-dev.deb", "libnl-3-200.deb"])
        self.assertEqual(values["runtime_path"], "platform/vs/docker-syncd-vs")
        self.assertEqual(values["debug_path"], values["runtime_path"])
        self.assertIn("--privileged", values["run_opt"])
        self.assertIn("--cap-add=SYS_RAWIO", values["run_opt"])
        self.assertIn("-v /etc/sonic:/etc/sonic:ro", values["run_opt"])

    def test_default_and_unsupported_configurations_keep_make(self):
        default = self.values(self.run_make())
        self.assertEqual(default["bazel"], "")
        self.assertEqual(default["switchable"].split(), ["docker-syncd-vs.gz", "docker-syncd-vs-dbg.gz"])
        for setting in (
            {"BLDENV": "bookworm"}, {"CONFIGURED_PLATFORM": "mellanox"}, {"CONFIGURED_ARCH": "arm64"},
            {"CONFIGURED_ARCH": "armhf"}, {"DBG_IMAGE_MARK": "debug"}, {"INCLUDE_VS_DASH_SAI": "n"},
            {"INCLUDE_FIPS": "n"}, {"ENABLE_ASAN": "y"}, {"ENABLE_SYNCD_RPC": "y"},
            {"CROSS_BUILD_ENVIRON": "y"}, {"MULTIARCH_QEMU_ENVIRON": "y"},
            {"SONIC_BUILD_TARGET": "target/docker-sonic-vs.gz"}, {"EXTRA_DOCKER_TARGETS": "docker-sonic-vs"},
        ):
            with self.subTest(setting=setting):
                selected = self.values(self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y", **setting))
                legacy = self.values(self.run_make(**setting))
                self.assertEqual(selected, legacy)
                self.assertEqual(selected["bazel"], "")
                self.assertEqual(selected["bazel_debug"], "")
                self.assertEqual(selected["oci_bases"], "")

    def test_invalid_selector_is_rejected(self):
        for value in ("", "yes", "y n"):
            with self.subTest(value=value):
                result = self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be y or n", result.stderr)

    def test_package_prerequisites_follow_the_legacy_runtime_expansion(self):
        values = self.values(self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y"))
        packages = values["runtime_debs"].split()
        self.assertEqual(packages[:2], ["libnl-3-dev.deb", "libnl-route-3-dev.deb"])
        for package in ("syncd-vs", "libsairedis", "libsaimetadata", "libsaivs", "libswsscommon",
                        "libsai", "p4lang-pi", "p4lang-bmv2", "p4lang-p4c"):
            self.assertIn(package + ".deb", packages)
        self.assertLess(packages.index("p4lang-pi.deb"), packages.index("libsai.deb"))
        self.assertLess(packages.index("libsai.deb"), packages.index("syncd-vs.deb"))
        self.assertIn("base-dbgsym.deb", values["debug_debs"].split())
        fips = "openssh-client_10.0p1-7+fips_amd64.deb"
        self.assertEqual(packages.count(fips), 1)
        self.assertNotIn(fips, values["debug_debs"].split())
        self.assertIn("openssh-client", values["runtime_required"].split())
        self.assertNotIn("openssh-client", values["debug_required"].split())
        # The OCI handoff fixes runtime selection without rewriting legacy rules.
        self.assertNotIn(fips, values["runtime_packages"].split())
        result = self.run_make("target/bazel-inputs/docker-syncd-vs/debug/payload.tar", dry_run=True,
                               BUILD_WITH_BAZEL_WHEN_AVAILABLE="y")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("python3 dockers/docker-syncd-vs/bazel/prepare_packages.py"), 2)
        self.assertIn("--runtime-manifest target/bazel-inputs/docker-syncd-vs/runtime/manifest.json", result.stdout)
        self.assertIn("--package target/debs/trixie/libnl-route-3-dev.deb", result.stdout)
        self.assertEqual(result.stdout.count("--package target/debs/trixie/" + fips), 1)
        self.assertIn('test -s "target/bazel-inputs/docker-syncd-vs/debug/payload.tar"', result.stdout)
        self.assertNotIn("bazel build", result.stdout)

    def test_dockerfile_direct_apt_packages_remain_declared(self):
        dockerfile = (ROOT / "platform/vs/docker-syncd-vs/Dockerfile.j2").read_text()
        packages = set()
        for line in dockerfile.splitlines():
            if line.startswith("RUN apt-get install -f -y "):
                packages.update(line.split()[5:])
        concrete = {"libgrpc++1.51": "libgrpc++1.51t64", "libgrpc29": "libgrpc29t64",
                    "libprotobuf32": "libprotobuf32t64", "libpcap0.8": "libpcap0.8t64"}
        packages = {concrete.get(name, name) for name in packages}
        module = (ROOT / "MODULE.bazel").read_text()
        self.assertIn('include("//dockers/docker-syncd-vs/bazel:apt_inputs.MODULE.bazel")', module)
        declarations = (ROOT / "dockers/docker-syncd-vs/bazel/apt_inputs.MODULE.bazel").read_text()
        block = declarations.split('dependency_set = "syncd_vs_debian"', 1)[1].split("suites =", 1)[0]
        declared = set(re.findall(r'"([^" ]+) \(= [^\)]+\) \[amd64\]"', block))
        providers = {"libc-ares2": "libcares2", "pkg-config": "pkgconf"}
        packages = {providers.get(name, name) for name in packages}
        self.assertTrue(packages.issubset(declared), "Dockerfile APT packages missing from OCI inputs: " +
                        repr(sorted(packages - declared)))
        lock = json.loads((ROOT / "dockers/docker-syncd-vs/bazel/apt.lock.json").read_text())
        roots = lock["dependency_sets"]["syncd_vs_debian"]["sets"]["amd64"]
        locked = {key.rsplit("/", 1)[1].split(":", 1)[0] for key in roots}
        providers = {"libc-ares2": "libcares2", "pkg-config": "pkgconf"}
        concrete_packages = {providers.get(name, name) for name in packages}
        self.assertTrue(concrete_packages.issubset(locked), "Dockerfile APT packages missing from canonical lock: " +
                        repr(sorted(concrete_packages - locked)))


if __name__ == "__main__":
    unittest.main()
