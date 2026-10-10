#!/usr/bin/env python3
"""Check syncd-vs selection and package prerequisites without running Bazel."""

import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]


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
LIBSWSSCOMMON = libswsscommon.deb
LIBSAIMETADATA = libsaimetadata.deb
LIBSAIREDIS = libsairedis.deb
LIBSWSSCOMMON_DBG = libswsscommon-dbgsym.deb
LIBSAIMETADATA_DBG = libsaimetadata-dbgsym.deb
LIBSAIREDIS_DBG = libsairedis-dbgsym.deb
LIBSAIVS_DBG = libsaivs-dbgsym.deb
FIPS_OPENSSH_CLIENT = openssh-client_10.0p1-7+fips_amd64.deb
syncd-vs.deb_RDEPENDS = libsairedis.deb libsaimetadata.deb libsaivs.deb libsai.deb
libsairedis.deb_RDEPENDS = libswsscommon.deb
libswsscommon.deb_RDEPENDS = libyang3.deb
libswsscommon-dbgsym.deb_RDEPENDS = libswsscommon.deb
libsairedis-dbgsym.deb_RDEPENDS = libsairedis.deb
libsaimetadata-dbgsym.deb_RDEPENDS = libsaimetadata.deb
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
\t@echo base_debug_debs=$(SYNCD_VS_BAZEL_BASE_DEBUG_DEBS)
\t@echo runtime_packages=$($(DOCKER_SYNCD_BASE)_DEPENDS)
\t@echo runtime_path=$($(DOCKER_SYNCD_BASE)_PATH)
\t@echo debug_path=$($(DOCKER_SYNCD_BASE_DBG)_PATH)
\t@echo run_opt=$($(DOCKER_SYNCD_BASE)_RUN_OPT)
.PHONY: debug_dependencies
debug_dependencies: $($(DOCKER_SYNCD_BASE_DBG)_BAZEL_DEPENDS)
\t@echo complete-debug-inputs
target/debs/trixie/%.deb:
\t@echo fixture-deb $@
target/docker-config-engine-trixie.oci:
\t@echo fixture-base $@
target/bazel-manifests/%/manifest.json:
\t@echo fixture-manifest $@
"""

    def run_make(self, target="selected", *, dry_run=False, make_source=None, **overrides):
        settings = {
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n", "BLDENV": "trixie",
            "CONFIGURED_PLATFORM": "vs", "CONFIGURED_ARCH": "amd64", "DBG_IMAGE_MARK": "dbg",
            "INCLUDE_VS_DASH_SAI": "y", "INCLUDE_FIPS": "y", "ENABLE_ASAN": "n", "ENABLE_SYNCD_RPC": "n",
        }
        settings.update(overrides)
        arguments = ["make", "--no-print-directory", "-f", "-"]
        if dry_run:
            arguments.extend(["--dry-run", "--always-make"])
        return subprocess.run(arguments + [target] + [f"{key}={value}" for key, value in settings.items()],
                              input=make_source if make_source is not None else self.makefile(),
                              text=True, cwd=ROOT, capture_output=True, check=False)

    def values(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_opt_in_uses_oci_and_keeps_the_installer_and_runtime_metadata(self):
        """Selecting Bazel changes the image producer while retaining installer names and service settings."""
        values = self.values(self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y"))
        self.assertEqual(values["bazel"], "docker-syncd-vs.gz")
        self.assertEqual(values["bazel_debug"], "docker-syncd-vs-dbg.gz")
        self.assertEqual(values["runtime_label"], "//dockers/docker-syncd-vs:docker-syncd-vs.gz")
        self.assertEqual(values["debug_label"], "//dockers/docker-syncd-vs:docker-syncd-vs-dbg.gz")
        self.assertEqual(values["runtime_oci"], "//dockers/docker-syncd-vs:docker-syncd-vs")
        self.assertEqual(values["debug_oci"], "//dockers/docker-syncd-vs:docker-syncd-vs-dbg")
        self.assertEqual(values["runtime_inputs"].split(), [
            "target/docker-config-engine-trixie.oci",
            *["target/debs/trixie/" + name for name in values["runtime_debs"].split()],
            "target/bazel-manifests/docker-syncd-vs/manifest.json"])
        self.assertEqual(values["debug_inputs"].split(), values["runtime_inputs"].split() + [
            *["target/debs/trixie/" + name for name in values["debug_debs"].split()],
            "target/debs/trixie/libswsscommon-dbgsym.deb",
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
        """Profiles outside native AMD64 Trixie DASH/FIPS must keep the complete legacy path."""
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
        """Reject misspelled selectors instead of silently choosing a different build producer."""
        for value in ("", "yes", "y n"):
            with self.subTest(value=value):
                result = self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be y or n", result.stderr)

    def test_package_prerequisites_preserve_make_dependencies_except_shared_source_libraries(self):
        """Reusing three source libraries must keep DASH, VS SAI, FIPS and their Make dependencies."""
        values = self.values(self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y"))
        packages = values["runtime_debs"].split()
        self.assertEqual(packages[:2], ["libnl-3-dev.deb", "libnl-route-3-dev.deb"])
        for package in ("syncd-vs", "libsaivs", "libsai", "libyang3", "p4lang-pi", "p4lang-bmv2", "p4lang-p4c"):
            self.assertIn(package + ".deb", packages)
        self.assertLess(packages.index("p4lang-pi.deb"), packages.index("libsai.deb"))
        self.assertLess(packages.index("libsai.deb"), packages.index("syncd-vs.deb"))
        self.assertIn("base-dbgsym.deb", values["debug_debs"].split())
        fips = "openssh-client_10.0p1-7+fips_amd64.deb"
        self.assertEqual(packages.count(fips), 1)
        self.assertNotIn(fips, values["debug_debs"].split())
        # The OCI handoff fixes runtime selection without rewriting legacy rules.
        self.assertNotIn(fips, values["runtime_packages"].split())
        result = self.run_make("debug_dependencies", dry_run=True, BUILD_WITH_BAZEL_WHEN_AVAILABLE="y")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for package in set(values["runtime_debs"].split() + values["debug_debs"].split() +
                           values["base_debug_debs"].split()):
            self.assertEqual(result.stdout.count("fixture-deb target/debs/trixie/" + package + "\n"), 1)
        self.assertIn("fixture-manifest target/bazel-manifests/docker-syncd-vs/manifest.json", result.stdout)
        self.assertIn("fixture-manifest target/bazel-manifests/docker-syncd-vs-dbg/manifest.json", result.stdout)
        self.assertNotIn("prepare_packages.py", result.stdout)
        self.assertNotIn("bazel-inputs/docker-syncd-vs", result.stdout)
        self.assertNotIn("bazel build", result.stdout)

    def test_shared_source_packages_cannot_reenter_through_debug_dependencies(self):
        """Filter both expanded handoffs so Make symbols cannot restore the replaced runtime libraries."""
        values = self.values(self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y"))
        moved = {"libswsscommon", "libsairedis", "libsaimetadata"}
        moved |= {name + "-dbgsym" for name in moved}
        for variant in ("runtime", "debug"):
            self.assertTrue({name + ".deb" for name in moved}.isdisjoint(values[variant + "_debs"].split()))
        self.assertEqual(values["base_debug_debs"].split(), ["libswsscommon-dbgsym.deb"])
        self.assertNotIn("target/debs/trixie/libswsscommon-dbgsym.deb", values["runtime_inputs"].split())
        self.assertEqual(values["debug_inputs"].split().count("target/debs/trixie/libswsscommon-dbgsym.deb"), 1)
        # Expand first, then filter: a moved library's retained libyang dependency
        # must survive, including when reached through a debug-symbol package.
        self.assertIn("libyang3.deb", values["runtime_debs"].split())
        self.assertIn("libyang3.deb", values["debug_debs"].split())
        self.assertIn("libsaivs-dbgsym.deb", values["debug_debs"].split())
        self.assertIn("syncd-vs-dbgsym.deb", values["debug_debs"].split())

    def test_custom_package_directories_keep_the_legacy_producer(self):
        """Bazel cannot select stale default-path DEBs when Make uses a different package directory."""
        for setting in ({"docker-syncd-vs.gz_DEBS_PATH": "custom-debs/trixie"},
                        {"DEBS_PATH": "custom-debs/trixie"}):
            with self.subTest(setting=setting):
                selected = self.values(self.run_make(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y", **setting))
                legacy = self.values(self.run_make(**setting))
                self.assertEqual(selected, legacy)
                self.assertEqual(selected["bazel"], "")
                self.assertEqual(selected["bazel_debug"], "")
                self.assertEqual(selected["runtime_inputs"], "")
                self.assertEqual(selected["debug_inputs"], "")
                self.assertIn("docker-syncd-vs.gz", selected["legacy"].split())

    def test_exact_bazel_exports_follow_real_make_package_versions_and_closure(self):
        """A version or dependency change must update the explicit Bazel input boundary."""
        source = """
DBG_IMAGE_MARK = dbg
PLATFORM_PATH = platform/vs
TARGET_PATH = target
DEBS_PATH = target/debs/trixie
include rules/functions
include rules/libnl3.mk
include rules/libyang3.mk
include rules/swss-common.mk
include rules/p4lang.mk
include rules/dash-sai.mk
include rules/sairedis.mk
include rules/eventd.mk
include rules/sonic-fips.mk
include rules/docker-base-trixie.mk
include rules/docker-config-engine-trixie.mk
include platform/vs/syncd-vs.mk
include platform/vs/docker-syncd-vs.mk
.PHONY: selected
selected:
\t@echo runtime_debs=$(SYNCD_VS_BAZEL_RUNTIME_DEBS)
\t@echo debug_debs=$(SYNCD_VS_BAZEL_DEBUG_DEBS)
\t@echo base_debug_debs=$(SYNCD_VS_BAZEL_BASE_DEBUG_DEBS)
"""
        values = self.values(self.run_make(make_source=source, BUILD_WITH_BAZEL_WHEN_AVAILABLE="y"))
        packages = {"target/debs/trixie/" + name for names in values.values() for name in names.split()}
        self.assertTrue(packages)
        exports = set(re.findall(r'"(target/debs/trixie/[^"*]+\.deb)"', (ROOT / "BUILD.bazel").read_text()))
        self.assertEqual(exports, packages)
        self.assertNotIn("libswsscommon-dbgsym_1.0.0_amd64.deb", values["debug_debs"].split())

    def test_dockerfile_direct_apt_packages_remain_declared(self):
        """The reviewed OCI APT declarations and lock must cover the legacy Dockerfile requests."""
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
