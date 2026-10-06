"""Checks for registration secret handling and storage accounting, without sudo."""
import contextlib
import gzip
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import sys
from unittest.mock import patch

import preflight
import rearm
import apparmor_gs


class RunnerTests(unittest.TestCase):
    registration = {"id": 42, "name": "sonic-vs-9-unique", "attempt": "/data/sonic-runner/attempts/unique"}

    def test_legacy_nat_checks_loaded_and_builtin_support_not_only_nft_nat(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            modules = directory / "modules"
            config = directory / "config"
            compressed = directory / "config.gz"
            builtin = directory / "modules.builtin"
            def check():
                with contextlib.redirect_stdout(io.StringIO()):
                    preflight.check_legacy_nat(modules, config, compressed, builtin)
            modules.write_text("nf_nat 65536 2 nft_chain_nat, Live 0x0000\n")
            config.write_text("CONFIG_NF_NAT=y\nCONFIG_IP_NF_NAT=m\n")
            with self.assertRaisesRegex(RuntimeError, "sudo modprobe iptable_nat"):
                check()
            modules.write_text("iptable_nat 12288 0 - Live 0x0000\n")
            check()
            modules.write_text("")
            config.write_text("CONFIG_IP_NF_NAT=y\n")
            check()
            config.unlink()
            with gzip.open(compressed, "wt") as stream:
                stream.write("CONFIG_IP_NF_NAT=y\n")
            check()
            compressed.unlink()
            builtin.write_text("kernel/net/ipv4/netfilter/iptable_nat.ko\n")
            check()

    def test_ghostscript_rules_preserve_local_policy_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "gs").write_text('profile gs /usr/bin/gs {\n  include if exists <local/gs>\n}\n')
            (directory / "local").mkdir()
            local = directory / "local/gs"
            previous = "# Administrator's existing rule\nowner /srv/printing/*.pdf r,"
            local.write_text(previous)
            with patch.object(apparmor_gs.os, "chown") as chown:
                apparmor_gs.install_rules(directory)
                first = local.read_text()
                apparmor_gs.install_rules(directory)
            self.assertEqual(local.read_text(), first)
            self.assertEqual(first, previous + "\n" + apparmor_gs.INCLUDE + "\n")
            fragment = directory / apparmor_gs.FRAGMENT
            self.assertEqual(fragment.read_text(), apparmor_gs.CONTENTS)
            self.assertEqual(fragment.stat().st_mode & 0o777, 0o644)
            chown.assert_called_with(fragment, 0, 0)
            profiles = directory / "loaded-profiles"
            profiles.write_text("gs (enforce)\nother-profile (enforce)\n")
            with contextlib.redirect_stdout(io.StringIO()):
                apparmor_gs.check_ghostscript(directory, profiles)

    def test_ghostscript_unrecognized_profile_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "gs").write_text('profile gs /usr/bin/gs {}\n')
            with self.assertRaisesRegex(RuntimeError, "does not include local/gs"):
                apparmor_gs.install_rules(directory)
            self.assertFalse((directory / "local").exists())

    def test_new_ghostscript_local_file_is_readable_despite_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "gs").write_text('profile gs /usr/bin/gs {\ninclude <local/gs>\n}\n')
            previous_mask = apparmor_gs.os.umask(0o077)
            try:
                with patch.object(apparmor_gs.os, "chown") as chown:
                    apparmor_gs.install_rules(directory)
            finally:
                apparmor_gs.os.umask(previous_mask)
            local = directory / "local/gs"
            self.assertEqual(local.stat().st_mode & 0o777, 0o644)
            self.assertEqual(local.parent.stat().st_mode & 0o777, 0o755)
            chown.assert_any_call(local, 0, 0)
            local.chmod(0o600)
            with patch.object(apparmor_gs.os, "chown"):
                apparmor_gs.install_rules(directory)
            self.assertEqual(local.stat().st_mode & 0o777, 0o600)

    def test_ghostscript_unreadable_existing_rules_have_actionable_error(self):
        with patch.object(apparmor_gs, "gs_loaded", return_value=True), \
                patch.object(apparmor_gs.Path, "read_text", side_effect=PermissionError(13, "Permission denied", "/etc/apparmor.d/local/gs")):
            with self.assertRaisesRegex(RuntimeError, "review readability for sonic-runner"):
                apparmor_gs.check_ghostscript()

    def test_ghostscript_does_not_replace_foreign_fragment_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "gs").write_text('profile gs /usr/bin/gs {\n#include <local/gs>\n}\n')
            (directory / "local").mkdir()
            fragment = directory / apparmor_gs.FRAGMENT
            fragment.write_text("owner /srv/reports/*.pdf r,\n")
            with self.assertRaisesRegex(RuntimeError, "contains other rules"):
                apparmor_gs.install_rules(directory)
            self.assertEqual(fragment.read_text(), "owner /srv/reports/*.pdf r,\n")

    def test_preflight_rejects_loaded_ghostscript_without_scoped_allowance(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            profiles = directory / "loaded-profiles"
            profiles.write_text("gs (enforce)\n")
            with self.assertRaisesRegex(RuntimeError, "reload only the gs profile"):
                apparmor_gs.check_ghostscript(directory, profiles)
            profiles.write_text("other-profile (enforce)\n")
            with contextlib.redirect_stdout(io.StringIO()):
                apparmor_gs.check_ghostscript(directory, profiles)

    def test_ghostscript_installer_compiles_then_reloads_only_its_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            profile = directory / "gs"
            profile.write_text('profile gs /usr/bin/gs {\ninclude if exists <local/gs>\n}\n')
            profiles = directory / "loaded-profiles"
            profiles.write_text("gs (enforce)\n")
            with patch.object(apparmor_gs, "PROFILE_DIR", directory), \
                    patch.object(apparmor_gs, "PROFILES", profiles), \
                    patch.object(apparmor_gs.os, "geteuid", return_value=0), \
                    patch.object(apparmor_gs.os, "chown"), \
                    patch.object(apparmor_gs.shutil, "which", return_value="/usr/sbin/apparmor_parser"), \
                    patch.object(sys, "argv", ["apparmor_gs.py", "--install"]), \
                    patch.object(apparmor_gs.subprocess, "run") as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                apparmor_gs.main()
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["/usr/sbin/apparmor_parser", "-Q", "-K", str(profile)],
                ["/usr/sbin/apparmor_parser", "-r", "-T", str(profile)]])

    def test_umask_guard_matches_actual_modes_of_copied_build_inputs(self):
        program = """
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import preflight
mask = int(sys.argv[3], 8)
os.umask(mask)
directory = pathlib.Path(sys.argv[2]) / 'build-context'
directory.mkdir()
config = directory / 'pip.conf'
config.write_text('[global]\\nbreak-system-packages = true\\n')
try:
    preflight.check_umask(mask, 'test')
    accepted = True
except RuntimeError:
    accepted = False
print(json.dumps({'accepted': accepted, 'file': config.stat().st_mode & 0o777,
                  'directory': directory.stat().st_mode & 0o777}))
"""
        for mask, accepted, file_mode, directory_mode in (("0022", True, 0o644, 0o755),
                                                          ("0077", False, 0o600, 0o700)):
            with self.subTest(mask=mask), tempfile.TemporaryDirectory() as temporary:
                result = subprocess.check_output([sys.executable, "-B", "-c", program, str(Path(__file__).parent), temporary, mask], text=True)
                self.assertEqual(json.loads(result), {"accepted": accepted, "file": file_mode, "directory": directory_mode})

    def test_preflight_checks_service_umask_even_when_operator_umask_is_safe(self):
        with patch.object(preflight.Path, "read_text", return_value="Name: python3\nUmask:\t0022\n"), \
                patch.object(preflight, "output", return_value="0077") as output:
            with self.assertRaisesRegex(RuntimeError, "Runner service umask 0077"):
                preflight.check_worker_umasks()
        output.assert_called_once_with("systemctl", "show", "sonic-vs-runner.service", "--property=UMask", "--value")

    def test_register_reads_runner_written_bom_identity_before_starting_service(self):
        # Runner.Listener writes this JSON using a UTF-8 BOM. Exercise the real
        # register stage and filesystem, replacing only privileged/external work.
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "attempts").mkdir()
            lock = home / "register.lock"
            account = SimpleNamespace(pw_uid=1001, pw_gid=1001)

            def configure(command, *, token=None, cwd=None):
                if command[0].endswith("config.sh"):
                    self.assertEqual(token, "short-lived")
                    name = command[command.index("--name") + 1]
                    (cwd / ".runner").write_text(json.dumps({"agentId": 42, "agentName": name,
                                                           "ephemeral": True, "workFolder": "_work"}),
                                                  encoding="utf-8-sig")

            with patch.object(rearm, "RUNNER_HOME", home), \
                    patch.object(rearm.os, "geteuid", return_value=0), \
                    patch.object(rearm.os.path, "ismount", return_value=True), \
                    patch.object(rearm.os, "chown"), \
                    patch.object(rearm, "open", side_effect=lambda *args: lock.open("w"), create=True), \
                    patch.object(rearm, "idle_host"), \
                    patch.object(rearm.pwd, "getpwnam", return_value=account), \
                    patch.object(rearm, "archive_digest", return_value=rearm.SHA256), \
                    patch.object(rearm, "as_runner", side_effect=configure), \
                    patch.object(rearm.subprocess, "check_output", return_value="loaded\n"), \
                    patch.object(rearm.subprocess, "run") as run, \
                    patch.object(rearm.sys, "stdin", io.StringIO("short-lived\n")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                rearm.register(SimpleNamespace(repo="owner/repo", pr=9))
            metadata = json.loads(output.getvalue().split(rearm.REGISTRATION_PREFIX)[1])
            attempt = Path(metadata["attempt"])
            self.assertEqual(metadata["id"], 42)
            self.assertEqual((home / "current").resolve(), attempt)
            self.assertTrue((attempt / ".runner").read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertNotIn("short-lived", output.getvalue())
            self.assertEqual(run.call_args.args[0], ["systemctl", "start", rearm.SERVICE])

    def test_extraction_exhausting_capacity_stops_before_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            previous = home / "attempts/previous"
            previous.mkdir(parents=True)
            retained = previous / "build.log"
            retained.write_text("Retained build evidence\n")
            (home / "current").symlink_to(previous)
            lock = home / "register.lock"
            account = SimpleNamespace(pw_uid=1001, pw_gid=1001)
            token_input = io.StringIO("short-lived\n")
            workspace_gib = 100.5
            checked_workspaces = []
            extracted_attempts = []

            def run_as_runner(command, **kwargs):
                nonlocal workspace_gib
                if command[0] == "tar":
                    attempt = Path(command[command.index("-C") + 1])
                    (attempt / "extracted-runner").write_text("Keep failed attempts for diagnosis\n")
                    extracted_attempts.append(attempt)
                    workspace_gib -= 1
                elif command[:2] == ["/usr/bin/python3", "/opt/sonic-runner-tools/runner/preflight.py"]:
                    workspace = Path(command[command.index("--workspace") + 1]) if "--workspace" in command else home
                    checked_workspaces.append(workspace)
                    # Exercise real budget accounting on separate filesystems;
                    # extraction uses the headroom that allowed the first check.
                    with patch.object(preflight.shutil, "disk_usage", side_effect=[
                            SimpleNamespace(free=n * 1024**3) for n in (workspace_gib, 150)]), \
                            patch.object(preflight.os, "stat", side_effect=[
                                SimpleNamespace(st_dev=n) for n in (1, 2)]):
                        preflight.check_disk(workspace, "/docker", 100, 100)
                else:
                    self.fail(f"Unexpected runner command after capacity was exhausted: {command[0]}")

            with patch.object(rearm, "RUNNER_HOME", home), \
                    patch.object(rearm.os, "geteuid", return_value=0), \
                    patch.object(rearm.os.path, "ismount", return_value=True), \
                    patch.object(rearm.os, "chown"), \
                    patch.object(rearm, "open", side_effect=lambda *args: lock.open("w"), create=True), \
                    patch.object(rearm, "idle_host"), \
                    patch.object(rearm.pwd, "getpwnam", return_value=account), \
                    patch.object(rearm, "archive_digest", return_value=rearm.SHA256), \
                    patch.object(rearm, "as_runner", side_effect=run_as_runner), \
                    patch.object(rearm.subprocess, "check_output", return_value="loaded\n"), \
                    patch.object(rearm.subprocess, "run") as run, \
                    patch.object(rearm.sys, "stdin", token_input), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaisesRegex(RuntimeError, "100 GiB workspace"):
                    rearm.register(SimpleNamespace(repo="owner/repo", pr=9))

            self.assertEqual(len(extracted_attempts), 1)
            attempt = extracted_attempts[0]
            self.assertEqual(checked_workspaces, [home, attempt])
            self.assertTrue((attempt / "extracted-runner").is_file())
            self.assertFalse((attempt / ".runner").exists())
            self.assertEqual((home / "current").resolve(), previous)
            self.assertEqual(retained.read_text(), "Retained build evidence\n")
            self.assertEqual(token_input.tell(), 0)
            self.assertNotIn("short-lived", output.getvalue())
            run.assert_not_called()

    def test_readiness_accepts_only_exact_runner_online_or_busy(self):
        for status, busy in (("online", False), ("offline", True)):
            with self.subTest(status=status, busy=busy), \
                    patch.object(rearm, "github_json", return_value={**self.registration, "status": status, "busy": busy}) as api, \
                    contextlib.redirect_stdout(io.StringIO()) as result:
                rearm.wait_until_ready("owner/repo", self.registration)
            self.assertEqual(api.call_args.args[0], "repos/owner/repo/actions/runners/42")
            self.assertIn("Verified sonic-vs-9-unique", result.getvalue())

    def test_readiness_rejects_another_runner_even_if_online(self):
        for replacement in ({"id": 43}, {"name": "another-runner"}):
            with self.subTest(replacement=replacement), \
                    patch.object(rearm, "github_json", return_value={**self.registration, **replacement, "status": "online"}):
                with self.assertRaisesRegex(RuntimeError, "different runner identity"):
                    rearm.wait_until_ready("owner/repo", self.registration)

    def test_readiness_retries_transient_errors_and_offline_status(self):
        responses = [rearm.GitHubApiError("HTTP 502"), {**self.registration, "status": "offline"},
                     {**self.registration, "status": "online"}]
        with patch.object(rearm, "github_json", side_effect=responses) as api, \
                patch.object(rearm.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            rearm.wait_until_ready("owner/repo", self.registration)
        self.assertEqual(api.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_readiness_timeout_is_bounded_and_reports_recovery_details(self):
        clock = [0.0]
        with patch.object(rearm.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(rearm.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)), \
                patch.object(rearm, "github_json", side_effect=rearm.GitHubApiError("HTTP 404")) as api:
            with self.assertRaises(RuntimeError) as error:
                rearm.wait_until_ready("owner/repo", self.registration, timeout=5)
        self.assertEqual(clock[0], 5)
        self.assertEqual(api.call_count, 2)
        for detail in (self.registration["attempt"], "journalctl", "already consumed a job", "not stopped"):
            self.assertIn(detail, str(error.exception))

    def test_api_request_cannot_outlive_remaining_readiness_budget(self):
        with patch.object(rearm.time, "monotonic", return_value=10), \
                patch.object(rearm.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{}')) as run:
            self.assertEqual(rearm.github_json("repos/owner/repo/actions/runners/42", deadline=12), {})
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_registration_metadata_does_not_expose_the_token_in_arguments(self):
        result = SimpleNamespace(returncode=0, stdout="Attempt created\n" + rearm.REGISTRATION_PREFIX + json.dumps(self.registration) + "\n")
        command = ["sudo", "-n", "--", "/usr/bin/python3", "/root-owned/rearm.py", "--register"]
        with patch.object(rearm.subprocess, "run", return_value=result) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rearm.register_from_operator(command, "short-lived"), self.registration)
        self.assertEqual(run.call_args.args[0], command)
        self.assertNotIn("short-lived", command)
        self.assertEqual(run.call_args.kwargs["input"], "short-lived\n")
        self.assertEqual(output.getvalue(), "Attempt created\n")

    def test_service_start_without_registration_identity_is_not_success(self):
        with patch.object(rearm.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
            with self.assertRaisesRegex(RuntimeError, "did not return a runner identity"):
                rearm.register_from_operator(["sudo", "helper"], "short-lived")

    def test_passwordless_sudo_does_not_validate_interactively(self):
        with patch.object(rearm.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(rearm.sudo_command(), ["sudo", "-n", "--"])
        run.assert_called_once_with(["sudo", "-n", "--", "/usr/bin/true"], capture_output=True)

    def test_sudo_without_a_terminal_fails_before_prompting(self):
        with patch.object(rearm.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run, \
                patch.object(rearm.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "interactive terminal"):
                rearm.sudo_command()
        self.assertEqual(run.call_count, 1)

    def test_sudo_interactive_fallback_validates_once(self):
        with patch.object(rearm.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run, \
                patch.object(rearm.sys.stdin, "isatty", return_value=True):
            self.assertEqual(rearm.sudo_command(), ["sudo", "-n", "--"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["sudo", "-v"])

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
        with patch.object(preflight.shutil, "disk_usage", return_value=SimpleNamespace(free=150 * 1024**3)), \
                patch.object(preflight.os, "stat", return_value=SimpleNamespace(st_dev=1)), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "200 GiB"):
                preflight.check_disk("/workspace", "/docker", 100, 100)

    def test_separate_filesystems_each_need_their_own_budget(self):
        with patch.object(preflight.shutil, "disk_usage", side_effect=[SimpleNamespace(free=n * 1024**3) for n in (444, 90)]), \
                patch.object(preflight.os, "stat", side_effect=[SimpleNamespace(st_dev=n) for n in (1, 2)]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "100 GiB Docker"):
                preflight.check_disk("/workspace", "/docker", 100, 100)


if __name__ == "__main__":
    unittest.main()
