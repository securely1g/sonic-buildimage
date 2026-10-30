"""Exercise P4 Make dependencies and in-session verification without a builder."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
ARCHIVES = ["docker-orchagent.gz", "docker-orchagent-dbg.gz",
            "docker-syncd-vs.gz", "docker-syncd-vs-dbg.gz"]
PACKAGES = ["sonic-p4rt_0.0.1_amd64.deb", "sonic-p4rt-dbgsym_0.0.1_amd64.deb"]
DEB_PATHS = ["target/debs/trixie/" + name for name in PACKAGES]


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
        self.make = self.directory / "make command"
        self.make.write_text("#!" + sys.executable + "\n" +
                            "import json, os, pathlib, sys\n"
                            "with pathlib.Path('make-calls.jsonl').open('a') as output:\n"
                            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n" +
                            "os.execv(" + repr(shutil.which("make")) + ", ['make', *sys.argv[1:]])\n")
        self.make.chmod(0o755)
        self.settings = {
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE": "y", "BLDENV": "trixie", "CONFIGURED_PLATFORM": "vs",
            "CONFIGURED_ARCH": "amd64", "INCLUDE_FIPS": "y", "INCLUDE_VS_DASH_SAI": "y",
            "TARGET_PATH": "target", "DEBS_PATH": "target/debs/$(BLDENV)",
            "SONIC_BAZEL_DOCKER_IMAGES": " ".join(ARCHIVES[::2]),
            "SONIC_BAZEL_DBG_DOCKER_IMAGES": " ".join(ARCHIVES[1::2]),
            "SONIC_P4RT_VERSION": "0.0.1",
            "SONIC_P4RT": "sonic-p4rt_$(SONIC_P4RT_VERSION)_$(CONFIGURED_ARCH).deb",
            "SONIC_P4RT_DBG": "sonic-p4rt-dbgsym_$(SONIC_P4RT_VERSION)_$(CONFIGURED_ARCH).deb",
        }
        (self.directory / "input.source").write_text("first source revision\n")
        (self.directory / "slave.mk").write_text(
            "\n".join(name + " = " + value for name, value in self.settings.items()) + "\n" + r'''
.DEFAULT_GOAL := forbidden-default
.PHONY: forbidden-default
forbidden-default:
	@echo 'The metadata query must not build the default goal' >&2; exit 39
input.ready: input.source
	@cp "$<" "$@"
$(addprefix $(DEBS_PATH)/,$(SONIC_P4RT) $(SONIC_P4RT_DBG)): input.ready
	@mkdir recipe-lock
	@printf '%s\n' '$@' >> producers.log
	@sleep 0.02
	@rmdir recipe-lock
	@test '$@' != '$(FAIL_TARGET)'
	@mkdir -p '$(@D)'
	@if test '$@' = '$(OMIT_OUTPUT)'; then :; \
	elif test '$@' = '$(EMPTY_OUTPUT)'; then : > '$@'; \
	else cp input.ready '$@'; fi
# Existing image archives are validation inputs, never package-stage goals.
$(addprefix target/,docker-orchagent.gz docker-orchagent-dbg.gz docker-syncd-vs.gz docker-syncd-vs-dbg.gz):
	@echo 'Validation must not request container producers' >&2; exit 41
''')
        self.environment = dict(os.environ, BAZEL=str(self.bazel))
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL",
                     "BAZEL_CONTAINER_ARGS", "BAZEL_SWSS_ARGS", "BAZEL_CONTAINER_CACHE_DIR", "BAZEL_SWSS_CACHE_DIR"):
            self.environment.pop(name, None)

    def run_verifier(self, *, settings=None, **environment):
        arguments = [str(self.make), "--no-print-directory", "-j8", "-f", "slave.mk"]
        arguments += [key + "=" + value for key, value in (settings or {}).items()]
        return subprocess.run(
            ["bash", "tools/bazel/ci/verify_vs_oci.sh", *arguments],
            cwd=self.directory, env=dict(self.environment, **environment),
            text=True, capture_output=True, check=False)

    def producers(self):
        path = self.directory / "producers.log"
        return path.read_text().splitlines() if path.exists() else []

    def make_calls(self):
        path = self.directory / "make-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

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

    def test_resolved_package_paths_use_the_passed_make_and_serial_producers(self):
        """A no-op query selects actual Make variables before fresh package builds."""
        settings = {"SONIC_P4RT_VERSION": "7.4.9", "DEBS_PATH": "target/selected-debs"}
        result = self.run_verifier(settings=settings)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = ["target/selected-debs/" + name.replace("0.0.1", "7.4.9") for name in PACKAGES]
        self.assertEqual(self.producers(), expected)
        calls = self.make_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("-", calls[0])
        self.assertNotIn("-", calls[1])
        self.assertIn("-j1", calls[1])
        self.assertEqual(calls[1][-2:], expected)
        for call in calls:
            self.assertIn("SONIC_P4RT_VERSION=7.4.9", call)
            self.assertIn("DEBS_PATH=target/selected-debs", call)
        debug = json.loads((self.directory / "debug-arguments.json").read_text())
        self.assertEqual(debug[:4], ["--runtime-deb", expected[0], "--debug-deb", expected[1]])

    def test_existing_outputs_still_check_prerequisite_freshness(self):
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.producers(), DEB_PATHS)
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.producers(), DEB_PATHS)
        (self.directory / "input.source").write_text("changed source revision\n")
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.producers(), DEB_PATHS * 2)
        for name in DEB_PATHS:
            self.assertEqual((self.directory / name).read_text(), "changed source revision\n")

    def test_build_failure_stops_later_outputs_and_verification(self):
        for index, target in enumerate(DEB_PATHS):
            with self.subTest(target=target):
                shutil.rmtree(self.directory / "target", ignore_errors=True)
                (self.directory / "producers.log").unlink(missing_ok=True)
                result = self.run_verifier(settings={"FAIL_TARGET": target})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.producers(), DEB_PATHS[:index + 1])
                self.assertFalse((self.directory / "bazel-arguments.json").exists())
                self.assertFalse((self.directory / "debug-arguments.json").exists())

    def test_missing_or_empty_package_outputs_stop_before_image_validation(self):
        for mode in ("OMIT_OUTPUT", "EMPTY_OUTPUT"):
            for target in DEB_PATHS:
                with self.subTest(mode=mode, target=target):
                    shutil.rmtree(self.directory / "target", ignore_errors=True)
                    result = self.run_verifier(settings={mode: target})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((self.directory / "bazel-arguments.json").exists())
                    self.assertFalse((self.directory / "debug-arguments.json").exists())

    def test_unsupported_profiles_fail_before_any_producer(self):
        for overrides in ({"BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n"}, {"CONFIGURED_ARCH": "arm64"},
                          {"BLDENV": "bookworm"}, {"INCLUDE_FIPS": "n"}, {"INCLUDE_VS_DASH_SAI": "n"},
                          {"CONFIGURED_PLATFORM": "broadcom"}, {"ENABLE_ASAN": "y"},
                          {"ENABLE_SYNCD_RPC": "y"}, {"CROSS_BUILD_ENVIRON": "y"},
                          {"MULTIARCH_QEMU_ENVIRON": "y"},
                          {"SONIC_BAZEL_DOCKER_IMAGES": "docker-orchagent.gz"},
                          {"SONIC_BAZEL_DBG_DOCKER_IMAGES": "docker-syncd-vs-dbg.gz"},
                          {"SONIC_P4RT": ""}, {"SONIC_P4RT_DBG": ""}):
            with self.subTest(overrides=overrides):
                result = self.run_verifier(settings=overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.producers(), [])
                self.assertFalse((self.directory / "bazel-arguments.json").exists())

    def test_missing_make_command_fails_without_validation(self):
        result = subprocess.run(["bash", "tools/bazel/ci/verify_vs_oci.sh"], cwd=self.directory,
                                env=self.environment, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.producers(), [])
        self.assertFalse((self.directory / "bazel-arguments.json").exists())


if __name__ == "__main__":
    unittest.main()
