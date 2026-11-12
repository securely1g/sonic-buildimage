"""Check bounded cleanup of previous container jobs' Bazel caches."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import cleanup_runner_bazel_cache as subject


class RunnerBazelCacheCleanupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "_github_home" / ".cache"
        self.cache.mkdir(parents=True)

    def populate(self):
        for name in subject.CACHE_NAMES:
            path = self.cache / name
            path.mkdir()
            (path / "data").write_text("cached data")

    def test_removes_only_the_three_named_directories(self):
        self.populate()
        retained = [
            self.cache / "bazelisk", self.root / "job-script.sh",
            self.root / "_github_home" / "credentials",
        ]
        for path in retained:
            path.write_text("retain")

        def remove(command, check):
            self.assertTrue(check)
            self.assertEqual(command[:-1],
                             ["sudo", "-n", "rm", "--recursive", "--force",
                              "--one-file-system", "--"])
            path = Path(command[-1])
            self.assertEqual(path.parent, self.cache)
            self.assertIn(path.name, subject.CACHE_NAMES)
            shutil.rmtree(path)

        with mock.patch.object(subject, "check_container_users") as containers, \
                mock.patch.object(subject, "check_mounts") as mounts, \
                mock.patch.object(subject.subprocess, "run", side_effect=remove) as run:
            subject.cleanup(self.root)
        containers.assert_called_once_with(self.cache)
        mounts.assert_called_once_with(self.cache)
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(path.read_text() == "retain" for path in retained))
        self.assertTrue(all(not (self.cache / name).exists() for name in subject.CACHE_NAMES))

    def test_missing_caches_need_no_docker_or_sudo(self):
        for root in (self.root, self.root / "fresh"):
            root.mkdir(exist_ok=True)
            with self.subTest(root=root), \
                    mock.patch.object(subject, "check_container_users") as containers, \
                    mock.patch.object(subject.subprocess, "run") as run:
                subject.cleanup(root)
                containers.assert_not_called()
                run.assert_not_called()

    def test_rejects_redirected_home_cache_and_targets(self):
        outside = self.root / "retained"
        outside.mkdir()
        for path in (self.cache / "bazel", self.cache, self.cache.parent):
            with self.subTest(path=path):
                if path == self.cache.parent:
                    self.cache.rmdir()
                if path.is_dir():
                    path.rmdir()
                path.symlink_to(outside, target_is_directory=True)
                with mock.patch.object(subject.subprocess, "run") as run:
                    with self.assertRaisesRegex(ValueError, "Refusing"):
                        subject.cleanup(self.root)
                    run.assert_not_called()
                path.unlink()
                if path != self.cache / "bazel":
                    path.mkdir()
        self.assertTrue(outside.is_dir())

    def test_rejects_invalid_runner_temp_and_non_directory_cache(self):
        for root in (Path("."), self.root / "missing"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                subject.cleanup(root)
        (self.cache / "bazel").write_text("not a directory")
        with self.assertRaisesRegex(ValueError, "unexpected Bazel cache"):
            subject.cleanup(self.root)

    def test_container_or_mount_reference_prevents_all_deletion(self):
        self.populate()
        for guard in ("check_container_users", "check_mounts"):
            with self.subTest(guard=guard), \
                    mock.patch.object(subject, "check_container_users"), \
                    mock.patch.object(subject, "check_mounts"), \
                    mock.patch.object(subject, guard, side_effect=ValueError("in use")), \
                    mock.patch.object(subject.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "in use"):
                    subject.cleanup(self.root)
                run.assert_not_called()
                self.assertTrue(all((self.cache / name).is_dir() for name in subject.CACHE_NAMES))

    def test_removal_failure_is_reported(self):
        self.populate()
        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, "rm")):
            with self.assertRaises(subprocess.CalledProcessError):
                subject.cleanup(self.root)
        self.assertTrue((self.cache / "bazel").is_dir())

    def test_success_without_removal_is_reported(self):
        self.populate()
        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "run"):
            with self.assertRaisesRegex(ValueError, "still exists"):
                subject.cleanup(self.root)
