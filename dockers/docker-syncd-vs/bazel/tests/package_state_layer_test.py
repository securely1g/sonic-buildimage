#!/usr/bin/env python3
"""Check generated package files against their locked owners and link targets."""

import hashlib
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest

OWNER = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(OWNER.parents[1]))
sys.path.insert(0, str(OWNER / "bazel"))
from tools.bazel.tests.oci_base_fixture import digest, oci_files, tar_entries as tar_bytes, write_layout
import package_state_layer as subject
import validate_image
import validate_payloads


def write_oci(path, entries):
    layer = tar_bytes(entries)
    config = {"architecture": "amd64", "os": "linux",
              "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
    files = oci_files(json.dumps(config).encode(), [layer])
    write_layout(path, files)
    return json.loads(files["index.json"])["manifests"][0]["digest"]


class CommittedPackageStateTest(unittest.TestCase):
    def test_reviewed_state_owners_match_committed_canonical_lock(self):
        lock_path = OWNER / "bazel/apt.lock.json"
        lock = json.loads(lock_path.read_bytes())
        contract = json.loads((OWNER / "bazel/runtime_package_state.json").read_bytes())
        self.assertEqual(contract["apt_lock_sha256"], hashlib.sha256(lock_path.read_bytes()).hexdigest())
        self.assertEqual(lock["version"], 2)
        for group, field in (("package_controls", "control_sha256"), ("package_payloads", "payload_sha256")):
            for name, identity in contract[group].items():
                owners = [item for item in lock["packages"].values() if item["name"] == name]
                self.assertTrue(owners, name)
                for item in owners:
                    self.assertEqual(identity, {"version": item["version"], field: item[field]}, name)


class PackageStateLayerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-state-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "base.oci"
        base_digest = write_oci(self.base, [("etc/base", b"base", 0o644)])
        self.dockerfile = self.root / "Dockerfile.j2"
        self.dockerfile.write_text("FROM fixture\n")
        self.license = b"sample license\n"
        self.apt = self.root / "apt.tar"
        self.write_apt(self.license)
        self.runtime_layer = self.root / "runtime.tar"
        self.init_bytes = b"#!/bin/sh\nexit 0\n"
        self.runtime_layer.write_bytes(tar_bytes([("etc/init.d/tool", self.init_bytes, 0o755)]))
        key = "/trixie/tool-package:amd64=1.0"
        package = {"name": "tool-package", "version": "1.0", "architecture": "amd64", "suite": "trixie",
                   "filename": "pool/tool.deb", "sha256": "a" * 64, "size": 1, "depends_on": [],
                   "payload_sha256": "b" * 64, "payload_size": 1, "control_sha256": "c" * 64, "control_size": 1}
        self.lock = self.root / "apt.lock.json"
        self.lock.write_text(json.dumps({"version": 2, "facts": {},
            "dependency_sets": {"runtime": {"sets": {"amd64": {key.rsplit("=", 1)[0]: "1.0"}}}},
            "sources": {"trixie": {"uris": ["https://snapshot.debian.org/archive/debian/20260727T143429Z"]}},
            "packages": {key: package}}))
        script_sha = hashlib.sha256(b"postinst").hexdigest()
        self.make_manifest = self.root / "make.json"
        self.make_manifest.write_text(json.dumps({"schema": 1, "image": "docker-syncd-vs", "variant": "runtime",
            "features": dict(validate_payloads.FEATURES), "packages": [{"package": "make-package", "version": "1.0",
            "architecture": "amd64", "control_fields": {"Depends": "libc6"},
            "control_files": {"postinst": script_sha, "md5sums": "d" * 64}}]}))
        self.selection = self.root / "selection.json"
        self.selection.write_text(json.dumps({"schema": 1, "variant": "runtime", "base_manifest_digest": base_digest,
            "apt_lock_sha256": hashlib.sha256(self.lock.read_bytes()).hexdigest(),
            "make_manifest_sha256": hashlib.sha256(self.make_manifest.read_bytes()).hexdigest(),
            "selected": [{"key": key, "package": "tool-package", "version": "1.0",
                          "control_sha256": package["control_sha256"], "payload_sha256": package["payload_sha256"]}]}))
        state = "auto\n/usr/bin/tool\n\n/usr/bin/tool-1\n10\n\n"
        self.contract = self.root / "state.json"
        self.contract.write_text(json.dumps({"schema": 1,
            "reference": {"buildimage_revision": "1" * 40, "base_archive_sha256": "2" * 64,
                          "runtime_archive_sha256": "3" * 64,
                          "legacy_dockerfile_sha256": hashlib.sha256(self.dockerfile.read_bytes()).hexdigest()},
            "apt_lock_sha256": hashlib.sha256(self.lock.read_bytes()).hexdigest(),
            "package_controls": {"tool-package": {"version": "1.0", "control_sha256": package["control_sha256"]}},
            "package_payloads": {"tool-package": {"version": "1.0", "payload_sha256": package["payload_sha256"]}},
            "make_package_state_inputs": {"make-package": {"version": "1.0", "architecture": "amd64",
                "control_fields": {"Depends": "libc6"}, "maintainer_scripts": {"postinst": script_sha}}},
            "make_package_files": {"etc/init.d/tool": {"package": "make-package",
                "sha256": hashlib.sha256(self.init_bytes).hexdigest(), "size": len(self.init_bytes), "mode": 0o755, "uid": 0, "gid": 0}},
            "entries": [
                {"path": "etc/alternatives/tool", "kind": "symlink", "mode": 0o777, "uid": 0, "gid": 0, "linkname": "/usr/bin/tool-1"},
                {"path": "usr/bin/tool", "kind": "symlink", "mode": 0o777, "uid": 0, "gid": 0, "linkname": "/etc/alternatives/tool"},
                {"path": "etc/rc2.d/S01tool", "kind": "symlink", "mode": 0o777, "uid": 0, "gid": 0, "linkname": "../init.d/tool"},
                {"path": "var/lib/dpkg/alternatives/tool", "kind": "file", "mode": 0o644, "uid": 0, "gid": 0,
                 "text": state, "sha256": hashlib.sha256(state.encode()).hexdigest(), "size": len(state.encode())}],
            "aliases": [{"package": "tool-package", "source": "usr/share/doc/tool-package/copyright",
                "path": "usr/share/doc/tool-old/copyright", "sha256": hashlib.sha256(self.license).hexdigest(),
                "size": len(self.license), "mode": 0o644, "uid": 0, "gid": 0}]}))
        self.output = self.root / "state.tar"

    def write_apt(self, license_data):
        self.apt.write_bytes(tar_bytes([("usr/bin/tool-1", b"#!/bin/sh\nexit 0\n", 0o755),
                                        ("usr/share/doc/tool-package/copyright", license_data, 0o644)]))

    def mutate(self, path, function):
        value = json.loads(path.read_bytes())
        function(value)
        path.write_text(json.dumps(value))

    def update_make_selection(self):
        self.mutate(self.selection, lambda value: value.update(
            make_manifest_sha256=hashlib.sha256(self.make_manifest.read_bytes()).hexdigest()))

    def build(self):
        return subject.build(self.contract, self.lock, self.selection, self.base, self.apt,
                             self.runtime_layer, self.make_manifest, self.dockerfile, self.output)

    def test_checked_links_state_and_alias_are_published(self):
        result = self.build()
        files = {}
        validate_image.apply_layer(self.output, files)
        self.assertEqual(result["links_checked"], 3)
        self.assertEqual(result["aliases"], 1)
        self.assertEqual(files["usr/bin/tool"]["linkname"], "/etc/alternatives/tool")
        self.assertEqual(files["etc/rc2.d/S01tool"]["linkname"], "../init.d/tool")
        self.assertEqual(files["usr/share/doc/tool-old/copyright"]["sha256"], hashlib.sha256(self.license).hexdigest())
        with tarfile.open(self.output, "r:") as archive:
            self.assertTrue(all(member.mtime == 0 for member in archive))

    def test_changed_owner_control_is_rejected(self):
        self.mutate(self.contract, lambda value: value["package_controls"]["tool-package"].update(control_sha256="e" * 64))
        with self.assertRaisesRegex(ValueError, "package state owner changed"):
            self.build()

    def test_selected_owner_must_match_canonical_lock_key(self):
        self.mutate(self.selection, lambda value: value["selected"][0].update(key="/trixie/other:amd64=1.0"))
        with self.assertRaisesRegex(ValueError, "selected package is absent from the checked lock"):
            self.build()

    def test_unselected_owner_is_rejected(self):
        self.mutate(self.selection, lambda value: value.update(selected=[]))
        with self.assertRaisesRegex(ValueError, "requires a selected APT package"):
            self.build()

    def test_missing_link_target_is_rejected(self):
        self.mutate(self.contract, lambda value: value["entries"][0].update(linkname="/usr/bin/missing"))
        with self.assertRaisesRegex(ValueError, "link target is absent"):
            self.build()

    def test_changed_alias_bytes_are_rejected(self):
        self.write_apt(b"different license\n")
        with self.assertRaisesRegex(ValueError, "copyright bytes differ"):
            self.build()

    def test_package_state_cannot_replace_a_base_elf(self):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        digest = write_oci(self.base, [("usr/bin/tool", bytes(header), 0o755)])
        self.mutate(self.selection, lambda value: value.update(base_manifest_digest=digest))
        with self.assertRaisesRegex(ValueError, "package state would replace an ELF"):
            self.build()

    def test_make_relationship_changes_require_review(self):
        self.mutate(self.make_manifest, lambda value: value["packages"][0]["control_fields"].update(Depends="libc6, new-library"))
        self.update_make_selection()
        with self.assertRaisesRegex(ValueError, "relationships or scripts changed"):
            self.build()

    def test_unrelated_package_md5_changes_keep_state_bytes(self):
        self.build()
        first = self.output.read_bytes()
        self.mutate(self.make_manifest, lambda value: value["packages"][0]["control_files"].update(md5sums="e" * 64))
        self.update_make_selection()
        self.build()
        self.assertEqual(self.output.read_bytes(), first)

    def test_reviewed_make_archive_identity_cannot_be_substituted(self):
        """Bind explicitly pinned state inputs to their reviewed source and control archives."""
        identity = {"source_sha256": "a" * 64, "control_sha256": "b" * 64}
        self.mutate(self.contract, lambda value: value["make_package_state_inputs"]["make-package"].update(identity))
        self.mutate(self.make_manifest, lambda value: value["packages"][0].update(identity))
        self.update_make_selection()
        self.build()
        original = self.output.read_bytes()
        for field in identity:
            with self.subTest(field=field):
                self.mutate(self.make_manifest, lambda value: value["packages"][0].update(identity))
                self.mutate(self.make_manifest, lambda value: value["packages"][0].update({field: "f" * 64}))
                self.update_make_selection()
                with self.assertRaisesRegex(ValueError, "relationships or scripts changed"):
                    self.build()
                self.assertEqual(self.output.read_bytes(), original)

    def test_make_state_file_changes_require_review(self):
        self.runtime_layer.write_bytes(tar_bytes([("etc/init.d/tool", b"changed init script\n", 0o755)]))
        with self.assertRaisesRegex(ValueError, "Make package state file changed"):
            self.build()

    def test_dockerfile_changes_require_review(self):
        self.dockerfile.write_text("FROM changed\n")
        with self.assertRaisesRegex(ValueError, "legacy Dockerfile changed"):
            self.build()

    def test_apt_lock_changes_require_review(self):
        self.mutate(self.lock, lambda value: value.update(note="changed selection"))
        with self.assertRaisesRegex(ValueError, "APT content lock changed"):
            self.build()


if __name__ == "__main__":
    unittest.main()
