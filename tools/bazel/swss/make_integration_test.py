#!/usr/bin/env python3
"""Check SWSS selection without starting a slave, Docker or Bazel."""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class MakeIntegrationTest(unittest.TestCase):
    def cache_mount(self, directory, mode="y", source=None):
        work = (ROOT / "Makefile.work").read_text()
        start = work.index("# Reuse downloads and action outputs")
        end = work.index('# User name and tag for "docker-*" images', start)
        fake_docker = directory / "docker.py"
        fake_docker.write_text("""import json, os, pathlib, sys
args = sys.argv[1:]
record = {'args': args}
if '-v' in args:
    source = pathlib.Path(args[args.index('-v') + 1].removesuffix(':/bazel-cache:rw'))
    record['source_exists'] = source.is_dir()
    record['source_owner'] = source.stat().st_uid
    (source / 'container-cache-write').write_text('writable')
print(json.dumps(record))
""")
        makefile = f"""DOCKER_RUN = python3 {fake_docker}
{work[start:end]}
.PHONY: launch
launch:
	@$(DOCKER_RUN) builder-image
"""
        return subprocess.run(
            ["make", "--no-print-directory", "-f", "-", "launch",
             f"BUILD_SWSS_WITH_BAZEL={mode}",
             f"BAZEL_SWSS_CACHE_SOURCE={source if source is not None else directory / 'cache'}"],
            input=makefile, text=True, cwd=ROOT, capture_output=True, check=False,
        )

    def test_bazel_cache_mount_is_created_as_builder_and_passed_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            source = directory / "builder's persistent cache"
            result = self.cache_mount(directory, source=source)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)
            self.assertTrue(record["source_exists"])
            self.assertEqual(record["source_owner"], os.getuid())
            self.assertIn(str(source) + ":/bazel-cache:rw", record["args"])
            self.assertIn("BAZEL_SWSS_CACHE_DIR=/bazel-cache", record["args"])
            self.assertIn("BAZELISK_HOME=/bazel-cache/bazelisk", record["args"])

    def test_native_make_and_empty_cache_setting_add_no_mount(self):
        for mode, source in (("n", None), ("y", "")):
            with self.subTest(mode=mode, source=source), tempfile.TemporaryDirectory() as tmp:
                directory = pathlib.Path(tmp)
                result = self.cache_mount(directory, mode, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["args"], ["builder-image"])
                self.assertFalse((directory / "cache").exists())

    def test_unusable_host_cache_stops_before_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            source = directory / "cache"
            source.write_bytes(b"existing file")
            result = self.cache_mount(directory, source=source)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(source.read_bytes(), b"existing file")

    def select(self, **overrides):
        settings = {
            "BUILD_SWSS_WITH_BAZEL": "n",
            "BLDENV": "trixie",
            "CONFIGURED_PLATFORM": "vs",
            "CONFIGURED_ARCH": "amd64",
            "ENABLE_ASAN": "n",
        }
        settings.update(overrides)
        makefile = """
DBG_IMAGE_MARK = dbg
SWSS = swss.deb
LIB_SONIC_DASH_API = dash.deb
SCAPY = scapy.whl
SONIC_DOCKER_IMAGES = docker-sysmgr.gz docker-swss-layer-trixie.gz
include rules/docker-orchagent.mk
.PHONY: selected
selected:
	@echo bazel=$(SONIC_BAZEL_SWSS_IMAGES)
	@echo legacy=$(filter-out $(SONIC_BAZEL_SWSS_IMAGES),$(SONIC_DOCKER_IMAGES))
	@echo debug=$(filter-out $(SONIC_BAZEL_SWSS_IMAGES),$(SONIC_DOCKER_DBG_IMAGES))
	@echo install=$(SONIC_INSTALL_DOCKER_IMAGES)
	@echo swss_packages=$($(DOCKER_ORCHAGENT)_DEPENDS)
	@echo swss_wheels=$($(DOCKER_ORCHAGENT)_PYTHON_WHEELS)
"""
        return subprocess.run(
            ["make", "--no-print-directory", "-f", "-", "selected"]
            + [f"{key}={value}" for key, value in settings.items()],
            input=makefile,
            text=True,
            cwd=ROOT,
            capture_output=True,
            check=False,
        )

    def values(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_default_preserves_make(self):
        values = self.values(self.select())
        self.assertEqual(values["bazel"], "")
        self.assertIn("docker-orchagent.gz", values["legacy"].split())
        self.assertEqual(values["debug"], "docker-orchagent-dbg.gz")

    def test_opt_in_selects_only_swss_and_keeps_installer_contract(self):
        values = self.values(self.select(BUILD_SWSS_WITH_BAZEL="y"))
        self.assertEqual(
            values["bazel"].split(),
            ["docker-orchagent.gz", "docker-orchagent-dbg.gz"],
        )
        self.assertEqual(
            values["legacy"].split(),
            ["docker-sysmgr.gz", "docker-swss-layer-trixie.gz"],
        )
        self.assertEqual(values["debug"], "")
        self.assertEqual(values["install"], "docker-orchagent.gz")
        # The combined docker-sonic-vs Make build imports this metadata.
        self.assertEqual(values["swss_packages"], "swss.deb dash.deb")
        self.assertEqual(values["swss_wheels"], "scapy.whl")

    def test_other_slave_phases_stay_on_make(self):
        for distro in ("bookworm", "bullseye"):
            with self.subTest(distro=distro):
                values = self.values(self.select(BUILD_SWSS_WITH_BAZEL="y", BLDENV=distro))
                self.assertEqual(values["bazel"], "")

    def test_unsupported_bazel_configurations_fail_explicitly(self):
        for setting in (
            {"CONFIGURED_PLATFORM": "mellanox"},
            {"CONFIGURED_ARCH": "arm64"},
            {"ENABLE_ASAN": "y"},
            {"CROSS_BUILD_ENVIRON": "y"},
            {"MULTIARCH_QEMU_ENVIRON": "y"},
        ):
            with self.subTest(setting=setting):
                result = self.select(BUILD_SWSS_WITH_BAZEL="y", **setting)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BUILD_SWSS_WITH_BAZEL=y", result.stderr)

    def test_make_remains_available_for_other_platforms(self):
        values = self.values(self.select(CONFIGURED_PLATFORM="mellanox", CONFIGURED_ARCH="arm64"))
        self.assertEqual(values["bazel"], "")

    def test_invalid_selector_fails(self):
        for selector in ("yes", "y n", ""):
            with self.subTest(selector=selector):
                result = self.select(BUILD_SWSS_WITH_BAZEL=selector)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be y or n", result.stderr)

    def test_builder_switch_invalidates_both_archives_once(self):
        slave = (ROOT / "slave.mk").read_text()
        start = slave.index("# Changing the SWSS builder")
        end = slave.index("# Let Bazel check its declared inputs", start)
        with tempfile.TemporaryDirectory() as tmp:
            makefile = f"""
TARGET_PATH = {tmp}
BLDENV = trixie
DOCKER_ORCHAGENT = docker-orchagent.gz
DOCKER_ORCHAGENT_DBG = docker-orchagent-dbg.gz
{slave[start:end]}
.PHONY: check
check: {tmp}/docker-orchagent.gz {tmp}/docker-orchagent-dbg.gz
{tmp}/docker-orchagent.gz {tmp}/docker-orchagent-dbg.gz:
	@echo $(BUILD_SWSS_WITH_BAZEL) > $@
"""
            paths = [pathlib.Path(tmp) / name for name in ("docker-orchagent.gz", "docker-orchagent-dbg.gz")]
            previous = None
            for mode in ("n", "n", "y", "y", "n", "n"):
                result = subprocess.run(
                    ["make", "--no-print-directory", "-f", "-", "check", f"BUILD_SWSS_WITH_BAZEL={mode}"],
                    input=makefile, text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                current = (mode, [p.stat().st_mtime_ns for p in paths])
                for path in paths:
                    self.assertEqual(path.read_text().strip(), mode)
                if previous:
                    if previous[0] == mode:
                        self.assertEqual(previous[1], current[1])
                    else:
                        self.assertNotEqual(previous[1], current[1])
                previous = current

    def test_archive_recipe_requires_make_base_and_scapy(self):
        slave = (ROOT / "slave.mk").read_text()
        start = slave.index("# Let Bazel check its declared inputs")
        end = slave.index("# Targets for building docker debug images", start)
        makefile = f"""
TARGET_PATH = target
DOCKER_CONFIG_ENGINE_TRIXIE = docker-config-engine-trixie.gz
PYTHON_WHEELS_PATH = target/python-wheels/trixie
SCAPY = scapy.whl
SONIC_BAZEL_SWSS_IMAGES = docker-orchagent.gz docker-orchagent-dbg.gz
{slave[start:end]}
.PHONY: .platform target/docker-config-engine-trixie.gz target/python-wheels/trixie/scapy.whl
target/docker-config-engine-trixie.gz target/python-wheels/trixie/scapy.whl:
	@echo make-input=$@
"""
        for archive in ("docker-orchagent.gz", "docker-orchagent-dbg.gz"):
            with self.subTest(archive=archive):
                result = subprocess.run(
                    ["make", "--no-print-directory", "-n", "-f", "-", f"target/{archive}"],
                    input=makefile, text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("make-input=target/docker-config-engine-trixie.gz", result.stdout)
                self.assertIn("prepare_oci_base.py --archive", result.stdout)
                self.assertIn('--output "target/docker-config-engine-trixie.oci"', result.stdout)
                self.assertIn("make-input=target/python-wheels/trixie/scapy.whl", result.stdout)
                self.assertIn(f'--archive "{archive}" --output "target/{archive}"', result.stdout)
                self.assertNotIn("swss.deb", result.stdout)

    def test_cached_base_is_prepared_once_before_parallel_archive_consumers(self):
        from prepare_oci_base_test import native_archive

        slave = (ROOT / "slave.mk").read_text()
        start = slave.index("# Let Bazel check its declared inputs")
        end = slave.index("# Targets for building docker debug images", start)
        # Keep the production prerequisite graph and producer; replace only the
        # heavyweight SWSS build with a consumer that requires the prepared base.
        recipe = slave[start:end].replace(
            'python3 tools/bazel/swss/build.py --archive "$(@F)" --output "$@" $(LOG)',
            'test -f "$(TARGET_PATH)/docker-config-engine-trixie.oci/index.json"; echo built > "$@"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            native_archive(directory / "docker-config-engine-trixie.gz")
            (directory / "scapy.whl").write_bytes(b"cached wheel")
            makefile = f"""
TARGET_PATH = {tmp}
DOCKER_CONFIG_ENGINE_TRIXIE = docker-config-engine-trixie.gz
PYTHON_WHEELS_PATH = {tmp}
SCAPY = scapy.whl
SONIC_BAZEL_SWSS_IMAGES = docker-orchagent.gz docker-orchagent-dbg.gz
.PHONY: .platform
{recipe}
"""
            command = ["make", "--no-print-directory", "-j2", "-f", "-",
                       str(directory / "docker-orchagent.gz"),
                       str(directory / "docker-orchagent-dbg.gz")]
            previous = None
            for _ in range(2):
                result = subprocess.run(command, input=makefile, cwd=ROOT,
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("python3 tools/bazel/swss/prepare_oci_base.py"), 1)
                layout = directory / "docker-config-engine-trixie.oci"
                current = layout.lstat().st_mtime_ns, (layout / "index.json").stat().st_mtime_ns
                if previous:
                    self.assertEqual(current, previous)
                previous = current
                for archive in ("docker-orchagent.gz", "docker-orchagent-dbg.gz"):
                    self.assertEqual((directory / archive).read_text(), "built\n")


if __name__ == "__main__":
    unittest.main()
