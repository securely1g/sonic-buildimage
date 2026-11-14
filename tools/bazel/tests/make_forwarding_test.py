#!/usr/bin/env python3
"""Exercise the real outer Makefile without starting Docker or a builder."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = (
    "target/docker-orchagent.gz",
    "target/docker-orchagent-dbg.gz",
    "target/sonic-vs.img.gz",
    "target/sonic-vs.raw",
)
VS_INSTALLER = "target/sonic-vs.bin"


class MakeForwardingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "target").mkdir()
        (self.directory / "scripts").mkdir()
        (self.directory / "scripts/run_with_retry").symlink_to(ROOT / "scripts/run_with_retry")
        (self.directory / "Makefile").symlink_to(ROOT / "Makefile")
        publisher = self.directory / "tools/bazel/image/build.py"
        publisher.parent.mkdir(parents=True)
        publisher.symlink_to(ROOT / "tools/bazel/image/build.py")
        self.log = self.directory / "forwarded.log"
        targets = " ".join((*ARTIFACTS, VS_INSTALLER, "jessie", "bookworm", "docker-cleanup"))
        # This controlled inner Make records dispatch only. Its phony targets
        # ensure it never masks a successful handoff from the real outer file.
        (self.directory / "Makefile.work").write_text(f""".PHONY: {targets}
{targets}:
	@printf '%s|%s|%s|%s\\n' '$@' '$(BUILD_WITH_BAZEL_WHEN_AVAILABLE)' '$(BLDENV)' '$(EXTRA_DOCKER_TARGETS)' >> forwarded.log
	@test '$@' != '$(FAIL_TARGET)'
	@if test '$@' = '{VS_INSTALLER}' && ! test -e '$@'; then printf 'native installer' > '$@'; fi
""")
        for target in (*ARTIFACTS, VS_INSTALLER):
            (self.directory / target).write_bytes(b"previously built artifact")

    def make(self, *targets, mode="y", **settings):
        self.log.unlink(missing_ok=True)
        environment = dict(os.environ)
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL"):
            environment.pop(name, None)
        environment["SONIC_BUILD_RETRY_COUNT"] = "0"
        options = {
            "NOJESSIE": "1", "NOSTRETCH": "1", "NOBUSTER": "1",
            "NOBULLSEYE": "1", "NOBOOKWORM": "1", "NOTRIXIE": "0",
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE": mode,
        }
        options.update(settings)
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), *targets]
            + [f"{key}={value}" for key, value in options.items()],
            cwd=self.directory, env=environment, text=True, capture_output=True,
            check=False,
        )
        calls = [line.split("|") for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def state(self, target):
        path = self.directory / target
        return path.read_bytes(), path.stat().st_mtime_ns

    def test_existing_outputs_reach_inner_make_in_both_modes_and_on_repeats(self):
        """Let the inner build check every existing SWSS/VS output, including unchanged builds
        and both builder-switch directions, without touching those outputs in the wrapper.
        """
        for target in ARTIFACTS:
            before = self.state(target)
            for mode in ("y", "y", "n", "n", "y"):
                with self.subTest(target=target, mode=mode):
                    result, calls = self.make(target, mode=mode)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(calls, [[target, mode, "trixie", ""],
                                             ["docker-cleanup", mode, "bookworm", ""]])
                    self.assertEqual(self.state(target), before)

    def test_missing_output_retains_implicit_forwarding_recipe(self):
        """Keep first-time artifact requests using the existing wrapper recipe as well as
        requests for files already on disk.
        """
        for target in ARTIFACTS:
            with self.subTest(target=target):
                (self.directory / target).unlink()
                result, calls = self.make(target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls[0], [target, "y", "trixie", ""])

    def test_vs_native_publisher_forwards_missing_and_retained_installers(self):
        """The native publisher must reach inner Make on every invocation."""
        (self.directory / VS_INSTALLER).unlink()
        previous = None
        for _ in range(3):
            result, calls = self.make(VS_INSTALLER, mode="n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(calls, [[VS_INSTALLER, "n", "trixie", ""],
                                     ["docker-cleanup", "n", "bookworm", ""]])
            current = self.state(VS_INSTALLER)
            self.assertEqual(current[0], b"native installer")
            if previous is not None:
                self.assertEqual(current, previous)
            previous = current

    def test_vs_bazel_publisher_requires_inputs_even_with_retained_installer(self):
        """A retained file cannot bypass declared inputs or fall back to native Make."""
        previous = self.state(VS_INSTALLER)
        for retained in (True, True, False):
            with self.subTest(retained=retained):
                if not retained:
                    (self.directory / VS_INSTALLER).unlink()
                result, calls = self.make(VS_INSTALLER, SECURE_UPGRADE_MODE="no_sign")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('build.py --build-with-bazel-when-available "y"', result.stdout)
                self.assertIn("native VS input bundle", result.stderr)
                self.assertEqual(calls, [])
                if retained:
                    self.assertEqual(self.state(VS_INSTALLER), previous)
                else:
                    self.assertFalse((self.directory / VS_INSTALLER).exists())

    def test_vs_selector_exports_literal_installer_options_to_the_publisher(self):
        publisher = self.directory / "tools/bazel/image/build.py"
        publisher.unlink()
        publisher.write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "names = ('INCLUDE_FIPS', 'ENABLE_FIPS', 'EXTRA_CMDLINE_LINUX')\n"
            "Path('request.json').write_text(json.dumps({name: os.environ.get('BAZEL_REQUESTED_' + name) for name in names}))\n"
        )
        options = {"INCLUDE_FIPS": "y", "ENABLE_FIPS": "n",
                   "EXTRA_CMDLINE_LINUX": '"console=ttyS0,115200n8 quiet"'}
        result, calls = self.make(VS_INSTALLER, SECURE_UPGRADE_MODE="no_sign", **options)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(json.loads((self.directory / "request.json").read_text()), options)

    def test_capture_request_overrides_inner_config_without_changing_native_forwarding(self):
        source = (ROOT / "slave.mk").read_text()
        start = source.index("# Preserve the outer capture request after config.user is read.")
        block = source[start:source.index("export ENABLE_DIALOUT", start)]
        makefile = self.directory / "capture-options.mk"
        makefile.write_text(
            'EXTRA_CMDLINE_LINUX := "console=ttyS0 quiet"\n' + block +
            ".PHONY: bazel-vs-native-inputs native\n"
            "bazel-vs-native-inputs native:\n"
            "\t@python3 -c 'import json, os; print(json.dumps(os.environ.get(\"EXTRA_CMDLINE_LINUX\")))'\n"
        )
        environment = dict(os.environ)
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL",
                     "EXTRA_CMDLINE_LINUX", "BAZEL_CAPTURE_EXTRA_CMDLINE_LINUX"):
            environment.pop(name, None)
        selected = '"console=ttyS1 quiet"'
        for goal, captured, expected in (
                ("bazel-vs-native-inputs", selected, selected),
                ("bazel-vs-native-inputs", None, '"console=ttyS0 quiet"'),
                ("native", selected, None)):
            with self.subTest(goal=goal, captured=captured):
                current = dict(environment)
                if captured is not None:
                    current["BAZEL_CAPTURE_EXTRA_CMDLINE_LINUX"] = captured
                result = subprocess.run(["make", "--no-print-directory", "-f", str(makefile), goal],
                                        cwd=self.directory, env=current, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_kvm_native_assembly_uses_the_shared_installer_transition(self):
        source = (ROOT / "build_image.sh").read_text()
        prologue = source[:source.index("image_password=$PASSWORD")]
        (self.directory / "build_image.sh").write_text(prologue + """
set -e
test "$SONIC_BAZEL_NATIVE_PUBLISH" = y
python3 - <<'PY'
import fcntl
from pathlib import Path
with Path('target/.bazel-vs-image.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise SystemExit('native KVM assembly did not hold the publisher lock')
PY
printf '%s' "$BUILD_WITH_BAZEL_WHEN_AVAILABLE" > component-selector
printf 'native installer' > target/sonic-vs.bin
if [ "${FAIL_ASSEMBLY:-n}" = y ]; then exit 1; fi
printf 'native KVM image' > target/sonic-vs.img.gz
""")
        makefile = self.directory / "kvm-writer.mk"
        makefile.write_text("export BUILD_WITH_BAZEL_WHEN_AVAILABLE\n"
                            ".PHONY: target/sonic-vs.img.gz\n"
                            "target/sonic-vs.img.gz:\n"
                            "\tTARGET_MACHINE=vs IMAGE_TYPE=kvm bash build_image.sh\n")
        environment = dict(os.environ)
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL", "SONIC_BAZEL_NATIVE_PUBLISH"):
            environment.pop(name, None)

        def build(*options):
            return subprocess.run(["make", "--no-print-directory", "-f", str(makefile),
                                   "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", *options,
                                   "target/sonic-vs.img.gz"], cwd=self.directory, env=environment,
                                  text=True, capture_output=True)

        installer = self.directory / VS_INSTALLER
        receipt = self.directory / "target/.bazel-vs-image.json"
        backup = self.directory / "target/.sonic-vs.bin.bazel-previous"
        installer.write_bytes(b"retained Bazel installer")
        receipt.write_bytes(b'{"producer":"bazel"}\n')
        result = build("FAIL_ASSEMBLY=y")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(installer.read_bytes(), b"retained Bazel installer")
        self.assertEqual(receipt.read_bytes(), b'{"producer":"bazel"}\n')
        self.assertFalse(backup.exists())

        result = build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(installer.read_bytes(), b"native installer")
        self.assertEqual((self.directory / "target/sonic-vs.img.gz").read_bytes(), b"native KVM image")
        self.assertEqual((self.directory / "component-selector").read_text(), "y")
        self.assertFalse(receipt.exists())
        self.assertFalse(backup.exists())

    def test_direct_slave_keeps_native_installer_and_protects_bazel_publication(self):
        """The native CI entry may build a fresh installer, but cannot replace a Bazel one."""
        source = (ROOT / "slave.mk").read_text()
        start = source.index("# Only the explicit preparation goal enables the native snapshot producer.")
        end = source.index("# targets for building installers with base image", start)
        native = self.directory / "tools/bazel/image/native"
        native.mkdir()
        (native / "native.mk").symlink_to(ROOT / "tools/bazel/image/native/native.mk")
        makefile = self.directory / "slave-selector.mk"
        makefile.write_text("TARGET_PATH := target\n" + source[start:end] +
                            "target/sonic-vs.bin:\n\t@printf 'native installer' > $@\n")
        environment = dict(os.environ)
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL"):
            environment.pop(name, None)

        def direct(*options):
            return subprocess.run(
                ["make", "--no-print-directory", "-f", str(makefile),
                 "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", *options, VS_INSTALLER],
                cwd=self.directory, env=environment, text=True, capture_output=True)

        installer = self.directory / VS_INSTALLER
        installer.unlink()
        result = direct()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(installer.read_bytes(), b"native installer")

        installer.write_bytes(b"retained Bazel installer")
        (self.directory / "target/.bazel-vs-image.json").write_text('{"producer":"bazel"}\n')
        result = direct()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Use the top-level Makefile", result.stderr)
        self.assertEqual(installer.read_bytes(), b"retained Bazel installer")
        self.assertEqual(direct("SONIC_BAZEL_NATIVE_PUBLISH=y").returncode, 0)

    def test_unrelated_existing_output_is_not_forced(self):
        """Leave existing outputs outside the SWSS and VS handoff scope up to date.
        """
        target = "target/docker-lldp.gz"
        (self.directory / target).write_bytes(b"unrelated archive")
        before = self.state(target)
        for mode in ("y", "n"):
            with self.subTest(mode=mode):
                result, calls = self.make(target, mode=mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, [])
                self.assertEqual(self.state(target), before)

    def test_default_goal_remains_jessie(self):
        """Keep bare Make on its original default goal instead of selecting the force helper.
        """
        result, calls = self.make(NOJESSIE="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [["jessie", "y", "", ""]])

    def test_distribution_phases_and_requested_target_are_preserved(self):
        """Preserve the Bookworm prerequisite phase and forward the full artifact name to
        Trixie through the unchanged implicit recipe.
        """
        target = ARTIFACTS[0]
        result, calls = self.make(target, NOBOOKWORM="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [["bookworm", "y", "bookworm", "docker-orchagent.gz"],
                                 [target, "y", "trixie", ""],
                                 ["docker-cleanup", "y", "bookworm", ""]])

    def test_inner_failure_propagates_and_preserves_previous_output(self):
        """Report a failed inner build even when the requested artifact already exists, and
        leave that previous artifact intact.
        """
        target = ARTIFACTS[0]
        before = self.state(target)
        result, calls = self.make(target, FAIL_TARGET=target)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [[target, "y", "trixie", ""]])
        self.assertEqual(self.state(target), before)

if __name__ == "__main__":
    unittest.main()
