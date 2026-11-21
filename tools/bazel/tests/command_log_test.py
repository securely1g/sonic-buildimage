"""Check command evidence and artifact-path separation."""

import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel.ci import command_log

OUTAGE = ("ERROR: /tools/BUILD.bazel:25:12: Executing genrule failed: "
          "Failed to query remote execution capabilities: finishConnect(..) failed: "
          "Connection refused: /172.16.0.4:9092\n"
          "ERROR: Build did NOT complete successfully\n")


class CommandEvidenceTest(unittest.TestCase):
    """Keep command results separate from their retained diagnostic evidence."""

    def test_diagnostics_cannot_become_an_artifact_path(self):
        """Return only stdout for path lookup while retaining stderr and success status."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            result = command_log.execute(
                [sys.executable, "-c", "import sys; print('runtime.tar'); print('INFO: query', file=sys.stderr)"],
                directory, receipt, "query", cwd=directory)
            self.assertEqual(result.splitlines(), ["runtime.tar"])
            self.assertIn("INFO: query", (directory / "query.log").read_text())
            self.assertEqual(receipt["commands"][0]["returncode"], 0)

    def test_failed_build_retains_its_diagnostics(self):
        """Record a failing command's message and exit status before propagating failure."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            with self.assertRaises(subprocess.CalledProcessError):
                command_log.execute([sys.executable, "-c", "raise SystemExit('build failed')"],
                           directory, receipt, "build", cwd=directory)
            self.assertIn("build failed", (directory / "build.log").read_text())
            self.assertEqual(receipt["commands"][0]["returncode"], 1)

    def test_private_query_output_stays_out_of_logs_even_on_failure(self):
        """Keep large action JSON private while preserving diagnostics and exit status."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / "actions.json"
            output.touch(mode=0o600)
            for returncode in (0, 7):
                with self.subTest(returncode=returncode):
                    receipt = {"commands": []}
                    console = io.StringIO()
                    command = [sys.executable, "-c",
                               "import sys; print('private-action-data'); "
                               "print('query diagnostic', file=sys.stderr); "
                               "sys.exit(" + str(returncode) + ")"]
                    with contextlib.redirect_stdout(console):
                        if returncode:
                            with self.assertRaises(subprocess.CalledProcessError):
                                command_log.execute(command, directory, receipt, "query",
                                                    cwd=directory, output_path=output)
                        else:
                            self.assertEqual(command_log.execute(
                                command, directory, receipt, "query", cwd=directory,
                                output_path=output), "")
                    self.assertEqual(output.read_text(), "private-action-data\n")
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual((directory / "query.log").read_text(), "query diagnostic\n")
                    self.assertEqual(console.getvalue(), "query diagnostic\n")
                    self.assertEqual(receipt["commands"][0]["returncode"], returncode)

    def test_cache_recovery_returns_only_final_stdout_and_retains_each_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            command = ["bazel", "cquery", "--remote_cache=grpc://cache:9092", "//:archive"]
            responses = [
                subprocess.CompletedProcess(command, 34, stdout="partial path\n", stderr=OUTAGE),
                subprocess.CompletedProcess(command, 34, stdout="", stderr=OUTAGE),
                subprocess.CompletedProcess(command, 0, stdout="runtime.tar\n", stderr=""),
            ]
            with mock.patch.object(command_log.subprocess, "run", side_effect=responses) as run, \
                    mock.patch.object(command_log.time, "sleep") as sleep, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                output = command_log.execute(command, directory, receipt, "query", cwd=directory)
            self.assertEqual(output, "runtime.tar\n")
            self.assertEqual(run.call_count, 3)
            self.assertTrue(all(call.args[0] == command and call.kwargs["cwd"] == directory
                                for call in run.call_args_list))
            self.assertEqual(sleep.call_args_list, [mock.call(10), mock.call(30)])
            entry, = receipt["commands"]
            self.assertEqual(entry["returncode"], 0)
            self.assertEqual([attempt["returncode"] for attempt in entry["attempts"]], [34, 34, 0])
            self.assertIn(OUTAGE, (directory / "query.attempt-1.log").read_text())
            self.assertIn(OUTAGE, (directory / "query.attempt-2.log").read_text())
            self.assertEqual((directory / "query.log").read_text(), "runtime.tar\n")

    def test_cache_outage_exhaustion_preserves_failure_without_changing_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = {"commands": []}
            command = ["bazel", "build", "--remote_upload_local_results=true", "//:archive"]
            result = subprocess.CompletedProcess(command, 34, stdout="", stderr=OUTAGE)
            with mock.patch.object(command_log.subprocess, "run", return_value=result) as run, \
                    mock.patch.object(command_log.time, "sleep"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    command_log.execute(command, directory, receipt, "build", cwd=directory)
            self.assertEqual(failure.exception.returncode, 34)
            self.assertEqual(run.call_count, 3)
            self.assertTrue(all(call.args[0] == command for call in run.call_args_list))
            self.assertEqual(receipt["commands"][0]["returncode"], 34)
            self.assertEqual(len(receipt["commands"][0]["attempts"]), 3)

    def test_http_cache_service_failure_retries_exit_one(self):
        error = subprocess.CalledProcessError(
            1, ["bazel", "build", "//:archive"], stderr=
            "ERROR: Error reading from the remote cache: Unexpected HTTP response code: 503\n")
        run = mock.Mock(side_effect=[error, "runtime.tar"])
        with mock.patch.object(command_log.time, "sleep") as sleep, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(command_log.retry_remote_cache(run), "runtime.tar")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_streamed_build_keeps_live_progress_and_retries_without_buffering_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            console = io.StringIO()
            calls = []

            def start(command, **kwargs):
                calls.append(command)
                process = mock.MagicMock()
                process.__enter__.return_value = process
                message = OUTAGE if len(calls) == 1 else "Built archive\n"

                def lines():
                    yield message
                    self.assertIn(message, console.getvalue())

                process.stdout = lines()
                process.wait.return_value = 34 if len(calls) == 1 else 0
                return process

            receipt = {"commands": []}
            with mock.patch.object(command_log.subprocess, "Popen", side_effect=start), \
                    mock.patch.object(command_log.time, "sleep"), \
                    contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                self.assertEqual(command_log.execute(
                    ["bazel", "build", "//:archive"], directory, receipt, "build",
                    cwd=directory, stream=True), "")
            self.assertEqual(len(calls), 2)
            self.assertEqual((directory / "build.log").read_text(), "Built archive\n")
            self.assertEqual((directory / "build.attempt-1.log").read_text(), OUTAGE)
            self.assertEqual(len(receipt["commands"][0]["attempts"]), 2)

    def test_private_graph_is_truncated_on_retry_and_never_logged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / "actions.json"
            output.touch(mode=0o600)
            console = io.StringIO()
            receipt = {"commands": []}
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                kwargs["stdout"].write("private-partial-graph" if len(calls) == 1 else "final-graph")
                return subprocess.CompletedProcess(
                    command, 34 if len(calls) == 1 else 0,
                    stderr=OUTAGE if len(calls) == 1 else "")

            with mock.patch.object(command_log.subprocess, "run", side_effect=run), \
                    mock.patch.object(command_log.time, "sleep"), \
                    contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                self.assertEqual(command_log.execute(
                    ["bazel", "aquery", "//:target"], directory, receipt, "query",
                    cwd=directory, output_path=output), "")
            self.assertEqual(output.read_text(), "final-graph")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-partial-graph", console.getvalue())
            self.assertNotIn("final-graph", console.getvalue())
            for log in directory.glob("*.log"):
                self.assertNotIn("graph", log.read_text())

    def test_unrecognized_mixed_permanent_and_cancelled_failures_are_not_retried(self):
        cases = [
            (1, "ERROR: Compiling source failed\n"),
            (34, OUTAGE + "ERROR: Compiling source failed\n"),
            (34, "ERROR: Compiling source failed\n"
             "WARNING: remote cache upload: Connection refused\n"),
            (34, OUTAGE.replace("Connection refused", "UNAUTHENTICATED")),
            (34, OUTAGE.replace("Connection refused", "certificate verify failed")),
            (34, OUTAGE + "ERROR: checksum mismatch\n"),
            (34, "ERROR: Unable to write all BEP events: Connection refused\n"),
            (34, OUTAGE + "source.cc:3:7: error: invalid code\n"),
            (34, OUTAGE + "x" * (2 * 1024 * 1024)),
            (8, OUTAGE), (-15, OUTAGE), (37, OUTAGE),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for code, diagnostic in cases:
                with self.subTest(code=code, diagnostic=diagnostic[:100]):
                    command = ["bazel", "build", "//:archive"]
                    result = subprocess.CompletedProcess(command, code, stdout="", stderr=diagnostic)
                    with mock.patch.object(command_log.subprocess, "run", return_value=result) as run, \
                            mock.patch.object(command_log.time, "sleep") as sleep, \
                            contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaises(subprocess.CalledProcessError):
                            command_log.execute(command, directory, {"commands": []}, "build", cwd=directory)
                    self.assertEqual(run.call_count, 1)
                    sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
