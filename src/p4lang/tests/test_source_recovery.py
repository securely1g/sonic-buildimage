"""Exercise P4 Make source selection without downloads or package compilation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]
PACKAGES = {
    "pi": ("PI", "0.1.3", "0.1.3-2"),
    "bmv2": ("BMV2", "1.15.0", "1.15.0-9"),
    "p4c": ("P4C", "1.2.4.2", "1.2.4.2-3"),
}


class SourceRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="p4-source-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "src" / "p4lang"
        self.source.mkdir(parents=True)
        for name in ("Makefile", "p4lang-p4c-regional-source.dsc.patch"):
            shutil.copy2(REPO / "src" / "p4lang" / name, self.source / name)
        (self.root / "rules").mkdir()
        for name in ("p4lang.mk", "p4lang.dep"):
            shutil.copy2(REPO / "rules" / name, self.root / "rules" / name)
        self.inputs = self.root / "local inputs"
        self.inputs.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.jsonl"
        # Commands that would download, extract, modify or compile source are
        # intercepted. Copying and SHA256 verification use the real host tools.
        command = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
with open(os.environ["P4_TEST_CALLS"], "a") as output:
    output.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
sys.exit(int(os.environ.get("P4_TEST_COMMAND_STATUS", "0")))
"""
        for name in ("dget", "dpkg-source", "dpkg-buildpackage", "patch"):
            executable = self.bin / name
            executable.write_text(command)
            executable.chmod(0o755)
        self.environment = dict(os.environ)
        # Prevent a parent Make invocation or local source setting from changing
        # these standalone recipe tests.
        for name in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "P4LANG_SOURCE_DIR"):
            self.environment.pop(name, None)
        self.environment.update(
            LC_ALL="C",
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            P4_TEST_CALLS=str(self.calls),
        )

    def run_make(self, target, *assignments, command_status=0):
        variables = ["DEST=output", "CONFIGURED_ARCH=amd64"]
        for variable, version, full in PACKAGES.values():
            variables += [
                f"P4LANG_{variable}_VERSION={version}",
                f"P4LANG_{variable}_VERSION_FULL={full}",
            ]
        return subprocess.run(
            ["make", "--no-print-directory", target, *variables, *assignments],
            cwd=self.source,
            env=dict(self.environment, P4_TEST_COMMAND_STATUS=str(command_status)),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def reset_calls(self):
        self.calls.unlink(missing_ok=True)

    def input_names(self, package):
        _, version, full = PACKAGES[package]
        return [
            f"p4lang-{package}_{full}.dsc",
            f"p4lang-{package}_{version}.orig.tar.gz",
            f"p4lang-{package}_{full}.debian.tar.xz",
        ]

    def corrupt_inputs(self, package):
        for name in self.input_names(package):
            (self.inputs / name).write_text("not the pinned release input\n")

    def test_default_source_preparation_downloads_one_descriptor(self):
        for package, (_, _, full) in PACKAGES.items():
            with self.subTest(package=package):
                self.reset_calls()
                result = self.run_make(f"prepare-{package}-source")
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(
                    self.recorded_calls(),
                    [[
                        "dget",
                        "-u",
                        "http://download.opensuse.org/repositories/home:/p4lang/"
                        f"Debian_11/p4lang-{package}_{full}.dsc",
                    ]],
                )

    def test_package_targets_use_the_same_default_download(self):
        for package, (_, _, full) in PACKAGES.items():
            with self.subTest(package=package):
                self.reset_calls()
                # Stop at the downloader, before ordinary package compilation.
                result = self.run_make(
                    f"output/p4lang-{package}_{full}_amd64.deb",
                    command_status=71,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(
                    self.recorded_calls(),
                    [[
                        "dget",
                        "-u",
                        "http://download.opensuse.org/repositories/home:/p4lang/"
                        f"Debian_11/p4lang-{package}_{full}.dsc",
                    ]],
                )

    def test_default_download_accepts_other_legacy_versions(self):
        for package, (variable, _, _) in PACKAGES.items():
            with self.subTest(package=package):
                self.reset_calls()
                result = self.run_make(
                    f"prepare-{package}-source",
                    f"P4LANG_{variable}_VERSION_FULL=99.99-1",
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(
                    self.recorded_calls(),
                    [[
                        "dget",
                        "-u",
                        "http://download.opensuse.org/repositories/home:/p4lang/"
                        f"Debian_11/p4lang-{package}_99.99-1.dsc",
                    ]],
                )

    def test_corrupt_local_inputs_never_extract_or_fall_back(self):
        for package, (_, _, full) in PACKAGES.items():
            self.corrupt_inputs(package)
            for target in (
                f"prepare-{package}-source",
                f"output/p4lang-{package}_{full}_amd64.deb",
            ):
                with self.subTest(package=package, target=target):
                    self.reset_calls()
                    result = self.run_make(
                        target, f"P4LANG_SOURCE_DIR={self.inputs}"
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("FAILED", result.stdout)
                    self.assertEqual(self.recorded_calls(), [], result.stdout)

    def test_missing_local_inputs_never_extract_or_fall_back(self):
        for package in PACKAGES:
            for missing in self.input_names(package):
                with self.subTest(package=package, missing=missing):
                    self.reset_calls()
                    self.corrupt_inputs(package)
                    (self.inputs / missing).unlink()
                    result = self.run_make(
                        f"prepare-{package}-source",
                        f"P4LANG_SOURCE_DIR={self.inputs}",
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("cannot stat", result.stdout)
                    self.assertIn(missing, result.stdout)
                    self.assertEqual(self.recorded_calls(), [], result.stdout)

    def test_unpinned_versions_rejected_before_copy(self):
        for package, (variable, _, _) in PACKAGES.items():
            for suffix in ("VERSION", "VERSION_FULL"):
                with self.subTest(package=package, variable=suffix):
                    self.reset_calls()
                    # A populated directory distinguishes a version rejection
                    # from merely detecting a missing source set.
                    self.corrupt_inputs(package)
                    result = self.run_make(
                        f"prepare-{package}-source",
                        f"P4LANG_SOURCE_DIR={self.inputs}",
                        f"P4LANG_{variable}_{suffix}=99.99",
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("Unsupported pinned local P4 source version", result.stdout)
                    self.assertEqual(self.recorded_calls(), [], result.stdout)
                    self.assertNotIn("cannot stat", result.stdout)
                    for name in self.input_names(package):
                        self.assertFalse((self.source / name).exists())

    def test_package_cache_tracks_source_selection_and_descriptor_patch(self):
        # Model a clean checkout: normal git ls-files dependency discovery
        # includes the newly committed descriptor adaptation.
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "src", "rules"], cwd=self.root, check=True)
        harness = self.root / "cache-test.mk"
        harness.write_text(
            "SRC_PATH := src\n"
            "CONFIGURED_ARCH := amd64\n"
            "SONIC_COMMON_FLAGS_LIST := existing-common-flag\n"
            "include rules/p4lang.mk\n"
            "include rules/p4lang.dep\n"
            ".PHONY: show\n"
            "show:\n"
            "\t@$(foreach package,$(SONIC_MAKE_DEBS),"
            "printf '%s\\n' '$(package)|$($(package)_DEP_FLAGS)"
            "|$($(package)_DEP_FILES)';)\n"
        )
        configurations = {}
        for selection in ("", "/cache/source-set-a", "/cache/source-set-b"):
            result = subprocess.run(
                ["make", "--no-print-directory", "-f", str(harness), "show",
                 f"P4LANG_SOURCE_DIR={selection}"],
                cwd=self.root,
                env=self.environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            rows = [line.split("|") for line in result.stdout.splitlines()]
            self.assertEqual(len(rows), 3, result.stdout)
            for package, flags, files in rows:
                self.assertIn("existing-common-flag", flags.split())
                if selection:
                    self.assertIn(selection, flags.split())
                self.assertIn(
                    "src/p4lang/p4lang-p4c-regional-source.dsc.patch",
                    files.split(),
                    package,
                )
            configurations[selection] = [flags for _, flags, _ in rows]
        self.assertNotEqual(configurations[""], configurations["/cache/source-set-a"])
        self.assertNotEqual(
            configurations["/cache/source-set-a"],
            configurations["/cache/source-set-b"],
        )


if __name__ == "__main__":
    unittest.main()
