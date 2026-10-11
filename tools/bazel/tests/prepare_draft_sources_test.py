"""Use source archives to check exact draft-module preparation."""

import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import prepare_draft_sources as prepare


def integrity(data):
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


class PrepareDraftSourcesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        contents = io.BytesIO()
        with tarfile.open(fileobj=contents, mode="w") as archive:
            for name, data in {"example/MODULE.bazel": b'module(name = "example")\n',
                               "example/source.txt": b"original\n"}.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        self.archive = contents.getvalue()
        first = b"--- a/source.txt\n+++ b/source.txt\n@@ -1 +1 @@\n-original\n+intermediate\n"
        second = b"--- a/source.txt\n+++ b/source.txt\n@@ -1 +1 @@\n-intermediate\n+final\n"
        overlay = b'filegroup(name = "source", srcs = ["source.txt"])\n'
        self.recipe = {
            "url": "https://example.invalid/source.tar", "integrity": integrity(self.archive),
            "strip_prefix": "example", "patch_strip": 1,
            "patches": {"first.patch": integrity(first), "second.patch": integrity(second)},
            "overlay": {"BUILD.bazel": integrity(overlay)},
        }
        self.recipe_bytes = json.dumps(self.recipe).encode()
        self.files = {"source.json": self.recipe_bytes, "source.tar": self.archive,
                      "first.patch": first, "second.patch": second, "BUILD.bazel": overlay}
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"schema": 1, "modules": {"example": {
            "version": "1.0.0.sonic.1", "registry_commit": "a" * 40,
            "source_json_integrity": integrity(self.recipe_bytes),
        }}}))
        self.fetch = patch.object(prepare, "download", self.download)
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def download(self, url, expected=None):
        data = self.files[url.rsplit("/", 1)[-1]]
        if expected:
            prepare.verify(data, expected)
        return data

    def attributes(self):
        registration = json.loads(self.manifest.read_text())["modules"]["example"]
        return prepare.recipe_attributes("example", registration)

    def test_native_bazel_recipe_preserves_patch_order_and_overlay_hashes(self):
        attributes, receipt = self.attributes()
        self.assertEqual([url.rsplit("/", 1)[-1] for url in attributes["remote_patches"]],
                         ["first.patch", "second.patch"])
        self.assertEqual(attributes["remote_patch_strip"], 1)
        self.assertEqual(attributes["remote_file_integrity"]["BUILD.bazel"],
                         integrity(self.files["BUILD.bazel"]))
        self.assertEqual(attributes["integrity"], integrity(self.archive))
        self.assertEqual(receipt["registry_commit"], "a" * 40)

    def test_changed_source_recipe_fails_integrity(self):
        self.files["source.json"] += b"unreviewed"
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.attributes()

    def test_overlay_path_cannot_escape_module(self):
        self.recipe["overlay"] = {"../outside": integrity(b"outside")}
        data = json.dumps(self.recipe).encode()
        self.files["source.json"] = data
        manifest = json.loads(self.manifest.read_text())
        manifest["modules"]["example"]["source_json_integrity"] = integrity(data)
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "invalid source path"):
            self.attributes()

    def test_registry_branch_name_is_rejected(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["modules"]["example"]["registry_commit"] = "main"
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "full commit"):
            self.attributes()

    def test_unregistered_archive_requires_immutable_source_revision(self):
        commit = "b" * 40
        registration = {"version": "0.0.2", "source_commit": commit, "archive": {
            "url": "https://github.com/securely1g/example/archive/" + commit + ".tar.gz",
            "integrity": integrity(self.archive), "strip_prefix": "example-" + commit,
        }}
        attributes, receipt = prepare.recipe_attributes("example", registration)
        self.assertEqual(receipt["source_commit"], commit)
        self.assertEqual(attributes["remote_patches"], {})
        registration["archive"]["url"] = "https://github.com/securely1g/example/archive/main.tar.gz"
        with self.assertRaisesRegex(ValueError, "pinned commit"):
            prepare.recipe_attributes("example", registration)

    def test_generated_rc_updates_but_never_overwrites_user_settings(self):
        path = self.root / ".bazelrc.user"
        result = {"manifest_sha256": "a" * 64,
                  "overrides": ["--override_module=example=/path with spaces/source"]}
        prepare.write_bazelrc(path, result)
        import shlex
        line = path.read_text().splitlines()[-1]
        self.assertEqual(shlex.split(line), ["common", result["overrides"][0]])
        prepare.write_bazelrc(path, {**result, "overrides": []})
        self.assertNotIn("override_module", path.read_text())
        path.write_text("build --jobs=2\n")
        with self.assertRaisesRegex(ValueError, "manually maintained"):
            prepare.write_bazelrc(path, result)
        self.assertEqual(path.read_text(), "build --jobs=2\n")

    def test_preparation_workspace_pins_the_consumers_bazel_version(self):
        self.manifest.write_text(json.dumps({"schema": 1, "modules": {}}))

        def fetch(command, *, cwd, check):
            self.assertEqual((cwd / ".bazelversion").read_bytes(),
                             (Path(__file__).resolve().parents[3] / ".bazelversion").read_bytes())

        with patch.object(prepare.subprocess, "run", side_effect=fetch), \
                patch.object(prepare.subprocess, "check_output",
                             side_effect=[str(self.root / "output-base"), "{}"]):
            result = prepare.prepare(self.manifest, self.root / "prepared")
        self.assertEqual(result["overrides"], [])


if __name__ == "__main__":
    unittest.main()
