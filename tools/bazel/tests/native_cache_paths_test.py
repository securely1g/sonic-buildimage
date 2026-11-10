"""Pure temporary-directory tests; no real cache, project build or DEB."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tools.bazel.ci import native_cache_paths as observer


class NativeCachePathTests(unittest.TestCase):
    def mounts(self, path):
        info = path.stat()
        return [{"mountpoint": "/", "device": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
                 "filesystem": "ext4", "mount_root_sha256": "0" * 64}]

    def test_six_leaves_no_output_base_or_other_old_worker(self):
        paths = observer.selected_paths("ks-new-registration")
        self.assertEqual(len(paths), 12)
        leaves = [(role, path) for role, path in paths if role.rsplit("_", 1)[-1] in observer.CHILDREN]
        self.assertEqual(len(leaves), 6)
        self.assertTrue(all("output-base" not in path and "p4rt-owner" not in path for _, path in paths))
        self.assertEqual(dict(paths)["old_repository"], observer.CACHE_ROOT + "/" + observer.OLD_WORKER + "/repository")
        with self.assertRaises(ValueError):
            observer.selected_paths(observer.OLD_WORKER)

    def test_missing_is_local_and_no_entries_are_enumerated(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with mock.patch.object(os, "listdir", side_effect=AssertionError("no scan")), mock.patch.object(os, "scandir", side_effect=AssertionError("no scan")):
                result = observer.inspect_directory(str(root / "missing"), self.mounts(root), time.monotonic() + 10)
            self.assertEqual(result["status"], "missing_on_this_registration_at_this_path")
            self.assertFalse(result["contents_read"])

    def test_symlink_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "real").mkdir(); (root / "link").symlink_to(root / "real")
            result = observer.inspect_directory(str(root / "link" / "repository"), self.mounts(root), time.monotonic() + 10)
            self.assertEqual(result["status"], "symlink_rejected")

    def test_regular_file_is_not_a_cache_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "file").write_bytes(b"fixture")
            result = observer.inspect_directory(str(root / "file"), self.mounts(root), time.monotonic() + 10)
            self.assertEqual(result["status"], "non_directory")

    def test_ancestor_replacement_invalidates_observation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "leaf").mkdir(); original = os.stat; calls = 0
            def changed(path, *args, **kwargs):
                nonlocal calls
                if path == "leaf" and "dir_fd" in kwargs:
                    calls += 1
                    if calls == 2:
                        raise FileNotFoundError("fixture ancestor replaced")
                return original(path, *args, **kwargs)
            with mock.patch.object(os, "stat", side_effect=changed):
                result = observer.inspect_directory(str(root / "leaf"), self.mounts(root), time.monotonic() + 10)
            self.assertEqual(result["status"], "not_fully_observed")

    def test_nested_mount_is_not_the_approved_actions_backing(self):
        context = {"worker": "ks-new-registration"}
        good = {"status": "DIRECTORY_METADATA_OBSERVED_CONTENTS_UNREAD", "metadata": {"device": "8:1"},
                "mount": {"mountpoint": "/actions", "filesystem": "ext4", "mount_root_sha256": "a" * 64}}
        def inspect(path, mounts, expires):
            result = {"path": path, "status": good["status"], "metadata": dict(good["metadata"]), "mount": dict(good["mount"])}
            if path.endswith("/repository"):
                result["mount"]["mountpoint"] = path
            elif path.endswith("/disk"):
                result = {"path": path, "status": "missing_on_this_registration_at_this_path"}
            return result
        with mock.patch.object(observer.observation, "read", return_value="fixture"), mock.patch.object(observer.observation, "parse_mounts", return_value=[]), mock.patch.object(observer, "inspect_directory", side_effect=inspect):
            result = observer.collect(context)
        self.assertFalse(result["directories"]["old_repository"]["same_current_actions_backing"])
        self.assertIsNone(result["directories"]["current_disk"]["same_current_actions_backing"])
        self.assertTrue(result["directories"]["current_bazelisk"]["same_current_actions_backing"])
        self.assertEqual(result["old_native"]["conclusion"], "failure")
        self.assertFalse(result["cache_reuse"])

    def test_mount_snapshot_change_invalidates_comparison(self):
        row = {"status": "missing_on_this_registration_at_this_path"}
        with mock.patch.object(observer.observation, "read", side_effect=["before", "after"]), mock.patch.object(observer.observation, "parse_mounts", return_value=[]), mock.patch.object(observer, "inspect_directory", return_value=row.copy()):
            result = observer.collect({"worker": "ks-new-registration"})
        self.assertFalse(result["mount_snapshot_stable"])
        self.assertIsNone(result["current_actions_mount_compatible"])
        self.assertTrue(all(x["same_current_actions_backing"] is None for x in result["directories"].values()))


if __name__ == "__main__":
    unittest.main()
