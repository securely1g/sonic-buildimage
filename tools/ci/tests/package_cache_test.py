#!/usr/bin/env python3
"""Check native Make cache reuse across isolated source checkouts, without DEBs."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class PackageCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "shared-cache"
        self.cache.mkdir()
        commands = self.root / "commands"
        commands.mkdir()
        # Make's cache changes permissions through sudo. All fixture files are
        # already ours, so exercise chmod without requiring privileged tests.
        sudo = commands / "sudo"
        sudo.write_text('#!/bin/sh\n[ "$1" = chmod ] || exit 99\nexec "$@"\n')
        sudo.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]}

    def workspace(self, name, content=b"first source contents\n", *, declarations="",
                  after_include="", target_group="SONIC_MAKE_FILES", producer="cp source.txt $@"):
        directory = self.root / name
        directory.mkdir()
        (directory / "target").mkdir()
        (directory / "rules").mkdir()
        (directory / "rules" / "fixture.dep").write_text("# One declared fixture below.\n")
        (directory / "source.txt").write_bytes(content)
        # Include the complete production framework: it generates the flags
        # and dependency hashes and executes its real LOAD_CACHE/SAVE_CACHE.
        # The only producer is a file copy; no package builder is involved.
        makefile = """SHELL := /bin/bash
.SHELLFLAGS := -ec
.ONESHELL:
.SECONDEXPANSION:
.DEFAULT_GOAL := target/artifact.bin
RULES_PATH := rules
TARGET_PATH := target
FILES_PATH := target
{target_group} := artifact.bin
SONIC_DPKG_CACHE_METHOD := rwcache
SONIC_BUILD_QUIETER := 1
CONFIGURED_PLATFORM := vs
CONFIGURED_ARCH := amd64
BLDENV := trixie
artifact.bin_CACHE_MODE := GIT_CONTENT_SHA
artifact.bin_DEP_FILES := source.txt
artifact.bin_DEP_FLAGS = $(SONIC_COMMON_FLAGS_LIST)
include {functions_makefile}
{declarations}
include {cache_makefile}
{after_include}

target/artifact.bin: target/artifact.bin.dep
\t$(call LOAD_CACHE,artifact.bin,$@)
\tif [ -z '$(artifact.bin_CACHE_LOADED)' ]; then
\t  {producer}
\t  echo produced >> producer.log
\t  $(call SAVE_CACHE,artifact.bin,$@)
\tfi
""".format(cache_makefile=ROOT / "Makefile.cache", functions_makefile=ROOT / "rules/functions",
           declarations=declarations,
           after_include=after_include, target_group=target_group, producer=producer)
        (directory / "Makefile").write_text(makefile)
        for command in (
            ["git", "init", "--quiet"],
            ["git", "add", "Makefile", "rules", "source.txt"],
            ["git", "-c", "user.name=Cache Test", "-c", "user.email=cache-test@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Cache fixture"],
        ):
            subprocess.run(command, cwd=directory, env=self.environment,
                           check=True, capture_output=True, text=True)
        return directory

    def run_make(self, workspace, *settings):
        return subprocess.run(
            ["make", "--no-print-directory", "SONIC_DPKG_CACHE_DIR=" + str(self.cache), *settings],
            cwd=workspace, env=self.environment, capture_output=True, text=True,
        )

    def build(self, workspace, *settings, expected=None):
        result = self.run_make(workspace, *settings)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((workspace / "target/artifact.bin").read_bytes(),
                         expected if expected is not None else (workspace / "source.txt").read_bytes())
        return (workspace / "target/artifact.bin.log").read_text()

    def docker_workspace(self, name, base=b"base one\n", *, debug_metadata=False,
                         declarations="", debug_image=False):
        # Match BMP: an inherited debug DEB maps to its main FIPS package,
        # before the actual Bazel base in GET_MOD_DEP_SHA's input order.
        directory = self.workspace(name, declarations="""
artifact.bin_DBG_DEPENDS := debug-extra.deb
debug-extra.deb_MAIN_DEB := fips.deb
fips.deb_DEP_FLAGS_FILE := inputs/fips.flags
fips.deb_MOD_HASH_FILE := inputs/fips.dep.sha
fips.deb_SMOD_HASH_FILE := inputs/fips.smdep.smsha
fips.deb_DEP_MOD_SHA := prior-dependency-hash
fips.deb_MOD_HASH := prior-module-hash
artifact.bin_LOAD_DOCKERS := base.gz
SONIC_BAZEL_DOCKER_IMAGES := base.gz
""" + ("artifact.bin_MAIN_DOCKER := runtime.gz\n" if debug_image else "") + declarations,
            target_group="SONIC_DOCKER_DBG_IMAGES" if debug_image else "SONIC_DOCKER_IMAGES",
            producer="cat source.txt target/base.gz > $@")
        (directory / "target/base.gz").write_bytes(base)
        (directory / "inputs").mkdir()
        # A partially present optional entry must be omitted as a whole.
        (directory / "inputs/fips.flags").write_text("FIPS flags\n")
        if debug_metadata:
            (directory / "inputs/fips.dep.sha").write_text("FIPS source hash\n")
            (directory / "inputs/fips.smdep.smsha").write_text("FIPS submodule hash\n")
        return directory

    def assert_cache_rejected(self, workspace, *settings, message="cache dependency hash"):
        before = set(self.cache.glob("*.tgz"))
        result = self.run_make(workspace, *settings)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertFalse((workspace / "producer.log").exists())
        self.assertFalse((workspace / "target/artifact.bin").exists())
        self.assertEqual(set(self.cache.glob("*.tgz")), before)

    def test_fresh_run_reuses_cache_and_changed_inputs_rebuild(self):
        first = self.workspace("run-one")
        self.assertIn("[ CACHE::SAVED ]", self.build(first))
        self.assertEqual((first / "producer.log").read_text(), "produced\n")
        original_entries = set(self.cache.glob("*.tgz"))
        self.assertEqual(len(original_entries), 1)

        second = self.workspace("run-two")
        self.assertIn("[ CACHE::LOADED ]", self.build(second))
        self.assertFalse((second / "producer.log").exists(), "cache hit reran the producer")
        self.assertEqual(set(self.cache.glob("*.tgz")), original_entries)

        changed = self.workspace("changed-source", b"new source contents\n")
        log = self.build(changed)
        self.assertIn("[ CACHE::SAVED ]", log)
        self.assertNotIn("[ CACHE::LOADED ]", log)
        self.assertEqual((changed / "producer.log").read_text(), "produced\n")
        source_entries = set(self.cache.glob("*.tgz"))
        self.assertEqual(len(source_entries - original_entries), 1)

        changed_flags = self.workspace("changed-architecture")
        log = self.build(changed_flags, "CONFIGURED_ARCH=arm64")
        self.assertIn("[ CACHE::SAVED ]", log)
        self.assertNotIn("[ CACHE::LOADED ]", log)
        self.assertEqual((changed_flags / "producer.log").read_text(), "produced\n")
        self.assertEqual(len(set(self.cache.glob("*.tgz")) - source_entries), 1)

    def test_missing_inactive_debug_metadata_still_hashes_bazel_base(self):
        for name, base, hit in [("base-first", b"base one\n", False),
                                ("base-reused", b"base one\n", True),
                                ("base-changed", b"base two\n", False)]:
            with self.subTest(name=name):
                workspace = self.docker_workspace(name, base)
                expected = (workspace / "source.txt").read_bytes() + base
                log = self.build(workspace, expected=expected)
                self.assertEqual("[ CACHE::LOADED ]" in log, hit)
                self.assertEqual((workspace / "producer.log").exists(), not hit)
        self.assertEqual(len(list(self.cache.glob("*.tgz"))), 2)

    def test_all_present_inputs_keep_the_legacy_key(self):
        workspace = self.docker_workspace("present", debug_metadata=True,
                                         declarations="artifact.bin_DBG_IMAGE_PACKAGES := debug-extra.deb")
        self.build(workspace, expected=(workspace / "source.txt").read_bytes() + b"base one\n")
        # Pin the successful pre-fix byte stream, including ordered sidecars,
        # duplicates, and the inherited package's dependency/module hash line.
        dependency_stream = subprocess.check_output([
            "bash", "-c", "git hash-object inputs/fips.flags inputs/fips.dep.sha "
            "inputs/fips.smdep.smsha inputs/fips.flags inputs/fips.dep.sha "
            "inputs/fips.smdep.smsha target/base.gz && "
            "echo prior-dependency-hash prior-module-hash prior-dependency-hash prior-module-hash"],
            cwd=workspace)
        module_stream = subprocess.check_output([
            "git", "hash-object", "target/artifact.bin.flags", "target/artifact.bin.dep.sha"],
            cwd=workspace)
        dependency_hash = hashlib.sha1(dependency_stream).hexdigest()[:23]
        module_hash = hashlib.sha1(module_stream).hexdigest()[:23]
        self.assertTrue((self.cache / f"artifact.bin-{dependency_hash}-{module_hash}.tgz").is_file())

    def test_commit_mode_keeps_the_legacy_key(self):
        workspace = self.workspace("commit-mode", declarations=
                                   "artifact.bin_CACHE_MODE := GIT_COMMIT_SHA\n"
                                   "artifact.bin_SRC_PATH := .")
        self.build(workspace)
        module_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace,
                                              text=True).strip()[:23]
        dependency_hash = hashlib.sha1(b"\n").hexdigest()[:23]
        self.assertTrue((self.cache / f"artifact.bin-{dependency_hash}-{module_hash}.tgz").is_file())

    def test_required_and_enabled_debug_inputs_cannot_be_omitted(self):
        cases = [("required", {"declarations": "artifact.bin_DEPENDS := debug-extra.deb\n"}, ()),
                 ("transitive-runtime", {"declarations": "artifact.bin_DEPENDS := runtime.deb\n"
                   "runtime.deb_RDEPENDS := debug-extra.deb\n"
                   "runtime.deb_DEP_FLAGS_FILE := inputs/fips.flags\n"}, ()),
                 ("debug-tools", {}, ("INSTALL_DEBUG_TOOLS=y",)),
                 ("debug-alias", {}, ("SONIC_INSTALL_DEBUG_TOOLS=y",)),
                 ("debug-image", {"debug_image": True}, ())]
        for name, options, settings in cases:
            with self.subTest(name=name):
                self.assert_cache_rejected(self.docker_workspace(name, **options), *settings)

    def test_missing_base_and_unreadable_dependency_refuse_cache(self):
        workspace = self.docker_workspace("missing-base")
        (workspace / "target/base.gz").unlink()
        self.assert_cache_rejected(workspace)
        workspace = self.docker_workspace("unreadable", debug_metadata=True)
        # A directory exists for wildcard checks but cannot be hashed as a
        # file. Exercise a genuine read failure after valid preceding input.
        path = workspace / "inputs/fips.dep.sha"
        path.unlink()
        path.mkdir()
        self.assert_cache_rejected(workspace)

    def test_module_hash_failure_refuses_cache(self):
        workspace = self.workspace("bad-module", after_include=
                                   "artifact.bin_MOD_HASH_FILE := missing.dep.sha")
        self.assert_cache_rejected(workspace, message="cache input hash")

    def test_commit_hash_failure_refuses_cache(self):
        workspace = self.workspace("bad-repository", declarations=
                                   "artifact.bin_CACHE_MODE := GIT_COMMIT_SHA\n"
                                   "artifact.bin_SRC_PATH := missing-repository")
        self.assert_cache_rejected(workspace, message="cache input hash")


if __name__ == "__main__":
    unittest.main()
