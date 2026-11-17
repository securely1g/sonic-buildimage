"""Check bounded cleanup of previous container and VS jobs' Bazel caches."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from tools.bazel.ci import cleanup_runner_bazel_cache as subject


class RunnerBazelCacheCleanupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "_github_home" / ".cache"
        self.cache.mkdir(parents=True)
        self.workspace = self.root / "runner workspace"
        self.vs_cache = self.workspace / "vs-cache" / "bazel"
        self.vs_cache.parent.mkdir(parents=True)
        self.paths = [self.cache / name for name in subject.CACHE_NAMES] + [self.vs_cache]

    def populate(self):
        for path in self.paths:
            path.mkdir()
            (path / "data").write_text("cached data")

    def test_removes_only_container_and_vs_bazel_caches(self):
        self.populate()
        retained = [
            self.cache / "bazelisk", self.root / "job-script.sh",
            self.root / "_github_home" / "credentials",
            self.vs_cache.parent / "packages", self.workspace / "checkout-marker",
        ]
        for path in retained:
            path.write_text("retain")

        def remove(command, check):
            self.assertTrue(check)
            self.assertEqual(command[:-1],
                             ["sudo", "-n", "rm", "--recursive", "--force",
                              "--one-file-system", "--"])
            path = Path(command[-1])
            self.assertIn(path, self.paths)
            shutil.rmtree(path)

        with mock.patch.object(subject, "check_container_users") as containers, \
                mock.patch.object(subject, "check_mounts") as mounts, \
                mock.patch.object(subject.subprocess, "run", side_effect=remove) as run:
            subject.cleanup(self.root, self.workspace)
        self.assertEqual(containers.call_args_list, [mock.call(path) for path in self.paths])
        self.assertEqual(mounts.call_args_list, [mock.call(path) for path in self.paths])
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all(path.read_text() == "retain" for path in retained))
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_missing_caches_need_no_docker_or_sudo(self):
        for root in (self.root, self.root / "fresh"):
            root.mkdir(exist_ok=True)
            with self.subTest(root=root), \
                    mock.patch.object(subject, "check_container_users") as containers, \
                    mock.patch.object(subject.subprocess, "run") as run:
                subject.cleanup(root, self.workspace)
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
                        subject.cleanup(self.root, self.workspace)
                    run.assert_not_called()
                path.unlink()
                if path != self.cache / "bazel":
                    path.mkdir()
        self.assertTrue(outside.is_dir())

    def test_rejects_invalid_runner_temp_and_non_directory_cache(self):
        for root in (Path("."), self.root / "missing"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                subject.cleanup(root, self.workspace)
        (self.cache / "bazel").write_text("not a directory")
        with self.assertRaisesRegex(ValueError, "unexpected local cache"):
            subject.cleanup(self.root, self.workspace)

    def test_container_or_mount_reference_prevents_all_deletion(self):
        self.populate()
        for guard in ("check_container_users", "check_mounts"):
            with self.subTest(guard=guard), \
                    mock.patch.object(subject, "check_container_users"), \
                    mock.patch.object(subject, "check_mounts"), \
                    mock.patch.object(subject, guard, side_effect=ValueError("in use")), \
                    mock.patch.object(subject.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "in use"):
                    subject.cleanup(self.root, self.workspace)
                run.assert_not_called()
                self.assertTrue(all((self.cache / name).is_dir() for name in subject.CACHE_NAMES))

    def test_removal_failure_is_reported(self):
        self.populate()
        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, "rm")):
            with self.assertRaises(subprocess.CalledProcessError):
                subject.cleanup(self.root, self.workspace)
        self.assertTrue((self.cache / "bazel").is_dir())

    def test_success_without_removal_is_reported(self):
        self.populate()
        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "run"):
            with self.assertRaisesRegex(ValueError, "still exists"):
                subject.cleanup(self.root, self.workspace)

    def test_vs_cache_is_removed_even_without_container_home_caches(self):
        self.vs_cache.mkdir()
        (self.vs_cache / "output").write_text("VS build output")
        packages = self.vs_cache.parent / "packages"
        packages.mkdir()
        (packages / "package.deb").write_text("retain")

        def remove(command, check):
            self.assertTrue(check)
            self.assertEqual(Path(command[-1]), self.vs_cache)
            shutil.rmtree(self.vs_cache)

        with mock.patch.object(subject, "check_container_users") as containers, \
                mock.patch.object(subject, "check_mounts") as mounts, \
                mock.patch.object(subject.subprocess, "run", side_effect=remove):
            subject.cleanup(self.root, self.workspace)
        containers.assert_called_once_with(self.vs_cache)
        mounts.assert_called_once_with(self.vs_cache)
        self.assertFalse(self.vs_cache.exists())
        self.assertEqual((packages / "package.deb").read_text(), "retain")

    def test_vs_reference_blocks_all_deletion(self):
        self.populate()

        def check(path):
            if path == self.vs_cache:
                raise ValueError("VS cache is in use")

        for guard in ("check_container_users", "check_mounts"):
            with self.subTest(guard=guard), \
                    mock.patch.object(subject, "check_container_users"), \
                    mock.patch.object(subject, "check_mounts"), \
                    mock.patch.object(subject, guard, side_effect=check), \
                    mock.patch.object(subject.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "VS cache is in use"):
                    subject.cleanup(self.root, self.workspace)
                run.assert_not_called()
                self.assertTrue(all(path.is_dir() for path in self.paths))

    def test_rejects_redirected_vs_cache_and_parent(self):
        outside = self.root / "retained"
        outside.mkdir()
        (outside / "sentinel").write_text("retain")
        for path in (self.vs_cache, self.vs_cache.parent):
            with self.subTest(path=path):
                if path.is_dir():
                    path.rmdir()
                path.symlink_to(outside, target_is_directory=True)
                with mock.patch.object(subject.subprocess, "run") as run:
                    with self.assertRaisesRegex(ValueError, "Refusing"):
                        subject.cleanup(self.root, self.workspace)
                    run.assert_not_called()
                path.unlink()
                if path == self.vs_cache.parent:
                    path.mkdir()
        self.assertEqual((outside / "sentinel").read_text(), "retain")

    def test_rejects_invalid_workspace(self):
        for workspace in (Path("."), self.root / "missing"):
            with self.subTest(workspace=workspace), self.assertRaisesRegex(
                    ValueError, "runner workspace"):
                subject.cleanup(self.root, workspace)

    def test_cli_accepts_workspace_with_spaces(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", subject.__name__,
             "--runner-workspace", str(self.workspace)],
            cwd=Path(__file__).resolve().parents[3],
            env=dict(os.environ, RUNNER_TEMP=str(self.root)),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No selected local build caches", result.stdout)

    def test_explicit_package_cleanup_removes_only_the_obsolete_local_directory(self):
        self.populate()
        packages = self.vs_cache.parent / "packages"
        packages.mkdir()
        (packages / "package.tgz").write_text("old local cache")
        nfs = self.root / "shared-nfs"
        nfs.mkdir()
        (nfs / "package.tgz").write_text("retain shared cache")
        expected = [*self.paths, packages]

        def remove(command, check):
            self.assertTrue(check)
            path = Path(command[-1])
            self.assertIn(path, expected)
            shutil.rmtree(path)

        with mock.patch.object(subject, "check_container_users") as containers, \
                mock.patch.object(subject, "check_mounts") as mounts, \
                mock.patch.object(subject.subprocess, "run", side_effect=remove) as run:
            subject.cleanup(self.root, self.workspace, remove_package_cache=True)
        self.assertEqual(containers.call_args_list, [mock.call(path) for path in expected])
        self.assertEqual(mounts.call_args_list, [mock.call(path) for path in expected])
        self.assertEqual(run.call_count, 5)
        self.assertTrue(all(not path.exists() for path in expected))
        self.assertEqual((nfs / "package.tgz").read_text(), "retain shared cache")

    def test_package_reference_blocks_all_cleanup(self):
        self.populate()
        packages = self.vs_cache.parent / "packages"
        packages.mkdir()

        def check(path):
            if path == packages:
                raise ValueError("package cache is in use")

        for guard in ("check_container_users", "check_mounts"):
            with self.subTest(guard=guard), \
                    mock.patch.object(subject, "check_container_users"), \
                    mock.patch.object(subject, "check_mounts"), \
                    mock.patch.object(subject, guard, side_effect=check), \
                    mock.patch.object(subject.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "package cache is in use"):
                    subject.cleanup(self.root, self.workspace, remove_package_cache=True)
                run.assert_not_called()
                self.assertTrue(all(path.is_dir() for path in [*self.paths, packages]))

    def test_package_symlink_is_never_followed_or_removed(self):
        outside = self.root / "shared-nfs"
        outside.mkdir()
        (outside / "sentinel").write_text("retain")
        packages = self.vs_cache.parent / "packages"
        packages.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(subject.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "Refusing unexpected local cache"):
                subject.cleanup(self.root, self.workspace, remove_package_cache=True)
            run.assert_not_called()
        self.assertEqual((outside / "sentinel").read_text(), "retain")

    def test_retains_cache_only_within_both_exact_budgets(self):
        gib = 1024**3
        self.vs_cache.mkdir()
        sentinel = self.vs_cache / "repository"
        sentinel.write_text("retained")
        for size, free, retained in (
                (40 * gib, 200 * gib, True),
                (40 * gib + 1, 200 * gib, False),
                (40 * gib, 200 * gib - 1, False)):
            with self.subTest(size=size, free=free), \
                    mock.patch.object(subject, "check_container_users") as containers, \
                    mock.patch.object(subject, "check_mounts") as mounts, \
                    mock.patch.object(subject.subprocess, "check_output",
                                      return_value=f"{size}\t{self.vs_cache}\n") as du, \
                    mock.patch.object(subject.shutil, "disk_usage",
                                      return_value=SimpleNamespace(free=free)), \
                    mock.patch.object(subject.subprocess, "run",
                                      side_effect=lambda command, check: shutil.rmtree(command[-1])) as remove:
                if not self.vs_cache.exists():
                    self.vs_cache.mkdir()
                    sentinel.write_text("retained")
                subject.cleanup(self.root, self.workspace, retain_vs_cache_max_gib=40)
                self.assertEqual(self.vs_cache.exists(), retained)
                self.assertEqual(remove.call_count, int(not retained))
                containers.assert_called_once_with(self.vs_cache)
                mounts.assert_called_once_with(self.vs_cache)
                du.assert_called_once_with(
                    ["sudo", "-n", "du", "--summarize", "--one-file-system",
                     "--block-size=1", "--", str(self.vs_cache)], text=True)
                if retained:
                    self.assertEqual(sentinel.read_text(), "retained")

    def test_retention_still_removes_obsolete_caches_before_measuring_space(self):
        self.populate()
        packages = self.vs_cache.parent / "packages"
        packages.mkdir()

        def measure(path):
            self.assertTrue(all(not cache.exists() for cache in self.paths[:-1]))
            self.assertFalse(packages.exists())
            return SimpleNamespace(free=200 * 1024**3)

        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "check_output", return_value="1\tcache\n"), \
                mock.patch.object(subject.shutil, "disk_usage", side_effect=measure), \
                mock.patch.object(subject.subprocess, "run",
                                  side_effect=lambda command, check: shutil.rmtree(command[-1])):
            subject.cleanup(self.root, self.workspace, remove_package_cache=True,
                            retain_vs_cache_max_gib=40)
        self.assertTrue(self.vs_cache.is_dir())

    def test_retention_never_bypasses_active_cache_guards(self):
        self.vs_cache.mkdir()
        for guard in ("check_container_users", "check_mounts"):
            with self.subTest(guard=guard), \
                    mock.patch.object(subject, "check_container_users"), \
                    mock.patch.object(subject, "check_mounts"), \
                    mock.patch.object(subject, guard, side_effect=ValueError("in use")), \
                    mock.patch.object(subject.subprocess, "check_output") as du, \
                    mock.patch.object(subject.subprocess, "run") as remove:
                with self.assertRaisesRegex(ValueError, "in use"):
                    subject.cleanup(self.root, self.workspace, retain_vs_cache_max_gib=40)
                du.assert_not_called()
                remove.assert_not_called()

    def test_retention_measurement_failure_stops_cleanup(self):
        self.vs_cache.mkdir()
        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "check_output",
                                  side_effect=subprocess.CalledProcessError(1, "du")), \
                mock.patch.object(subject.subprocess, "run") as remove:
            with self.assertRaises(subprocess.CalledProcessError):
                subject.cleanup(self.root, self.workspace, retain_vs_cache_max_gib=40)
            remove.assert_not_called()

    def test_negative_retention_limits_are_rejected(self):
        for options in ({"retain_vs_cache_max_gib": -1}, {"min_free_gib": -1}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "nonnegative"):
                subject.cleanup(self.root, self.workspace, **options)

    def test_discards_large_action_cache_before_downloaded_repositories(self):
        disk_cache = self.vs_cache / "disk_cache"
        disk_cache.mkdir(parents=True)
        repository = self.vs_cache / "repository_cache"
        repository.mkdir()
        sentinel = repository / "retained"
        sentinel.write_text("downloaded toolchain archive")
        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "check_output",
                                  side_effect=[f"{41 * 1024**3}\tcache\n", "1\tcache\n"]), \
                mock.patch.object(subject.shutil, "disk_usage",
                                  return_value=SimpleNamespace(free=200 * 1024**3)), \
                mock.patch.object(subject.subprocess, "run",
                                  side_effect=lambda command, check: shutil.rmtree(command[-1])) as remove:
            subject.cleanup(self.root, self.workspace, retain_vs_cache_max_gib=40)
        self.assertEqual([Path(call.args[0][-1]) for call in remove.call_args_list], [disk_cache])
        self.assertEqual(sentinel.read_text(), "downloaded toolchain archive")

    def test_redirected_action_cache_is_refused_when_reclaiming_for_retention(self):
        self.vs_cache.mkdir()
        outside = self.root / "shared-action-cache"
        outside.mkdir()
        (self.vs_cache / "disk_cache").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(subject, "check_container_users"), \
                mock.patch.object(subject, "check_mounts"), \
                mock.patch.object(subject.subprocess, "check_output",
                                  return_value=f"{41 * 1024**3}\tcache\n"), \
                mock.patch.object(subject.shutil, "disk_usage",
                                  return_value=SimpleNamespace(free=200 * 1024**3)), \
                mock.patch.object(subject.subprocess, "run") as remove:
            with self.assertRaisesRegex(ValueError, "changed during cleanup"):
                subject.cleanup(self.root, self.workspace, retain_vs_cache_max_gib=40)
            remove.assert_not_called()
        self.assertTrue(outside.is_dir())
