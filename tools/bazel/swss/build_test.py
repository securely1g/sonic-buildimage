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
        for name in ("target/docker-config-engine-trixie.oci/index.json",
                     "target/docker-config-engine-trixie.oci/oci-layout",
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
        source.write_bytes(b"new container archive")
        with patch.object(build.subprocess, "run") as run:
            run.return_value.stdout = "bazel-bin/docker-orchagent.gz\n"
            build.build("docker-orchagent.gz", self.destination, workspace=self.root)
        self.assertEqual(self.destination.read_bytes(), b"new container archive")
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
        (self.root / "target/docker-config-engine-trixie.oci/index.json").unlink()
        with patch.object(build.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "Make prerequisite"):
                build.build("docker-orchagent.gz", self.destination, workspace=self.root)
        run.assert_not_called()

    def test_shared_caches_are_used_for_build_and_output_query(self):
        source = self.root / "bazel-bin/docker-orchagent.gz"
        source.parent.mkdir()
        source.write_bytes(b"new container archive")
        cache = self.root / "persistent cache"
        with patch.object(build.subprocess, "run") as run:
            run.return_value.stdout = "bazel-bin/docker-orchagent.gz\n"
            build.build("docker-orchagent.gz", self.destination, workspace=self.root,
                        cache_directory=cache, options=["--jobs=2"])
        for invocation in run.call_args_list:
            command = invocation.args[0]
            self.assertIn(f"--repository_cache={cache}/repository", command)
            self.assertIn(f"--disk_cache={cache}/disk", command)
            self.assertIn("--jobs=2", command)
            self.assertFalse(any(arg.startswith("--output_base") for arg in command))
        self.assertTrue((cache / "repository").is_dir())
        self.assertTrue((cache / "disk").is_dir())

    def test_optional_cache_does_not_change_normal_bazel_defaults(self):
        self.assertEqual(build.cache_options(None), [])
        self.assertEqual(build.cache_options(""), [])

    def test_unusable_cache_fails_before_build_and_preserves_output(self):
        cache = self.root / "cache-is-file"
        cache.write_bytes(b"not a directory")
        with patch.object(build.subprocess, "run") as run:
            with self.assertRaises(OSError):
                build.build("docker-orchagent.gz", self.destination, workspace=self.root,
                            cache_directory=cache)
        run.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), b"previous image")


if __name__ == "__main__":
    unittest.main()
