"""Configuration observations do not replace source or complete image validation."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import observe_original_archive as observer


class OriginalArchiveObservationTest(unittest.TestCase):
    def make_archive(self, root, *, version="bazel-ci", architecture="amd64", tag="docker-eventd:latest",
                     extra=(), tamper_config=False, missing_layer=False, duplicate_version=False):
        env = ["IMAGE_VERSION=" + version, "PRIVATE_VALUE=do-not-publish"]
        if duplicate_version:
            env.append("IMAGE_VERSION=" + version)
        config = {"os": "linux", "architecture": architecture,
                  "config": {"Entrypoint": ["/usr/local/bin/supervisord"], "Env": env,
                             "Labels": {"com.azure.sonic.manifest": "unverified-label"}}}
        data = json.dumps(config).encode()
        name = ("0" * 64 if tamper_config else hashlib.sha256(data).hexdigest()) + ".json"
        manifest = [{"RepoTags": [tag], "Config": name, "Layers": ["layer.tar"]}]
        members = [(name, data), ("manifest.json", json.dumps(manifest).encode())]
        if not missing_layer:
            members.append(("layer.tar", b"not validated or extracted as a layer"))
        members.extend(extra)
        path = root / "archive.gz"
        with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for name, payload in members:
                    item = tarfile.TarInfo(name)
                    if payload is None:
                        item.type = tarfile.SYMTYPE
                        item.linkname = "outside"
                        archive.addfile(item)
                    else:
                        item.size = len(payload)
                        archive.addfile(item, io.BytesIO(payload))
        return path

    def observe(self, path, **kwargs):
        return observer.observe(path, "docker-eventd:latest", ["/usr/local/bin/supervisord"], **kwargs)

    def test_exact_configuration_is_observation_not_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = self.make_archive(root)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result = self.observe(path, expected_sha256=digest)
            self.assertTrue(result["matches_expected_configuration"])
            self.assertEqual(result["sha256"], digest)
            self.assertEqual(result["bytes"], path.stat().st_size)
            self.assertFalse(result["source_admission"])
            self.assertFalse(result["full_layer_validation"])
            self.assertFalse(result["independent_Make_manifest_checked"])
            self.assertNotIn("PRIVATE_VALUE", json.dumps(result))
            self.assertEqual(list(root.iterdir()), [path])

    def test_configuration_differences_are_retained(self):
        for values, key in [({"version": "old-version"}, "image_version_matches"),
                            ({"architecture": "arm64"}, "linux_amd64"),
                            ({"tag": "other:tag"}, "tag_matches")]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                result = self.observe(self.make_archive(Path(tmp), **values))
                self.assertFalse(result["checks"][key])
                self.assertFalse(result["matches_expected_configuration"])
                self.assertFalse(result["VS_assembly_admission"])

    def test_duplicate_version_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "exactly one image version"):
                self.observe(self.make_archive(Path(tmp), duplicate_version=True))

    def test_wrong_original_hash_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "original runtime receipt"):
                self.observe(self.make_archive(Path(tmp)), expected_sha256="0" * 64)

    def test_config_digest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "config digest differs"):
                self.observe(self.make_archive(Path(tmp), tamper_config=True))

    def test_missing_layer_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "layer is missing"):
                self.observe(self.make_archive(Path(tmp), missing_layer=True))

    def test_links_and_traversal_rejected(self):
        for name, data in [("escape", None), ("../escape", b"x")]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    self.observe(self.make_archive(Path(tmp), extra=[(name, data)]))

    def test_duplicate_member_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.observe(self.make_archive(Path(tmp), extra=[("layer.tar", b"second")]))

    def test_json_and_member_limits_enforced(self):
        for name, limit in [("MAX_JSON", 5), ("MAX_MEMBERS", 2), ("MAX_EXPANDED", 5)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = self.make_archive(Path(tmp))
                with mock.patch.object(observer, name, limit), self.assertRaises(ValueError):
                    self.observe(path)

    def test_gzip_trailer_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_archive(Path(tmp))
            data = bytearray(path.read_bytes()); data[-8] ^= 1; path.write_bytes(data)
            with self.assertRaises(gzip.BadGzipFile):
                self.observe(path)

    def test_unexpected_version_text_is_hashed_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.observe(self.make_archive(Path(tmp), version="private value with spaces"))
            self.assertIsNone(result["image_version"])
            self.assertFalse(result["checks"]["image_version_matches"])


    def test_extended_headers_are_bounded_before_body_read(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                item = tarfile.TarInfo("extended-header")
                item.type = kind
                item.size = observer.MAX_JSON + 1
                path = Path(tmp) / "archive.gz"
                path.write_bytes(gzip.compress(item.tobuf(format=tarfile.USTAR_FORMAT), mtime=0))
                self.assertLess(path.stat().st_size, 1024)
                with self.assertRaisesRegex(ValueError, "extended TAR metadata exceeds"):
                    self.observe(path)

    def test_nonblocking_fifo_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.gz"
            os.mkfifo(path)
            with self.assertRaisesRegex(ValueError, "regular file"):
                self.observe(path)

    def test_symlink_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); original = self.make_archive(root)
            link = root / "link.gz"; link.symlink_to(original)
            with self.assertRaises(OSError):
                self.observe(link)


    def make_blob_archive(self, root, *, layer=b"binary layer bytes", config_bytes=None):
        config = {"os": "linux", "architecture": "amd64", "config": {
            "Entrypoint": ["/usr/local/bin/supervisord"], "Env": ["IMAGE_VERSION=bazel-ci"]}}
        data = json.dumps(config).encode() if config_bytes is None else config_bytes
        config_name = "blobs/sha256/" + hashlib.sha256(data).hexdigest()
        layer_name = "blobs/sha256/" + hashlib.sha256(layer).hexdigest()
        manifest = [{"RepoTags": ["docker-eventd:latest"], "Config": config_name, "Layers": [layer_name]}]
        path = root / "blob-archive.gz"
        with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for name, payload in [(layer_name, layer), (config_name, data),
                                      ("manifest.json", json.dumps(manifest).encode())]:
                    item = tarfile.TarInfo(name); item.size = len(payload)
                    archive.addfile(item, io.BytesIO(payload))
        return path

    def test_binary_blob_before_manifest_is_not_decoded_as_json(self):
        for layer in (b"raw layer contents", gzip.compress(b"layer data", mtime=0)):
            with self.subTest(layer=layer[:4]), tempfile.TemporaryDirectory() as tmp:
                path = self.make_blob_archive(Path(tmp), layer=layer)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result = self.observe(path, expected_sha256=digest)
                self.assertTrue(result["matches_expected_configuration"])
                self.assertFalse(result["VS_assembly_admission"])
                self.assertEqual(result["sha256"], digest)

    def test_unselected_object_shaped_blob_is_not_json_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_blob_archive(Path(tmp), layer=b"{not JSON, still an unselected layer")
            self.assertTrue(self.observe(path)["matches_expected_configuration"])

    def test_large_layer_blob_does_not_consume_json_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_blob_archive(Path(tmp), layer=b"x" * 4096)
            with mock.patch.object(observer, "MAX_JSON", 1024):
                self.assertTrue(self.observe(path)["matches_expected_configuration"])

    def test_selected_non_object_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_blob_archive(Path(tmp), config_bytes=b"not a config")
            with self.assertRaisesRegex(ValueError, "configuration is missing"):
                self.observe(path)

    def test_selected_malformed_object_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_blob_archive(Path(tmp), config_bytes=b"{invalid JSON")
            with self.assertRaises(json.JSONDecodeError):
                self.observe(path)

    def test_selected_oversized_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_blob_archive(Path(tmp), config_bytes=b"{" + b" " * 2048)
            with mock.patch.object(observer, "MAX_JSON", 1024), self.assertRaises(ValueError):
                self.observe(path)

    def test_selected_object_whitespace_can_cross_read_chunks(self):
        data = b" \t\r\n" * 3 + json.dumps({"os": "linux", "architecture": "amd64", "config": {
            "Entrypoint": ["/usr/local/bin/supervisord"], "Env": ["IMAGE_VERSION=bazel-ci"]}}).encode()
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_blob_archive(Path(tmp), config_bytes=data)
            with mock.patch.object(observer, "CHUNK", 4):
                self.assertTrue(self.observe(path)["matches_expected_configuration"])



if __name__ == "__main__":
    unittest.main()
