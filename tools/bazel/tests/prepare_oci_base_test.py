#!/usr/bin/env python3
"""Check publication of OCI directories from synthetic Docker-save archives.

The cases protect source bytes, stable timestamps, retained generations and
concurrent publication, using local files and helper processes only."""

import concurrent.futures
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.oci import prepare_oci_base


def native_archive(path, content=b"base contents", platform="linux/amd64", extra=()):
    """Minimal dual-format Docker-save archive with byte-addressed OCI entries."""
    files = {}

    def blob(data, media_type):
        digest = hashlib.sha256(data).hexdigest()
        files["blobs/sha256/" + digest] = data
        return {"mediaType": media_type, "size": len(data), "digest": "sha256:" + digest}

    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        member = tarfile.TarInfo("etc/base")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    layer_desc = blob(layer.getvalue(), "application/vnd.oci.image.layer.v1.tar")
    operating_system, arch = platform.split("/")
    config = {"os": operating_system, "architecture": arch,
              "rootfs": {"type": "layers", "diff_ids": [layer_desc["digest"]]},
              "config": {"Env": ["BASE=preserved"], "Cmd": ["/bin/base"]}}
    config_desc = blob(json.dumps(config).encode(), "application/vnd.oci.image.config.v1+json")
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config_desc, "layers": [layer_desc]}
    manifest_desc = blob(json.dumps(manifest).encode(), manifest["mediaType"])
    manifest_desc["annotations"] = {"org.opencontainers.image.ref.name": "latest"}
    files["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [manifest_desc]}).encode()
    files["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    files["manifest.json"] = json.dumps([{
        "Config": "blobs/" + config_desc["digest"].replace(":", "/"),
        "Layers": ["blobs/" + layer_desc["digest"].replace(":", "/")],
        "RepoTags": ["docker-config-engine-trixie:latest"],
    }]).encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        for member in extra:
            archive.addfile(member)
    return {name: data for name, data in files.items() if name != "manifest.json"}


class PrepareOciBaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "docker-config-engine-trixie.gz"
        self.output = self.root / "docker-config-engine-trixie.oci"
        self.files = native_archive(self.archive)

    def prepare(self):
        prepare_oci_base.prepare(self.archive, self.output)

    def snapshot(self):
        return {path.relative_to(self.output).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.output.rglob("*") if path.is_file()}

    def test_cache_hit_archive_produces_byte_identical_oci_without_changing_archive(self):
        """Extract only native OCI entries from a saved archive while preserving the source file."""
        saved = self.archive.read_bytes(), self.archive.stat().st_mtime_ns
        self.prepare()
        self.assertTrue(self.output.is_symlink())
        self.assertEqual({name: value[0] for name, value in self.snapshot().items()}, self.files)
        self.assertEqual((self.archive.read_bytes(), self.archive.stat().st_mtime_ns), saved)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_repeated_requests_and_gzip_metadata_changes_preserve_output_mtimes(self):
        """Avoid invalidating downstream builds when only requests or gzip metadata change."""
        self.prepare()
        before = self.snapshot(), self.output.lstat().st_mtime_ns, os.readlink(self.output)
        self.prepare()
        self.assertEqual((self.snapshot(), self.output.lstat().st_mtime_ns, os.readlink(self.output)), before)
        self.archive.write_bytes(gzip.compress(gzip.decompress(self.archive.read_bytes()), mtime=123))
        self.prepare()
        self.assertEqual((self.snapshot(), self.output.lstat().st_mtime_ns, os.readlink(self.output)), before)

    def test_changed_archive_publishes_new_generation_and_preserves_previous_reader(self):
        """Publish changed content separately while keeping the prior generation intact for readers."""
        self.prepare()
        previous = self.output.resolve()
        previous_files = self.snapshot()
        changed = native_archive(self.archive, b"changed base contents")
        self.prepare()
        self.assertNotEqual(self.output.resolve(), previous)
        self.assertEqual({name: value[0] for name, value in self.snapshot().items()}, changed)
        self.assertEqual({p.relative_to(previous).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in previous.rglob("*") if p.is_file()}, previous_files)

    def test_missing_output_and_damaged_blob_are_repaired(self):
        """Recover a missing publication link and replace a generation with damaged blob bytes."""
        self.prepare()
        self.output.unlink()
        self.prepare()
        blob = next((self.output / "blobs/sha256").iterdir())
        blob.write_bytes(b"damaged")
        damaged_generation = self.output.resolve()
        self.prepare()
        self.assertNotEqual(self.output.resolve(), damaged_generation)
        self.assertEqual({name: value[0] for name, value in self.snapshot().items()}, self.files)

    def test_invalid_new_archive_preserves_previous_output(self):
        """Reject a replacement with the wrong platform without disturbing the usable base."""
        self.prepare()
        previous = self.snapshot(), os.readlink(self.output)
        native_archive(self.archive, platform="linux/arm64")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual((self.snapshot(), os.readlink(self.output)), previous)

    def test_malformed_metadata_in_existing_generation_is_repaired(self):
        """Rebuild damaged metadata in a fresh generation rather than reusing the corrupted one."""
        for name, contents in (("oci-layout", b"[]"), ("index.json", b"null")):
            with self.subTest(name=name):
                self.prepare()
                (self.output / name).write_bytes(contents)
                damaged_generation = self.output.resolve()
                self.prepare()
                self.assertNotEqual(self.output.resolve(), damaged_generation)
                self.assertEqual({key: value[0] for key, value in self.snapshot().items()}, self.files)

    def test_docker_only_archive_requires_rebuild_and_is_never_converted(self):
        """Require native OCI entries and identify the archive to rebuild when they are absent."""
        self.archive = self.root / "another-base.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo("manifest.json")
            member.size = 2
            archive.addfile(member, io.BytesIO(b"[]"))
        with self.assertRaises(ValueError) as error:
            self.prepare()
        self.assertIn("rebuild " + self.archive.name, str(error.exception))
        self.assertFalse(self.output.exists())

    def test_unsafe_links_traversal_and_duplicate_entries_are_rejected(self):
        """Reject unsafe or ambiguous tar entries without writing outside the publication area."""
        for name, kind in (("../outside", tarfile.REGTYPE),
                           ("/outside", tarfile.REGTYPE),
                           ("blobs/sha256/link", tarfile.SYMTYPE),
                           ("index.json", tarfile.REGTYPE)):
            with self.subTest(name=name, kind=kind):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "../outside"
                native_archive(self.archive, extra=[member])
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse((self.root / "outside").exists())

    def test_parallel_normal_and_debug_requests_share_complete_generation(self):
        """Have two helper processes publish one complete generation for simultaneous consumers."""
        command = [sys.executable, str(Path(prepare_oci_base.__file__)),
                   "--archive", str(self.archive), "--output", str(self.output)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: subprocess.run(command, capture_output=True, text=True), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({name: value[0] for name, value in self.snapshot().items()}, self.files)
        generations = [p for p in (self.root / ("." + self.output.name + ".layouts")).iterdir() if p.is_dir()]
        self.assertEqual(len(generations), 1)


if __name__ == "__main__":
    unittest.main()
