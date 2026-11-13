"""Mock metadata and plain-file staging tests; no API, archive or DEB generation."""
import copy
import hashlib
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import vs_reuse_stage as stage


def fixtures():
    record = lambda raw: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    row = {"artifact_id": 123, "name": "original-images", "digest": "sha256:" + "a" * 64,
           "zip_bytes": 100, "expanded_bytes": 120, "run_id": 456, "attempt": 1, "job_id": 789,
           "job_name": "Original producer", "upload_step": "Retain original outputs",
           "head": "b" * 40, "event": "workflow_dispatch", "workflow": ".github/workflows/bazel-oci.yml",
           "members": {"runtime.gz": "target/docker-example.gz"}}
    value = {"files": {"MODULE.bazel": record(b"source"), "target/docker-example.gz": record(b"original")},
             "groups": {"source": ["MODULE.bazel"]}, "transfer": [row]}
    artifact = {"id": 123, "name": "original-images", "digest": row["digest"], "size_in_bytes": 100,
                "expired": False, "workflow_run": {"id": 456, "head_sha": row["head"]},
                "created_at": "2026-11-10T10:02:00Z"}
    run = {"id": 456, "run_attempt": 1, "head_sha": row["head"], "event": row["event"],
           "path": row["workflow"], "status": "completed",
           "repository": {"full_name": "securely1g/sonic-buildimage"},
           "actor": {"login": "securely1g"}, "triggering_actor": {"login": "securely1g"}}
    job = {"id": 789, "run_id": 456, "run_attempt": 1, "head_sha": row["head"],
           "name": row["job_name"], "status": "completed", "conclusion": "success",
           "started_at": "2026-11-10T10:00:00Z", "completed_at": "2026-11-10T10:03:00Z",
           "steps": [{"name": row["upload_step"], "status": "completed", "conclusion": "success"}]}
    return value, artifact, run, job


class OriginalArtifactStagingTest(unittest.TestCase):
    def test_exact_metadata_binding_is_not_content_acceptance(self):
        value, artifact, run, job = fixtures()
        calls = []
        responses = {"/actions/artifacts/123": artifact, "/actions/runs/456/attempts/1": run,
                     "/actions/jobs/789": job}
        def api(endpoint):
            calls.append(endpoint)
            return responses[endpoint]
        result = stage.admit(value, api)
        self.assertEqual(calls, list(responses))
        self.assertTrue(result["123"]["metadata_digest_matched"])
        self.assertFalse(result["123"]["raw_zip_digest_recomputed_here"])

    def test_wrong_digest_attempt_or_upload_link_stops(self):
        changes = (("artifact", "digest", "sha256:" + "c" * 64),
                   ("artifact", "created_at", "2026-11-10T11:00:00Z"),
                   ("run", "run_attempt", 2), ("job", "run_id", 457),
                   ("job", "name", "Different producer"))
        for object_name, key, new in changes:
            value, artifact, run, job = fixtures()
            {"artifact": artifact, "run": run, "job": job}[object_name][key] = new
            with self.subTest(object=object_name, key=key), self.assertRaises(ValueError):
                stage.check_producer(value["transfer"][0], artifact, run, job)
        value, artifact, run, job = fixtures()
        job["steps"][0]["conclusion"] = "failure"
        with self.assertRaises(ValueError):
            stage.check_producer(value["transfer"][0], artifact, run, job)

    def test_transfer_cannot_overwrite_source_or_escape_staging(self):
        for member, target in (("../runtime.gz", "target/docker-example.gz"),
                               ("runtime.gz", "../outside"), ("runtime.gz", "MODULE.bazel")):
            value, *_ = fixtures()
            value["transfer"][0]["members"] = {member: target}
            with self.subTest(member=member, target=target), self.assertRaises(ValueError):
                stage.transfers(value)
        value, *_ = fixtures()
        value["transfer"].append(copy.deepcopy(value["transfer"][0]))
        with self.assertRaises(ValueError):
            stage.transfers(value)


    def test_storage_counts_all_extracted_members_and_only_actual_destination_copies(self):
        value, *_ = fixtures()
        budget = stage.storage_budget(value)
        self.assertEqual(budget, {"artifact_expanded_bytes": 120, "destination_copy_bytes": 8,
                         "assembly_reserve_bytes": 90 * 1024**3,
                         "required_free_bytes": 90 * 1024**3 + 128})
        # An unselected member still occupies the extraction inbox.
        value["transfer"][0]["expanded_bytes"] = 500
        self.assertEqual(stage.storage_budget(value)["required_free_bytes"], 90 * 1024**3 + 508)

    def test_missing_false_or_understated_expanded_size_is_refused(self):
        for size in (None, False, 0, 7, stage.MAX_FILES_BYTES + 1):
            value, *_ = fixtures()
            if size is None:
                del value["transfer"][0]["expanded_bytes"]
            else:
                value["transfer"][0]["expanded_bytes"] = size
            with self.subTest(size=size), self.assertRaises(ValueError):
                stage.storage_budget(value)


    def test_insufficient_storage_stops_before_any_artifact_api_or_download(self):
        value, *_ = fixtures()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            argv = ["vs_reuse_stage.py", "admit", "--workspace", str(root),
                    "--inbox", str(inbox), "--manifest", str(root / "unused.json"),
                    "--manifest-sha256", "a" * 64, "--source-head", "b" * 40,
                    "--source-tree", "c" * 40, "--merge", "d" * 40,
                    "--output", str(root / "admission.json")]
            with (
                mock.patch.object(stage.sys, "argv", argv),
                mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}),
                mock.patch.object(stage.policy, "inventory", return_value=(root, value)),
                mock.patch.object(stage.shutil, "disk_usage", return_value=mock.Mock(free=1)),
                mock.patch.object(stage, "admit", side_effect=AssertionError("No API")) as admission,
                self.assertRaisesRegex(ValueError, "1 free bytes.*required"),
            ):
                stage.main()
            admission.assert_not_called()
            self.assertFalse((root / "admission.json").exists())

    def test_download_groups_preserve_single_and_multi_artifact_member_roots(self):
        value, *_ = fixtures()
        self.assertEqual(stage.download_groups(value),
                         [{"run_id": 456, "artifact_ids": [123], "directory": "original-images"}])
        for index, run in ((1, 456), (2, 457)):
            row = copy.deepcopy(value["transfer"][0])
            row.update(artifact_id=123 + index, name="original-" + str(index), run_id=run)
            target = "target/other-" + str(index)
            row["members"] = {"member": target}
            value["files"][target] = {"bytes": 3, "sha256": "a" * 64}
            value["transfer"].append(row)
        self.assertEqual(stage.download_groups(value), [
            {"run_id": 456, "artifact_ids": [123, 124], "directory": ""},
            {"run_id": 457, "artifact_ids": [125], "directory": "original-2"}])

    def test_plain_file_copy_preserves_original_and_refuses_replay(self):
        value, *_ = fixtures()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, inbox = base / "source", base / "downloads"
            root.mkdir()
            (inbox / "original-images").mkdir(parents=True)
            (root / "MODULE.bazel").write_bytes(b"source")
            source = inbox / "original-images/runtime.gz"
            source.write_bytes(b"original")  # Plain text, not a gzip or package fixture.
            result = stage.stage(root, inbox, value)
            self.assertEqual((root / "target/docker-example.gz").read_bytes(), b"original")
            self.assertEqual(source.read_bytes(), b"original")
            self.assertFalse(result["kernel_native_package_acceptance"])
            with self.assertRaises(ValueError):
                stage.stage(root, inbox, value)

    def test_corrupt_payload_stops_before_any_destination_is_created(self):
        value, *_ = fixtures()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, inbox = base / "source", base / "downloads"
            root.mkdir()
            (inbox / "original-images").mkdir(parents=True)
            (root / "MODULE.bazel").write_bytes(b"source")
            (inbox / "original-images/runtime.gz").write_bytes(b"different")
            with self.assertRaises(ValueError):
                stage.stage(root, inbox, value)
            self.assertFalse((root / "target").exists())


if __name__ == "__main__":
    unittest.main()
