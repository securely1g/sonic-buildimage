"""Exercise durable canary handoff and recovery without Docker or root deletion."""

import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import yaml

from tools.ci.runner import vs_cleanup_client as client
from tools.ci.runner import vs_cleanup_supervisor as module


ROOT = Path(__file__).resolve().parents[3]


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.work = self.root / "work"
        self.runner = self.root / "runner"
        self.state_dir = self.root / "state"
        for path in (self.work, self.runner, self.state_dir):
            path.mkdir()
        self.workspace = self.work / "sonic-buildimage" / "sonic-buildimage"
        self.checkout = self.workspace / "vs-123-2"
        self.checkout.mkdir(parents=True)
        self.uid = os.getuid()
        self.config = {
            "runner_user": "fixture-runner",
            "runner_dir": str(self.runner), "work_root": str(self.work),
            "repository": "securely1g/sonic-buildimage", "runner_name": "test-canary",
            "job_name": "VS installer in SONiC slave (AMD64)",
            "defer_cleanup": True, "completion_timeout": 0, "reclaim_caches": True,
        }
        self.github = Mock()
        self.github.conclusion.return_value = "success"
        self.supervisor = self.make_supervisor()
        self.supervisor.state = {"phase": "listening", "session": "test-session",
                                 "runner_id": 456, "started": 1,
                                 "work_device": self.work.stat().st_dev,
                                 "work_inode": self.work.stat().st_ino}
        self.supervisor.save()
        self.events = []
        self.guard = self.enterContext(patch.object(
            self.supervisor, "check_idle", side_effect=lambda: self.events.append("idle")))
        self.storage = self.enterContext(patch.object(self.supervisor, "check_storage"))
        self.nfs = self.enterContext(patch.object(
            self.supervisor, "validate_nfs", side_effect=lambda: self.events.append("nfs")))
        self.empty = self.enterContext(patch.object(
            self.supervisor, "no_containers", side_effect=lambda: self.events.append("empty")))
        self.command = self.enterContext(patch.object(self.supervisor, "command", side_effect=self.fake_command))
        self.remove = self.enterContext(patch.object(
            module, "cleanup_checkout", side_effect=lambda *_: shutil.rmtree(self.checkout)))
        self.caches = self.enterContext(patch.object(module, "cleanup_caches"))

    def make_supervisor(self):
        account = SimpleNamespace(pw_uid=self.uid or 1, pw_gid=os.getgid(),
                                  pw_name="fixture-runner", pw_dir=str(self.root))
        with patch.object(module.pwd, "getpwnam", return_value=account):
            supervisor = module.Supervisor(self.config, self.state_dir, self.github)
        # Listener launches are mocked; local socket clients use the test process UID,
        # which is root in OCI CI, independently of the non-root account validation.
        account.pw_uid = self.uid
        return supervisor

    def test_root_listener_account_is_rejected(self):
        with patch.object(module.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=0)):
            with self.assertRaisesRegex(ValueError, "must not run as root"):
                module.Supervisor(self.config, self.state_dir, self.github)

    def fake_command(self, arguments, **kwargs):
        self.events.append(arguments)
        if arguments[:3] == ["docker", "ps", "--all"]:
            return SimpleNamespace(stdout="a" * 64 + "\n")
        return SimpleNamespace(stdout="")

    def message(self, action="enroll", **changes):
        return dict({"action": action, "run_id": "123", "run_attempt": "2", "artifact_id": []},
                    **changes)

    def enroll(self, authorize=True):
        self.supervisor.handoff(self.message(), self.uid)
        if authorize:
            self.supervisor.handoff(
                self.message("authorize", artifact_id=["10", "11", "12"]), self.uid)

    def test_real_socket_acknowledges_only_after_durable_enrollment(self):
        socket_path = self.root / "control.sock"
        with self.supervisor.server(socket_path):
            self.assertTrue(client.request(self.message(), socket_path))
            state = json.loads(self.supervisor.journal.read_text())
            self.assertEqual(state["record"]["run_id"], "123")
            self.assertEqual(state["record"]["inode"], self.checkout.stat().st_ino)
            self.assertEqual(self.supervisor.journal.stat().st_mode & 0o777, 0o600)
            self.assertTrue(client.request(
                self.message("authorize", artifact_id=["10", "11"]), socket_path))
        self.assertFalse(socket_path.exists())

    def test_observation_mode_enrolls_but_keeps_workflow_cleanup(self):
        self.config["defer_cleanup"] = False
        with self.supervisor.server(self.root / "control.sock"):
            self.assertFalse(client.request(self.message(), self.root / "control.sock"))
        self.assertIsNotNone(self.supervisor.state["record"])

    def test_client_cli_writes_deferred_output_after_acknowledgment(self):
        output = self.root / "github-output"
        send = client.request
        with self.supervisor.server(self.root / "control.sock"), \
                patch.object(client, "request",
                             side_effect=lambda message: send(message, self.root / "control.sock")), \
                patch("sys.argv", ["client", "enroll", "--run-id", "123", "--run-attempt", "2",
                                   "--github-output", str(output)]):
            client.main()
        self.assertEqual(output.read_text(), "enrolled=true\n")
        self.assertEqual(json.loads(self.supervisor.journal.read_text())["record"]["run_id"], "123")

    def test_failed_durable_write_is_not_acknowledged(self):
        with self.supervisor.server(self.root / "control.sock"), \
                patch.object(self.supervisor, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "refused"):
                client.request(self.message(), self.root / "control.sock")
        self.assertNotIn("record", json.loads(self.supervisor.journal.read_text()))

    def test_existing_registration_must_be_ephemeral_with_only_canary_labels(self):
        settings = {"agentId": 456, "ephemeral": False, "workFolder": str(self.work)}
        (self.runner / ".runner").write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError, "not ephemeral"):
            self.supervisor.configure()
        settings["ephemeral"] = True
        (self.runner / ".runner").write_text(json.dumps(settings))
        self.github.runner.return_value = {
            "name": "test-canary", "busy": False, "status": "offline",
            "labels": [{"name": "vs-cleanup-canary"}, {"name": "vs"}]}
        with self.assertRaisesRegex(ValueError, "production"):
            self.supervisor.configure()
        self.github.runner.return_value["labels"] = [{"name": "vs-cleanup-canary"}]
        self.assertEqual(self.supervisor.configure(), 456)

    def stale_registration(self):
        settings = self.runner / ".runner"
        settings.write_text(json.dumps({"agentId": 456, "ephemeral": True,
                                        "workFolder": str(self.work)}))
        self.supervisor.state = {"phase": "idle"}
        self.supervisor.save()
        self.github.registration.return_value = "private-registration-token"
        return settings

    def test_retired_ephemeral_registration_is_replaced_only_after_idle_recovery(self):
        settings = self.stale_registration()
        self.github.runner.side_effect = [
            module.GitHubAPIError(404),
            {"name": "test-canary", "busy": False, "status": "offline",
             "labels": [{"name": "vs-cleanup-canary"}]},
        ]

        def configure(arguments, **kwargs):
            if arguments[-2:] == ["remove", "--local"]:
                settings.unlink()
            else:
                settings.write_text(json.dumps({"agentId": 457, "ephemeral": True,
                                                "workFolder": str(self.work)}))
            return SimpleNamespace(returncode=0)

        with patch.object(module.subprocess, "run", side_effect=configure) as run, \
                patch.object(self.supervisor, "launcher", return_value="/usr/sbin/runuser"):
            self.assertEqual(self.supervisor.configure(), 457)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-3:],
                         [str(self.runner / "config.sh"), "remove", "--local"])
        self.assertIn("--ephemeral", run.call_args_list[1].args[0])
        self.github.runner.assert_has_calls([unittest.mock.call(456), unittest.mock.call(457)])
        self.guard.assert_called_once()
        self.assertEqual(json.loads(self.supervisor.journal.read_text()), {"phase": "idle"})
        self.assertTrue(self.checkout.exists())
        self.remove.assert_not_called()
        self.caches.assert_not_called()

    def test_stale_registration_reset_requires_idle_state_and_no_workers(self):
        settings = self.stale_registration()
        self.github.runner.side_effect = module.GitHubAPIError(404)
        with patch.object(module.subprocess, "run") as run:
            for state in (None, {"phase": "listening"}, {"phase": "cleanup"},
                          {"phase": "idle", "record": {"run_id": "123"}}):
                with self.subTest(state=state):
                    self.supervisor.state = state
                    with self.assertRaisesRegex(ValueError, "idle"):
                        self.supervisor.configure()
                    self.assertTrue(settings.exists())
            self.supervisor.state = {"phase": "idle"}
            self.guard.side_effect = ValueError("worker remains")
            with self.assertRaisesRegex(ValueError, "worker remains"):
                self.supervisor.configure()
        run.assert_not_called()
        self.github.registration.assert_not_called()

    def test_other_api_errors_do_not_reset_local_registration(self):
        settings = self.stale_registration()
        with patch.object(module.subprocess, "run") as run:
            for code in (401, 403, 500):
                with self.subTest(code=code):
                    self.github.runner.side_effect = module.GitHubAPIError(code)
                    with self.assertRaises(module.GitHubAPIError):
                        self.supervisor.configure()
                    self.assertTrue(settings.exists())
        run.assert_not_called()
        self.github.registration.assert_not_called()

    def test_local_registration_replacement_fails_closed_and_is_bounded(self):
        for case in ("remove_fails", "metadata_remains", "new_registration_missing"):
            with self.subTest(case=case):
                settings = self.stale_registration()
                self.github.registration.reset_mock()
                self.github.runner.side_effect = module.GitHubAPIError(404)

                def configure(arguments, **kwargs):
                    if case == "new_registration_missing":
                        if arguments[-2:] == ["remove", "--local"]:
                            settings.unlink()
                        else:
                            settings.write_text(json.dumps({"agentId": 457, "ephemeral": True,
                                                            "workFolder": str(self.work)}))
                    return SimpleNamespace(returncode=1 if case == "remove_fails" else 0)

                with patch.object(module.subprocess, "run", side_effect=configure) as run, \
                        patch.object(self.supervisor, "launcher", return_value="/usr/sbin/runuser"):
                    with self.assertRaises(RuntimeError):
                        self.supervisor.configure()
                self.assertEqual(run.call_count, 2 if case == "new_registration_missing" else 1)
                self.assertEqual(self.github.registration.call_count,
                                 1 if case == "new_registration_missing" else 0)
                self.assertEqual(json.loads(self.supervisor.journal.read_text()), {"phase": "idle"})
                self.assertTrue(self.checkout.exists())

    def test_bom_prefixed_runner_metadata_is_accepted_without_relaxing_profile_checks(self):
        settings = {"agentId": 456, "ephemeral": True, "workFolder": str(self.work)}
        path = self.runner / ".runner"
        path.write_text(json.dumps(settings), encoding="utf-8-sig")
        self.github.runner.return_value = {
            "name": "test-canary", "busy": False, "status": "offline",
            "labels": [{"name": "vs-cleanup-canary"}]}
        self.assertEqual(self.supervisor.configure(), 456)
        self.github.registration.assert_not_called()
        settings["ephemeral"] = False
        path.write_text(json.dumps(settings), encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "not ephemeral"):
            self.supervisor.configure()

    def test_bad_peer_or_job_cannot_enroll_or_authorize(self):
        with self.assertRaises(ValueError):
            self.supervisor.handoff(self.message(), self.uid + 1)
        with self.assertRaisesRegex(ValueError, "did not enroll"):
            self.supervisor.handoff(
                self.message("authorize", artifact_id=["10", "11"]), self.uid)
        for field, value in (("run_id", "../123"), ("run_attempt", "0")):
            message = self.message()
            message[field] = value
            with self.assertRaises(ValueError):
                self.supervisor.handoff(message, self.uid)
        self.enroll(False)
        other = self.workspace / "vs-124-2"
        other.mkdir()
        message = self.message()
        message["run_id"] = "124"
        with self.assertRaisesRegex(ValueError, "Another job"):
            self.supervisor.handoff(message, self.uid)

    def test_redirected_checkout_is_not_enrolled(self):
        self.checkout.rmdir()
        self.checkout.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "redirected"):
            self.enroll(False)
        self.assertNotIn("record", self.supervisor.state)

    def test_cleanup_follows_final_result_and_only_removes_exact_run_containers(self):
        self.enroll()
        self.supervisor.finish()
        self.github.conclusion.assert_called_once_with(
            unittest.mock.ANY, 456, self.config["job_name"])
        self.github.verify_artifacts.assert_called_once()
        self.remove.assert_called_once_with(self.workspace, "123", "2")
        self.assertIn(
            ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter",
             "label=io.sonic-buildimage.ci-job=securely1g/sonic-buildimage:123:2:vs"],
            self.events)
        self.assertIn(["docker", "rm", "--force", "a" * 64], self.events)
        self.assertLess(self.events.index("nfs"),
                        self.events.index(["docker", "image", "prune", "--all", "--force"]))
        self.assertEqual(self.supervisor.state, {"phase": "idle"})
        self.assertTrue((self.state_dir / "test-session.json").exists())

    def test_exhausted_api_retries_retain_enrollment_and_block_cleanup_until_recovery(self):
        self.enroll(False)
        github = object.__new__(module.GitHub)
        github.repository = self.config["repository"]
        github.token_file = self.root / "token"
        github.token_file.write_text("private-test-credential")
        self.supervisor.github = github
        error = HTTPError("https://api.github.com/test", 500, "private-response-body", {}, None)
        with patch.object(module.urllib.request, "urlopen", side_effect=error) as open_url, \
                patch.object(module, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, r"\(HTTP 500\)"):
                self.supervisor.finish()
            self.assertEqual(open_url.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
        journal = json.loads(self.supervisor.journal.read_text())
        self.assertEqual(journal["phase"], "awaiting_result")
        self.assertEqual(journal["record"]["run_id"], "123")
        self.command.assert_not_called()
        self.remove.assert_not_called()
        self.caches.assert_not_called()
        self.assertTrue(self.checkout.exists())
        result = {"jobs": [{"runner_id": 456, "name": self.config["job_name"],
                            "status": "completed", "conclusion": "failure"}]}
        with patch.object(module.urllib.request, "urlopen",
                          return_value=io.BytesIO(json.dumps(result).encode())):
            self.supervisor.recover()
        self.assertEqual(self.supervisor.state, {"phase": "idle"})
        self.assertTrue(self.checkout.exists())

    def test_failed_cancelled_and_post_step_failure_preserve_checkout(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                self.supervisor.state = {"phase": "listening", "session": conclusion,
                                         "runner_id": 456, "started": 1}
                self.enroll()
                self.github.conclusion.return_value = conclusion
                self.supervisor.finish()
                self.assertTrue(self.checkout.exists())
        self.remove.assert_not_called()
        self.github.verify_artifacts.assert_not_called()

    def test_missing_authorization_preserves_successful_checkout(self):
        self.enroll(False)
        self.supervisor.finish()
        self.assertTrue(self.checkout.exists())
        self.remove.assert_not_called()

    def test_unknown_result_and_live_worker_block_all_cleanup(self):
        self.enroll()
        self.github.conclusion.return_value = None
        with self.assertRaisesRegex(ValueError, "not yet known"):
            self.supervisor.finish()
        self.command.assert_not_called()
        self.guard.side_effect = ValueError("worker remains")
        with self.assertRaisesRegex(ValueError, "worker"):
            self.supervisor.finish()
        self.remove.assert_not_called()
        self.assertTrue(self.supervisor.journal.exists())

    def test_late_authorization_is_rejected_while_final_result_is_pending(self):
        self.enroll(False)

        def final_result(*_):
            with self.assertRaisesRegex(ValueError, "No active listener"):
                self.supervisor.handoff(
                    self.message("authorize", artifact_id=["10", "11"]), self.uid)
            return "success"

        self.github.conclusion.side_effect = final_result
        self.supervisor.finish()
        self.remove.assert_not_called()
        self.assertTrue(self.checkout.exists())

    def test_replacement_and_wrong_artifact_leave_pending_recovery(self):
        self.enroll()
        self.github.verify_artifacts.side_effect = ValueError("wrong artifact")
        with self.assertRaisesRegex(ValueError, "artifact"):
            self.supervisor.finish()
        self.github.verify_artifacts.side_effect = None
        self.checkout.rename(self.workspace / "retained-original")
        self.checkout.mkdir()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.supervisor.finish()
        self.remove.assert_not_called()
        self.caches.assert_not_called()
        self.assertEqual(json.loads(self.supervisor.journal.read_text())["phase"], "cleanup")

    def test_nfs_failure_recovers_after_restart_without_readmitting_early(self):
        self.enroll(False)
        self.nfs.side_effect = ValueError("NFS unavailable")
        with self.assertRaisesRegex(ValueError, "NFS"):
            self.supervisor.finish()
        self.caches.assert_not_called()
        recovered = self.make_supervisor()
        with patch.object(recovered, "check_idle"), patch.object(recovered, "no_containers"), \
                patch.object(recovered, "check_storage"), \
                patch.object(recovered, "validate_nfs"), \
                patch.object(recovered, "command", side_effect=self.fake_command):
            recovered.finish()
        self.assertEqual(recovered.state["phase"], "idle")
        self.assertTrue(self.checkout.exists())
        self.caches.assert_called_once()

    def test_missing_mount_or_replaced_storage_blocks_cleanup(self):
        self.enroll()
        self.config["build_mount"] = str(self.root)
        self.storage.side_effect = lambda: module.Supervisor.check_storage(self.supervisor)
        with patch.object(Path, "is_mount", return_value=False):
            with self.assertRaisesRegex(ValueError, "not mounted"):
                self.supervisor.finish()
        with patch.object(Path, "is_mount", return_value=True):
            self.work.rename(self.root / "retained-work")
            self.work.mkdir()
            with self.assertRaisesRegex(ValueError, "storage identity"):
                self.supervisor.finish()
        self.command.assert_not_called()
        self.remove.assert_not_called()

    def test_second_job_cannot_enter_during_cleanup_and_failed_recovery_blocks_admission(self):
        self.enroll(False)
        self.supervisor.state["phase"] = "cleanup"
        self.supervisor.save()
        with self.assertRaisesRegex(ValueError, "No active listener"):
            self.supervisor.handoff(self.message(), self.uid)
        with patch.object(self.supervisor, "finish", side_effect=ValueError("pending cleanup")), \
                patch.object(self.supervisor, "admission") as admission, \
                patch.object(self.supervisor, "configure") as configure:
            with self.assertRaises(ValueError):
                self.supervisor.cycle()
        admission.assert_not_called()
        configure.assert_not_called()

    def test_listener_is_reaped_before_cleanup(self):
        self.supervisor.state = {"phase": "idle"}
        process = Mock()
        process.__enter__ = Mock(return_value=process)
        process.__exit__ = Mock(return_value=False)
        process.wait.side_effect = lambda: self.events.append("listener-exit") or 0
        process.poll.return_value = 0
        with patch.object(self.supervisor, "admission"), \
                patch.object(self.supervisor, "configure", return_value=456), \
                patch.object(module.subprocess, "Popen", return_value=process), \
                patch.object(self.supervisor, "finish",
                             side_effect=lambda: self.events.append("cleanup")):
            self.supervisor.cycle()
        self.assertLess(self.events.index("listener-exit"), self.events.index("cleanup"))

    def test_real_child_gets_build_umask_and_sbin_path(self):
        self.supervisor.state = {"phase": "idle"}
        executable = self.root / "test-launcher"
        result = self.root / "child-result"
        directory = self.root / "child-directory"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, subprocess\n"
            "subprocess.run(['runuser', '--version'], check=True, stdout=subprocess.DEVNULL)\n"
            f"pathlib.Path({str(result)!r}).write_text('completed')\n"
            f"pathlib.Path({str(directory)!r}).mkdir()\n")
        executable.chmod(0o755)
        previous_umask = os.umask(0o077)
        try:
            with patch.object(self.supervisor, "admission"), \
                    patch.object(self.supervisor, "configure", return_value=456), \
                    patch.object(module.shutil, "which", return_value=str(executable)), \
                    patch.object(self.supervisor, "finish") as finish:
                self.supervisor.cycle()
            self.assertEqual(os.umask(0o077), 0o077)
        finally:
            os.umask(previous_umask)
        self.assertEqual(result.read_text(), "completed")
        self.assertEqual(result.stat().st_mode & 0o777, 0o644)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.supervisor.journal.stat().st_mode & 0o777, 0o600)
        finish.assert_called_once()

    def test_missing_launcher_blocks_registration_before_start_journal(self):
        self.supervisor.state = {"phase": "idle"}
        with patch.object(self.supervisor, "admission"), \
                patch.object(self.supervisor, "configure") as configure, \
                patch.object(module.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "runuser executable"):
                self.supervisor.cycle()
        configure.assert_not_called()
        self.assertEqual(self.supervisor.state["phase"], "idle")

    def test_failed_exec_preserves_record_and_can_recover_without_claiming_a_job(self):
        self.supervisor.state = {"phase": "idle"}
        with patch.object(self.supervisor, "admission"), \
                patch.object(self.supervisor, "configure", return_value=456), \
                patch.object(module.subprocess, "Popen", side_effect=FileNotFoundError("launcher")):
            with self.assertRaisesRegex(RuntimeError, "could not start"):
                self.supervisor.cycle()
        session = self.supervisor.state["session"]
        self.assertEqual(json.loads(self.supervisor.journal.read_text())["phase"], "not_started")
        recovered = self.make_supervisor()
        with patch.object(recovered, "check_idle"), \
                patch.object(recovered, "admission", side_effect=ValueError("stop after archive")):
            with self.assertRaisesRegex(ValueError, "stop after archive"):
                recovered.cycle()
        self.assertEqual(recovered.state["phase"], "idle")
        archive = json.loads((self.state_dir / (session + ".json")).read_text())
        self.assertEqual(archive["spawn_error"], "FileNotFoundError")
        self.assertNotIn("record", archive)


class GitHubResultTest(unittest.TestCase):
    def setUp(self):
        self.github = object.__new__(module.GitHub)
        self.github.repository = "owner/repository"
        self.record = {"run_id": "123", "run_attempt": "2", "artifact_ids": ["10"]}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.github.token_file = Path(directory.name) / "token"
        self.github.token_file.write_text("private-test-credential")

    def test_transient_api_reads_retry_with_bounded_backoff_and_redacted_logs(self):
        failures = [HTTPError("https://api.github.com/test", code, "private-response-body", {}, None)
                    for code in (500, 502, 503, 504)]
        failures.extend((TimeoutError("private-timeout"), URLError(TimeoutError("private-timeout")),
                         URLError(ConnectionResetError("private-reset")),
                         URLError(module.socket.gaierror(module.socket.EAI_AGAIN, "private-dns"))))
        for error in failures:
            with self.subTest(error=type(error), code=getattr(error, "code", None)):
                output = io.StringIO()
                with patch.object(module.urllib.request, "urlopen",
                                  side_effect=[error, io.BytesIO(b'{"ok": true}')]) as open_url, \
                        patch.object(module, "sleep") as sleep, \
                        patch.object(sys, "stdout", output):
                    self.assertEqual(self.github.api("test"), {"ok": True})
                self.assertEqual(open_url.call_count, 2)
                self.assertEqual(sleep.call_args_list, [unittest.mock.call(5)])
                first, second = open_url.call_args_list
                self.assertIs(first.args[0], second.args[0])
                self.assertEqual(first.args[0].get_method(), "GET")
                self.assertEqual(first.kwargs, {"timeout": 30})
                self.assertIn("attempt 2/3", output.getvalue())
                self.assertNotIn("private", output.getvalue())

    def test_api_failures_exhaust_retries_and_do_not_repeat_posts_or_invalid_responses(self):
        cases = [
            (HTTPError("https://api.github.com/test", 500, "private-body", {}, None), False, 3),
            (TimeoutError("private-timeout"), False, 3),
            (HTTPError("https://api.github.com/test", 500, "private-body", {}, None), True, 1),
            (TimeoutError("private-timeout"), True, 1),
            (HTTPError("https://api.github.com/test", 401, "private-body", {}, None), False, 1),
            (HTTPError("https://api.github.com/test", 403, "private-body", {}, None), False, 1),
            (HTTPError("https://api.github.com/test", 404, "private-body", {}, None), False, 1),
            (URLError(ssl.SSLCertVerificationError("private-certificate")), False, 1),
        ]
        for error, post, attempts in cases:
            with self.subTest(error=type(error), post=post, attempts=attempts):
                with patch.object(module.urllib.request, "urlopen", side_effect=error) as open_url, \
                        patch.object(module, "sleep") as sleep, \
                        patch.object(sys, "stdout", io.StringIO()) as output:
                    with self.assertRaises(RuntimeError) as caught:
                        self.github.api("test", post=post)
                self.assertEqual(open_url.call_count, attempts)
                self.assertEqual(sleep.call_args_list,
                                 [unittest.mock.call(5), unittest.mock.call(15)][:attempts - 1])
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("private", output.getvalue())
        with patch.object(module.urllib.request, "urlopen",
                          return_value=io.BytesIO(b"private-invalid-json")) as open_url, \
                patch.object(module, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as caught:
                self.github.api("test")
        open_url.assert_called_once()
        sleep.assert_not_called()
        self.assertNotIn("private", str(caught.exception))

    def test_job_lookup_binds_attempt_runner_and_exact_job(self):
        self.github.api = Mock(return_value={"jobs": [
            {"runner_id": 7, "name": "VS", "status": "completed", "conclusion": "success"},
            {"runner_id": 8, "name": "VS", "status": "in_progress", "conclusion": None},
        ]})
        self.assertIsNone(self.github.conclusion(self.record, 8, "VS"))
        self.github.api.assert_called_once_with(
            "repos/owner/repository/actions/runs/123/attempts/2/jobs?per_page=100&page=1")
        with self.assertRaisesRegex(ValueError, "uniquely"):
            self.github.conclusion(self.record, 9, "VS")

    def test_duplicate_jobs_and_foreign_or_expired_artifacts_are_rejected(self):
        job = {"runner_id": 8, "name": "VS", "status": "completed", "conclusion": "success"}
        self.github.api = Mock(return_value={"jobs": [job, job]})
        with self.assertRaisesRegex(ValueError, "uniquely"):
            self.github.conclusion(self.record, 8, "VS")
        for run_id, expired in ((124, False), (123, True)):
            self.github.api.return_value = {"workflow_run": {"id": run_id}, "expired": expired}
            with self.assertRaisesRegex(ValueError, "artifact"):
                self.github.verify_artifacts(self.record)

    def test_api_failure_reports_status_without_credential_or_response_body(self):
        with tempfile.TemporaryDirectory() as directory:
            self.github.token_file = Path(directory) / "token"
            self.github.token_file.write_text("private-test-credential")
            error = HTTPError("https://api.github.com/test", 403,
                              "private-response-body", {}, None)
            with patch.object(module.urllib.request, "urlopen", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, r"\(HTTP 403\)") as caught:
                    self.github.api("test")
            self.assertNotIn("private", str(caught.exception))


class CanaryWorkflowTest(unittest.TestCase):
    def test_routing_enrollment_fallback_and_authorization_are_canary_only(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
        self.assertFalse(inputs["vs_cleanup_canary"]["default"])
        job = workflow["jobs"]["vs_container"]
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.vs_cleanup_canary",
                      job["runs-on"])
        steps = job["steps"]
        enroll = next(step for step in steps if step.get("id") == "vs_cleanup_handoff")
        self.assertIn("workflow_dispatch", enroll["if"])
        self.assertIn("--github-output", enroll["run"])
        for name in ("Reclaim unused images and build cache", "Reclaim local build caches",
                     "Remove only this job's build containers", "Remove only this run's checkout"):
            step = next(step for step in steps if step["name"] == name)
            self.assertIn("steps.vs_cleanup_handoff.outputs.enrolled != 'true'", step["if"])
        logout = next(step for step in steps if step["name"] == "Log out of the slave image registry")
        self.assertEqual(logout["if"], "always()")
        authorize = steps[-1]
        self.assertIn("!cancelled()", authorize["if"])
        self.assertIn("!failure()", authorize["if"])
        self.assertGreater(steps.index(authorize), steps.index(logout))
        self.assertIn("vs_receipts_upload", authorize["if"])

    def test_unavailable_enrollment_falls_back_without_failing_the_job(self):
        steps = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())[
            "jobs"]["vs_container"]["steps"]
        enroll = next(step for step in steps if step.get("id") == "vs_cleanup_handoff")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", "-c", enroll["run"]], cwd=directory, capture_output=True, text=True,
                env=dict(os.environ, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
                         GITHUB_OUTPUT=str(Path(directory) / "output")))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("retaining workflow cleanup", result.stderr)


if __name__ == "__main__":
    unittest.main()
