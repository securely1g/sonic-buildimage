"""Exercise PMON archive selection through its production Make rule."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[3]
PROFILE = {
    "BLDENV": "trixie", "CONFIGURED_PLATFORM": "vs", "CONFIGURED_ARCH": "amd64",
    "BUILD_WITH_BAZEL_WHEN_AVAILABLE": "y", "INCLUDE_FIPS": "y", "DBG_IMAGE_MARK": "dbg",
}
RECIPE = """include rules/config
PDDF_SUPPORT = $(if $(filter y,$(SONIC_USE_PDDF_FRAMEWORK)),y,n)
include rules/docker-platform-monitor.mk
selection:
	@echo '$(SONIC_BAZEL_DOCKER_IMAGES)' '$(SONIC_BAZEL_DBG_DOCKER_IMAGES)'
	@echo '$(SONIC_INSTALL_DOCKER_IMAGES)' '$(SONIC_INSTALL_DOCKER_DBG_IMAGES)'
	@echo '$(strip $($(DOCKER_PLATFORM_MONITOR)_PYTHON_WHEELS))'
	@echo '$($(DOCKER_PLATFORM_MONITOR)_BAZEL_ARGS)' '$($(DOCKER_PLATFORM_MONITOR_DBG)_BAZEL_ARGS)'
"""


class PmonMakeTest(unittest.TestCase):
    def selection(self, **overrides):
        settings = PROFILE | overrides
        result = subprocess.run(
            ["make", "-sf", "-", "selection", "PDDF_PLATFORM_API_BASE_PY3=pddf.whl",
             *[f"{key}={value}" for key, value in settings.items()]],
            cwd=ROOT, text=True, input=RECIPE, capture_output=True, check=True,
        )
        return [line.split() for line in result.stdout.splitlines()]

    def test_only_supported_profile_switches_both_producers(self):
        images = ["docker-platform-monitor.gz", "docker-platform-monitor-dbg.gz"]
        self.assertEqual(self.selection(), [images, images, ["pddf.whl"], []])
        disabled = "--//dockers/docker-platform-monitor:pddf=false"
        self.assertEqual(self.selection(SONIC_USE_PDDF_FRAMEWORK="n"), [images, images, [], [disabled, disabled]])
        for key, value in {
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n", "CONFIGURED_ARCH": "arm64",
            "CONFIGURED_PLATFORM": "mellanox", "BLDENV": "bookworm", "INCLUDE_FIPS": "n",
            "DBG_IMAGE_MARK": "custom", "CROSS_BUILD_ENVIRON": "y",
            "MULTIARCH_QEMU_ENVIRON": "y", "ENABLE_ASAN": "y",
        }.items():
            with self.subTest(key=key, value=value):
                selected, installed, wheels, options = self.selection(**{key: value})
                self.assertEqual(selected, [])
                debug = f"docker-platform-monitor-{value}.gz" if key == "DBG_IMAGE_MARK" else images[1]
                self.assertEqual(installed, [images[0], debug])
                self.assertEqual(wheels, ["pddf.whl"])
                self.assertEqual(options, [])


if __name__ == "__main__":
    unittest.main()
