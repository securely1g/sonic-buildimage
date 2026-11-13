"""Focused fixed-roster orchestration tests; no package generation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import observe_original_host_siblings as subject

MANIFEST = Path(subject.__file__).with_name("vs_original_host_siblings.json")


class OriginalSiblingTest(unittest.TestCase):
    def test_fixed_manifest_retains_six_members_in_four_original_objects(self):
        value = subject.load_manifest(MANIFEST)
        self.assertEqual(len(value["inputs"]), 6)
        self.assertEqual(len({r["cache_name"] for r in value["inputs"]}), 4)
        self.assertEqual(value["artifact"]["id"], 14030424431)

    def test_changed_manifest_is_rejected_before_reading_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.json"
            path.write_bytes(MANIFEST.read_bytes() + b" ")
            with self.assertRaisesRegex(subject.original.RecoveryError, "manifest changed"):
                subject.load_manifest(path)

    def test_all_groups_use_the_existing_reader_and_reserve_metadata(self):
        value = subject.load_manifest(MANIFEST)
        calls = []
        def cached(path, rows, scratch, *, observer):
            calls.append((path, rows))
            return [observer(Path("/unused-member"), row, scratch) for row in rows]
        def inspect(path, row, scratch):
            return {"name": row["required_name"], "sha256": row["member_sha256"],
                    "semantic_identity": "1" * 64}
        with mock.patch.object(subject.host, "inspect_cached", side_effect=cached), \
             mock.patch.object(subject.host, "inspect_path", side_effect=inspect):
            report, code = subject.observe(value, Path("/artifact"), Path("/scratch"))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(report["results"]), 6)
        self.assertTrue(all(path.parent == Path("/artifact/execution") for path, rows in calls))
        self.assertFalse(report["package_generation"])
        self.assertFalse(report["VS_assembly_admission"])

    def test_later_failure_preserves_completed_original_groups(self):
        value = subject.load_manifest(MANIFEST)
        first = sorted({r["cache_name"] for r in value["inputs"]})[0]
        first_rows = [r for r in value["inputs"] if r["cache_name"] == first]
        complete = [{"name": r["required_name"], "sha256": r["member_sha256"]} for r in first_rows]
        with mock.patch.object(subject.host, "inspect_cached", side_effect=[complete, ValueError("private failure")]):
            report, code = subject.observe(value, Path("/artifact"), Path("/scratch"))
        self.assertEqual(code, 1)
        self.assertEqual(report["results"], complete)
        self.assertEqual(report["failure"]["error_type"], "ValueError")
        self.assertNotIn("private failure", json.dumps(report))

    def test_metadata_budget_rejects_before_accumulating_a_large_result(self):
        value = subject.load_manifest(MANIFEST)
        def cached(path, rows, scratch, *, observer):
            return [observer(Path("/unused-member"), rows[0], scratch)]
        budget = subject.host.MetadataBudget(16)
        with mock.patch.object(subject.host, "MetadataBudget", return_value=budget), \
             mock.patch.object(subject.host, "inspect_cached", side_effect=cached), \
             mock.patch.object(subject.host, "inspect_path", return_value={"data": "x" * 32}):
            report, code = subject.observe(value, Path("/artifact"), Path("/scratch"))
        self.assertEqual(code, 1)
        self.assertEqual(report["results"], [])
        self.assertIn("metadata exceeds bound", report["failure"]["reason"])


if __name__ == "__main__":
    unittest.main()
