"""Exercise the builder-exit observer without a Docker daemon or privileges."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import trace_builder_storage as storage


class TraceBuilderStorageTest(unittest.TestCase):
    def test_selects_only_root_overlay_and_decodes_kernel_escaping(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / "42").mkdir()
            mountinfo = proc / "42/mountinfo"
            mountinfo.write_text(
                "1 0 0:1 / /sonic rw - overlay overlay rw,upperdir=/other/diff\n"
                r"2 0 0:2 / / rw - overlay overlay rw,lowerdir=/lower,upperdir=/data\040root/diff"
                "\n"
            )
            with patch.object(storage, "PROC", proc):
                self.assertEqual(storage.selected_mount(42), Path("/data root/diff"))
                mountinfo.write_text("2 0 0:2 / / rw - ext4 /dev/fake rw\n")
                self.assertIsNone(storage.selected_mount(42))

    @unittest.skipUnless(hasattr(os, "O_PATH"), "requires Linux O_PATH")
    def test_directory_state_survives_rename_and_unlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "diff"
            original.mkdir()
            tracked = storage.TrackedDirectory(original)
            try:
                self.assertEqual(tracked.state(), "original")
                self.assertGreaterEqual(storage.free_space(tracked)["inodes"], 0)
                moved = original.rename(original.with_name("retired"))
                self.assertEqual(tracked.state(), "renamed")
                moved.rmdir()
                self.assertEqual(tracked.state(), "unlinked")
            finally:
                tracked.close()

    def test_reports_only_target_directory_categories_and_allowed_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = Path(temporary)
            (daemon / "fd").mkdir()
            upper = "/layers/chosen/diff"
            (daemon / "fd/9").symlink_to(upper + "/var/user/.cache/bazel/external/private-name")
            (daemon / "fd/10").symlink_to("/layers/another/diff/private-name")
            for tid, fd in [(1, 9), (2, 10)]:
                task = daemon / "task" / str(tid)
                task.mkdir(parents=True)
                (task / "wchan").write_text("io_schedule")
                (task / "syscall").write_text(f"263 {fd:#x} 0x1 0x0\n")
            (daemon / "io").write_text("read_bytes: 17\nsyscw: 7\nrchar: 12345\n")
            self.assertEqual(storage.daemon_sample(daemon, upper, "/layers/chosen", "a" * 64, True), {
                "target_fds": {"bazel_external": 1},
                "target_syscalls": {"unlinkat/bazel_external": 1},
                "target_wait_channels": {"io_schedule": 1},
                "process_cpu_ticks": None,
                "process_io_cumulative": {"read_bytes": 17, "syscw": 7},
            })
