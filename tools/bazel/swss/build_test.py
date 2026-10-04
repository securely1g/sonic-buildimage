#!/usr/bin/env python3
"""Exercise the archive handoff without running Bazel or Docker."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build


class BuildTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("target/docker-config-engine-trixie.gz",
                     "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"declared Make input")
        self.destination = self.root / "target/docker-orchagent.gz"
        self.destination.write_bytes(b"previous image")

    def test_failed_build_preserves_previous_image(self):
        with patch.object(build.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "bazel")):
            with self.assertRaises(subprocess.CalledProcessError):
                build.build("docker-orchagent.gz", self.destination, workspace=self.root)
        self.assertEqual(self.destination.read_bytes(), b"previous image")

    def test_exports_only_the_selected_bazel_output(self):
        source = self.root / "bazel-bin/docker-orchagent.gz"
        source.parent.mkdir()
        source.write_bytes(b"new OCI archive")
        with patch.object(build.subprocess, "run") as run:
            run.return_value.stdout = "bazel-bin/docker-orchagent.gz\n"
            build.build("docker-orchagent.gz", self.destination, workspace=self.root)
        self.assertEqual(self.destination.read_bytes(), b"new OCI archive")
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o644)
        self.assertEqual(run.call_args_list[0].args[0][-1],
                         "//dockers/docker-orchagent:docker-orchagent.gz")

    def test_failed_copy_preserves_previous_image(self):
        source = self.root / "source"
        source.write_bytes(b"new image")
        with patch.object(build.shutil, "copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                build.export_archive(source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"previous image")
        self.assertEqual(list(self.destination.parent.glob("docker-orchagent.gz.*")), [])

    def test_unchanged_archive_keeps_timestamp(self):
        source = self.root / "source"
        source.write_bytes(self.destination.read_bytes())
        before = self.destination.stat().st_mtime_ns
        build.export_archive(source, self.destination)
        self.assertEqual(self.destination.stat().st_mtime_ns, before)

    def test_missing_make_input_fails_before_bazel(self):
        (self.root / "target/docker-config-engine-trixie.gz").unlink()
        with patch.object(build.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "Make prerequisite"):
                build.build("docker-orchagent.gz", self.destination, workspace=self.root)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
