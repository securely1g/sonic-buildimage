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

    def build(self, **kwargs):
        return build(self.base, self.receipt, [self.control], self.spec,
                     {"libexample": self.payload}, "amd64", **kwargs)

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

    def test_normalized_source_ownership_paths_and_modes_match_the_declared_layer(self):
        contents = b"\x7fELF" + bytes(60)
        normalization = self.root / "normalization.json"
        normalization.write_text(json.dumps({
            "base_manifest_digest": json.loads(self.receipt.read_text())["base_manifest_digest"],
            "directory_aliases": {"var/run": {"linkname": "/run", "target": "run"}},
            "root_owned": True, "modes": {"run/redis/plugin.so": "0644"},
        }))
        spec = json.loads(self.spec.read_text())
        spec["packages"]["libexample"]["required_paths"] = ["var/run/redis/plugin.so"]
        self.spec.write_text(json.dumps(spec))
        # Both raw and already-canonical producer paths satisfy the same
        # normalized contract; bytes remain tied to the original source TAR.
        for path in ("var/run/redis/plugin.so", "run/redis/plugin.so"):
            with self.subTest(path=path):
                self.payload = self.root / "normalized-source.tar"
                with tarfile.open(self.payload, "w") as archive:
                    member = tarfile.TarInfo(path)
                    member.size, member.uid, member.gid, member.mode = len(contents), 1000, 2000, 0o664
                    archive.addfile(member, io.BytesIO(contents))
                _, receipt = self.build(normalization_receipt=normalization)
                package = receipt["source_packages"][0]
                self.assertEqual(package["native_files"], [{
                    "path": "run/redis/plugin.so", "uid": 0, "gid": 0, "mode": 0o644,
                    "sha256": hashlib.sha256(contents).hexdigest(),
                }])
                self.assertEqual(package["payload_sha256"], hashlib.sha256(self.payload.read_bytes()).hexdigest())
                self.assertEqual(receipt["source_normalization_sha256"],
                                 hashlib.sha256(normalization.read_bytes()).hexdigest())

    def test_source_normalization_receipt_is_bound_to_the_same_base(self):
        normalization = self.root / "wrong-normalization.json"
        normalization.write_text(json.dumps({"base_manifest_digest": "sha256:" + "0" * 64}))
        with self.assertRaisesRegex(ValueError, "normalization receipt belongs to another"):
            self.build(normalization_receipt=normalization)

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

    def retained(self):
        imported = {"Package": "cached-runtime", "Version": "1.0", "Architecture": "amd64",
                    "Depends": "libexample (>= 2.0)"}
        source = json.loads(self.spec.read_text())["packages"]["libexample"]
        document = {
            "variant": "runtime", "architecture": "amd64",
            "packages": [{"package": "cached-runtime", "version": "1.0", "architecture": "amd64",
                          "control_fields": imported, "source_sha256": "b" * 64}],
            "source_packages": [{"package": "libexample", "control_fields": source["control_fields"],
                                 "source": {"commit": source["source_revision"], "target": source["target"]},
                                 "files": {"usr/lib/libexample.so": {"kind": "file"}},
                                 "input_tar_sha256": hashlib.sha256(self.payload.read_bytes()).hexdigest()}],
        }
        path = self.root / "retained.json"
        path.write_text(json.dumps(document))
        apt = json.loads(self.receipt.read_text())
        apt.update(retained_manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                   variant="runtime", source_receipt_sha256="c" * 64, make_manifest_sha256="d" * 64)
        apt["dependency_check"]["packages"].update(
            libexample=source["control_fields"], **{"cached-runtime": imported})
        self.receipt.write_text(json.dumps(apt))
        return path

    def test_mixed_image_records_source_and_cached_package_identities(self):
        path = self.retained()
        status, receipt = build(self.base, self.receipt, [self.control], self.spec,
                                {"libexample": self.payload}, "amd64", retained_manifest=path)
        self.assertIn("X-Sonic-Imported-Deb-Sha256: " + "b" * 64, status)
        self.assertEqual(receipt["dependency_check"]["packages"]["libexample"]["Version"], "2.0")
        self.assertEqual(receipt["variant"], "runtime")
        self.assertEqual(receipt["source_receipt_sha256"], "c" * 64)

    def test_foreign_contract_keeps_the_sonic_patch_version_in_installed_metadata(self):
        self.spec.write_text(json.dumps({"schema": 1, "packages": [{
            "package": "libexample", "control_fields": dict(self.original, Version="3.7.0"),
            "source": {"module": "libexample", "version": "3.7.0.sonic.3",
                       "release": "3.7.0", "target": "@example//:runtime"},
            "required_paths": {"usr/lib/libexample.so": "file"},
        }]}))
        status, receipt = self.build()
        self.assertIn("X-Sonic-Source-Revision: 3.7.0.sonic.3", status)
        self.assertEqual(receipt["source_packages"][0]["source_revision"], "3.7.0.sonic.3")

    def test_mixed_image_cannot_reuse_a_source_receipt_for_changed_bytes(self):
        path = self.retained()
        self.payload = self.tar("changed-source.tar", {"usr/lib/libexample.so": b"changed source bytes"})
        with self.assertRaisesRegex(ValueError, "source TAR differs"):
            build(self.base, self.receipt, [self.control], self.spec,
                  {"libexample": self.payload}, "amd64", retained_manifest=path)

    def test_mixed_image_uses_canonical_paths_and_normalized_owners(self):
        contents = b"\x7fELF" + bytes(60)
        with tarfile.open(self.payload, "w") as archive:
            for name, data in {"usr/lib/libexample.so": contents, "var/run/example/config": b"config"}.items():
                member = tarfile.TarInfo(name)
                member.size, member.uid, member.gid, member.mode = len(data), 1000, 1000, 0o644
                archive.addfile(member, io.BytesIO(data))
        spec = json.loads(self.spec.read_text())
        spec["packages"]["libexample"]["required_paths"].append("run/example/config")
        self.spec.write_text(json.dumps(spec))
        path = self.retained()
        document = json.loads(path.read_text())
        document["source_packages"][0]["files"] = {
            "run/example/config": {"kind": "file"},
            "usr/lib/libexample.so": {"kind": "file", "elf_machine": 62,
                                      "sha256": hashlib.sha256(contents).hexdigest(),
                                      "mode": 0o644, "uid": 0, "gid": 0},
        }
        for gid, mode in ((0, 0o644), (104, 0o2755)):
            with self.subTest(gid=gid, mode=mode):
                document["source_packages"][0]["files"]["usr/lib/libexample.so"].update(gid=gid, mode=mode)
                path.write_text(json.dumps(document))
                apt = json.loads(self.receipt.read_text())
                apt["retained_manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                self.receipt.write_text(json.dumps(apt))
                _, receipt = build(self.base, self.receipt, [self.control], self.spec,
                                   {"libexample": self.payload}, "amd64", retained_manifest=path)
                native = receipt["source_packages"][0]["native_files"][0]
                self.assertEqual((native["uid"], native["gid"], native["mode"]), (0, gid, mode))
                self.assertEqual(native["sha256"], hashlib.sha256(contents).hexdigest())

    def test_mixed_image_cannot_change_cached_package_control_after_selection(self):
        path = self.retained()
        document = json.loads(path.read_text())
        document["packages"][0]["control_fields"]["Depends"] = "missing-library"
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "retained manifest differs"):
            build(self.base, self.receipt, [self.control], self.spec,
                  {"libexample": self.payload}, "amd64", retained_manifest=path)


if __name__ == "__main__":
    unittest.main()
