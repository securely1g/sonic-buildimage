"""Small in-memory/file fixtures for fixed original P4RT materialization."""
from contextlib import ExitStack
import copy
import hashlib
import json
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


class ProbeInputPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = self.root / "loaded-inputs"
        self.original.mkdir(mode=0o700)
        self.contents = {"validation.json": b'{"status":"passed"}\n', "payload.tar": b"original-payload"}
        self.fixed = {"members_to_materialize": [], "fresh_report_names": ["fresh.runtime.json"]}
        for name, value in self.contents.items():
            path = self.original / name
            path.write_bytes(value)
            path.chmod(0o600)
            self.fixed["members_to_materialize"].append({"name": name, "bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
                "role": "original_validation_report" if name == "validation.json" else "original_payload"})

    def stage(self):
        with loaded.intake.directory(self.root) as parent:
            result = loaded.stage_probe_inputs(parent, self.fixed, time.monotonic() + 10)
            loaded.intake.write_json(parent, "materialization.json", {"probe_inputs": result})
        return self.root / "loaded-probe-inputs", result

    def test_public_copy_is_distinct_and_private_payload_has_one_owned_alias(self):
        before = {name: (self.original / name).stat() for name in self.contents}
        active, result = self.stage()
        self.assertEqual(loaded.checked_probe_inputs(self.original, self.fixed), active)
        self.assertEqual(stat.S_IMODE(active.stat().st_mode), 0o700)
        for name, value in self.contents.items():
            retained, selected = self.original / name, active / name
            self.assertEqual(retained.read_bytes(), value)
            self.assertEqual(selected.read_bytes(), value)
            self.assertEqual(stat.S_IMODE(retained.stat().st_mode), 0o600)
            self.assertTrue(os.path.samestat(before[name], retained.stat()))
            if name == "validation.json":
                self.assertFalse(os.path.samestat(retained.stat(), selected.stat()))
                self.assertEqual(stat.S_IMODE(selected.stat().st_mode), 0o644)
                self.assertEqual(retained.stat().st_nlink, 1)
            else:
                self.assertTrue(os.path.samestat(retained.stat(), selected.stat()))
                self.assertEqual(selected.stat().st_nlink, 2)
                self.assertEqual(stat.S_IMODE(selected.stat().st_mode), 0o600)
        self.assertTrue(result["owned_links_change_nlink_and_ctime"])
        self.assertEqual(result["private_input_links"], 1)

    def test_existing_probe_directory_is_preserved(self):
        active = self.root / "loaded-probe-inputs"
        active.mkdir()
        (active / "sentinel").write_bytes(b"preserve")
        with loaded.intake.directory(self.root) as parent, self.assertRaises(FileExistsError):
            loaded.stage_probe_inputs(parent, self.fixed, time.monotonic() + 10)
        self.assertEqual((active / "sentinel").read_bytes(), b"preserve")
        self.assertEqual((self.original / "payload.tar").stat().st_nlink, 1)

    def test_original_with_an_unowned_alias_is_refused(self):
        os.link(self.original / "payload.tar", self.root / "outside-alias")
        with loaded.intake.directory(self.root) as parent, self.assertRaisesRegex(ValueError, "unique inode"):
            loaded.stage_probe_inputs(parent, self.fixed, time.monotonic() + 10)
        self.assertEqual((self.original / "payload.tar").read_bytes(), self.contents["payload.tar"])

    def test_changed_validation_hash_never_becomes_public_readable(self):
        row = next(row for row in self.fixed["members_to_materialize"] if row["name"] == "validation.json")
        row["sha256"] = "0" * 64
        with loaded.intake.directory(self.root) as parent, self.assertRaises(ValueError):
            loaded.stage_probe_inputs(parent, self.fixed, time.monotonic() + 10)
        self.assertEqual(stat.S_IMODE((self.root / "loaded-probe-inputs/validation.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.original / "validation.json").stat().st_mode), 0o600)

    def test_non_public_json_is_refused_before_mode_change(self):
        value = b'{"stderr":"private diagnostic"}\n'
        (self.original / "validation.json").write_bytes(value)
        row = next(row for row in self.fixed["members_to_materialize"] if row["name"] == "validation.json")
        row.update(bytes=len(value), sha256=hashlib.sha256(value).hexdigest())
        with loaded.intake.directory(self.root) as parent, self.assertRaises(loaded.intake.public_artifacts.PublicArtifactError):
            loaded.stage_probe_inputs(parent, self.fixed, time.monotonic() + 10)
        self.assertEqual(stat.S_IMODE((self.root / "loaded-probe-inputs/validation.json").stat().st_mode), 0o600)

    def test_replaced_alias_and_extra_link_are_refused(self):
        active, _ = self.stage()
        os.link(active / "payload.tar", self.root / "third-alias")
        with self.assertRaisesRegex(ValueError, "Private probe alias differs"):
            loaded.checked_probe_inputs(self.original, self.fixed)
        (self.root / "third-alias").unlink()
        (active / "payload.tar").unlink()
        (active / "payload.tar").write_bytes(self.contents["payload.tar"])
        (active / "payload.tar").chmod(0o600)
        with self.assertRaisesRegex(ValueError, "Private probe alias differs"):
            loaded.checked_probe_inputs(self.original, self.fixed)

    def test_public_copy_mutation_and_existing_runtime_report_refuse(self):
        active, _ = self.stage()
        (active / "validation.json").write_bytes(b'{"status":"failed"}\n')
        with self.assertRaises(ValueError):
            loaded.checked_probe_inputs(self.original, self.fixed)
        (active / "validation.json").write_bytes(self.contents["validation.json"])
        (active / "fresh.runtime.json").symlink_to(self.root / "absent")
        with self.assertRaisesRegex(ValueError, "Preserve existing fresh probe report"):
            loaded.checked_probe_inputs(self.original, self.fixed)

    def test_open_failure_closes_the_already_owned_directory_fd(self):
        real_open, real_close = os.open, os.close
        captured = []
        def opening(path, flags, *args, **kwargs):
            if path == "loaded-probe-inputs":
                raise PermissionError("fixture")
            fd = real_open(path, flags, *args, **kwargs)
            if path == "loaded-inputs":
                captured.append(fd)
            return fd
        with loaded.intake.directory(self.root) as parent:
            with mock.patch.object(loaded.os, "open", side_effect=opening), mock.patch.object(loaded.os, "close", wraps=real_close) as close:
                with self.assertRaises(PermissionError):
                    loaded.stage_probe_inputs(parent, self.fixed, time.monotonic() + 10)
                self.assertEqual(len(captured), 1)
                close.assert_called_once_with(captured[0])

    def test_umask_is_a_child_only_popen_option(self):
        child = SimpleNamespace(pid=1234, wait=mock.Mock(return_value=0))
        with mock.patch.object(runtime.subprocess, "Popen", return_value=child) as spawn, \
             mock.patch.object(runtime.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=4321)), \
             mock.patch.object(runtime, "wait_group_gone", return_value=True), \
             mock.patch.object(runtime.os, "umask", side_effect=AssertionError("parent umask must remain unchanged")):
            self.assertEqual(runtime.bounded(["fixture"], self.root, "child-mask", 1,
                                            worker_user=True, worker_umask=0o022), b"")
        self.assertEqual(spawn.call_args.kwargs["umask"], 0o022)
        self.assertEqual(spawn.call_args.kwargs["user"], 1000)
        self.assertEqual(spawn.call_args.kwargs["group"], 1000)
        self.assertEqual(spawn.call_args.kwargs["extra_groups"], [4321])

    def test_umask_override_is_unavailable_to_other_scopes(self):
        with mock.patch.object(runtime.subprocess, "Popen") as spawn:
            for user, mask in ((False, 0o022), (True, 0o077), (True, True)):
                with self.assertRaises(runtime.ProbeFailure):
                    runtime.bounded(["fixture"], self.root, "bad-mask", 1, worker_user=user, worker_umask=mask)
        spawn.assert_not_called()


class LoadedProbeDiagnosticsTest(unittest.TestCase):
    def raw(self, inner, image="docker-sonic-p4rt"):
        return ("Traceback (most recent call last):\n"
                '  File "/private/source/tools/bazel/ci/p4rt_loaded.py", line 94, in main\n'
                "ValueError: " + image + " installed probe failed:\n" + inner).encode()

    def test_validation_permission_has_fixed_check_and_no_raw_path(self):
        raw = self.raw('Traceback (most recent call last):\n'
                       '  File "/probe/runtime_probe.py", line 120, in main\n'
                       "PermissionError: [Errno 13] Permission denied: '/probe/validation.json'\n")
        result = loaded.loaded_probe_diagnostics(raw)
        self.assertEqual(result["image"], "runtime")
        self.assertEqual(result["exceptions"], [{"kind": "PermissionError", "errno": 13}])
        self.assertEqual(result["check_ids"], ["validation_json_read_denied"])
        self.assertEqual(result["source_frames"], [{"source": "original_runtime_probe", "line": 120}])
        self.assertNotIn("/private", json.dumps(result))
        self.assertNotIn("/probe/validation.json", json.dumps(result))

    def test_services_directory_config_failure_is_separate(self):
        raw = self.raw("Traceback (most recent call last):\nPermissionError: [Errno 13] Permission denied: '/probe/services/database_config.json'\n", "docker-sonic-p4rt-dbg")
        result = loaded.loaded_probe_diagnostics(raw)
        self.assertEqual(result["image"], "debug")
        self.assertEqual(result["check_ids"], ["database_config_read_denied"])

    def test_known_frames_and_exception_types_discard_all_message_text(self):
        raw = self.raw('Traceback (most recent call last):\n'
                       '  File "/probe/runtime_smoke.py", line 20, in probe\n'
                       '  File "/probe/p4rt_runtime_package.py", line 30, in debug_lookup\n'
                       '  File "/secret/credential.py", line 40, in private\n'
                       'ModuleNotFoundError: secret-module\n'
                       'subprocess.TimeoutExpired: private command token\n'
                       'UnknownPrivateException: sensitive\n')
        result = loaded.loaded_probe_diagnostics(raw)
        self.assertEqual([row["source"] for row in result["source_frames"]],
                         ["original_runtime_smoke", "original_owner_runtime_helper"])
        self.assertEqual([row["kind"] for row in result["exceptions"]], ["ModuleNotFoundError", "TimeoutExpired"])
        for private in ("secret", "credential", "token", "sensitive", "UnknownPrivateException"):
            self.assertNotIn(private, json.dumps(result))

    def test_no_outer_traceback_or_ambiguous_guard_has_no_projected_claim(self):
        for raw in (b"ValueError: docker-sonic-p4rt installed probe failed:\nTraceback (most recent call last):\nPermissionError: private\n",
                    self.raw("ValueError: docker-sonic-p4rt-dbg installed probe failed:\n")):
            result = loaded.loaded_probe_diagnostics(raw)
            self.assertEqual(result["status"], "unrecognized")
            self.assertEqual(result["exceptions"], [])
            self.assertEqual(result["source_frames"], [])

    def test_known_check_prefix_does_not_publish_installed_path_or_tool_output(self):
        raw = self.raw("Traceback (most recent call last):\nAssertionError: installed source differs: /private/token\n"
                       "AssertionError: installed P4Runtime VERIFY failed: private-rpc-output\n")
        result = loaded.loaded_probe_diagnostics(raw)
        self.assertEqual(result["check_ids"], ["installed_source_hash_differs", "p4runtime_verify_failed"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("token", json.dumps(result))

    def test_projection_bounds_mark_omitted_frames_and_long_lines(self):
        rows = ''.join('  File "/probe/runtime_probe.py", line %d, in main\n' % line for line in range(1, 30))
        result = loaded.loaded_probe_diagnostics(self.raw("Traceback (most recent call last):\n" + rows + "x" * 3000))
        self.assertEqual(len(result["source_frames"]), 16)
        self.assertTrue(result["truncated"])
        self.assertLess(len(json.dumps(result)), 4096)

    def test_inner_error_text_without_nested_traceback_is_not_classified(self):
        result = loaded.loaded_probe_diagnostics(self.raw("PermissionError: [Errno 13] Permission denied: '/probe/validation.json'\n"))
        self.assertEqual(result["exceptions"], [])
        self.assertEqual(result["check_ids"], [])

    def test_existing_failure_reader_adds_typed_facts_and_keeps_raw_private(self):
        raw = self.raw("Traceback (most recent call last):\nPermissionError: [Errno 13] Permission denied: '/probe/validation.json'\n")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = state / "original-p4rt-loaded.stderr"
            path.write_bytes(raw)
            path.chmod(0o600)
            value = loaded.loaded_failure_evidence(state, ValueError("private error"))
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(value["stderr"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(value["stderr"]["probe_failure"]["check_ids"], ["validation_json_read_denied"])
        self.assertFalse(value["canonical_native_acceptance"])
        self.assertNotIn("private error", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
