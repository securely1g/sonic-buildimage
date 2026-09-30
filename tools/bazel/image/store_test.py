#!/usr/bin/env python3
"""Archive-contract tests; live Docker restore is a separate integration check."""
import base64
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("store", Path(__file__).with_name("store.py"))
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def native(self, name, tag="service:latest", diffs=None, salt="1"):
        root = self.root / name
        diffs = diffs or ["sha256:" + "1" * 64, "sha256:" + "2" * 64]
        chains = store.chain_ids(diffs)
        configs = root / "image/overlay2/imagedb/content/sha256"
        configs.mkdir(parents=True)
        (root / "overlay2/l").mkdir(parents=True)
        oldlinks = []
        for index, (chain, diff) in enumerate(zip(chains, diffs)):
            cache = hashlib.sha256((salt + str(index)).encode()).hexdigest()
            link = (salt.upper() * 25 + str(index))[:26]
            layer = root / "image/overlay2/layerdb/sha256" / chain[7:]
            layer.mkdir(parents=True)
            for field, value in (("diff", diff), ("cache-id", cache), ("size", "4")):
                (layer / field).write_text(value)
            if index:
                (layer / "parent").write_text(chains[index - 1])
            (layer / "tar-split.json.gz").write_bytes(gzip.compress(b'{"payload":"unchanged"}\n', mtime=1790000000))
            tree = root / "overlay2" / cache
            (tree / "diff").mkdir(parents=True)
            (tree / "link").write_text(link)
            (root / "overlay2/l" / link).symlink_to("../" + cache + "/diff")
            if oldlinks:
                (tree / "lower").write_text(":".join("l/" + value for value in reversed(oldlinks)))
            oldlinks.append(link)
            binary = tree / "diff/payload"
            binary.write_bytes(b"data")
            binary.chmod(0o751)
            os.utime(binary, (123456, 123456))
            os.link(binary, tree / "diff/hardlink")
            (tree / "diff/symlink").symlink_to("payload")
            os.utime(tree / "diff/symlink", (123456, 123456), follow_symlinks=False)
        config = store.canonical({"architecture": "amd64", "os": "linux", "config": {},
                                  "rootfs": {"type": "layers", "diff_ids": diffs}})
        image = "sha256:" + hashlib.sha256(config).hexdigest()
        (configs / image[7:]).write_bytes(config)
        (root / "image/overlay2/repositories.json").write_bytes(store.canonical(
            {"Repositories": {tag.split(":")[0]: {tag: image}}}))
        return root, chains, image

    def test_real_tar_reader_sees_all_members_and_hardlinks(self):
        native, chains, image = self.native("native")
        part = self.root / "part"
        store.collect(native, part, pigz=shutil.which("pigz"), jobs=2)
        output = self.root / "dockerfs.tar.gz"
        receipt = store.merge([part], output, pigz=shutil.which("pigz"))
        self.assertEqual(receipt["layers"], 2)
        with tarfile.open(output) as archive:
            for chain in chains:
                prefix = "overlay2/" + chain[7:]
                self.assertEqual(archive.extractfile(prefix + "/diff/payload").read(), b"data")
                self.assertEqual(archive.getmember(prefix + "/diff/payload").mode, 0o751)
                # Lexicographic traversal writes hardlink first; payload points to it.
                self.assertTrue(archive.getmember(prefix + "/diff/payload").islnk())
                self.assertEqual(archive.getmember(prefix + "/diff/symlink").linkname, "payload")
            refs = json.load(archive.extractfile("image/overlay2/repositories.json"))
            self.assertEqual(refs["Repositories"]["service"]["service:latest"], image)
            top = "overlay2/" + chains[-1][7:]
            self.assertEqual(archive.extractfile(top + "/lower").read(), ("l/" + store.link_id(chains[0])).encode())
        if shutil.which("tar"):
            listed = subprocess.check_output(["tar", "tzf", str(output)], text=True).splitlines()
            self.assertIn(top + "/diff/payload", listed)
        if shutil.which("busybox"):
            listed = subprocess.check_output(["busybox", "tar", "tzf", str(output)], text=True).splitlines()
            self.assertIn(top + "/diff/payload", listed)

    def test_native_random_ids_do_not_affect_fragments(self):
        a, chains, _ = self.native("a", salt="1")
        b, _, _ = self.native("b", salt="2")
        first = store.collect(a, self.root / "first")
        second = store.collect(b, self.root / "second")
        self.assertEqual(first, second)
        for chain in chains:
            filename = "layers/" + chain[7:] + ".tar.gz"
            self.assertEqual((self.root / "first" / filename).read_bytes(),
                             (self.root / "second" / filename).read_bytes())

    def test_merge_declared_tree_artifact_through_sandbox_symlinks(self):
        native, _, _ = self.native("native")
        part, sandbox = self.root / "part", self.root / "sandbox"
        store.collect(native, part)
        sandbox.mkdir()
        (sandbox / "layers").mkdir()
        for source in part.rglob("*"):
            if source.is_file():
                (sandbox / source.relative_to(part)).symlink_to(source)
        direct, linked = self.root / "direct.tar.gz", self.root / "linked.tar.gz"
        self.assertEqual(store.merge([part], direct), store.merge([sandbox], linked))
        self.assertEqual(direct.read_bytes(), linked.read_bytes())
        # Accepting declared input links must not relax native-store validation.
        with self.assertRaisesRegex(ValueError, "expected regular file"):
            store.regular(sandbox / "manifest.json")
        fragment = next((sandbox / "layers").iterdir())
        fragment.unlink()
        fragment.symlink_to(self.root / "missing-fragment")
        with self.assertRaisesRegex(ValueError, "missing regular fragment"):
            store.merge([sandbox], self.root / "broken.tar.gz")

    def test_shared_chain_reused_and_changed_top_layer_selected(self):
        a, first_chains, _ = self.native("a", tag="other:latest")
        b, second_chains, _ = self.native("b", tag="swss:latest", diffs=["sha256:" + "1" * 64, "sha256:" + "3" * 64])
        store.collect(a, self.root / "first")
        store.collect(b, self.root / "second", level=1)
        shared_fragment = "layers/" + first_chains[0][7:] + ".tar.gz"
        self.assertNotEqual((self.root / "first" / shared_fragment).read_bytes(),
                            (self.root / "second" / shared_fragment).read_bytes())
        output = self.root / "merged.tar.gz"
        receipt = store.merge([self.root / "first", self.root / "second"], output)
        self.assertEqual((receipt["images"], receipt["layers"]), (2, 3))
        original = (self.root / "first/layers" / (first_chains[0][7:] + ".tar.gz")).read_bytes()
        self.assertEqual(output.read_bytes().count(original), 1)
        with tarfile.open(output) as archive:
            all_names = archive.getnames()
            self.assertEqual(len(all_names), len(set(all_names)))
            self.assertIn("overlay2/" + second_chains[-1][7:] + "/diff/payload", all_names)

    def test_reject_native_wrong_lower_order(self):
        native, _, _ = self.native("native")
        for folder in (native / "overlay2").iterdir():
            if (folder / "lower").exists():
                (folder / "lower").write_text("l/" + "X" * 26)
        with self.assertRaisesRegex(ValueError, "lower-order mismatch"):
            store.collect(native, self.root / "part")

    def test_reject_conflicting_tags_and_corrupt_fragments(self):
        a, _, _ = self.native("a")
        b, _, _ = self.native("b", diffs=["sha256:" + "3" * 64])
        first = self.root / "first"
        second = self.root / "second"
        store.collect(a, first)
        store.collect(b, second)
        with self.assertRaisesRegex(ValueError, "conflicting reference"):
            store.merge([first, second], self.root / "conflict.tar.gz")
        fragment = next((first / "layers").iterdir())
        with fragment.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "fragment digest mismatch"):
            store.merge([first], self.root / "tamper.tar.gz")

    def test_reject_native_diff_id_disagreement(self):
        native, chains, _ = self.native("native")
        (native / "image/overlay2/layerdb/sha256" / chains[-1][7:] / "diff").write_text("sha256:" + "9" * 64)
        with self.assertRaisesRegex(ValueError, "native layer DiffID mismatch"):
            store.collect(native, self.root / "part")

    def test_reject_inconsistent_part_parent(self):
        native, chains, _ = self.native("native")
        part = self.root / "part"
        store.collect(native, part)
        manifest = json.loads((part / "manifest.json").read_text())
        manifest["layers"][chains[-1]]["parent"] = ""
        (part / "manifest.json").write_bytes(store.canonical(manifest))
        with self.assertRaisesRegex(ValueError, "parent/ancestor mismatch"):
            store.merge([part], self.root / "broken.tar.gz")

    def test_reject_metadata_only_staging_image(self):
        with self.assertRaisesRegex(ValueError, "host-metadata-only"):
            store.validate_image({"config": {"Labels": {"org.sonic.bazel.host-metadata-only": "true"}}})

    def test_opaque_lowering_hides_nested_entries_without_following_symlinks(self):
        upper, lower = self.root / "upper", self.root / "lower"
        upper.mkdir()
        lower.mkdir()
        for name in ("gone", "gone-dir/nested", "same/old", "same/deep/old", "replace-dir/old"):
            path = lower / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old")
        (lower / "replace-link").symlink_to("/outside")
        (upper / "same/deep").mkdir(parents=True)
        (upper / "same/new").write_text("new")
        (upper / "replace-dir").symlink_to("/outside")
        (upper / "replace-link").mkdir()
        generated = set()
        store.opaque_whiteouts(upper, lower, Path("opaque"), generated)
        self.assertEqual(generated, {"opaque/gone", "opaque/gone-dir", "opaque/same/old", "opaque/same/deep/old"})

    def test_parent_lookup_does_not_follow_symlink_ancestors(self):
        (self.root / "parent").mkdir()
        (self.root / "outside/child").mkdir(parents=True)
        (self.root / "parent/link").symlink_to(self.root / "outside")
        self.assertIsNone(store.parent_directory(self.root / "parent", Path("link/child")))

    def test_binary_xattrs_preserved_in_pax_headers(self):
        native, chains, _ = self.native("native")
        source = next((native / "overlay2").glob("*/diff/payload"))
        expected = b"\x00\xff\x01capability"
        try:
            os.setxattr(source, "user.sonic_test", expected)
        except OSError as error:
            self.skipTest("xattrs unavailable: " + str(error))
        part = self.root / "part"
        manifest = store.collect(native, part)
        values = []
        for layer in manifest["layers"].values():
            with tarfile.open(part / layer["fragment"]) as archive:
                values += [item.pax_headers.get("SCHILY.xattr.user.sonic_test") for item in archive]
        values = [value.encode("utf-8", "surrogateescape") for value in values if value is not None]
        self.assertIn(expected, values)
        self.assertEqual(sum(layer["native_xattrs"].get("user.sonic_test", 0) for layer in manifest["layers"].values()), 2)
        with self.assertRaisesRegex(ValueError, "require an xattr-aware SONiC installer"):
            store.merge([part], self.root / "store.tar.gz")


if __name__ == "__main__":
    unittest.main()
