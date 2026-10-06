"""Check sysmgr and SWSS coexist through the actual maintained Make bridge."""

import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]

class SysmgrMakeIntegrationTest(unittest.TestCase):
    def selection(self, **overrides):
        settings = dict(BUILD_WITH_BAZEL_WHEN_AVAILABLE="y", BLDENV="trixie",
                        CONFIGURED_ARCH="amd64", CONFIGURED_PLATFORM="vs", ENABLE_ASAN="n")
        settings.update(overrides)
        source = """
DBG_IMAGE_MARK = dbg
DOCKERS_PATH = dockers
TARGET_PATH = target
DOCKER_CONFIG_ENGINE_TRIXIE = docker-config-engine-trixie.gz
SYSMGR = sysmgr.deb
SYSMGR_DBG = sysmgr-dbgsym.deb
include rules/docker-sysmgr.mk
include rules/docker-orchagent.mk
include tools/bazel/docker.mk
.PHONY: selected
selected:
	@echo images=$(SONIC_BAZEL_DOCKER_IMAGES)
	@echo target=$($(DOCKER_SYSMGR)_BAZEL_TARGET)
	@echo inputs=$($(DOCKER_SYSMGR)_BAZEL_DEPENDS)
	@echo debug_path=$($(DOCKER_SYSMGR_DBG)_PATH)
	@echo packages=$($(DOCKER_SYSMGR)_DEPENDS)
	@echo bases=$(sort $(SONIC_BAZEL_OCI_BASES))
"""
        result = subprocess.run(["make", "--no-print-directory", "-f", "-", "selected"] +
                                [f"{key}={value}" for key,value in settings.items()],
                                input=source, text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=",1) for line in result.stdout.splitlines())

    def test_both_containers_share_prepared_base(self):
        values = self.selection()
        self.assertEqual(values["images"].split(), ["docker-sysmgr.gz", "docker-orchagent.gz"])
        self.assertEqual(values["target"], "//dockers/docker-sysmgr:docker-sysmgr.gz")
        self.assertEqual(values["inputs"], "target/docker-config-engine-trixie.oci")
        self.assertEqual(values["bases"], "docker-config-engine-trixie.oci")
        self.assertEqual(values["debug_path"], "dockers/docker-sysmgr")
        self.assertEqual(values["packages"], "sysmgr.deb")

    def test_unsupported_or_disabled_configuration_keeps_make(self):
        for settings in [dict(BUILD_WITH_BAZEL_WHEN_AVAILABLE="n"), dict(BLDENV="bookworm"),
                         dict(CONFIGURED_ARCH="arm64"), dict(ENABLE_ASAN="y"),
                         dict(CROSS_BUILD_ENVIRON="y"),dict(MULTIARCH_QEMU_ENVIRON="y")]:
            with self.subTest(settings=settings):
                values=self.selection(**settings)
                self.assertNotIn("docker-sysmgr.gz", values["images"].split())
                self.assertEqual(values["packages"], "sysmgr.deb")

if __name__ == "__main__":
    unittest.main()
