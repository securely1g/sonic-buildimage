"""Policy installation and failure checks without changing the host AppArmor state."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import apparmor_kernel


@contextlib.contextmanager
def root_owned_fixture():
    """Model root ownership while preserving real file type and mode checks."""
    original_stat = Path.stat

    def root_stat(path, *args, **kwargs):
        fields = list(original_stat(path, *args, **kwargs))
        fields[4] = 0
        return os.stat_result(fields)

    with patch.object(Path, "stat", root_stat), patch.object(os, "chown") as chown:
        yield chown


class KernelAppArmorTests(unittest.TestCase):
    def test_hosts_without_namespace_restriction_need_no_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            restriction = directory / "restriction"
            with contextlib.redirect_stdout(io.StringIO()):
                apparmor_kernel.check_kernel_sandbox(directory, directory / "profiles", restriction)
                restriction.write_text("0\n")
                apparmor_kernel.check_kernel_sandbox(directory, directory / "profiles", restriction)
            self.assertEqual(list(directory.iterdir()), [restriction])
            restriction.write_text("unexpected\n")
            with self.assertRaisesRegex(RuntimeError, "Unexpected AppArmor"):
                apparmor_kernel.restriction_enabled(restriction)

    def test_install_parses_before_loading_and_preserves_other_policy(self):
        with tempfile.TemporaryDirectory() as temporary, root_owned_fixture() as chown:
            directory = Path(temporary)
            other = directory / "unprivileged_userns"
            other.write_text("Administrator's existing namespace policy\n")
            profile = directory / apparmor_kernel.PROFILE_NAME
            commands = []

            def run(command, check):
                self.assertTrue(check)
                staged = Path(command[-1])
                self.assertEqual(staged.read_text(), apparmor_kernel.CONTENTS)
                self.assertFalse(profile.exists())
                commands.append(command[:-1])

            with patch.object(apparmor_kernel.subprocess, "run", side_effect=run):
                apparmor_kernel.install_profile(directory, "/usr/sbin/apparmor_parser")
            self.assertEqual(commands, [["/usr/sbin/apparmor_parser", "-Q", "-K"],
                                        ["/usr/sbin/apparmor_parser", "-r", "-T"]])
            self.assertEqual(profile.read_text(), apparmor_kernel.CONTENTS)
            self.assertEqual(profile.stat().st_mode & 0o777, 0o644)
            self.assertEqual(other.read_text(), "Administrator's existing namespace policy\n")
            self.assertEqual(set(directory.iterdir()), {profile, other})
            self.assertEqual(chown.call_args.args[1:], (0, 0))
            with patch.object(apparmor_kernel.subprocess, "run"):
                apparmor_kernel.install_profile(directory)
            self.assertEqual(profile.read_text(), apparmor_kernel.CONTENTS)

    def test_parse_or_load_failure_does_not_publish_policy(self):
        for failed_step in (0, 1):
            for existing in (False, True):
                with self.subTest(step=failed_step, existing=existing), \
                        tempfile.TemporaryDirectory() as temporary, root_owned_fixture():
                    directory = Path(temporary)
                    profile = directory / apparmor_kernel.PROFILE_NAME
                    if existing:
                        profile.write_text(apparmor_kernel.CONTENTS)
                        profile.chmod(0o644)
                    calls = 0

                    def run(command, check):
                        nonlocal calls
                        step = calls
                        calls += 1
                        if step == failed_step:
                            raise subprocess.CalledProcessError(1, command)

                    with patch.object(apparmor_kernel.subprocess, "run", side_effect=run):
                        with self.assertRaises(subprocess.CalledProcessError):
                            apparmor_kernel.install_profile(directory)
                    self.assertEqual(profile.exists(), existing)
                    self.assertEqual(list(directory.iterdir()), [profile] if existing else [])
                    if existing:
                        self.assertEqual(profile.read_text(), apparmor_kernel.CONTENTS)

    def test_different_policy_and_symlinks_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary, root_owned_fixture():
            directory = Path(temporary)
            profile = directory / apparmor_kernel.PROFILE_NAME
            profile.write_text("Administrator's policy\n")
            profile.chmod(0o644)
            with patch.object(apparmor_kernel.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "contains other policy"):
                    apparmor_kernel.install_profile(directory)
                self.assertEqual(profile.read_text(), "Administrator's policy\n")
                profile.unlink()
                target = directory / "another-profile"
                target.write_text(apparmor_kernel.CONTENTS)
                profile.symlink_to(target)
                with self.assertRaisesRegex(RuntimeError, "root-owned regular file"):
                    apparmor_kernel.install_profile(directory)
                self.assertTrue(profile.is_symlink())
                run.assert_not_called()

    def test_writable_policy_or_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, root_owned_fixture():
            directory = Path(temporary)
            profile = directory / apparmor_kernel.PROFILE_NAME
            profile.write_text(apparmor_kernel.CONTENTS)
            profile.chmod(0o666)
            with self.assertRaisesRegex(RuntimeError, "root-owned regular file"):
                apparmor_kernel.install_profile(directory)
            profile.chmod(0o644)
            directory.chmod(0o777)
            with self.assertRaisesRegex(RuntimeError, "root-owned directory"):
                apparmor_kernel.install_profile(directory)

    def test_preflight_requires_matching_config_and_loaded_profile(self):
        with tempfile.TemporaryDirectory() as temporary, root_owned_fixture():
            directory = Path(temporary)
            restriction = directory / "restriction"
            restriction.write_text("1\n")
            profiles = directory / "profiles"
            profiles.write_text("unprivileged_userns (enforce)\n")
            with self.assertRaisesRegex(RuntimeError, "apparmor_kernel.py --install"):
                apparmor_kernel.check_kernel_sandbox(directory, profiles, restriction)
            profile = directory / apparmor_kernel.PROFILE_NAME
            profile.write_text(apparmor_kernel.CONTENTS)
            profile.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "not loaded"):
                apparmor_kernel.check_kernel_sandbox(directory, profiles, restriction)
            profiles.write_text("unprivileged_userns (enforce)\nsonic-vs-kernel-sandbox (unconfined)\n")
            with contextlib.redirect_stdout(io.StringIO()):
                apparmor_kernel.check_kernel_sandbox(directory, profiles, restriction)
            self.assertEqual(restriction.read_text(), "1\n")


if __name__ == "__main__":
    unittest.main()
