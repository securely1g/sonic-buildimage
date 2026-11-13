"""Pure private-log fixtures: mocked processes, no namespace or assembly calls."""
import ast
from contextlib import nullcontext
import hashlib
import io
import os
import re
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tools/bazel/ci/vs_runtime_check.py"


def namespace():
    functions = {"require", "bounded", "full_vs_log_identity",
                 "full_vs_wait_with_logs", "full_vs_log_record", "diagnostics", "traceback_diagnostics"}
    constants = {"FULL_VS_PRIVATE_LOG_CAP", "FULL_VS_PRIVATE_LOG_CHUNK",
                 "FULL_VS_STAGE_LABELS", "STOPPED_LOGS", "RUNNING_LOGS"}
    body = []
    for node in ast.parse(SOURCE.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in {"ProbeFailure", "SupervisorUnclosed"}:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants
                                                 for t in node.targets):
            body.append(node)
    values = {name: globals()[name] for name in ("os", "Path", "stat", "hashlib", "time", "subprocess", "re")}
    values.update(CAP=2 * 1024 * 1024, ROOT_CLEANUP_SECONDS=420,
                  cleanup_window=nullcontext, stop_owned_session=mock.Mock(),
                  wait_group_gone=mock.Mock(return_value=True),
                  safe_failure=lambda error: {"kind": type(error).__name__},
                  grp=SimpleNamespace(getgrnam=mock.Mock(return_value=SimpleNamespace(gr_gid=77))))
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), values)
    return values


class PrivateAssemblyLogTest(unittest.TestCase):
    def setUp(self):
        self.n = namespace()
        self.temporary = tempfile.TemporaryDirectory(prefix="vs-log-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.child = SimpleNamespace(pid=991, args=["never-executed"],
                                     stdin=io.BytesIO(), wait=mock.Mock(return_value=0))
        self.calls = []

    def invoke(self, stdout=b"output", stderr=b"", effect=None, **options):
        def start(argv, **kwargs):
            self.calls.append(kwargs)
            os.write(kwargs["stdout"].fileno(), stdout)
            os.write(kwargs["stderr"].fileno(), stderr)
            if effect is not None:
                effect(kwargs)
            return self.child
        with mock.patch.object(subprocess, "Popen", side_effect=start) as self.popen:
            return self.n["bounded"](["never-executed"], self.state,
                                     options.pop("label", "full-vs-assembly"), 5, **options)

    def test_records_only_stream_hashes_sizes_and_preserves_scoped_umask(self):
        data = b"private fixture text"
        result = self.invoke(data, b"warning", private_log_records=True,
                             worker_user=True, worker_umask=0o022)
        self.assertEqual(result, {
            "stdout": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            "stderr": {"bytes": 7, "sha256": hashlib.sha256(b"warning").hexdigest()},
        })
        self.assertNotIn(data.decode(), repr(result))
        self.assertNotIn(str(self.state), repr(result))
        self.assertEqual(self.calls[0]["umask"], 0o022)
        self.assertEqual(self.calls[0]["user"], 1000)
        self.assertEqual(self.calls[0]["extra_groups"], [77])
        for name in ("stdout", "stderr"):
            self.assertEqual(stat.S_IMODE((self.state / ("full-vs-assembly." + name)).stat().st_mode), 0o600)

    def test_default_still_returns_stdout_bytes(self):
        self.assertEqual(self.invoke(b"unchanged", b"private stderr"), b"unchanged")
        self.n["wait_group_gone"].assert_called_once_with(991, 1)
        self.n["stop_owned_session"].assert_not_called()

    def test_opt_in_requires_exact_worker_scope_before_process_creation(self):
        cases = [dict(private_log_records=1, worker_user=True),
                 dict(private_log_records=True, worker_user=False),
                 dict(private_log_records=True, worker_user=True, root_supervisor=True),
                 dict(private_log_records=True, worker_user=True, label="other")]
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(self.n["ProbeFailure"], "private log scope"):
                    self.invoke(**options)
                self.popen.assert_not_called()

    def test_growth_during_wait_stops_the_owned_child(self):
        self.n["FULL_VS_PRIVATE_LOG_CAP"] = 4
        def effect(streams):
            def waiting(**unused):
                os.write(streams["stdout"].fileno(), b"12345")
                raise subprocess.TimeoutExpired("fixture", 1)
            self.child.wait.side_effect = waiting
        with self.assertRaisesRegex(self.n["ProbeFailure"], "exceeded its byte limit"):
            self.invoke(b"", effect=effect, private_log_records=True, worker_user=True)
        self.n["stop_owned_session"].assert_called_once_with(self.child)

    def test_already_oversized_output_is_refused_before_wait(self):
        self.n["FULL_VS_PRIVATE_LOG_CAP"] = 4
        with self.assertRaisesRegex(self.n["ProbeFailure"], "exceeded its byte limit"):
            self.invoke(b"12345", private_log_records=True, worker_user=True)
        self.child.wait.assert_not_called()
        self.n["stop_owned_session"].assert_called_once_with(self.child)

    def test_deadline_uses_the_existing_owned_cleanup(self):
        self.n["time"] = SimpleNamespace(monotonic=mock.Mock(side_effect=[0, 6]))
        with self.assertRaises(subprocess.TimeoutExpired):
            self.invoke(private_log_records=True, worker_user=True)
        self.n["stop_owned_session"].assert_called_once_with(self.child)

    def test_nonzero_returncode_is_not_a_successful_log_record(self):
        self.child.wait.return_value = 17
        with self.assertRaises(self.n["ProbeFailure"]) as caught:
            self.invoke(private_log_records=True, worker_user=True)
        self.assertEqual(caught.exception.returncode, 17)

    def test_surviving_group_is_stopped_before_hashing(self):
        self.n["wait_group_gone"].return_value = False
        with self.assertRaisesRegex(self.n["ProbeFailure"], "surviving descendants"):
            self.invoke(private_log_records=True, worker_user=True)
        self.n["stop_owned_session"].assert_called_once_with(self.child)

    def test_default_root_lifeline_failure_path_is_preserved(self):
        self.child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.invoke(root_supervisor=True)
        self.assertTrue(self.child.stdin.closed)
        self.assertEqual(self.child.wait.call_args_list,
                         [mock.call(timeout=5), mock.call(timeout=420)])
        self.n["stop_owned_session"].assert_not_called()

    def test_installer_output_is_independent_of_the_private_log_limit(self):
        self.n["FULL_VS_PRIVATE_LOG_CAP"] = 4
        artifact = self.state / "fixture-image.bin"
        def effect(unused):
            artifact.write_bytes(b"x" * 64)
        result = self.invoke(b"ok", effect=effect, private_log_records=True, worker_user=True)
        self.assertEqual(result["stdout"]["bytes"], 2)
        self.assertEqual(artifact.stat().st_size, 64)
        self.assertNotIn("preexec_fn", self.calls[0])

    def test_unlinked_log_fails_held_file_identity(self):
        def effect(unused):
            (self.state / "full-vs-assembly.stdout").unlink()
        with self.assertRaisesRegex(self.n["ProbeFailure"], "private log identity"):
            self.invoke(effect=effect, private_log_records=True, worker_user=True)
        self.n["stop_owned_session"].assert_called_once_with(self.child)

    def test_replaced_name_is_refused_after_the_group_closes(self):
        def waiting(**unused):
            original = self.state / "full-vs-assembly.stdout"
            original.rename(self.state / "retained-original")
            original.write_bytes(b"replacement")
            return 0
        self.child.wait.side_effect = waiting
        with self.assertRaisesRegex(self.n["ProbeFailure"], "pathname changed"):
            self.invoke(private_log_records=True, worker_user=True)

    def test_non_private_mode_is_refused(self):
        path = self.state / "mode-fixture"
        with path.open("x+b") as stream:
            os.fchmod(stream.fileno(), 0o644)
            with self.assertRaisesRegex(self.n["ProbeFailure"], "private log identity"):
                self.n["full_vs_log_record"](stream, path)

    def test_hash_reads_are_chunked_and_match_the_held_bytes(self):
        self.n["FULL_VS_PRIVATE_LOG_CHUNK"] = 3
        path = self.state / "chunk-fixture"
        calls = []
        with path.open("x+b") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(b"abcdefgh")
            class Reader:
                fileno = stream.fileno
                flush = stream.flush
                seek = stream.seek
                def read(self, count):
                    calls.append(count)
                    return stream.read(count)
            result = self.n["full_vs_log_record"](Reader(), path)
        self.assertEqual(result, {"bytes": 8, "sha256": hashlib.sha256(b"abcdefgh").hexdigest()})
        self.assertEqual(calls, [3, 3, 3, 3])

    def test_cleanup_error_retains_the_first_authored_failure(self):
        self.n["FULL_VS_PRIVATE_LOG_CAP"] = 4
        self.n["stop_owned_session"].side_effect = RuntimeError("fixture cleanup")
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(b"12345", private_log_records=True, worker_user=True)
        self.assertEqual(caught.exception._probe_original_failure, {"kind": "ProbeFailure"})



    def test_failed_make_retains_complete_hash_and_allowlisted_source_line(self):
        private = b"make: *** [slave.mk:1900: target/PRIVATE_TOKEN_X] Error 2\n"
        self.child.wait.return_value = 2
        with self.assertRaises(self.n["ProbeFailure"]):
            self.invoke(b"PRIVATE_TOKEN_X\n", private, private_log_records=True, worker_user=True)
        result = self.n["diagnostics"](self.state)
        stderr = next(row for row in result if row["stage_log"] == "full-vs-assembly.stderr")
        self.assertEqual(stderr["bytes"], len(private))
        self.assertEqual(stderr["complete_log"]["sha256"], hashlib.sha256(private).hexdigest())
        self.assertIn({"source": "slave.mk", "line": 1900}, stderr["source_frames"])
        self.assertIn({"error_type": "MakeError", "returncode": 2}, stderr["exception_facts"])
        self.assertNotIn("PRIVATE_TOKEN_X", repr(result))
        self.assertTrue(all(row["raw_text_published"] is False for row in result))

    def test_planner_and_kernel_traceback_sites_do_not_publish_private_text(self):
        expected = (("full-vs-plan", "tools/bazel/ci/vs_reuse_plan.py"),
                    ("full-vs-kernel-verify", "tools/bazel/kernel/prepare.py"),
                    ("full-vs-post-archive-verify", "tools/bazel/ci/vs_reuse_plan.py"),
                    ("full-vs-installer-version", "scripts/validate_sonic_version.py"),
                    ("full-vs-installed-images", "tools/bazel/ci/validate_runtime.py"))
        for label, source in expected:
            body = ('Traceback (most recent call last):\n'
                    '  File "/actions/_work/PRIVATE_TOKEN_X/' + source + '", line 321, in verify\n'
                    '    raise ValueError("PRIVATE_TOKEN_X")\n'
                    'ValueError: PRIVATE_TOKEN_X\n').encode()
            (self.state / (label + ".stderr")).write_bytes(body)
        result = self.n["diagnostics"](self.state)
        self.assertEqual({frame["source"] for row in result for frame in row["source_frames"]},
                         {source for _, source in expected})
        self.assertEqual({row["stage_log"] for row in result}, {label + ".stderr" for label, _ in expected})
        self.assertNotIn("PRIVATE_TOKEN_X", repr(result))
        self.assertTrue(all(row["tail_sha256"] for row in result))

    def test_working_directory_is_scoped_to_worker_children(self):
        for options in ({"working_directory": self.state, "worker_user": False},
                        {"working_directory": "relative", "worker_user": True}):
            with self.assertRaisesRegex(self.n["ProbeFailure"], "working directory"):
                self.invoke(**options)
        self.invoke(worker_user=True, working_directory=self.state)
        self.assertEqual(self.calls[-1]["cwd"], str(self.state))


if __name__ == "__main__":
    unittest.main()
