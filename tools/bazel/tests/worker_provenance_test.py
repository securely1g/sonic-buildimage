#!/usr/bin/env python3
import ast
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import yaml
from tools.bazel.tests.registry_native_worker_test import condition

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("diagnostic", (HERE / "worker_provenance.py") if (HERE / "worker_provenance.py").exists() else HERE.parent / "ci/worker_provenance.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "securely1g/sonic-buildimage",
                    "GITHUB_ACTOR": "securely1g", "RUNNER_NAME": worker.WORKERS[0],
                    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
        self.marker = {"uid": 1000, "gid": 1000, "runner_root": "/opt/actions-runner", "work_root": "/actions/_work",
                       "base_image": "sha256:" + "a" * 64, "base_image_ref": "sonic-slave-trixie:reviewed",
                       "manager_revision": "b" * 40, "runner_version": "2.337.0"}

    def test_admission_is_manual_owned_and_one_worker(self):
        worker.admission(worker.WORKERS[0], self.env)
        for key, value in [("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_ACTOR", "other"),
                           ("GITHUB_REPOSITORY", "foreign/repo"), ("RUNNER_NAME", worker.WORKERS[1]),
                           ("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ATTEMPT", "x")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.admission(worker.WORKERS[0], {**self.env, key: value})

    def test_marker_whitelist_does_not_serialize_extra_private_fields(self):
        content = json.dumps({**self.marker, "secret": "not-public"})
        result = worker.marker_fields(content)
        self.assertEqual(result, self.marker)
        self.assertNotIn("not-public", json.dumps(result))

    def test_marker_rejects_invalid_identity_and_command_text(self):
        for key, value in [("base_image", "tag"), ("manager_revision", "master"),
                           ("base_image_ref", "$(command)"), ("base_image_ref", "https://name"),
                           ("runner_version", "2;command"), ("uid", 0), ("work_root", "/tmp")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.marker_fields(json.dumps({**self.marker, key: value}))

    def stat(self, value, **changes):
        result = {key: getattr(value, key) for key in
                  ("st_mode", "st_uid", "st_gid", "st_size", "st_dev", "st_ino", "st_mtime_ns")}
        result.update(changes)
        return NS(**result)

    def test_actual_regular_file_bytes_and_hash_are_recorded(self):
        path = self.root / "marker"
        path.write_bytes(b"fixed file")
        path.chmod(0o444)
        original = os.fstat
        with patch.object(worker.os, "fstat", side_effect=lambda fd: self.stat(original(fd), st_uid=0)):
            data, record = worker.read_root_file(path, 64)
        self.assertEqual(data, b"fixed file")
        self.assertEqual(record["uid"], 0)
        self.assertEqual(record["mode"], 0o444)
        self.assertEqual(record["bytes"], 10)

    def test_reader_rejects_symlink_wrong_owner_writable_and_oversized(self):
        path = self.root / "marker"
        path.write_bytes(b"fixed file")
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            worker.read_root_file(link, 64)
        path.chmod(0o444)
        original = os.fstat
        for changes, maximum in [({"st_uid": 1000}, 64), ({"st_uid": 0, "st_mode": 0o100644}, 64),
                                 ({"st_uid": 0}, 2)]:
            with self.subTest(changes=changes), patch.object(worker.os, "fstat",
                    side_effect=lambda fd: self.stat(original(fd), **changes)), self.assertRaises(ValueError):
                worker.read_root_file(path, maximum)

    def test_reader_rejects_file_changed_during_read(self):
        path = self.root / "marker"
        path.write_bytes(b"fixed file")
        path.chmod(0o444)
        actual = path.stat()
        with patch.object(worker.os, "fstat", side_effect=[
                self.stat(actual, st_uid=0), self.stat(actual, st_uid=0, st_mtime_ns=actual.st_mtime_ns+1)]):
            with self.assertRaisesRegex(ValueError, "changed"):
                worker.read_root_file(path, 64)

    def test_collect_reads_only_fixed_files_and_claims_only_base_provisioner(self):
        record = {"sha256": "c"*64, "bytes": 10, "uid": 0, "gid": 0, "mode": 0o555}
        def read(path, limit):
            self.assertIn(path, (worker.MARKER, *worker.HELPERS))
            return (json.dumps(self.marker).encode(), {**record, "mode": 0o444}) if path == worker.MARKER else (b"code", record)
        with patch.object(worker.os, "getuid", return_value=1000), patch.object(worker.os, "getgid", return_value=1000), \
             patch.object(worker.platform, "machine", return_value="x86_64"), \
             patch.object(worker.platform, "freedesktop_os_release", return_value={"ID":"debian","VERSION_CODENAME":"trixie"}), \
             patch.object(worker.Path, "is_file", return_value=True), \
             patch.object(worker, "marker_identity", return_value={"base_image": self.marker["base_image"]}), \
             patch.object(worker, "read_root_file", side_effect=read) as read_file, \
             patch.object(worker.subprocess, "run") as run, patch.object(worker.subprocess, "check_output") as output:
            result = worker.collect(worker.WORKERS[0], {**self.env, "TOKEN": "private"})
        self.assertEqual(read_file.call_count, 4)
        run.assert_not_called()
        output.assert_not_called()
        self.assertFalse(result["derived_image_digest_verified"])
        self.assertFalse(result["registry_manifest_digest_verified"])
        self.assertEqual(result["identity_kind"], "local_base_config_and_provisioner")
        self.assertNotIn("private", json.dumps(result))

    def test_workflow_is_single_worker_read_only_and_dispatch_only(self):
        flow = yaml.safe_load(((HERE / "bazel-oci.yml") if (HERE / "bazel-oci.yml").exists() else worker.ROOT / ".github/workflows/bazel-oci.yml").read_text())
        job = flow["jobs"]["worker_provenance"]
        self.assertNotIn("needs", job)
        self.assertNotIn("strategy", job)
        self.assertIn("github.event_name == 'workflow_dispatch'", job["if"])
        self.assertIn("github.actor == 'securely1g'", job["if"])
        self.assertIn("github.repository == 'securely1g/sonic-buildimage'", job["if"])
        self.assertEqual(job["runs-on"], ["sonic-slave-container-dpool", "$"+"{{ inputs.source_worker }}"])
        commands = [s["run"] for s in job["steps"] if "run" in s]
        self.assertEqual(len(commands), 1)
        self.assertIn("worker_provenance.py", commands[0])
        for prohibited in ("sudo ", "docker ", "apt-get ", "bazel ", "make "):
            self.assertNotIn(prohibited, commands[0])

    def test_duplicate_marker_fields_are_rejected(self):
        content = json.dumps(self.marker)
        with self.assertRaises(ValueError):
            worker.marker_fields(content[:-1] + ', "manager_revision": "' + "d"*40 + '"}')

    def test_failure_receipt_never_serializes_error_or_environment(self):
        output = self.root / "artifacts/worker-provenance/identity.json"
        with patch.object(worker, "ROOT", self.root), \
             patch.dict(worker.os.environ, {**self.env, "TOKEN": "secret-value"}, clear=True), \
             patch.object(worker.sys, "argv", ["worker_provenance.py", "--worker", worker.WORKERS[0],
                                              "--output", str(output)]), \
             patch.object(worker, "collect", side_effect=ValueError("private diagnostic")), \
             patch.object(worker.subprocess, "check_output") as command:
            self.assertEqual(worker.main(), 1)
        command.assert_not_called()
        result = json.loads(output.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "ValueError")
        self.assertEqual(result["failed_stage"], "runtime_identity")
        self.assertFalse(result["build_executed"])
        self.assertNotIn("private", output.read_text())
        self.assertNotIn("secret-value", output.read_text())

    def test_success_is_public_and_bound_to_control_commit(self):
        output = self.root / "artifacts/worker-provenance/identity.json"
        result = {"schema": 1, "status": "passed", "marker": self.marker,
                  "installed_provisioner_helpers": {"cache_credentials.py": {"sha256": "c"*64}}}
        with patch.object(worker, "ROOT", self.root), patch.dict(worker.os.environ, self.env, clear=True), \
             patch.object(worker.sys, "argv", ["worker_provenance.py", "--worker", worker.WORKERS[0],
                                              "--output", str(output)]), \
             patch.object(worker, "collect", return_value=result), \
             patch.object(worker.subprocess, "check_output", return_value="d"*40 + "\n") as command:
            self.assertEqual(worker.main(), 0)
        self.assertEqual(json.loads(output.read_text())["dispatcher_revision"], "d"*40)
        command.assert_called_once_with(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True)

    def test_output_cannot_escape_evidence_directory(self):
        for output in (self.root / "other.json", self.root.parent / "outside.json"):
            with patch.object(worker, "ROOT", self.root), patch.dict(worker.os.environ, self.env, clear=True), \
                 patch.object(worker.sys, "argv", ["worker_provenance.py", "--worker", worker.WORKERS[0],
                                                  "--output", str(output)]), \
                 patch.object(worker, "collect") as collect, self.assertRaises(ValueError):
                worker.main()
            collect.assert_not_called()

    def test_only_diagnostic_routes_and_bad_context_never_routes(self):
        flow = yaml.safe_load(((HERE / "bazel-oci.yml") if (HERE / "bazel-oci.yml").exists()
                               else worker.ROOT / ".github/workflows/bazel-oci.yml").read_text())
        self.assertEqual([name for name, job in flow["jobs"].items()
                          if condition(str(job.get("if", "true")), mode="worker-provenance")],
                         ["worker_provenance"])
        for changes in ({"event": "pull_request"}, {"event": "push"}, {"actor": "other"},
                        {"repo": "foreign/repo"}, {"mode": "registry-native"}):
            context = {"mode": "worker-provenance", **changes}
            self.assertFalse(condition(flow["jobs"]["worker_provenance"]["if"], **context))

    def test_private_marker_failure_keeps_only_stable_stage(self):
        output = self.root / "artifacts/worker-provenance/identity.json"
        def fail(_worker, _environment, progress):
            progress["stage"] = "marker_profile"
            raise ValueError("secret private marker content")
        with patch.object(worker, "ROOT", self.root), patch.dict(worker.os.environ, self.env, clear=True), \
             patch.object(worker.sys, "argv", ["worker_provenance.py", "--worker", worker.WORKERS[0],
                                              "--output", str(output)]), \
             patch.object(worker, "collect", side_effect=fail):
            self.assertEqual(worker.main(), 1)
        result = json.loads(output.read_text())
        self.assertEqual(result["failed_stage"], "marker_profile")
        self.assertEqual(result["error_class"], "ValueError")
        self.assertNotIn("private", output.read_text())
        self.assertNotIn("secret", output.read_text())


if __name__ == "__main__":
    unittest.main()
