"""Exercise original-byte capture with inert plain-text fixtures, never packages."""
import hashlib
import json
from pathlib import Path
import tempfile
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import capture_vs_original_objects as capture


class OriginalObjectCaptureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.output = self.root / "outputs"
        self.scratch = self.root / "scratch"
        for path in (self.source, self.output, self.scratch):
            path.mkdir(mode=0o700)
        patch = mock.patch.object(capture, "ROOT", self.source)
        patch.start()
        self.addCleanup(patch.stop)
        capacity = mock.patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(
            f_bavail=1024**3, f_frsize=1))
        capacity.start()
        self.addCleanup(capacity.stop)

    def item(self, name="plain.tgz", data=b"original bytes"):
        (self.source / name).write_bytes(data)
        return dict(name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def run_capture(self, items):
        return capture.capture(dict(objects=items, original_observation={"run": 42865507717}),
                               self.output, self.scratch, {"run": 123})

    def test_fixed_manifest_has_the_exact_original_scope(self):
        path = Path(__file__).resolve().parents[1] / "ci/vs_original_cache_objects.json"
        with mock.patch.object(capture, "ROOT", Path("/dpkg_cache")):
            value = capture.selection(path)
        self.assertEqual(len(value["objects"]), 106)
        self.assertEqual(sum(row["bytes"] for row in value["objects"]), 276022500)

    def test_changed_manifest_is_rejected(self):
        path = self.root / "changed.json"
        path.write_bytes(b"x" * 29014)
        with self.assertRaises(capture.recovery.RecoveryError):
            capture.selection(path)

    def test_exact_original_bytes_are_preserved_without_admission(self):
        item = self.item()
        result = self.run_capture([item])
        self.assertEqual((self.output / "execution/plain.tgz").read_bytes(), b"original bytes")
        self.assertTrue(result["complete_original_byte_capture"])
        self.assertFalse(result["current_source_or_full_vs_admission"])
        self.assertEqual(result["source_key_compatibility"], "not_established")

    def test_missing_source_preserves_an_explicit_partial_report(self):
        item = self.item()
        (self.source / item["name"]).unlink()
        result = self.run_capture([item])
        self.assertEqual(result["objects"][0]["status"], "original_missing")
        self.assertFalse(result["complete_original_byte_capture"])
        self.assertFalse((self.output / "execution/plain.tgz").exists())

    def test_same_size_different_hash_is_not_retained(self):
        item = self.item()
        (self.source / item["name"]).write_bytes(b"modified bytes")
        result = self.run_capture([item])
        self.assertEqual(result["objects"][0]["status"], "original_hash_mismatch")
        self.assertEqual(list((self.output / "execution").glob("*.tgz")), [])

    def test_different_size_is_not_read_or_retained(self):
        item = self.item()
        (self.source / item["name"]).write_bytes(b"short")
        result = self.run_capture([item])
        self.assertEqual(result["objects"][0]["status"], "original_size_mismatch")

    def test_source_symlink_is_rejected(self):
        item = self.item()
        actual = self.source / "actual"
        (self.source / item["name"]).rename(actual)
        (self.source / item["name"]).symlink_to(actual)
        result = self.run_capture([item])
        self.assertEqual(result["objects"][0]["status"], "original_not_safely_read")

    def test_source_parent_symlink_is_rejected(self):
        item = self.item()
        link = self.root / "link"
        link.symlink_to(self.source, target_is_directory=True)
        with mock.patch.object(capture, "ROOT", link):
            result = self.run_capture([item])
        self.assertEqual(result["objects"][0]["status"], "original_not_safely_read")

    def test_source_mutation_during_read_is_not_retained(self):
        item = self.item()
        original_copy = capture.recovery.copy_member
        def mutate(*args):
            result = original_copy(*args)
            (self.source / item["name"]).write_bytes(b"modified bytes")
            return result
        with mock.patch.object(capture.recovery, "copy_member", side_effect=mutate):
            result = self.run_capture([item])
        self.assertEqual(result["objects"][0]["status"], "original_not_safely_read")
        self.assertFalse((self.output / "execution/plain.tgz").exists())

    def test_partial_capture_keeps_good_original_without_substitutions(self):
        good = self.item("good.tgz")
        bad = self.item("bad.tgz")
        (self.source / bad["name"]).write_bytes(b"modified bytes")
        result = self.run_capture([good, bad])
        self.assertEqual(result["captured_objects"], 1)
        self.assertEqual(result["missing_or_mismatched_objects"], 1)
        self.assertEqual(result["objects"][1]["sha256"], bad["sha256"])
        self.assertEqual([p.name for p in (self.output / "execution").glob("*.tgz")], ["good.tgz"])

    def test_existing_output_is_preserved(self):
        item = self.item()
        (self.output / "execution").mkdir()
        keep = self.output / "execution/keep"
        keep.write_text("preserve")
        with self.assertRaises(FileExistsError):
            self.run_capture([item])
        self.assertEqual(keep.read_text(), "preserve")

    def test_output_failure_is_not_mislabeled_as_missing_input(self):
        item = self.item()
        with mock.patch.object(capture.recovery.Outputs, "open", side_effect=OSError("fixture output failure")):
            with self.assertRaises(OSError):
                self.run_capture([item])

    def test_staging_write_failure_is_fatal_without_a_partial_report(self):
        item = self.item()
        with tempfile.TemporaryFile(dir=self.scratch) as staged:
            with mock.patch.object(staged, "write", side_effect=OSError("fixture staging failure")):
                with mock.patch.object(capture.tempfile, "TemporaryFile", return_value=staged):
                    with self.assertRaises(OSError):
                        self.run_capture([item])
        self.assertFalse((self.output / "execution/capture-lineage.json").exists())

    def test_staging_fsync_failure_is_fatal_without_a_partial_report(self):
        item = self.item()
        with mock.patch.object(capture.recovery.os, "fsync", side_effect=OSError("fixture fsync failure")):
            with self.assertRaises(OSError):
                self.run_capture([item])
        self.assertFalse((self.output / "execution/capture-lineage.json").exists())

    def test_insufficient_capture_space_is_rejected_before_output(self):
        item = self.item()
        with mock.patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=1)):
            with self.assertRaises(capture.recovery.RecoveryError):
                self.run_capture([item])
        self.assertFalse((self.output / "execution").exists())


    def test_legacy_fixed_manifest_has_the_observed34_scope(self):
        path = Path(__file__).resolve().parents[1] / "ci/vs_original_legacy_objects.json"
        with mock.patch.object(capture, "ROOT", Path("/dpkg_cache")):
            value = capture.selection(path, "legacy")
        self.assertEqual(len(value["objects"]), 34)
        self.assertEqual(sum(row["bytes"] for row in value["objects"]), 4644482017)
        self.assertEqual(value["original_observation"]["run"], 43419969021)

    def test_fixed_scopes_reject_the_other_manifest(self):
        root = Path(__file__).resolve().parents[1] / "ci"
        for name, scope in (("vs_original_cache_objects.json", "legacy"),
                            ("vs_original_legacy_objects.json", "host")):
            with self.subTest(scope=scope), self.assertRaises(capture.recovery.RecoveryError):
                capture.selection(root / name, scope)

    def test_legacy_capture_preserves_bytes_and_its_original_lineage(self):
        item = self.item()
        value = dict(objects=[item], original_observation={"run": 43419969021})
        with mock.patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(
                f_bavail=20 * 1024**3, f_frsize=1)):
            result = capture.capture(value, self.output, self.scratch, {"run": 123}, "legacy")
        self.assertEqual((self.output / "execution/plain.tgz").read_bytes(), b"original bytes")
        self.assertEqual(result["selection_sha256"], capture.SCOPES["legacy"]["sha256"])
        self.assertEqual(result["original_observation"], value["original_observation"])
        self.assertEqual(result["ambiguous_required_names"], 4)
        self.assertFalse(result["current_source_or_full_vs_admission"])

    def test_host_sized_workspace_cannot_start_the_larger_legacy_capture(self):
        item = self.item()
        with self.assertRaises(capture.recovery.RecoveryError):
            capture.capture(dict(objects=[item], original_observation={}),
                            self.output, self.scratch, {"run": 123}, "legacy")
        self.assertFalse((self.output / "execution").exists())


if __name__ == "__main__":
    unittest.main()
