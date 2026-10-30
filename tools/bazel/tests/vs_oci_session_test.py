"""Exercise the VS aggregate and in-session verification without a builder."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
ARCHIVES = ["docker-orchagent.gz", "docker-orchagent-dbg.gz",
            "docker-syncd-vs.gz", "docker-syncd-vs-dbg.gz"]
PACKAGES = ["sonic-p4rt_0.0.1_amd64.deb", "sonic-p4rt-dbgsym_0.0.1_amd64.deb"]
DEB_PATHS = ["target/debs/trixie/" + name for name in PACKAGES]
OUTPUTS = DEB_PATHS + ["target/" + name for name in ARCHIVES[1:]]


class VsOciSessionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "tools/bazel/ci").mkdir(parents=True)
        for source in ("tools/bazel/ci/verify_vs_oci.sh", "tools/bazel/build_helpers.py"):
            shutil.copyfile(ROOT / source, self.directory / source)
        (self.directory / "tools/bazel/ci/verify_p4rt_debug.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path('debug-arguments.json').write_text(json.dumps(sys.argv[1:]))\n"
            "print('debug verification output')\n"
            "sys.exit(int(os.environ.get('DEBUG_STATUS', '0')))\n")
        self.bazel = self.directory / "bazel"
        self.bazel.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path('bazel-arguments.json').write_text(json.dumps(sys.argv[1:]))
output = pathlib.Path('bazel-testlogs/tools/bazel/tests/syncd_p4_images_test')
for name in ('test.log', 'test.xml', 'test.outputs/validation.json'):
    target = output / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name)
print('image verification output')
sys.exit(int(os.environ.get('IMAGE_STATUS', '0')))
""")
        self.bazel.chmod(0o755)
        self.environment = dict(os.environ, BAZEL=str(self.bazel))
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL",
                     "BAZEL_CONTAINER_ARGS", "BAZEL_SWSS_ARGS", "BAZEL_CONTAINER_CACHE_DIR", "BAZEL_SWSS_CACHE_DIR"):
            self.environment.pop(name, None)

    def run_verifier(self, **environment):
        return subprocess.run(
            ["bash", "tools/bazel/ci/verify_vs_oci.sh", *DEB_PATHS],
            cwd=self.directory, env=dict(self.environment, **environment),
            text=True, capture_output=True, check=False)

    def test_verification_keeps_archive_options_logs_and_deb_inputs(self):
        cache = self.directory / "cache with spaces"
        result = self.run_verifier(BAZEL_CONTAINER_CACHE_DIR=str(cache),
                                   BAZEL_CONTAINER_ARGS="--distdir='/input with spaces' --jobs=3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = json.loads((self.directory / "bazel-arguments.json").read_text())
        self.assertEqual(arguments, [
            "test", f"--repository_cache={cache}/repository_cache", f"--disk_cache={cache}/disk_cache",
            "--distdir=/input with spaces", "--jobs=3", "--config=syncd-dash",
            "--platforms=@sonic_build_infra//platforms:x86_64_trixie",
            "--host_platform=@sonic_build_infra//platforms:x86_64_trixie",
            "//tools/bazel/tests:syncd_p4_images_test", "--nocache_test_results", "--nozip_undeclared_test_outputs"])
        for name in ("test.log", "test.xml", "validation.json"):
            self.assertTrue((self.directory / "artifacts/vs/syncd-p4-images" / name).is_file())
        self.assertIn("image verification output", (self.directory / "artifacts/vs/syncd-p4-images.log").read_text())
        debug = json.loads((self.directory / "debug-arguments.json").read_text())
        self.assertEqual(debug[:4], ["--runtime-deb", DEB_PATHS[0], "--debug-deb", DEB_PATHS[1]])
        self.assertIn("debug verification output", (self.directory / "artifacts/vs/p4rt-debug-verification.log").read_text())

    def test_image_test_failure_retains_evidence_and_stops_debug_verification(self):
        result = self.run_verifier(IMAGE_STATUS="7")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertTrue((self.directory / "artifacts/vs/syncd-p4-images/test.xml").is_file())
        self.assertFalse((self.directory / "debug-arguments.json").exists())

    def test_debug_verification_failure_is_not_hidden_by_tee(self):
        result = self.run_verifier(DEBUG_STATUS="9")
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)

    def run_make(self, *, goal="bazel-vs-oci", **overrides):
        settings = {
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE": "y", "BLDENV": "trixie", "CONFIGURED_PLATFORM": "vs",
            "CONFIGURED_ARCH": "amd64", "INCLUDE_FIPS": "y", "INCLUDE_VS_DASH_SAI": "y",
            "TARGET_PATH": "target", "DEBS_PATH": "target/debs/trixie",
            "DOCKER_ORCHAGENT": ARCHIVES[0], "DOCKER_ORCHAGENT_DBG": ARCHIVES[1],
            "DOCKER_SYNCD_BASE": ARCHIVES[2], "DOCKER_SYNCD_BASE_DBG": ARCHIVES[3],
            "SONIC_BAZEL_DOCKER_IMAGES": " ".join(ARCHIVES[::2]),
            "SONIC_BAZEL_DBG_DOCKER_IMAGES": " ".join(ARCHIVES[1::2]),
            "SONIC_P4RT": PACKAGES[0], "SONIC_P4RT_DBG": PACKAGES[1],
        }
        settings.update(overrides)
        # Concurrent prerequisites would fail the lock acquisition. This checks
        # Make's actual scheduling, including an inherited parallel jobserver.
        recipe = f"include {ROOT}/tools/bazel/vs_oci.mk\n.PHONY: {' '.join(OUTPUTS)}\n"
        recipe += " ".join(OUTPUTS) + ":\n"
        recipe += "\t@mkdir recipe-lock && printf '%s\\n' '$@' >> producers.log && sleep 0.01 && rmdir recipe-lock\n"
        recipe += "\t@test '$@' != '$(FAIL_TARGET)'\n"
        return subprocess.run(["make", "--no-print-directory", "-j8", "-f", "-", goal,
                               *[key + "=" + value for key, value in settings.items()]],
                              input=recipe, cwd=self.directory, env=self.environment,
                              text=True, capture_output=True, check=False)

    def test_aggregate_checks_each_artifact_in_order_before_verification(self):
        for repetition in range(2):
            result = self.run_make()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((self.directory / "producers.log").read_text().splitlines(), OUTPUTS * (repetition + 1))
        self.assertTrue((self.directory / "debug-arguments.json").is_file())

    def test_verification_only_builds_p4_packages_without_requesting_archives(self):
        result = self.run_make(goal="bazel-vs-oci-verify")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.directory / "producers.log").read_text().splitlines(), DEB_PATHS)
        self.assertTrue((self.directory / "debug-arguments.json").is_file())

    def test_build_failure_stops_later_outputs_and_verification(self):
        result = self.run_make(FAIL_TARGET=OUTPUTS[1])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.directory / "producers.log").read_text().splitlines(), OUTPUTS[:2])
        self.assertFalse((self.directory / "bazel-arguments.json").exists())

    def test_unsupported_profiles_fail_before_any_producer(self):
        for overrides in ({"BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n"}, {"CONFIGURED_ARCH": "arm64"},
                          {"BLDENV": "bookworm"}, {"INCLUDE_FIPS": "n"}, {"INCLUDE_VS_DASH_SAI": "n"},
                          {"ENABLE_ASAN": "y"}, {"CROSS_BUILD_ENVIRON": "y"},
                          {"SONIC_BAZEL_DOCKER_IMAGES": "docker-orchagent.gz"},
                          {"SONIC_BAZEL_DBG_DOCKER_IMAGES": "docker-syncd-vs-dbg.gz"}):
            with self.subTest(overrides=overrides):
                result = self.run_make(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("bazel-vs-oci", result.stderr)
                self.assertFalse((self.directory / "producers.log").exists())


if __name__ == "__main__":
    unittest.main()
