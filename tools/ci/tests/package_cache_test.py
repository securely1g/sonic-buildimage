#!/usr/bin/env python3
"""Check native Make cache reuse across isolated source checkouts, without DEBs."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
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
        self.environment = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                            "CACHE_TEST_GZIP": shutil.which("gzip")}

    def controlled_writer(self, workspace, *, fail=False, legacy_archive=None):
        """Pause the real tar compressor after it opens its output archive."""
        ready = self.root / "compressor-ready"
        release = self.root / "release-compressor"
        gzip = self.root / "commands/gzip"
        gzip.write_text("""#!/bin/sh
if [ -n "$CACHE_TEST_READY" ]; then
    : > "$CACHE_TEST_READY"
    while [ ! -e "$CACHE_TEST_RELEASE" ]; do sleep 0.02; done
    if [ "$CACHE_TEST_FAIL" = 1 ]; then
        printf 'incomplete archive'
        exit 77
    fi
fi
exec "$CACHE_TEST_GZIP" "$@"
""")
        gzip.chmod(0o755)
        environment = {**self.environment, "CACHE_TEST_READY": str(ready),
                       "CACHE_TEST_RELEASE": str(release), "CACHE_TEST_FAIL": str(int(fail)),
                       "CACHE_TEST_GZIP": shutil.which("gzip")}
        command = ["make", "--no-print-directory", "SONIC_DPKG_CACHE_DIR=" + str(self.cache),
                   "SONIC_DPKG_CACHE_METHOD=wcache"]
        if legacy_archive is not None:
            # Reproduce the existing cache protocol: lock writers, then open
            # the final archive directly. New readers must coexist with jobs
            # still running that historical recipe during deployment.
            (workspace / "target/artifact.bin.cached.log").write_text("legacy cache writer\n")
            command = ["bash", "-ec", '''exec 9<>"$1"
flock -x 9
tar -C "$2" -mczvf "$3" target/artifact.bin target/artifact.bin.cached.log
''', "legacy-writer", str(self.cache / "artifact.bin_cache_access.lock"),
                       str(workspace), str(legacy_archive)]
        process = subprocess.Popen(
            command,
            cwd=workspace, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def finish():
            release.touch()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.addCleanup(finish)
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "Writer never reached the compressor")
        self.assertIsNone(process.poll(), "Writer exited before its release")
        return process, release

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
                         (workspace / "source.txt").read_bytes())
        return (workspace / "target/artifact.bin.log").read_text()

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

    def test_reader_uses_complete_archive_while_another_writer_is_paused(self):
        self.build(self.workspace("seed"))
        archive, = self.cache.glob("*.tgz")
        original = archive.read_bytes()
        writer, release = self.controlled_writer(self.workspace("writer"))

        self.assertEqual(archive.read_bytes(), original, "Writer exposed an incomplete archive")
        reader = self.workspace("reader")
        self.assertIn("[ CACHE::LOADED ]", self.build(reader, "SONIC_DPKG_CACHE_METHOD=rcache"))
        self.assertFalse((reader / "producer.log").exists())
        release.touch()
        stdout, stderr = writer.communicate(timeout=15)
        self.assertEqual(writer.returncode, 0, stdout + stderr)
        self.assertEqual(set(self.cache.glob("*.tgz")), {archive})
        self.assertFalse(list(self.cache.glob(".*.tgz.*")), "Temporary cache archives remain")
        self.assertIn("[ CACHE::LOADED ]", self.build(self.workspace("after-publish")))

    def test_writable_reader_waits_for_legacy_writer_lock(self):
        seed = self.workspace("seed")
        self.build(seed)
        archive, = self.cache.glob("*.tgz")
        writer, release = self.controlled_writer(seed, legacy_archive=archive)
        self.assertEqual(archive.stat().st_size, 0, "Legacy writer did not truncate its archive")
        reader = self.workspace("reader")
        waiting = self.root / "reader-waiting-for-lock"
        flock = self.root / "commands/flock"
        flock.write_text('''#!/bin/sh
if [ -n "$CACHE_TEST_LOCK_WAIT" ]; then : > "$CACHE_TEST_LOCK_WAIT"; fi
exec "$CACHE_TEST_FLOCK" "$@"
''')
        flock.chmod(0o755)
        environment = {**self.environment, "CACHE_TEST_LOCK_WAIT": str(waiting),
                       "CACHE_TEST_FLOCK": shutil.which("flock")}
        process = subprocess.Popen(
            ["make", "--no-print-directory", "SONIC_DPKG_CACHE_DIR=" + str(self.cache)],
            cwd=reader, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not waiting.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(waiting.exists(), "Reader did not take the legacy writer's lock")
            self.assertIsNone(process.poll(), "Reader exited while the legacy archive was incomplete")
        finally:
            release.touch()
            stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        stdout, stderr = writer.communicate(timeout=15)
        self.assertEqual(writer.returncode, 0, stdout + stderr)
        self.assertFalse((reader / "producer.log").exists())
        self.assertEqual((reader / "target/artifact.bin").read_bytes(), (seed / "source.txt").read_bytes())

    def test_read_only_cache_hit_does_not_create_a_lock_file(self):
        self.build(self.workspace("seed"))
        lock = self.cache / "artifact.bin_cache_access.lock"
        lock.unlink()
        reader = self.workspace("read-only")
        self.assertIn("[ CACHE::LOADED ]", self.build(reader, "SONIC_DPKG_CACHE_METHOD=rcache"))
        self.assertFalse(lock.exists())
        self.assertFalse((reader / "producer.log").exists())

    def test_failed_writer_preserves_previous_cache_and_removes_temporary_archive(self):
        self.build(self.workspace("seed"))
        archive, = self.cache.glob("*.tgz")
        original = archive.read_bytes()
        writer, release = self.controlled_writer(self.workspace("failed-writer"), fail=True)
        release.touch()
        stdout, stderr = writer.communicate(timeout=15)
        self.assertNotEqual(writer.returncode, 0, stdout + stderr)
        self.assertEqual(archive.read_bytes(), original)
        self.assertFalse(list(self.cache.glob(".*.tgz.*")), "Failed archive was not removed")
        reader = self.workspace("after-failure")
        self.assertIn("[ CACHE::LOADED ]", self.build(reader))
        self.assertFalse((reader / "producer.log").exists())


if __name__ == "__main__":
    unittest.main()
