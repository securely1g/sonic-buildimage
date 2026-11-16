"""Pure supervised frontend phase fixtures; no host preparation or daemon."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

SHELL = Path(__file__).resolve().parents[3] / "tools/bazel/ci/prepare_vs_slave.sh"


class SupervisedFrontendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.working = self.root / "working"
        self.working.mkdir()
        for name in ("packages", "bazel", "tmp"):
            (self.working / name).mkdir()
        self.environment = {"RUNNER_NAME": "fixture-worker", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
        self.preparation = {"status": "prepared", "stage": "before-daemon"}
        self.storage = {"status": "passed", "working_root": str(self.working),
                        "worker": "fixture-worker", "run": "123", "attempt": "2"}
        self.write_records()

    def write_records(self):
        self.digests = []
        for name, value in (("slave-preparation.json", self.preparation), ("k3s-storage.json", self.storage)):
            body = (json.dumps(value, sort_keys=True) + "\n").encode()
            (self.artifacts / name).write_bytes(body)
            self.digests.append(hashlib.sha256(body).hexdigest())

    def resume(self, *, record_owner=1000, directory_owner=1000, unstable=False, record_mode=None):
        # Execute only the actual embedded data-reader against temporary fixtures.
        # Kernel ownership measurements are mocked for portability to non-1000 hosts.
        body = SHELL.read_text().split("<<'PY_VS_RESUME'\n", 1)[1].split("\nPY_VS_RESUME\n", 1)[0]
        original_fstat, original_lstat = os.fstat, Path.lstat
        calls = 0
        def measured(info, owner, mode=None):
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields.update(st_uid=owner, st_gid=owner)
            if mode is not None:
                fields["st_mode"] = mode
            return types.SimpleNamespace(**fields)
        def fstat(fd):
            nonlocal calls
            calls += 1
            value = measured(original_fstat(fd), record_owner, record_mode)
            if unstable and calls == 2:
                value.st_mtime_ns += 1
            return value
        def lstat(path):
            return measured(original_lstat(path), directory_owner)
        argv = ["resume", str(self.artifacts), str(self.working), *self.digests]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, self.environment, clear=True), \
             mock.patch.object(os, "fstat", side_effect=fstat), mock.patch.object(Path, "lstat", lstat), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("No subprocess in resume")):
            exec(compile(body, str(SHELL) + ":PY_VS_RESUME", "exec"), {})

    def test_exact_records_resume_without_modifying_original_bytes(self):
        before = {p.name: p.read_bytes() for p in self.artifacts.iterdir()}
        self.resume()
        self.assertEqual({p.name: p.read_bytes() for p in self.artifacts.iterdir()}, before)

    def test_record_hash_mismatch_is_rejected(self):
        self.digests[0] = "0" * 64
        with self.assertRaisesRegex(SystemExit, "record changed"):
            self.resume()

    def test_record_symlink_is_rejected(self):
        path = self.artifacts / "slave-preparation.json"
        target = self.root / "original.json"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(SystemExit, "not canonical"):
            self.resume()
        self.assertTrue(target.is_file())

    def test_worker_run_and_attempt_are_bound(self):
        for key in ("worker", "run", "attempt"):
            original = self.storage[key]
            self.storage[key] = "different"
            self.write_records()
            with self.subTest(key=key), self.assertRaisesRegex(SystemExit, "worker/run/attempt differs"):
                self.resume()
            self.storage[key] = original

    def test_failed_or_wrong_preparation_phase_is_rejected(self):
        for value in ({"status": "failed", "stage": "before-daemon"},
                      {"status": "prepared", "stage": "complete"}):
            self.preparation = value
            self.write_records()
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "phase did not pass"):
                self.resume()

    def test_storage_status_and_exact_working_root_are_required(self):
        for key, value in (("status", "failed"), ("working_root", str(self.root / "other"))):
            original = self.storage[key]
            self.storage[key] = value
            self.write_records()
            with self.subTest(key=key), self.assertRaisesRegex(SystemExit, "working-state proof differs"):
                self.resume()
            self.storage[key] = original

    def test_working_directory_owner_is_required(self):
        with self.assertRaisesRegex(SystemExit, "directory identity differs"):
            self.resume(directory_owner=999)

    def test_working_directory_symlink_is_rejected(self):
        path = self.working / "bazel"
        target = self.working / "original-bazel"
        path.rename(target)
        path.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "directory identity differs"):
            self.resume()
        self.assertTrue(target.is_dir())

    def test_held_record_metadata_change_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "record changed"):
            self.resume(unstable=True)

    def test_record_owner_and_regular_type_are_required(self):
        for options in ({"record_owner": 999}, {"record_mode": stat.S_IFDIR | 0o700}):
            with self.subTest(options=options), self.assertRaisesRegex(SystemExit, "bounded worker-owned"):
                self.resume(**options)

    def bash(self, fragment, reuse, phase=""):
        return subprocess.run(["/bin/bash", "-euc", "die() { exit 91; };\n" + fragment],
            env={"PATH": "/usr/bin:/bin", "reuse_only": reuse, "SONIC_VS_SUPERVISED_PHASE": phase,
                 "artifacts": str(self.artifacts)}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10)

    def test_phase_guard_requires_owned_phases_and_preserves_legacy(self):
        shell = SHELL.read_text()
        fragment = shell[shell.index("supervised_phase="):shell.index("controller_root=")]
        for reuse, phase, code in (("y", "before-daemon", 0), ("y", "after-daemon", 0),
                                   ("y", "", 91), ("y", "other", 91), ("n", "before-daemon", 91), ("n", "", 0)):
            with self.subTest(reuse=reuse, phase=phase):
                self.assertEqual(self.bash(fragment, reuse, phase).returncode, code)

    def test_before_phase_exits_before_legacy_daemon_branch(self):
        shell = SHELL.read_text()
        marker = shell.index("    stage=before-daemon\n")
        begin = shell.rindex("if [[ $reuse_only == y ]]; then", 0, marker)
        end = shell.index("\nstage=docker\n", marker)
        fragment = shell[begin:end] + "\nprintf 'legacy-daemon-branch\\n'\n"
        for reuse, expected in (("y", ""), ("n", "legacy-daemon-branch\n")):
            with self.subTest(reuse=reuse):
                result = self.bash(fragment, reuse)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_reuse_selects_canonical_verify_while_legacy_keeps_all(self):
        shell = SHELL.read_text()
        marker = shell.index("make --no-print-directory -C src/sonic-build-hooks verify")
        begin = shell.rindex("if [[ $reuse_only == y ]]; then", 0, marker)
        end = shell.index("\nfi\n", marker) + len("\nfi\n")
        # Both commands are inert Bash functions, so no Make recipe or external tee runs.
        fragment = "make() { printf '%s\\n' \"$*\"; }; tee() { while IFS= read -r row; do printf '%s\\n' \"$row\"; done; };\n" + shell[begin:end]
        for reuse, target in (("y", "verify"), ("n", "all")):
            result = self.bash(fragment, reuse)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "--no-print-directory -C src/sonic-build-hooks " + target)


if __name__ == "__main__":
    unittest.main()
