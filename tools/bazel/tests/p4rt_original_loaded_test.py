"""Small in-memory/file fixtures for fixed original P4RT materialization."""
from contextlib import ExitStack
import copy
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from tools.bazel.ci import p4rt_original_loaded as loaded
from tools.bazel.tests import vs_runtime_test as runtime_tests
runtime = loaded.runtime


class OriginalLoadedInputsTest(unittest.TestCase):
    def test_original_validation_report_hash_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "validation.json"
            original = b'{"status":"passed"}\n'
            path.write_bytes(original)
            expected = hashlib.sha256(original).hexdigest()
            self.assertEqual(loaded.read_record(path, expected_sha256=expected)["status"], "passed")
            path.write_bytes(b'{"status":"failed"}\n')
            with self.assertRaisesRegex(ValueError, "Original validation report hash differs"):
                loaded.read_record(path, expected_sha256=expected)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payloads = {"p4rt.source.tar": b"original opaque TAR fixture",
                         "validation.json": b'{"status":"passed"}\n',
                         "docker-sonic-p4rt.runtime.json": b"historical runtime evidence"}
        self.payloads.update({f"public-{i:02d}.json": b"{}\n" for i in range(57)})
        self.fixed = {"members_to_materialize": [
            {"name": name, "bytes": len(self.payloads[name]),
             "sha256": hashlib.sha256(self.payloads[name]).hexdigest()}
            for name in ("p4rt.source.tar", "validation.json")],
            "fresh_report_names": ["docker-sonic-p4rt.runtime.json", "docker-sonic-p4rt-dbg.runtime.json"],
            "materialized_members": 2,
            "materialized_total_bytes": sum(len(self.payloads[x]) for x in
                                             ("p4rt.source.tar", "validation.json"))}
        self.write_zip()
        patcher = mock.patch.object(loaded.os, "fstatvfs", return_value=SimpleNamespace(
            f_bavail=16 * 1024**3, f_frsize=1))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_zip(self, *, symlink=None, duplicate=None):
        with zipfile.ZipFile(self.root / "original.zip", "w", zipfile.ZIP_STORED) as archive:
            for name, raw in self.payloads.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = ((stat.S_IFLNK if name == symlink else stat.S_IFREG) | 0o600) << 16
                archive.writestr(info, raw)
            if duplicate is not None:
                archive.writestr(duplicate, b"duplicate")
        raw = (self.root / "original.zip").read_bytes()
        self.fixed["original_producer"] = {"artifact_id": 13803477455, "conclusion": "failure",
                                          "run_id": 42860613563, "job_id": 129985415221,
                                          "zip_bytes": len(raw), "zip_sha256": hashlib.sha256(raw).hexdigest()}

    def materialize(self, fixed=None, expires=None):
        with loaded.intake.directory(self.root) as directory:
            return loaded.materialize(directory, fixed or self.fixed,
                                      expires if expires is not None else time.monotonic() + 10)

    def test_exact_original_bytes_and_history_separation(self):
        result = self.materialize()
        active = self.root / "loaded-inputs"
        self.assertEqual(sorted(x.name for x in active.iterdir()), ["p4rt.source.tar", "validation.json"])
        for row in self.fixed["members_to_materialize"]:
            self.assertEqual((active / row["name"]).read_bytes(), self.payloads[row["name"]])
            self.assertEqual(stat.S_IMODE((active / row["name"]).stat().st_mode), 0o600)
        self.assertFalse(result["historical_runtime_reports_copied"])
        self.assertFalse(result["fresh_runtime_validation"])
        self.assertFalse(result["canonical_native_acceptance"])
        self.assertEqual(result["original_producer"]["conclusion"], "failure")
        self.assertTrue((self.root / "original.zip").is_file())

    def test_changed_zip_rejected_before_output_directory(self):
        with (self.root / "original.zip").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_selected_hash_mismatch_is_not_accepted(self):
        changed = copy.deepcopy(self.fixed)
        changed["members_to_materialize"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.materialize(changed)

    def test_selected_missing_before_copy(self):
        del self.payloads["p4rt.source.tar"]
        self.payloads["unexpected.tar"] = b"different"
        self.write_zip()
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_duplicate_central_name_rejected(self):
        del self.payloads["public-00.json"]
        self.write_zip(duplicate="validation.json")
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_symlink_member_rejected_before_copy(self):
        self.write_zip(symlink="p4rt.source.tar")
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_existing_active_directory_preserved(self):
        active = self.root / "loaded-inputs"
        active.mkdir()
        (active / "sentinel").write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            self.materialize()
        self.assertEqual((active / "sentinel").read_bytes(), b"preserve")

    def test_original_zip_symlink_is_never_followed(self):
        original = self.root / "original.zip"
        original.rename(self.root / "saved.zip")
        original.symlink_to("saved.zip")
        with self.assertRaises(OSError):
            self.materialize()
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_unsafe_selected_name_rejected(self):
        changed = copy.deepcopy(self.fixed)
        changed["members_to_materialize"][0]["name"] = "../outside"
        with self.assertRaises(ValueError):
            self.materialize(changed)
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_historical_runtime_is_not_an_active_input(self):
        changed = copy.deepcopy(self.fixed)
        changed["members_to_materialize"][0]["name"] = "docker-sonic-p4rt.runtime.json"
        with self.assertRaises(ValueError):
            self.materialize(changed)

    def test_expired_materialization_has_no_output(self):
        with self.assertRaises(ValueError):
            self.materialize(expires=time.monotonic() - 1)
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_declared_member_size_mismatch_is_rejected(self):
        self.payloads["p4rt.source.tar"] += b"different bytes"
        self.write_zip()
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertFalse((self.root / "loaded-inputs").exists())

    def test_whole_original_sha_is_required_even_with_matching_selected_bytes(self):
        self.payloads["public-00.json"] = b'{"changed_unselected":true}\n'
        expected = copy.deepcopy(self.fixed)
        self.write_zip()
        with self.assertRaises(ValueError):
            self.materialize(expected)
        self.assertFalse((self.root / "loaded-inputs").exists())

    def collection_record(self):
        row = {"worker": "ks-fixture", "run": "123", "attempt": "1",
               "controller": "1" * 40, "pool": "sonic-slave-container-k3s"}
        original = self.fixed["original_producer"]
        record = dict(row, status="ORIGINAL_P4RT_ZIP_INVENTORY_ONLY",
                      public_semantic_validation=True,
                      public_semantic_review="PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED",
                      canonical_native_acceptance=False, PR206_adoption=False,
                      original_artifact=original["artifact_id"], original_run=original["run_id"],
                      original_job=original["job_id"], original_conclusion="failure",
                      zip={"bytes": original["zip_bytes"], "sha256": original["zip_sha256"]})
        return row, record

    def test_consistent_public_inventory_stays_failed_producer_data(self):
        row, record = self.collection_record()
        loaded.require_collection(record, row, self.fixed)
        self.assertEqual(record["original_conclusion"], "failure")
        self.assertFalse(record["canonical_native_acceptance"])

    def test_successful_job_without_public_semantic_consistency_is_insufficient(self):
        row, record = self.collection_record()
        record["public_semantic_validation"] = False
        with self.assertRaises(ValueError):
            loaded.require_collection(record, row, self.fixed)

    def test_other_attempt_or_worker_cannot_supply_active_inputs(self):
        row, record = self.collection_record()
        for field, value in (("attempt", "2"), ("worker", "another"), ("controller", "2" * 40)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                loaded.require_collection(dict(record, **{field: value}), row, self.fixed)

    def test_original_failure_cannot_be_relabelled_success(self):
        row, record = self.collection_record()
        record["original_conclusion"] = "success"
        with self.assertRaises(ValueError):
            loaded.require_collection(record, row, self.fixed)

    def test_fixed_loaded_guard_reason_does_not_publish_paths(self):
        raw = (b'Traceback (most recent call last):\n  File "/private/path.py", line 30\n'
               b'ValueError: Loaded checkout differs from the built source\n')
        self.assertEqual(loaded.loaded_error_reason(raw), "original_source_revision_mismatch")

    def test_loaded_tool_error_without_traceback_is_not_a_guard(self):
        self.assertIsNone(loaded.loaded_error_reason(b'ValueError: Loaded P4RT platform differs\n'))

    def test_unknown_text_and_secret_suffix_are_never_projected(self):
        for message in (b'Unknown failure token=secret',
                        b'Loaded P4RT platform differs https://user:secret@example.invalid'):
            with self.subTest(message=message):
                self.assertIsNone(loaded.loaded_error_reason(
                    b'Traceback (most recent call last):\nValueError: ' + message + b'\n'))

    def test_installed_probe_failure_is_a_reason_only(self):
        raw = (b'Traceback (most recent call last):\nValueError: docker-sonic-p4rt-dbg installed probe failed:\n'
               b'private child text including token=secret\n')
        self.assertEqual(loaded.loaded_error_reason(raw), "installed_debug_probe_failed")


class OriginalP4RTLoadedLifecycleTest(unittest.TestCase):
    def test_fixed_loaded_identity_does_not_change_default_probe(self):
        self.assertEqual(runtime.native_identity({}), ("gearbox", runtime.NATIVE_CONTROLLER))
        self.assertEqual(runtime.native_identity({"original_p4rt_loaded": True}),
                         ("p4rt", "4bf648e812f28b05a2683c6cf7559470b0883d2a"))
        for bad in (False, "p4rt", 1, None):
            with self.subTest(bad=bad), self.assertRaises(runtime.ProbeFailure):
                runtime.native_identity({"original_p4rt_loaded": bad})

    def test_loaded_capture_uses_same_private_endpoint_and_fixed_controller(self):
        paths = {"state": Path("/actions/_work/_temp/owned"), "exec": Path("/run/owned"),
                 "identity": {"worker": "ks-fixture", "run": "123", "attempt": "1"},
                 "original_p4rt_loaded": True}
        with mock.patch.object(runtime, "bounded") as bounded:
            runtime.native_action("capture-docker-mount-before", paths, Path("/native"), 30)
        argv, state, label, seconds, environment = bounded.call_args.args
        self.assertEqual(argv[argv.index("--image") + 1], "p4rt")
        self.assertEqual(argv[argv.index("--revision") + 1], runtime.ORIGINAL_P4RT_NATIVE_CONTROLLER)
        self.assertEqual(environment["RUNNER_TEMP"], str(paths["state"]))
        self.assertEqual(environment["GITHUB_RUN_ID"], "123")
        self.assertNotIn("BAZEL_REMOTE_AUTH", environment)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertTrue(bounded.call_args.kwargs["worker_user"])

    def test_loaded_body_bound_covers_start_captures_and_original_probe(self):
        self.assertGreaterEqual(runtime.ORIGINAL_P4RT_BODY_SECONDS, 2100 + 120 + 30 + 2 * 30)
        self.assertEqual(runtime.ROOT_CLEANUP_SECONDS, 420)

    def test_loaded_workload_order_failure_and_proved_cleanup(self):
        for failure in (None, "loaded", "stop"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                paths = runtime_tests.NativeCaptureLifecycleTest.paths(self, directory)
                paths["original_p4rt_loaded"] = True
                child = mock.Mock(pid=321, returncode=None)
                child.poll.return_value = None
                events = []
                def capture(stage, *_):
                    events.append(stage)
                    if stage == "before":
                        self.assertFalse(paths["docker_data"].exists())
                    if stage == failure:
                        raise runtime.ProbeFailure("Capture failed")
                    return {"status": "passed"}
                def spawn(*args, **kwargs):
                    events.append("spawn")
                    paths["docker_data"].mkdir()
                    (paths["exec"] / "dockerd.pid").write_text("321\n")
                    return child
                def loaded(*_):
                    events.append("loaded")
                    if failure == "loaded":
                        raise runtime.ProbeFailure("Original loaded workload failed")
                    return {"status": "passed"}
                def stopped(*_):
                    events.append("stopped")
                    return {"status": "passed", "ever_started_observed": False}
                def running(*_):
                    events.append("running")
                    return {"status": "passed", "running_observed": True}
                def native_stop(*_):
                    events.append("native-stop")
                    if failure == "stop":
                        raise runtime.ProbeFailure("Proved stop failed")
                    child.returncode = 0
                    return {"status": "passed"}
                def fallback(owned):
                    self.assertIs(owned, child)
                    events.append("fallback")
                    child.returncode = 0
                    return True
                original_stat = Path.stat
                def private_stat(path, *args, **kwargs):
                    info = original_stat(path, *args, **kwargs)
                    if path == paths["state"]:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=1000, st_gid=1000)
                    return info
                with ExitStack() as stack:
                    for patch in (
                        mock.patch.object(runtime.os, "geteuid", return_value=0),
                        mock.patch.object(runtime.Path, "stat", autospec=True, side_effect=private_stat),
                        mock.patch.object(runtime.Path, "is_file", return_value=True),
                        mock.patch.object(runtime.inspection, "marker", return_value={"base_image": runtime.BASE_IMAGE, "manager_revision": runtime.MANAGER}),
                        mock.patch.object(runtime, "native_stop_source", return_value=Path("/native-cleanup")),
                        mock.patch.object(runtime, "native_capture", side_effect=capture),
                        mock.patch.object(runtime, "native_stop", side_effect=native_stop),
                        mock.patch.object(runtime, "native_mount_diagnostics", return_value=None),
                        mock.patch.object(runtime, "checked_helper", return_value=Path("/reviewed")),
                        mock.patch.object(runtime.shutil, "which", return_value="reviewed"),
                        mock.patch.object(runtime.os, "fstat", return_value=mock.Mock(st_mode=stat.S_IFIFO)),
                        mock.patch.object(runtime.os, "set_blocking"),
                        mock.patch.object(runtime.os, "chown"),
                        mock.patch.object(runtime.signal, "signal"),
                        mock.patch.object(runtime.signal, "setitimer"),
                        mock.patch.object(runtime, "root_lifeline_tick"),
                        mock.patch.object(runtime, "bounded"),
                        mock.patch.object(runtime.subprocess, "Popen", side_effect=spawn),
                        mock.patch.object(runtime, "docker_information", return_value={}),
                        mock.patch.object(runtime, "validate_daemon", return_value="28.5.2"),
                        mock.patch.object(runtime, "stopped_container", side_effect=stopped),
                        mock.patch.object(runtime, "running_container", side_effect=running),
                        mock.patch.object(runtime, "stop_owned_daemon", side_effect=fallback),
                        mock.patch.object(runtime, "diagnostics", return_value=[]),
                    ):
                        stack.enter_context(patch)
                    result = runtime.private_daemon(paths, p4rt_workload=loaded)
                expected = ["before", "spawn", "ready", "loaded", "native-stop"]
                if failure == "stop":
                    expected.append("fallback")
                self.assertEqual(events, expected)
                self.assertTrue(result["daemon_closed"])
                self.assertEqual(result["status"], "failed" if failure else "passed")


if __name__ == "__main__":
    unittest.main()
