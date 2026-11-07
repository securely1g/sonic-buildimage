#!/usr/bin/env python3
"""Check native Make cache reuse across isolated source checkouts, without DEBs."""

import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
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

    def workspace(self, name, content=b"first source contents\n"):
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
SONIC_MAKE_FILES := artifact.bin
SONIC_DPKG_CACHE_METHOD := rwcache
SONIC_BUILD_QUIETER := 1
CONFIGURED_PLATFORM := vs
CONFIGURED_ARCH := amd64
BLDENV := trixie
artifact.bin_CACHE_MODE := GIT_CONTENT_SHA
artifact.bin_DEP_FILES := source.txt
artifact.bin_DEP_FLAGS = $(SONIC_COMMON_FLAGS_LIST)
include {cache_makefile}

target/artifact.bin: target/artifact.bin.dep
\t$(call LOAD_CACHE,artifact.bin,$@)
\tif [ -z '$(artifact.bin_CACHE_LOADED)' ]; then
\t  cp source.txt $@
\t  if [ -f target/base.gz ]; then cat target/base.gz >> $@; fi
\t  echo produced >> producer.log
\t  $(call SAVE_CACHE,artifact.bin,$@)
\tfi
""".format(cache_makefile=ROOT / "Makefile.cache")
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

    def build(self, workspace, *settings):
        result = subprocess.run(
            ["make", "--no-print-directory", "SONIC_DPKG_CACHE_DIR=" + str(self.cache), *settings],
            cwd=workspace, env=self.environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((workspace / "target/artifact.bin").read_bytes(),
                         (workspace / "source.txt").read_bytes() + (
                             (workspace / "target/base.gz").read_bytes()
                             if (workspace / "target/base.gz").exists() else b""))
        return (workspace / "target/artifact.bin.log").read_text()

    def dependencies(self, workspace, *, broken=None, base=b"base one\n", failed_bazel=False):
        """Model complete package sidecars, optional FIPS sidecars, then base bytes."""
        lines = ["artifact.bin_DEPENDS := valid.deb", "artifact.bin_DBG_DEPENDS := optional.deb",
                 "artifact.bin_LOAD_DOCKERS := base.gz", "SONIC_BAZEL_DOCKER_IMAGES := base.gz"]
        if failed_bazel:
            lines.append("base.gz_CACHE_FINGERPRINT_FAILED := y")
        paths = []
        for package in ("valid.deb", "optional.deb"):
            for variable, suffix in (("DEP_FLAGS_FILE", "flags"), ("MOD_HASH_FILE", "dep.sha")):
                relative = "target/" + package + "." + suffix
                paths.append(relative)
                lines.append(package + "_" + variable + " := " + relative)
                path = workspace / relative
                if package == "optional.deb" and suffix == "flags" and broken:
                    if broken == "directory":
                        path.mkdir()
                else:
                    path.write_text(package + " " + suffix + "\n")
        (workspace / "target/base.gz").write_bytes(base)
        paths.append("target/base.gz")
        with (workspace / "Makefile").open("a") as makefile:
            makefile.write("\n" + "\n".join(lines) + "\n")
        return paths

    def metadata(self, workspace):
        subprocess.run(["make", "--no-print-directory", "SONIC_DPKG_CACHE_DIR=" + str(self.cache),
                        "target/artifact.bin.dep"], cwd=workspace, env=self.environment,
                       check=True, capture_output=True, text=True)

    def legacy_key(self, workspace, dependencies, own=None, recursive=b""):
        """Reproduce the pre-fix byte stream, including partial Git output."""
        own = own or ["target/artifact.bin.flags", "target/artifact.bin.dep.sha"]
        result = subprocess.run(["git", "hash-object", *dependencies], cwd=workspace,
                                capture_output=True, check=False)
        # The old && echo emits an additional newline only after Git succeeds.
        dep_hash = hashlib.sha1(result.stdout + (recursive + b"\n" if result.returncode == 0 else b"")).hexdigest()[:23]
        result = subprocess.run(["git", "hash-object", *own], cwd=workspace, capture_output=True, check=False)
        own_hash = hashlib.sha1(result.stdout).hexdigest()[:23]
        return "artifact.bin-" + dep_hash + "-" + own_hash + ".tgz"

    def seed_stale(self, key):
        path = self.cache / key
        with tarfile.open(path, "w:gz") as archive:
            for name, data in (("target/artifact.bin", b"stale output\n"),
                               ("target/artifact.bin.cached.log", b"unsafe legacy cache\n")):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        return path

    def test_missing_or_unhashable_dependency_rejects_legacy_read_and_write(self):
        for broken in ("missing", "directory"):
            with self.subTest(broken=broken):
                workspace = self.workspace("bad-dependency-" + broken)
                paths = self.dependencies(workspace, broken=broken)
                self.metadata(workspace)
                key = self.legacy_key(workspace, paths)
                self.seed_stale(key)
                before = {p.name: p.read_bytes() for p in self.cache.glob("*.tgz")}
                # This changed base is after the failing Git input in the old
                # stream: it must not restore the stale entry or publish one.
                (workspace / "target/base.gz").write_bytes(b"changed base\n")
                self.assertEqual(self.legacy_key(workspace, paths), key)
                log = self.build(workspace)
                self.assertIn("Incomplete fingerprint", log)
                self.assertNotIn("[ CACHE::LOADED ]", log)
                self.assertNotIn("[ CACHE::SAVED ]", log)
                self.assertEqual({p.name: p.read_bytes() for p in self.cache.glob("*.tgz")}, before)

    def test_failed_own_fingerprint_rejects_legacy_read_and_write(self):
        workspace = self.workspace("bad-own-fingerprint")
        with (workspace / "Makefile").open("a") as makefile:
            makefile.write("\nartifact.bin_MOD_HASH_FILE := target/missing-own.sha\n")
        self.metadata(workspace)
        key = self.legacy_key(workspace, [], ["target/artifact.bin.flags", "target/missing-own.sha"])
        seeded = self.seed_stale(key)
        original = seeded.read_bytes()
        log = self.build(workspace)
        self.assertIn("Incomplete fingerprint", log)
        self.assertNotIn("[ CACHE::LOADED ]", log)
        self.assertNotIn("[ CACHE::SAVED ]", log)
        self.assertEqual(seeded.read_bytes(), original)
        self.assertEqual(list(self.cache.glob("*.tgz")), [seeded])

    def test_complete_key_preserves_legacy_newline_stream_and_tracks_actual_base(self):
        first = self.workspace("complete-dependencies")
        paths = self.dependencies(first, failed_bazel=True)
        self.metadata(first)
        key = self.legacy_key(first, paths)
        self.assertIn("[ CACHE::SAVED ]", self.build(first))
        self.assertTrue((self.cache / key).is_file(), "valid legacy key format changed")
        second = self.workspace("complete-reuse")
        self.dependencies(second, failed_bazel=True)
        self.assertIn("[ CACHE::LOADED ]", self.build(second))
        self.assertFalse((second / "producer.log").exists())
        changed = self.workspace("complete-new-base")
        self.dependencies(changed, base=b"new actual base\n", failed_bazel=True)
        log = self.build(changed)
        self.assertNotIn("[ CACHE::LOADED ]", log)
        self.assertIn("[ CACHE::SAVED ]", log)

    def test_digest_failure_cannot_publish_its_plausible_partial_output(self):
        workspace = self.workspace("failed-digest")
        commands = self.root / "commands"
        digest = commands / "sha1sum"
        digest.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s  -\\n' '" + "1" * 40 + "'\nexit 7\n")
        digest.chmod(0o755)
        seeded = self.seed_stale("artifact.bin-" + "1" * 23 + "-" + "1" * 23 + ".tgz")
        before = seeded.read_bytes()
        log = self.build(workspace)
        self.assertIn("Incomplete fingerprint", log)
        self.assertNotIn("[ CACHE::LOADED ]", log)
        self.assertNotIn("[ CACHE::SAVED ]", log)
        self.assertEqual(seeded.read_bytes(), before)
        self.assertEqual(list(self.cache.glob("*.tgz")), [seeded])

    def test_failed_make_dependency_disables_cache_for_its_consumer(self):
        workspace = self.workspace("failed-transitive-fingerprint")
        paths = self.dependencies(workspace)
        with (workspace / "Makefile").open("a") as makefile:
            makefile.write("""
optional.deb_CACHE_MODE := GIT_CONTENT_SHA
optional.deb_DST_PATH := target
optional.deb_BASE_PATH := $(CURDIR)
optional.deb_CACHE_DIR := $(SONIC_DPKG_CACHE_DIR)
optional.deb_DEPENDS := transitive.deb
transitive.deb_DEP_FLAGS_FILE := target/missing-transitive.flags
.PHONY: check-parent
target/artifact.bin: check-parent
check-parent:
\t$(call LOAD_CACHE,optional.deb,target/optional.deb)
\ttest -n '$(optional.deb_CACHE_FINGERPRINT_FAILED)'
""")
        self.metadata(workspace)
        own = subprocess.check_output(["git", "hash-object", "target/optional.deb.flags",
                                       "target/optional.deb.dep.sha"], cwd=workspace)
        recursive = hashlib.sha1(own).hexdigest()[:23].encode()
        seeded = self.seed_stale(self.legacy_key(workspace, paths, recursive=recursive))
        before = seeded.read_bytes()
        log = self.build(workspace)
        self.assertIn("Incomplete fingerprint", log)
        self.assertNotIn("[ CACHE::LOADED ]", log)
        self.assertNotIn("[ CACHE::SAVED ]", log)
        self.assertEqual(seeded.read_bytes(), before)
        self.assertEqual(list(self.cache.glob("*.tgz")), [seeded])

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


if __name__ == "__main__":
    unittest.main()
