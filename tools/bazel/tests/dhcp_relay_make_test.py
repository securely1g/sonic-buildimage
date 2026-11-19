"""Verify the relay archive handoff and the existing local package selector."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]


class RelayMakeTest(unittest.TestCase):
    def select(self, **settings):
        defaults = dict(BLDENV="trixie", CONFIGURED_PLATFORM="vs", CONFIGURED_ARCH="amd64",
                        INCLUDE_FIPS="y", INCLUDE_DHCP_RELAY="y", DBG_IMAGE_MARK="dbg",
                        BUILD_WITH_BAZEL_WHEN_AVAILABLE="y")
        recipe = """include rules/docker-dhcp-relay.mk
select:
	@echo '$(SONIC_BAZEL_DOCKER_IMAGES)|$(SONIC_BAZEL_DBG_DOCKER_IMAGES)|$(SONIC_PACKAGES_LOCAL)'
"""
        return subprocess.check_output(
            ["make", "--no-print-directory", "-f", "-", "select",
             *[f"{k}={v}" for k, v in (defaults | settings).items()]],
            input=recipe, cwd=ROOT, text=True).strip().split("|")

    def test_runtime_or_debug_installs_as_a_local_package(self):
        for install_debug, package in [("n", "docker-dhcp-relay.gz"),
                                       ("y", "docker-dhcp-relay-dbg.gz")]:
            with self.subTest(install_debug=install_debug):
                self.assertEqual(self.select(INSTALL_DEBUG_TOOLS=install_debug),
                                 ["docker-dhcp-relay.gz", "docker-dhcp-relay-dbg.gz", package])
        self.assertEqual(self.select(INCLUDE_DHCP_RELAY="n")[-1], "")

    def test_legacy_profiles(self):
        for key, value in [("BUILD_WITH_BAZEL_WHEN_AVAILABLE", "n"), ("CONFIGURED_ARCH", "arm64"),
                           ("CONFIGURED_PLATFORM", "broadcom"), ("INCLUDE_FIPS", "n"),
                           ("ENABLE_ASAN", "y"), ("CROSS_BUILD_ENVIRON", "y"),
                           ("MULTIARCH_QEMU_ENVIRON", "y"), ("BLDENV", "bookworm")]:
            with self.subTest(key=key):
                self.assertEqual(self.select(**{key: value}), ["", "", "docker-dhcp-relay.gz"])


if __name__ == "__main__":
    unittest.main()
