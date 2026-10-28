#!/usr/bin/env python3
"""Exercise capacity admission and safe scratch allocation independently of builds."""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import build_area


class BuildAreaTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def mount(self, fstype="ext4", options="rw,relatime"):
        return subprocess.CompletedProcess([], 0, json.dumps({
            "filesystems": [{"fstype": fstype, "options": options}]}), "")

    def test_live_capacity_and_filesystem_rejections(self):
        device = self.root.stat().st_dev
        with mock.patch.object(build_area.subprocess, "run", return_value=self.mount()) as command, \
                mock.patch.object(build_area.shutil, "disk_usage") as disk:
            disk.return_value.free = build_area.MIN_FREE
            self.assertEqual(build_area.check_filesystem(self.root, device), build_area.MIN_FREE)
            self.assertEqual(command.call_args.kwargs["timeout"], 10)
            disk.return_value.free -= 1
            with self.assertRaisesRegex(ValueError, "300 GiB"):
                build_area.check_filesystem(self.root, device)
        for fstype, options in (("tmpfs", "rw"), ("ext4", "rw,nodev"), ("ext4", "rw,noexec")):
            with self.subTest(fstype=fstype, options=options), \
                    mock.patch.object(build_area.subprocess, "run", return_value=self.mount(fstype, options)), \
                    self.assertRaises(ValueError):
                build_area.check_filesystem(self.root, device)
        with mock.patch.object(build_area.subprocess, "run", return_value=self.mount()), \
                mock.patch.object(build_area.os, "access", return_value=False), \
                self.assertRaisesRegex(ValueError, "writable"):
            build_area.check_filesystem(self.root, device)

    def test_separate_filesystem_is_accepted_and_its_device_stays_fixed(self):
        original = Path.stat
        root_device = Path("/").stat().st_dev
        selected_device = root_device + 1
        def stat_path(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.home:
                fields = list(result)
                fields[2] = selected_device
                return os.stat_result(fields)
            return result
        with mock.patch.object(Path, "stat", stat_path), \
                mock.patch.object(build_area.subprocess, "run", return_value=self.mount()), \
                mock.patch.object(build_area.shutil, "disk_usage") as disk:
            disk.return_value.free = build_area.MIN_FREE
            self.assertEqual(build_area.check_filesystem(self.home, selected_device), build_area.MIN_FREE)
            with self.assertRaisesRegex(ValueError, "selected filesystem"):
                build_area.check_filesystem(self.home, root_device)

    def test_persistent_attempts_are_unique_and_previous_files_are_retained(self):
        with mock.patch.object(build_area, "check_filesystem", return_value=build_area.MIN_FREE):
            first, state = build_area.allocate([str(self.home)])
            (state / "retained").write_text("previous attempt")
            second, _ = build_area.allocate([str(self.home)])
        self.assertNotEqual(first, second)
        self.assertEqual((state / "retained").read_text(), "previous attempt")
        self.assertEqual(stat.S_IMODE(first.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(first.parent.stat().st_mode), 0o755)
        self.assertEqual(first.parent.stat().st_uid, os.getuid())
        record = json.loads((state / "build-area.json").read_text())
        self.assertEqual(record["scratch_retention"], "persistent")
        self.assertFalse(record["automatic_cleanup"])

    def test_bad_home_falls_back_without_touching_it(self):
        def check(path, _device):
            if path.is_relative_to(self.home):
                raise ValueError("insufficient capacity")
            return build_area.MIN_FREE
        original = Path.stat
        def stat_path(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.root:
                fields = list(result)
                fields[0] |= stat.S_ISVTX
                fields[4] = 0
                return os.stat_result(fields)
            return result
        with mock.patch.object(build_area, "check_filesystem", side_effect=check), \
                mock.patch.object(Path, "stat", stat_path):
            workspace, state = build_area.allocate([str(self.home)], temporary=self.root)
        self.assertFalse((self.home / ".local").exists())
        self.assertEqual(json.loads((state / "build-area.json").read_text())["scratch_retention"],
                         "host temporary-directory retention")

    def test_symlinked_and_unowned_parents_cannot_be_adopted(self):
        alias = self.root / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            build_area.check_filesystem(alias, alias.stat().st_dev)
        with mock.patch.object(build_area, "check_filesystem", return_value=build_area.MIN_FREE), \
                mock.patch.object(build_area.os, "getuid", return_value=os.getuid() + 1), \
                self.assertRaisesRegex(ValueError, "belong to the runner"):
            build_area.allocate([str(self.home)], temporary=self.root)


if __name__ == "__main__":
    unittest.main()
