"""Filesystem/mock-only cache observation tests; no worker cache or build access."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import vs_cache_inspection as cache


class CacheMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "cache"
        self.root.mkdir()

    def inspect(self, **limits):
        device = self.root.parent.stat().st_dev
        mounts = [{"mountpoint": str(self.root.parent),
                   "device": str(os.major(device)) + ":" + str(os.minor(device)),
                   "filesystem": "fixture", "mount_root_sha256": "0" * 64}]
        return cache.inspect_root(self.root, mounts, cache.Budget(**limits))

    def test_missing_is_not_empty(self):
        self.root.rmdir()
        self.assertEqual(self.inspect()["status"], "missing")

    def test_empty_existing_directory_is_observed(self):
        result = self.inspect()
        self.assertEqual(result["status"], "observed_complete_at_selected_depth")
        self.assertEqual(result["entries"], [])
        self.assertGreater(result["storage"]["total_bytes"], 0)

    def test_root_symlink_is_not_followed(self):
        self.root.rmdir()
        self.root.symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(self.inspect()["status"], "symlink_rejected")

    def test_regular_root_is_not_opened(self):
        self.root.rmdir()
        self.root.write_bytes(b"not a cache directory")
        with mock.patch.object(cache.os, "open", side_effect=AssertionError("file opened")):
            self.assertEqual(self.inspect()["status"], "non_directory")

    def test_child_symlink_does_not_escape(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "never_observe").write_bytes(b"private")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        result = self.inspect()
        self.assertEqual([x["relative_name"] for x in result["entries"]], ["link"])
        self.assertEqual(result["entries"][0]["kind"], "symlink")

    def test_cache_files_are_never_opened_and_remain_unchanged(self):
        payload = self.root / "package.deb"
        payload.write_bytes(b"existing bytes, never parsed")
        before = payload.stat()
        original_open = os.open

        def directory_only(path, flags, **kwargs):
            self.assertTrue(flags & os.O_DIRECTORY)
            self.assertFalse(flags & (os.O_CREAT | os.O_TRUNC | os.O_RDWR | os.O_WRONLY))
            return original_open(path, flags, **kwargs)

        with mock.patch.object(cache.os, "open", side_effect=directory_only), \
                mock.patch("builtins.open", side_effect=AssertionError("content read")):
            result = self.inspect()
        self.assertEqual(result["entries"][0]["kind"], "file")
        self.assertEqual(payload.read_bytes(), b"existing bytes, never parsed")
        self.assertEqual((payload.stat().st_mtime_ns, payload.stat().st_mode),
                         (before.st_mtime_ns, before.st_mode))

    def test_permission_denied_is_not_missing(self):
        with mock.patch.object(cache.os, "open", side_effect=PermissionError):
            self.assertEqual(self.inspect()["status"], "permission_denied")

    def test_entry_limit_is_explicit(self):
        for name in ("a", "b", "c"):
            (self.root / name).write_bytes(b"")
        result = self.inspect(entries=1)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["status"], "observed_truncated")
        self.assertIn("entry_limit", result["truncation"])

    def test_directory_limit_does_not_descend(self):
        child = self.root / "child"
        child.mkdir()
        (child / "hidden").write_bytes(b"")
        result = self.inspect(directories=0)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["children"], "directory_limit")

    def test_depth_limit_preserves_only_selected_metadata(self):
        child = self.root / "one" / "two"
        child.mkdir(parents=True)
        (child / "too_deep").write_bytes(b"")
        result = self.inspect()
        self.assertEqual({x["relative_name"] for x in result["entries"]}, {"one", "one/two"})
        self.assertEqual(result["entries"][-1]["children"], "depth_limit")

    def test_directory_replacement_is_detected_before_descent(self):
        (self.root / "child").mkdir()
        outside = self.root.parent / "different"
        outside.mkdir()
        (outside / "never_observe").write_bytes(b"")
        original_open = os.open

        def changed(path, flags, **kwargs):
            return original_open(outside, flags) if path == "child" else original_open(path, flags, **kwargs)

        with mock.patch.object(cache.os, "open", side_effect=changed):
            result = self.inspect()
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["children"], "changed_during_observation")
        self.assertEqual(result["status"], "observed_truncated")

    def test_deadline_does_not_claim_empty_cache(self):
        result = self.inspect(seconds=-1)
        self.assertEqual(result["status"], "observed_truncated")
        self.assertEqual(result["truncation"], ["deadline"])

    def test_all_owned_descriptors_close_after_truncation(self):
        (self.root / "child").mkdir()
        (self.root / "child" / "beyond_limit").write_bytes(b"")
        original_open, original_close = os.open, os.close
        opened, closed = [], []

        def track_open(*args, **kwargs):
            fd = original_open(*args, **kwargs); opened.append(fd); return fd

        def track_close(fd):
            closed.append(fd); return original_close(fd)

        with mock.patch.object(cache.os, "open", side_effect=track_open), \
                mock.patch.object(cache.os, "close", side_effect=track_close):
            self.inspect(entries=1)
        self.assertCountEqual(opened, closed)


class MountAndIdentityTest(unittest.TestCase):
    def test_shared_storage_distinguishes_unknown_from_same_and_different(self):
        cases = [
            ([{"status": "missing"}, {"storage": {}, "metadata": {"device": 7}}], None),
            ([{"storage": {}, "metadata": {"device": 7}},
              {"storage": {}, "metadata": {"device": 7}}], True),
            ([{"storage": {}, "metadata": {"device": 7}},
              {"storage": {}, "metadata": {"device": 8}}], False),
        ]
        for roots, expected in cases:
            with self.subTest(expected=expected), \
                    mock.patch("builtins.open", mock.mock_open(read_data=b"")), \
                    mock.patch.object(cache, "inspect_root", side_effect=roots):
                self.assertIs(cache.collect({})["shared_backing_device"], expected)

    def environment(self):
        return {"RUNNER_NAME": "ks-example-1", "GITHUB_REPOSITORY": "securely1g/sonic-buildimage",
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "securely1g",
                "GITHUB_TRIGGERING_ACTOR": "securely1g", "GITHUB_RUN_ID": "1234",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40}

    def identity(self, **updates):
        env = self.environment(); env.update(updates)
        with mock.patch.object(cache.os, "getuid", return_value=1000), \
                mock.patch.object(cache.os, "geteuid", return_value=1000), \
                mock.patch.object(cache.os, "getgid", return_value=1000), \
                mock.patch.object(cache.os, "getegid", return_value=1000):
            return cache.identity("ks-example-1", env, {"manager_revision": cache.MANAGER,
                                                        "base_image": cache.IMAGE})

    def test_current_run_attempt_and_worker_are_bound(self):
        value = self.identity()
        self.assertEqual((value["worker"], value["run"], value["attempt"]), ("ks-example-1", 1234, 2))

    def test_other_actor_runner_or_invalid_attempt_is_rejected(self):
        for key, value in [("RUNNER_NAME", "ks-other"), ("GITHUB_ACTOR", "other"),
                           ("GITHUB_TRIGGERING_ACTOR", "other"), ("GITHUB_RUN_ATTEMPT", "0"),
                           ("GITHUB_RUN_ID", "../123"), ("GITHUB_SHA", "main"),
                           ("GITHUB_EVENT_NAME", "pull_request")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.identity(**{key: value})

    def test_different_manager_or_image_is_rejected(self):
        for field in ("base_image", "manager_revision"):
            actual = {"manager_revision": cache.MANAGER, "base_image": cache.IMAGE}
            actual[field] = "different"
            with mock.patch.object(cache.os, "getuid", return_value=1000), \
                    mock.patch.object(cache.os, "geteuid", return_value=1000), \
                    mock.patch.object(cache.os, "getgid", return_value=1000), \
                    mock.patch.object(cache.os, "getegid", return_value=1000), self.assertRaises(ValueError):
                cache.identity("ks-example-1", self.environment(), actual)

    def test_mount_source_and_options_are_not_retained(self):
        mounts = cache.parse_mounts("1 0 8:1 /private/root / rw,secret=hidden - ext4 private-source rw,password=hidden\n"
                                    "2 1 8:2 /cache /dpkg_cache rw - ext4 another-private-source rw\n")
        value = cache.covering_mount("/dpkg_cache", mounts)
        self.assertTrue(value["exact_mountpoint"])
        self.assertEqual(value["device"], "8:2")
        encoded = json.dumps(mounts)
        for word in ("password", "secret", "private-source", "/private/root"):
            self.assertNotIn(word, encoded)

    def test_mount_escape_and_malformed_input(self):
        mounts = cache.parse_mounts("1 0 8:1 / /space\\040name rw - ext4 source rw\n")
        self.assertEqual(mounts[0]["mountpoint"], "/space name")
        with self.assertRaises(ValueError):
            cache.parse_mounts("malformed\n")


if __name__ == "__main__":
    unittest.main()
