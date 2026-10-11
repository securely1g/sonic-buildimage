"""Check metadata against the image and payloads it claims to describe."""

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from installed_packages import build, inventory_digest


class InstalledPackagesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "image"
        self.blobs = self.base / "blobs/sha256"
        self.blobs.mkdir(parents=True)
        self.original = {"Package": "libexample", "Version": "1.0", "Architecture": "amd64",
                         "Status": "install ok installed", "Description": "original package"}
        status = "".join(key + ": " + value + "\n" for key, value in self.original.items()) + "\n"
        layer = self.tar("base.tar", {"var/lib/dpkg/status": status.encode()})
        layer_descriptor = self.blob(layer.read_bytes())
        config = {"os": "linux", "architecture": "amd64",
                  "rootfs": {"type": "layers", "diff_ids": [layer_descriptor["digest"]]}}
        manifest = {"schemaVersion": 2, "config": self.blob(json.dumps(config).encode()),
                    "layers": [layer_descriptor]}
        descriptor = self.blob(json.dumps(manifest).encode())
        (self.base / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [descriptor]}))
        (self.base / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        self.consumer = {"Package": "consumer", "Version": "1.0", "Architecture": "amd64",
                         "Depends": "libexample (>= 1.0)", "Description": "preserve this description"}
        self.control = self.tar("control.tar", {"control": "".join(
            key + ": " + value + "\n" for key, value in self.consumer.items()).encode()})
        self.receipt = self.root / "apt.json"
        self.receipt.write_text(json.dumps({
            "base_manifest_digest": descriptor["digest"],
            "selected": [{"package": "consumer", "version": "1.0",
                          "control_sha256": hashlib.sha256(self.control.read_bytes()).hexdigest()}],
            "dependency_check": {"status": "satisfied",
                                 "base_package_inventory_sha256": inventory_digest({"libexample": self.original}),
                                 "packages": {"libexample": self.original, "consumer": self.consumer}},
        }))
        self.payload = self.tar("source.tar", {"usr/lib/libexample.so": b"source-built payload"})
        self.spec = self.root / "source.json"
        self.write_spec("2.0")

    def write_spec(self, version):
        self.spec.write_text(json.dumps({"schema": 1, "packages": {"libexample": {
            "control_fields": dict(self.original, Version=version),
            "source_revision": "a" * 40, "target": "@example//:runtime",
            "required_paths": ["usr/lib/libexample.so"],
        }}}))

    def tar(self, name, members):
        path = self.root / name
        with tarfile.open(path, "w") as archive:
            for name, contents in members.items():
                item = tarfile.TarInfo(name)
                item.size = len(contents)
                archive.addfile(item, io.BytesIO(contents))
        return path

    def blob(self, contents):
        digest = hashlib.sha256(contents).hexdigest()
        (self.blobs / digest).write_bytes(contents)
        return {"digest": "sha256:" + digest, "size": len(contents)}

    def build(self):
        return build(self.base, self.receipt, [self.control], self.spec,
                     {"libexample": self.payload}, "amd64")

    def test_source_replaces_debian_version_with_bound_provenance(self):
        status, receipt = self.build()
        self.assertIn("Version: 2.0", status)
        self.assertIn("Description: preserve this description", status)
        self.assertIn("X-Sonic-Bazel-Target: @example//:runtime", status)
        self.assertEqual(receipt["source_packages"][0]["replaced_version"], "1.0")
        self.assertEqual(receipt["source_packages"][0]["payload_sha256"],
                         hashlib.sha256(self.payload.read_bytes()).hexdigest())
        self.assertEqual(receipt["dependency_check"]["packages"]["libexample"]["Version"], "2.0")

    def test_source_version_must_satisfy_installed_consumers(self):
        self.write_spec("0.5")
        with self.assertRaisesRegex(ValueError, "consumer"):
            self.build()

    def test_native_inventory_covers_source_elf_bytes_not_only_required_paths(self):
        contents = b"\x7fELF" + bytes(60)
        self.payload = self.tar("native-source.tar", {
            "usr/lib/libexample.so": contents,
            "usr/lib/example/plugin.so": contents + b"plugin",
            "usr/share/example/data": b"plain data",
        })
        _, receipt = self.build()
        rows = {item["path"]: item for item in receipt["source_packages"][0]["native_files"]}
        self.assertEqual(set(rows), {"usr/lib/libexample.so", "usr/lib/example/plugin.so"})
        self.assertEqual(rows["usr/lib/example/plugin.so"]["sha256"],
                         hashlib.sha256(contents + b"plugin").hexdigest())
        self.assertEqual((rows["usr/lib/libexample.so"]["uid"], rows["usr/lib/libexample.so"]["gid"]), (0, 0))

    def test_missing_source_installed_file_is_rejected(self):
        self.payload = self.tar("empty-source.tar", {"unrelated": b"not the library"})
        with self.assertRaisesRegex(ValueError, "reviewed installed paths"):
            self.build()

    def test_receipt_from_other_base_is_rejected(self):
        receipt = json.loads(self.receipt.read_text())
        receipt["base_manifest_digest"] = "sha256:" + "0" * 64
        self.receipt.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "another OCI base"):
            self.build()

    def test_changed_apt_control_is_rejected(self):
        self.control = self.tar("changed-control.tar", {"control": b"Package: consumer\nVersion: 2.0\nArchitecture: amd64\n"})
        with self.assertRaisesRegex(ValueError, "differs from selected"):
            self.build()


if __name__ == "__main__":
    unittest.main()
