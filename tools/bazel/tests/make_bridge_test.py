#!/usr/bin/env python3
"""Exercise the real Make selector and archive recipes without Docker or Bazel.

Only the relevant Make fragments are loaded: parsing all of slave.mk would start
unrelated configuration probes and require a prepared SONiC slave. A fake Bazel
supplies outputs so these tests cover the Make/Bazel publication boundary.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SLAVE = (ROOT / "slave.mk").read_text()


def fragment(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


GUARD = fragment(
    SLAVE,
    "# Validate the effective value after organization and user configuration.",
    "\n\nifneq ($(strip $(SONIC_EXTRA_EXPORT_VARS))",
)
SELECTION = fragment(
    SLAVE,
    "# A docker opts into the Bazel build",
    "$(foreach IMAGE,$(DOCKER_IMAGES),",
)
ARCHIVES = fragment(
    SLAVE,
    "# Let Bazel check its inputs whenever Make requests one of its images.",
    "# Targets for building docker debug images",
)
LOAD_TARGETS = fragment(
    SLAVE,
    "DOCKER_LOAD_TARGETS =",
    "$(DOCKER_LOAD_TARGETS) :",
)
DEFAULT = next(
    line for line in (ROOT / "rules/config").read_text().splitlines()
    if line.startswith("BAZEL_MIN_READINESS ?=")
)


def makefile(extra="", recipes=""):
    return "\n".join([
        ".DEFAULT_GOAL := report",
        ".ONESHELL:",
        ".SECONDEXPANSION:",
        "SHELL := /bin/bash",
        ".SHELLFLAGS := -ec",
        "BLDENV := trixie",
        "CONFIGURED_ARCH := amd64",
        "ENABLE_ASAN := n",
        "DBG_IMAGE_MARK := dbg",
        "DOCKERS_PATH := dockers",
        "TARGET_PATH := target",
        "PYTHON_WHEELS_PATH := target/python-wheels/trixie",
        "SCAPY := scapy-2.6.1.dev0-py3-none-any.whl",
        "DOCKER_CONFIG_ENGINE_TRIXIE := docker-config-engine-trixie.gz",
        "DOCKER_SWSS_LAYER_TRIXIE := docker-swss-layer-trixie.gz",
        "SWSS := swss.deb",
        "SWSS_DBG := swss-dbg.deb",
        "SYSMGR := sysmgr.deb",
        DEFAULT,
        f"include {ROOT}/rules/docker-sysmgr.mk",
        f"include {ROOT}/rules/docker-orchagent.mk",
        "SONIC_DOCKER_IMAGES += docker-legacy.gz",
        "SONIC_DOCKER_DBG_IMAGES += docker-legacy-dbg.gz",
        extra,
        GUARD,
        "DOCKER_IMAGES := $(SONIC_DOCKER_IMAGES)",
        "DOCKER_DBG_IMAGES := $(SONIC_DOCKER_DBG_IMAGES)",
        SELECTION,
        LOAD_TARGETS,
        "report:",
        "\t@echo bazel=$(SONIC_BAZEL_DOCKER_IMAGES)",
        "\t@echo debug=$(SONIC_BAZEL_DBG_DOCKER_IMAGES)",
        "\t@echo legacy=$(DOCKER_IMAGES)",
        "\t@echo legacy_debug=$(DOCKER_DBG_IMAGES)",
        "\t@echo loads=$(DOCKER_LOAD_TARGETS)",
        "\t@echo swss_deps=$(docker-orchagent.gz_DEPENDS)",
        "\t@echo swss_base=$(docker-orchagent.gz_LOAD_DOCKERS)",
        "\t@echo sysmgr_deps=$(docker-sysmgr.gz_DEPENDS)",
        "\t@echo sysmgr_base=$(docker-sysmgr.gz_LOAD_DOCKERS)",
        recipes,
    ])


class MakeHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.env = os.environ.copy()
        for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "BAZEL_MIN_READINESS"):
            self.env.pop(key, None)

    def run_make(self, text, *args, success=True):
        (self.work / "Makefile").write_text(text)
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", "Makefile", *args],
            cwd=self.work, env=self.env, text=True, capture_output=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result


class ReadinessTest(MakeHarness):
    def selected(self, *args, extra=""):
        result = self.run_make(makefile(extra), *args)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_default_and_stable_preserve_legacy_contract(self):
        for args in ((), ("BAZEL_MIN_READINESS=stable",)):
            with self.subTest(args=args):
                values = self.selected(*args)
                self.assertEqual(values["bazel"], "")
                self.assertEqual(values["debug"], "")
                self.assertIn("docker-orchagent.gz", values["legacy"].split())
                self.assertIn("docker-sysmgr-dbg.gz", values["legacy_debug"].split())
                self.assertIn("swss.deb", values["swss_deps"].split())
                self.assertEqual(values["swss_base"], "docker-swss-layer-trixie.gz")
                self.assertEqual(values["sysmgr_deps"], "sysmgr.deb")
                self.assertEqual(values["sysmgr_base"], "docker-config-engine-trixie.gz")

    def test_experimental_selects_normal_debug_and_load_targets(self):
        values = self.selected("BAZEL_MIN_READINESS=experimental")
        self.assertEqual(set(values["bazel"].split()), {"docker-sysmgr.gz", "docker-orchagent.gz"})
        self.assertEqual(set(values["debug"].split()), {"docker-sysmgr-dbg.gz", "docker-orchagent-dbg.gz"})
        self.assertEqual(values["legacy"], "docker-legacy.gz")
        self.assertEqual(values["legacy_debug"], "docker-legacy-dbg.gz")
        self.assertIn("target/docker-orchagent.gz-load", values["loads"].split())
        self.assertIn("target/docker-orchagent-dbg.gz-load", values["loads"].split())

    def test_stable_candidate_is_selected_at_both_levels(self):
        extra = "docker-sysmgr.gz_BAZEL_READINESS := stable"
        values = self.selected("BAZEL_MIN_READINESS=stable", extra=extra)
        self.assertEqual(values["bazel"], "docker-sysmgr.gz")
        self.assertEqual(values["debug"], "docker-sysmgr-dbg.gz")
        values = self.selected("BAZEL_MIN_READINESS=experimental", extra=extra)
        self.assertIn("docker-sysmgr.gz", values["bazel"].split())

    def test_arm_preserves_swss_normal_and_debug_make_builds(self):
        for arch in ("arm64", "armhf"):
            with self.subTest(arch=arch):
                values = self.selected("BAZEL_MIN_READINESS=experimental", "CONFIGURED_ARCH=" + arch)
                self.assertNotIn("docker-orchagent.gz", values["bazel"].split())
                self.assertNotIn("docker-orchagent-dbg.gz", values["debug"].split())
                self.assertIn("docker-orchagent.gz", values["legacy"].split())
                self.assertIn("docker-orchagent-dbg.gz", values["legacy_debug"].split())
                self.assertIn("swss.deb", values["swss_deps"].split())
                self.assertEqual(values["swss_base"], "docker-swss-layer-trixie.gz")

    def test_asan_preserves_swss_make_build_and_debug_package(self):
        values = self.selected("BAZEL_MIN_READINESS=experimental", "ENABLE_ASAN=y")
        self.assertNotIn("docker-orchagent.gz", values["bazel"].split())
        self.assertNotIn("docker-orchagent-dbg.gz", values["debug"].split())
        self.assertIn("docker-orchagent.gz", values["legacy"].split())
        self.assertIn("docker-orchagent-dbg.gz", values["legacy_debug"].split())
        self.assertEqual(set(values["swss_deps"].split()), {"swss.deb", "swss-dbg.deb"})
        self.assertEqual(values["swss_base"], "docker-swss-layer-trixie.gz")

    def test_absent_debug_target_is_not_invented(self):
        values = self.selected(
            "BAZEL_MIN_READINESS=experimental",
            extra="SONIC_DOCKER_DBG_IMAGES := docker-orchagent-dbg.gz",
        )
        self.assertEqual(values["debug"], "docker-orchagent-dbg.gz")

    def test_invalid_filter_is_rejected(self):
        for value in ("", "yes", "%", "experimental stable"):
            with self.subTest(value=value):
                result = self.run_make(makefile(), "BAZEL_MIN_READINESS=" + value, success=False)
                self.assertIn("BAZEL_MIN_READINESS", result.stderr)

    def test_invalid_or_unregistered_candidate_is_rejected_even_when_disabled(self):
        for extra in (
            "docker-orchagent.gz_BAZEL_READINESS :=",
            "docker-orchagent.gz_BAZEL_READINESS := yes",
            "docker-orchagent.gz_BAZEL_READINESS := %",
            "docker-orchagent.gz_BAZEL_READINESS := experimental stable",
            "docker-legacy.gz_BAZEL_READINESS := experimental",
        ):
            with self.subTest(extra=extra):
                result = self.run_make(makefile(extra), success=False)
                self.assertIn("_BAZEL_READINESS", result.stderr)

    def test_non_trixie_only_allows_disabled(self):
        self.selected("BLDENV=bookworm")
        for selector in ("experimental", "stable"):
            result = self.run_make(makefile(), "BLDENV=bookworm", "BAZEL_MIN_READINESS=" + selector, success=False)
            self.assertIn("only supports BLDENV=trixie", result.stderr)

    def test_work_makefile_checks_effective_config(self):
        text = (ROOT / "Makefile.work").read_text()
        guard = fragment(text, "# Validate the effective value", "\n\nifneq ($(DEFAULT_CONTAINER_REGISTRY)")
        result = self.run_make("BLDENV := bookworm\n" + DEFAULT + "\nBAZEL_MIN_READINESS := experimental\n" + guard + "\nall:;@:\n", success=False)
        self.assertIn("only supports BLDENV=trixie", result.stderr)

    def test_root_rejects_mixed_distro_build(self):
        text = fragment((ROOT / "Makefile").read_text(), "NOJESSIE ?=", "PLATFORM_PATH :=")
        self.run_make(text + "\nall:;@:\n", "BAZEL_MIN_READINESS=experimental", "NOBOOKWORM=1")
        result = self.run_make(text + "\nall:;@:\n", "BAZEL_MIN_READINESS=experimental", success=False)
        self.assertIn("only supports trixie builds", result.stderr)

    def test_native_dockerd_local_package_guard(self):
        for image in ("docker-orchagent.gz", "docker-orchagent-dbg.gz"):
            args = ("SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD=y", "SONIC_PACKAGES_LOCAL=" + image)
            self.selected(*args)
            result = self.run_make(makefile(), *args, "BAZEL_MIN_READINESS=experimental", success=False)
            self.assertIn("Bazel tags images as :latest", result.stderr)
        self.selected("SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD=y", "SONIC_PACKAGES_LOCAL=docker-legacy.gz", "BAZEL_MIN_READINESS=experimental")


class ArchiveHandoffTest(MakeHarness):
    def setUp(self):
        super().setUp()
        (self.work / "bin").mkdir()
        fake = self.work / "bin/bazel"
        fake.write_text("""#!/usr/bin/env python3
from pathlib import Path
import os
import sys
root = Path.cwd()
if sys.argv[1] == 'build':
    with (root / 'builds').open('a') as log:
        log.write(sys.argv[2] + '\\n')
    if os.environ.get('FAKE_BAZEL_FAIL'):
        sys.exit(1)
    package, name = sys.argv[2][2:].split(':')
    output = root / 'out' / package / name
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.chmod(0o644)
    output.write_bytes((root / 'input').read_bytes())
    output.chmod(0o444)
elif sys.argv[1:] == ['info', 'bazel-bin']:
    print(root / 'out')
else:
    sys.exit('Unexpected fake Bazel arguments: ' + repr(sys.argv))
""")
        fake.chmod(0o755)
        self.env["PATH"] = str(fake.parent) + os.pathsep + self.env["PATH"]
        (self.work / ".platform").write_text("vs\n")
        (self.work / "target").mkdir()
        (self.work / "target/docker-config-engine-trixie.gz").write_bytes(b"base")
        wheel_dir = self.work / "target/python-wheels/trixie"
        wheel_dir.mkdir(parents=True)
        (wheel_dir / "scapy-2.6.1.dev0-py3-none-any.whl").write_bytes(b"wheel")
        (self.work / "input").write_bytes(b"first image")

    def build(self, debug=False, success=True):
        name = "docker-orchagent-dbg.gz" if debug else "docker-orchagent.gz"
        self.run_make(makefile(recipes=ARCHIVES), "BAZEL_MIN_READINESS=experimental", "target/" + name, success=success)
        return self.work / "target" / name

    def test_sources_rechecked_and_unchanged_output_keeps_timestamp(self):
        for debug in (False, True):
            with self.subTest(debug=debug):
                output = self.build(debug)
                self.assertEqual(output.read_bytes(), b"first image")
                self.assertEqual(output.stat().st_mode & 0o777, 0o644)
                os.utime(output, ns=(1_000_000_000, 1_000_000_000))
                output.chmod(0o600)
                self.build(debug)
                self.assertEqual(output.stat().st_mtime_ns, 1_000_000_000)
                self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.assertEqual(len((self.work / "builds").read_text().splitlines()), 4)

    def test_changed_source_publishes_new_output(self):
        output = self.build()
        os.utime(output, ns=(1_000_000_000, 1_000_000_000))
        (self.work / "input").write_bytes(b"changed source")
        self.build()
        self.assertEqual(output.read_bytes(), b"changed source")
        self.assertGreater(output.stat().st_mtime_ns, 1_000_000_000)
        self.assertEqual(list(output.parent.glob("*.tmp.*")), [])

    def test_symlink_destination_is_replaced_without_mutating_referent(self):
        referent = self.work / "input"
        referent.chmod(0o444)
        output = self.work / "target/docker-orchagent.gz"
        output.symlink_to(referent)
        self.build()
        self.assertFalse(output.is_symlink())
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.assertEqual(referent.stat().st_mode & 0o777, 0o444)

    def test_failed_bazel_leaves_previous_archive_intact(self):
        output = self.build()
        before = output.stat().st_mtime_ns
        self.env["FAKE_BAZEL_FAIL"] = "1"
        self.build(success=False)
        self.assertEqual(output.read_bytes(), b"first image")
        self.assertEqual(output.stat().st_mtime_ns, before)

    def test_normal_and_debug_require_imported_scapy_wheel(self):
        (self.work / "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl").unlink()
        for debug in (False, True):
            self.build(debug, success=False)
        self.assertFalse((self.work / "builds").exists())

    def test_normal_and_debug_require_imported_config_engine(self):
        (self.work / "target/docker-config-engine-trixie.gz").unlink()
        for debug in (False, True):
            self.build(debug, success=False)
        self.assertFalse((self.work / "builds").exists())


if __name__ == "__main__":
    unittest.main()
