"""Keep root-container Bazel ownership repair inside its startup directories."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel.ci import repair_bazel_startup as repair


class BazelStartupTest(unittest.TestCase):
    def test_creates_default_startup_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            state = repair.repair_startup(home)
            self.assertEqual(state, home / ".cache/bazel/_bazel_root")
            self.assertEqual(state.stat().st_uid, os.geteuid())
            self.assertEqual(state.stat().st_gid, os.getegid())

    def test_only_directories_are_repaired_and_shared_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            state = home / ".cache/bazel/_bazel_root"
            nested = state / "install/hash"
            nested.mkdir(parents=True)
            outside = Path(temporary) / "shared"
            outside.mkdir()
            retained = outside / "cache-object"
            retained.write_text("keep")
            retained.chmod(0o444)
            os.link(retained, nested / "hardlink")
            (state / "symlink").symlink_to(outside, target_is_directory=True)
            regular = nested / "regular-file"
            regular.write_text("keep")
            before = retained.stat()
            changed = []

            def record(descriptor, uid, gid):
                self.assertEqual((uid, gid), (os.geteuid(), os.getegid()))
                changed.append(Path(f"/proc/self/fd/{descriptor}").resolve())

            with mock.patch.object(repair.os, "fchown", side_effect=record):
                repair.repair_startup(home)
            self.assertEqual(set(changed), {state, state / "install", nested})
            self.assertEqual(retained.stat(), before)
            self.assertEqual(regular.read_text(), "keep")
            self.assertTrue((state / "symlink").is_symlink())

    def test_symlinks_in_startup_path_are_rejected_without_changes(self):
        for component in ("home", ".cache", "bazel", "_bazel_root"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                candidates = [home, home / ".cache", home / ".cache/bazel",
                              home / ".cache/bazel/_bazel_root"]
                redirected = next(path for path in candidates if path.name == component)
                redirected.parent.mkdir(parents=True, exist_ok=True)
                outside = Path(temporary) / "outside"
                outside.mkdir()
                redirected.symlink_to(outside, target_is_directory=True)
                with mock.patch.object(repair.os, "fchown") as chown:
                    with self.assertRaises(OSError):
                        repair.repair_startup(home)
                    chown.assert_not_called()
                self.assertEqual(list(outside.iterdir()), [])

    def test_nested_mounts_are_skipped_and_redirected_startup_mounts_rejected(self):
        for redirected in (False, True):
            with self.subTest(redirected=redirected), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                state = home / ".cache/bazel/_bazel_root"
                mounted = state if redirected else state / "mounted"
                (mounted / "child").mkdir(parents=True)
                original_mount_id = repair.mount_id
                changed = []

                def selected_mount(descriptor):
                    path = Path(f"/proc/self/fd/{descriptor}").resolve()
                    return original_mount_id(descriptor) + (1 if path == mounted else 0)

                with mock.patch.object(repair, "mount_id", side_effect=selected_mount), \
                        mock.patch.object(repair.os, "fchown", side_effect=lambda fd, uid, gid:
                                          changed.append(Path(f"/proc/self/fd/{fd}").resolve())):
                    if redirected:
                        with self.assertRaisesRegex(ValueError, "redirected cache mount"):
                            repair.repair_startup(home)
                        self.assertEqual(changed, [])
                    else:
                        repair.repair_startup(home)
                        self.assertEqual(changed, [state])

    def test_permission_errors_are_not_hidden(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(repair.os, "fchown", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PermissionError, "denied"):
                repair.repair_startup(Path(temporary))

    def test_cli_rejects_unexpected_execution_context_before_repair(self):
        for actions, home, uid, container in [
            ("false", "/github/home", 0, True),
            ("true", "/other/home", 0, True),
            ("true", "/github/home", 1000, True),
            ("true", "/github/home", 0, False),
        ]:
            with self.subTest(actions=actions, home=home, uid=uid, container=container), \
                    mock.patch.dict(os.environ, GITHUB_ACTIONS=actions, HOME=home), \
                    mock.patch.object(repair.os, "geteuid", return_value=uid), \
                    mock.patch.object(Path, "is_file", return_value=container), \
                    mock.patch.object(repair, "repair_startup") as restore, \
                    mock.patch.object(sys, "argv", ["repair_bazel_startup.py"]), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as result:
                    repair.main()
                self.assertEqual(result.exception.code, 1)
                self.assertIn("Cannot repair Bazel startup ownership:", errors.getvalue())
                restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
