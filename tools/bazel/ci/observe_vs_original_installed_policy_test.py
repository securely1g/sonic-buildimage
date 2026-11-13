#!/usr/bin/env python3
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import observe_vs_original_installed_policy as subject
from tools.bazel.ci import recover_vs_original_inputs as original
from tools.bazel.ci import vs_archive_reuse


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tar_bytes(rows):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data in rows:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class ArchiveInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.manifest = {"package": {"name": "fixture"}}
        layer = tar_bytes([("usr/bin/proof", b"non-block-aligned")])
        stored = gzip.compress(layer, mtime=0)
        self.layer_name = "blobs/sha256/" + sha(stored) + ".tar.gz"
        config = {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha(layer)]},
                  "config": {"Entrypoint": ["/usr/bin/docker-init.sh"], "Cmd": None,
                             "Env": ["DEBIAN_FRONTEND=noninteractive"],
                             "Labels": {vs_archive_reuse.LABEL: json.dumps(self.manifest)}}}
        raw_config = json.dumps(config).encode()
        name = "blobs/sha256/" + sha(raw_config)
        docker_manifest = [{"RepoTags": ["fixture:latest"], "Config": name, "Layers": [self.layer_name]}]
        archive = gzip.compress(tar_bytes([(name, raw_config), (self.layer_name, stored),
                                          ("manifest.json", json.dumps(docker_manifest).encode())]), mtime=0)
        (self.root / "fixture.gz").write_bytes(archive)
        self.row = {"name": "fixture", "tag": "fixture:latest", "entrypoint": ["/usr/bin/docker-init.sh"],
                    "empty_parent": False, "archive": {"name": "fixture.gz", "sha256": sha(archive), "bytes": len(archive)}}
        self.proof = {"config_digest": "sha256:" + sha(raw_config), "rootfs": config["rootfs"]}
        self.checks = mock.Mock()
        def inventory(view):
            self.assertEqual(set(vars(view)), {"layers"})
            self.assertEqual([p.read_bytes() for p in view.layers], [stored])
            return {"usr/bin/proof": {"kind": "file"}}, {}
        self.checks.image_inventory.side_effect = inventory
        self.owners = (original, vs_archive_reuse, self.checks)

    def inspect(self):
        return subject.inspect_archive(self.row, self.root, self.scratch, self.manifest, self.proof, self.owners)

    def test_actual_original_archive_and_layer_bytes_are_preserved(self):
        result = self.inspect()
        self.assertEqual(result["snapshot"]["sha256"], self.row["archive"]["sha256"])
        self.assertEqual(result["snapshot"]["config_digest"], self.proof["config_digest"])
        self.assertEqual(result["snapshot"]["rootfs"], self.proof["rootfs"]["diff_ids"])
        self.checks.image_inventory.assert_called_once()

    def test_wrong_whole_archive_hash_fails_before_decode(self):
        self.row["archive"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Original archive bytes differ"):
            self.inspect()
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.checks.image_inventory.assert_not_called()

    def test_config_must_match_original_successful_validation(self):
        self.proof["config_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "differs from original successful validation"):
            self.inspect()
        self.checks.image_inventory.assert_not_called()

    def test_archive_symlink_is_rejected(self):
        path = self.root / "fixture.gz"
        path.rename(self.root / "actual.gz")
        path.symlink_to("actual.gz")
        with self.assertRaises(OSError):
            self.inspect()
        self.checks.image_inventory.assert_not_called()


class PolicyCompositionTests(unittest.TestCase):
    def setUp(self):
        item = {"kind": "file", "sha256": "a" * 64, "mode": 0o755, "uid": 0, "gid": 0}
        base = {"snapshot": {"sha256": "b" * 64, "config_digest": "sha256:" + "c" * 64,
                             "layers": [{"sha256": "d" * 64, "bytes": 9}], "rootfs": ["sha256:" + "e" * 64],
                             "tag": "parent:latest"}, "settings": {"Env": ["PATH=/usr/bin"]}, "files": {"usr/bin/proof": item}}
        child = copy.deepcopy(base)
        child["snapshot"]["tag"] = "child:latest"
        child["snapshot"]["layers"].append({"sha256": "f" * 64, "bytes": 7})
        child["snapshot"]["rootfs"].append("sha256:" + "1" * 64)
        self.observed = {"parent": base, "child": child}
        self.declarations = [{"architecture": "amd64", "images": [
            {"name": "parent", "tag": "parent:latest", "files": {"usr/bin/proof": {"mode": 0o755}}},
            {"name": "child", "tag": "child:latest", "parent": "parent", "inherit_files": ["usr/bin/proof"]}]}]
        self.checks = mock.Mock()
        def verify(files, path, expected, description="source file differs"):
            subject.require(all(files[path].get(k) == v for k, v in expected.items()), description)
            return files[path]
        self.checks.verify_file.side_effect = verify

    def policy(self):
        return subject.policy_from_observations(self.declarations, self.observed, self.checks)

    def test_policy_contains_only_actual_original_config_rootfs_and_files(self):
        result = self.policy()
        row = result["images"][1]
        self.assertEqual(row["expected_config"], self.observed["child"]["settings"])
        self.assertEqual(row["expected_rootfs"]["diff_ids"], self.observed["child"]["snapshot"]["rootfs"])
        self.assertEqual(row["files"], self.observed["parent"]["files"])
        self.assertNotIn("manifest", row)
        self.assertNotIn("layers", row)

    def test_stored_layer_prefix_cannot_change_even_with_equal_diff_ids(self):
        self.observed["child"]["snapshot"]["layers"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not extend its declared parent"):
            self.policy()

    def test_diff_ids_cannot_change_even_with_equal_stored_layer_prefix(self):
        self.observed["child"]["snapshot"]["rootfs"][0] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "does not extend its declared parent"):
            self.policy()

    def test_inherited_file_metadata_is_checked_by_existing_owner(self):
        self.observed["child"]["files"]["usr/bin/proof"]["mode"] = 0o644
        with self.assertRaisesRegex(ValueError, "inherited file"):
            self.policy()


class OriginalProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        producer = {"run_id": 1, "job_id": 2, "artifact_id": 3, "head_sha": "a" * 40,
                    "tested_revision": "b" * 40, "repository": "owner/repo", "artifact_digest": "sha256:" + "c" * 64,
                    "build_step": "build", "validation_step": "validate", "upload_step": "upload"}
        self.metadata = {"run": {"id": 1, "head_sha": producer["head_sha"], "status": "completed", "repository": {"full_name": "owner/repo"}},
                         "job": {"id": 2, "run_id": 1, "status": "completed", "conclusion": "success", "steps": [
                             {"name": n, "number": i + 1, "status": "completed", "conclusion": "success"}
                             for i, n in enumerate(("build", "validate", "upload"))]},
                         "artifact": {"id": 3, "workflow_run": {"id": 1, "head_sha": producer["head_sha"]}, "digest": producer["artifact_digest"]}}
        self.receipt = {"status": "built", "revision": producer["tested_revision"], "artifacts": {"fixture.gz": {"bytes": 11, "sha256": "d" * 64}}}
        self.validation = {"status": "passed", "config_digest": "sha256:" + "e" * 64,
                           "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "f" * 64]}, "archive": {"sha256": "d" * 64}}
        self.row = {"name": "fixture", "archive": {"name": "fixture.gz", "bytes": 11, "sha256": "d" * 64},
                    "evidence": {"producer": producer, "kind": "image-validation"}}

    def proof(self):
        for name, value in (("metadata", self.metadata), ("receipt", self.receipt), ("validation", self.validation)):
            data = json.dumps(value).encode()
            (self.root / (name + ".json")).write_bytes(data)
            self.row["evidence"][name] = {"path": name + ".json", "bytes": len(data), "sha256": sha(data)}
        return subject.original_proof(self.row, self.root, original)

    def test_exact_original_source_archive_and_successful_validation_are_required(self):
        result = self.proof()
        self.assertEqual(result["config_digest"], self.validation["config_digest"])

    def test_changed_original_tested_source_is_rejected(self):
        self.receipt["revision"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "build receipt differs"):
            self.proof()

    def test_successful_validation_after_upload_is_rejected(self):
        self.metadata["job"]["steps"][1]["number"] = 4
        with self.assertRaisesRegex(ValueError, "did not precede upload"):
            self.proof()

    def test_failed_original_validation_is_rejected(self):
        self.validation["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "did not pass"):
            self.proof()


class FixedScopeTests(unittest.TestCase):
    def declarations(self):
        return [{"architecture": "amd64", "images": [
            {"name": "docker-config-engine-trixie"},
            {"name": "docker-orchagent", "parent": "docker-config-engine-trixie"},
            {"name": "docker-orchagent-dbg", "parent": "docker-orchagent"}]}]

    def test_three_image_scope_rejects_known_parent_mismatch_before_any_archive_read(self):
        names = ("docker-config-engine-trixie", "docker-orchagent", "docker-orchagent-dbg")
        self.assertEqual(subject.SCOPES["swss"], names)
        plan = {"status": "FROZEN_REVIEWED_ORIGINAL_ARCHIVES", "scope": "swss",
                "images": [{"name": n} for n in names]}
        def proof(row, *unused):
            return {"rootfs": {"diff_ids": ["parent" if row["name"] == names[0] else "other"]}}
        with mock.patch.object(subject, "original_proof", side_effect=proof), mock.patch.object(subject, "inspect_archive") as inspect:
            with self.assertRaisesRegex(ValueError, "incompatible parent"):
                subject.observe(plan, self.declarations(), Path("/unused"), Path("/unused"), Path("/unused"), (None, None, None))
            inspect.assert_not_called()

    def test_extra_base_image_cannot_change_the_existing_emitted_policy_scope(self):
        names = ("docker-base-trixie", *subject.SCOPES["swss"])
        plan = {"status": "FROZEN_REVIEWED_ORIGINAL_ARCHIVES", "scope": "swss",
                "images": [{"name": n} for n in names]}
        with mock.patch.object(subject, "original_proof") as proof:
            with self.assertRaisesRegex(ValueError, "fixed owner declarations"):
                subject.observe(plan, self.declarations(), Path("/unused"), Path("/unused"), Path("/unused"), (None, None, None))
            proof.assert_not_called()


if __name__ == "__main__":
    unittest.main()
