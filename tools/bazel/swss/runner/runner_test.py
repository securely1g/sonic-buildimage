"""Checks for registration secret handling and storage accounting, without sudo."""
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import sys
from unittest.mock import patch

import preflight
import rearm


class RunnerTests(unittest.TestCase):
    def test_parsed_output_excludes_successful_stderr_banner(self):
        result = preflight.output(sys.executable, "-c", "import sys; print('42'); print('j2 version banner', file=sys.stderr)")
        self.assertEqual(result, "42")

    def test_failed_command_preserves_stderr_diagnostics(self):
        with self.assertRaisesRegex(RuntimeError, "render failed"):
            preflight.output(sys.executable, "-c", "import sys; print('render failed', file=sys.stderr); sys.exit(1)")

    def test_routing_is_scoped_to_the_selected_pr(self):
        self.assertEqual(rearm.routing_label(9), "sonic-vs-source-pr-9")
        self.assertEqual(rearm.routing_label(10), "sonic-vs-source-pr-10")
        self.assertEqual(rearm.routing_label(None), "sonic-vs-source-master")

    def test_token_is_in_runner_environment_and_not_arguments(self):
        account = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_name="sonic-runner")
        with patch.dict(rearm.os.environ, {"GH_TOKEN": "operator-secret", "SSH_AUTH_SOCK": "/operator/agent"}), \
                patch.object(rearm.pwd, "getpwnam", return_value=account), \
                patch.object(rearm.os, "getgrouplist", return_value=[1001, 998]), \
                patch.object(rearm.subprocess, "run") as run:
            rearm.as_runner(["/runner/config.sh", "--ephemeral"], token="short-lived")
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("short-lived", command)
        self.assertEqual(environment["ACTIONS_RUNNER_INPUT_TOKEN"], "short-lived")
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("SSH_AUTH_SOCK", environment)
        self.assertEqual(run.call_args.kwargs["user"], 1001)

    def test_dry_run_does_not_launch_processes(self):
        with patch.object(rearm.sys, "argv", ["rearm.py", "--dry-run"]), \
                patch.object(rearm.subprocess, "run", side_effect=AssertionError("process launched")), \
                patch.object(rearm.subprocess, "check_output", side_effect=AssertionError("process launched")), \
                contextlib.redirect_stdout(io.StringIO()) as result:
            rearm.main()
        self.assertIn("sonic-vs-source-pr-9", result.getvalue())

    def test_digest_rejects_altered_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive"
            path.write_bytes(b"abc")
            original = rearm.archive_digest(path)
            self.assertEqual(original, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
            path.write_bytes(b"abcd")
            self.assertNotEqual(original, rearm.archive_digest(path))

    def test_shared_filesystem_does_not_count_capacity_twice(self):
        with patch.object(preflight.shutil, "disk_usage", return_value=SimpleNamespace(free=350 * 1024**3)), \
                patch.object(preflight.os, "stat", return_value=SimpleNamespace(st_dev=1)), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "400 GiB"):
                preflight.check_disk("/workspace", "/docker", 300, 100)

    def test_separate_filesystems_each_need_their_own_budget(self):
        with patch.object(preflight.shutil, "disk_usage", side_effect=[SimpleNamespace(free=n * 1024**3) for n in (444, 90)]), \
                patch.object(preflight.os, "stat", side_effect=[SimpleNamespace(st_dev=n) for n in (1, 2)]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "100 GiB Docker"):
                preflight.check_disk("/workspace", "/docker", 300, 100)


if __name__ == "__main__":
    unittest.main()
