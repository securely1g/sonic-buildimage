#!/usr/bin/env python3
"""Exercise verified source handoffs without network or a Bazel build."""

import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/bazel/gzip"))
import source_archive as source


class SourceArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.archives = self.root / "source downloads"
        self.output = self.root / "published"
        self.graph = self.root / "module-graph.json"
        self.payloads = {
            "first-1.0": b"first GitLab source",
            "second-2.0": b"different GitLab source with the same filename",
            "pigz-2.8.bcr.2": b"fixture pigz tar.gz",
            "bison-3.3.2": b"fixture extension tar.xz",
        }
        self.sources = {}
        records = [
            ("first-1.0", "first", "1.0", "@@first+", "src.tar.gz", "sha512"),
            ("second-2.0", "second", "2.0", "@@second+", "src.tar.gz", "sha512"),
            ("pigz-2.8.bcr.2", "pigz", "2.8.bcr.2", "@@pigz+", "pigz-2.8.tar.gz", "sha256"),
            ("bison-3.3.2", "rules_bison", "0.4",
             "@@rules_bison++default_toolchain_ext+bison_v3.3.2", "bison-3.3.2.tar.xz", "sha256"),
        ]
        for name, module, version, repository, filename, algorithm in records:
            data = self.payloads[name]
            digest = hashlib.new(algorithm, data)
            self.sources[name] = {
                "module": module, "version": version, "repository": repository,
                "filename": filename,
                "integrity": algorithm + "-" + base64.b64encode(digest.digest()).decode(),
            }
            for path in (self.archives / name / filename,
                         self.cache / "content_addressable" / algorithm / digest.hexdigest() / "file"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        self.graph_data = {
            "name": "fixture", "version": "", "dependencies": [
                {"name": item["module"], "version": item["version"], "dependencies": []}
                for item in self.sources.values()
            ],
        }
        self.graph.write_text(json.dumps(self.graph_data))
        patcher = patch.object(source, "SOURCES", self.sources)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *arguments):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            source.main([str(argument) for argument in arguments])
        return stdout.getvalue()

    def path(self, name):
        return self.archives / name / self.sources[name]["filename"]

    def test_cache_export_and_artifact_import_keep_names_pins_and_bytes(self):
        receipt = json.loads(self.main(
            "--repository-cache", self.cache, "--module-graph", self.graph, "--output", self.output))
        destination = self.root / "imported"
        imported = json.loads(self.main("--archives-dir", self.output, "--output", destination))
        self.assertEqual(len(receipt), len(self.sources))
        self.assertEqual(len(imported), len(receipt))
        for record, name in zip(receipt, self.sources):
            metadata = self.sources[name]
            self.assertEqual(record["module"], metadata["module"])
            self.assertEqual(record["version"], metadata["version"])
            self.assertEqual(record["repository"], metadata["repository"])
            self.assertEqual(record["integrity"], metadata["integrity"])
            self.assertEqual(record["archive"], str(self.output / name / metadata["filename"]))
            self.assertEqual(record["bytes"], len(self.payloads[name]))
            path = destination / name / metadata["filename"]
            self.assertEqual(path.read_bytes(), self.payloads[name])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_last_archive_corruption_prevents_any_publication(self):
        sentinel = self.output / "first-1.0/src.tar.gz"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"previous verified handoff")
        self.path("bison-3.3.2").write_bytes(b"altered source")
        with self.assertRaisesRegex(ValueError, "pinned SHA256"):
            self.main("--archives-dir", self.archives, "--output", self.output)
        self.assertEqual(sentinel.read_bytes(), b"previous verified handoff")
        self.assertEqual(list(self.output.rglob("*")), [sentinel.parent, sentinel])

    def test_corrupt_sha512_cache_is_rejected_before_output_exists(self):
        metadata = self.sources["second-2.0"]
        algorithm, digest = source.archive_digest(metadata)
        (self.cache / "content_addressable" / algorithm / digest / "file").write_bytes(b"altered")
        with self.assertRaisesRegex(ValueError, "pinned SHA512"):
            self.main("--repository-cache", self.cache, "--module-graph", self.graph, "--output", self.output)
        self.assertFalse(self.output.exists())

    def test_missing_linked_and_nonregular_inputs_are_rejected(self):
        archive = self.path("bison-3.3.2")
        original = archive.read_bytes()
        archive.unlink()
        with self.assertRaisesRegex(ValueError, "missing source archive"):
            source.distdir_args(self.archives)
        outside = self.root / "outside.tar.xz"
        outside.write_bytes(original)
        archive.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "ordinary file"):
            source.distdir_args(self.archives)
        archive.unlink()
        archive.mkdir()
        with self.assertRaisesRegex(ValueError, "ordinary file"):
            source.distdir_args(self.archives)
        archive.rmdir()
        os.mkfifo(archive)
        with self.assertRaisesRegex(ValueError, "ordinary file"):
            source.distdir_args(self.archives)
        self.assertFalse(self.output.exists())

    def test_linked_archive_directory_and_root_are_rejected(self):
        directory = self.path("bison-3.3.2").parent
        moved = self.root / "moved"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ordinary directory"):
            source.distdir_args(self.archives)
        directory.unlink()
        moved.rename(directory)
        alias = self.root / "linked-root"
        alias.symlink_to(self.archives, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ordinary directory"):
            source.distdir_args(alias)

    def test_linked_cache_blob_is_rejected(self):
        algorithm, digest = source.archive_digest(self.sources["pigz-2.8.bcr.2"])
        blob = self.cache / "content_addressable" / algorithm / digest / "file"
        blob.unlink()
        blob.symlink_to(self.path("pigz-2.8.bcr.2"))
        with self.assertRaisesRegex(ValueError, "ordinary file"):
            self.main("--repository-cache", self.cache, "--module-graph", self.graph, "--output", self.output)
        self.assertFalse(self.output.exists())

    def test_selected_extension_owner_drift_is_rejected_before_export(self):
        self.graph_data["dependencies"][-1]["version"] = "0.5"
        self.graph.write_text(json.dumps(self.graph_data))
        with self.assertRaisesRegex(ValueError, "rules_bison selection changed"):
            self.main("--repository-cache", self.cache, "--module-graph", self.graph, "--output", self.output)
        self.assertFalse(self.output.exists())

    def test_missing_and_ambiguous_selected_modules_are_rejected(self):
        graph = self.graph_data
        missing = graph["dependencies"].pop()
        self.graph.write_text(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "rules_bison selection changed"):
            source.check_versions(self.graph)
        graph["dependencies"] += [missing, {**missing, "version": "0.5"}]
        self.graph.write_text(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "rules_bison selection changed"):
            source.check_versions(self.graph)

    def test_fetch_cli_lists_only_the_manifest_repository_selectors(self):
        self.assertEqual(self.main("--fetch-arguments").splitlines(), [
            "--repo=@@first+", "--repo=@@second+", "--repo=@@pigz+",
            "--repo=@@rules_bison++default_toolchain_ext+bison_v3.3.2",
        ])

    def test_distdir_cli_verifies_before_emitting_mapped_arguments(self):
        actual = self.main("--distdir-arguments", self.archives,
                           "--distdir-prefix", "/bazel_cache/distdir").splitlines()
        self.assertEqual(actual, ["--distdir=/bazel_cache/distdir/" + name for name in self.sources])
        self.assertEqual(source.distdir_args(self.archives), [
            "--distdir=" + str(self.archives / name) for name in self.sources])
        self.path("bison-3.3.2").write_bytes(b"wrong final archive")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(ValueError):
            source.main(["--distdir-arguments", str(self.archives)])
        self.assertEqual(stdout.getvalue(), "")

    def test_cli_requires_graph_for_cache_and_output_for_transfer(self):
        for args in (["--repository-cache", str(self.cache), "--output", str(self.output)],
                     ["--archives-dir", str(self.archives)],
                     ["--fetch-arguments", "--distdir-prefix", "/other"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                source.main(args)
        self.assertFalse(self.output.exists())

    def test_invalid_hash_algorithms_and_paths_fail_closed(self):
        self.sources["bison-3.3.2"]["integrity"] = "md5-" + base64.b64encode(b"0" * 16).decode()
        with self.assertRaisesRegex(ValueError, "unsupported"):
            source.distdir_args(self.archives)
        self.sources["bison-3.3.2"]["integrity"] = "sha256-" + base64.b64encode(b"0" * 16).decode()
        with self.assertRaisesRegex(ValueError, "length"):
            source.distdir_args(self.archives)
        with self.assertRaisesRegex(ValueError, "path"):
            source.archive_path("../outside", self.sources["first-1.0"])
        with self.assertRaisesRegex(ValueError, "path"):
            source.archive_path("safe", {"filename": "../outside.tar.gz"})


if __name__ == "__main__":
    unittest.main()
