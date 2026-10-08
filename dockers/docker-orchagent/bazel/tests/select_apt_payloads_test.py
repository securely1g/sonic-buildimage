#!/usr/bin/env python3
"""Check that orchagent APT assembly preserves locked content and base ELF files."""

import hashlib
import io
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
from tools.bazel.tests.oci_base_fixture import digest, oci_files, layer_tar, write_layout
from tools.bazel.oci.oci_inventory import assert_overlay_paths
import select_apt_payloads as subject


def tar_bytes(entries):
    return layer_tar(dict(entries))


def write_oci(path, entries):
    layer = tar_bytes(entries)
    config = {"architecture": "amd64", "os": "linux",
              "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
    write_layout(path, oci_files(json.dumps(config).encode(), [layer]))


class SelectAptPayloadsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orchagent-apt-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        self.base_elf = bytes(header) + b"FIPS runtime"
        self.status = ("Package: libssl3t64\nVersion: 3.5.7+fips\nArchitecture: amd64\n"
                       "Status: install ok installed\n\n").encode()
        self.base = self.root / "base.oci"
        self.write_base()
        self.paths, self.controls = {}, {}
        self.lock = self.root / "apt.lock.json"
        self.lock.write_text(json.dumps({"version": 2, "facts": {},
            "dependency_sets": {}, "sources": {"trixie": {
                "uris": ["https://snapshot.debian.org/archive/debian/20260727T143429Z"]}},
            "packages": {}}))
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(json.dumps({"architecture": "amd64", "packages": []}))
        self.add_package("new-runtime", depends="libssl3t64 (>= 3.0.0)")
        self.policy = self.root / "policy.json"
        self.policy.write_bytes((OWNER / "bazel/apt_policy.json").read_bytes())

    def write_base(self, *, status=True):
        entries = [("usr/lib/libssl.so.3", self.base_elf), ("etc/base", b"base config")]
        if status:
            entries.append(("var/lib/dpkg/status", self.status))
        write_oci(self.base, entries)

    def add_package(self, name, *, version="1.0", depends="", provided=False):
        path = self.root / (name + ".tar")
        path.write_bytes(tar_bytes([("usr/share/" + name + "/data", name.encode())]))
        control = self.root / (name + ".control.tar")
        fields = ("Package: " + name + "\nVersion: " + version + "\nArchitecture: amd64\n" +
                  ("Depends: " + depends + "\n" if depends else ""))
        control.write_bytes(tar_bytes([("control", fields.encode())]))
        self.paths[name], self.controls[name] = path, control
        lock = json.loads(self.lock.read_bytes())
        key = "/trixie/" + name + ":amd64=" + version
        lock["packages"][key] = {
            "name": name, "version": version, "architecture": "amd64", "suite": "trixie",
            "sha256": hashlib.sha256(name.encode()).hexdigest(), "size": 1,
            "filename": "pool/" + name + ".deb", "depends_on": [],
            "payload_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "payload_size": path.stat().st_size,
            "control_sha256": hashlib.sha256(control.read_bytes()).hexdigest(), "control_size": control.stat().st_size}
        self.lock.write_text(json.dumps(lock))
        mapping = json.loads(self.mapping.read_bytes())
        mapping.setdefault("provided_packages" if provided else "packages", []).append(
            {"payload": str(path), "control": str(control)})
        self.mapping.write_text(json.dumps(mapping))

    def update_payload(self, name, entries):
        path = self.paths[name]
        path.write_bytes(tar_bytes(entries))
        lock = json.loads(self.lock.read_bytes())
        package = next(value for value in lock["packages"].values() if value["name"] == name)
        package.update(payload_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), payload_size=path.stat().st_size)
        self.lock.write_text(json.dumps(lock))

    def select(self):
        return subject.select(self.base, self.lock, self.policy, self.mapping, variant="runtime")

    def test_base_openssl_satisfies_new_package_without_being_added(self):
        receipt = self.select()
        self.assertEqual([item["package"] for item in receipt["selected"]], ["new-runtime"])
        self.assertEqual(receipt["base_elf_count"], 1)

    def test_explicit_base_package_is_rejected(self):
        self.add_package("libssl3t64", version="3.5.6")
        with self.assertRaisesRegex(ValueError, "base|already|supplied"):
            self.select()

    def test_base_openssl_too_old_is_rejected(self):
        self.status = self.status.replace(b"3.5.7+fips", b"2.9.0")
        self.write_base()
        with self.assertRaisesRegex(ValueError, "libssl3t64"):
            self.select()

    def test_runtime_overlay_can_satisfy_debug_dependency(self):
        self.add_package("runtime-lib", version="2.0", provided=True)
        self.add_package("debug-tool", depends="runtime-lib (>= 2.0)")
        receipt = self.select()
        self.assertEqual([item["package"] for item in receipt["selected"]], ["new-runtime", "debug-tool"])

    def test_another_package_cannot_replace_a_base_elf(self):
        self.update_payload("new-runtime", [("usr/lib/libssl.so.3", self.base_elf + b"changed")])
        with self.assertRaisesRegex(ValueError, "would replace a base ELF"):
            self.select()

    def test_identical_base_elf_bytes_are_allowed(self):
        self.update_payload("new-runtime", [("usr/lib/libssl.so.3", self.base_elf)])
        receipt = self.select()
        self.assertEqual(receipt["changed_non_elf_base_paths"], [])

    def test_non_elf_base_changes_are_reported_for_review(self):
        self.update_payload("new-runtime", [("etc/base", b"changed config")])
        receipt = self.select()
        self.assertEqual(receipt["changed_non_elf_base_paths"], [{"package": "new-runtime", "path": "etc/base"}])

    def test_changed_data_and_control_bytes_are_rejected(self):
        payload = self.paths["new-runtime"]
        original = payload.read_bytes()
        payload.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "locked APT payload"):
            self.select()
        payload.write_bytes(original)
        self.controls["new-runtime"].write_bytes(tar_bytes([("control", b"Package: new-runtime\nVersion: 1.0\nArchitecture: amd64\nDescription: changed\n")]))
        with self.assertRaisesRegex(ValueError, "control"):
            self.select()

    def test_missing_base_status_and_wrong_policy_are_rejected(self):
        self.write_base(status=False)
        with self.assertRaisesRegex(ValueError, "lacks dpkg status"):
            self.select()
        self.write_base()
        value = json.loads(self.policy.read_bytes())
        value["architecture"] = "arm64"
        self.policy.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "invalid orchagent APT policy"):
            self.select()

    def test_debug_tools_cannot_change_a_source_built_runtime_library(self):
        # Source-built SWSS payloads need not have a dpkg-status entry. Inspect
        # the completed runtime's actual files as well as its package database.
        write_oci(self.base, [("var/lib/dpkg/status", self.status),
                             ("usr/lib/libswsscommon.so.0", self.base_elf)])
        self.update_payload("new-runtime", [("usr/lib/libswsscommon.so.0", self.base_elf + b"changed")])
        with self.assertRaisesRegex(ValueError, "would replace a base ELF"):
            subject.select(self.base, self.lock, self.policy, self.mapping, variant="debug")

    def test_debug_selection_records_the_exact_runtime_manifest(self):
        before = subject.select(self.base, self.lock, self.policy, self.mapping, variant="debug")
        write_oci(self.base, [("var/lib/dpkg/status", self.status), ("etc/runtime-version", b"next")])
        after = subject.select(self.base, self.lock, self.policy, self.mapping, variant="debug")
        self.assertNotEqual(before["base_manifest_digest"], after["base_manifest_digest"])
        self.assertEqual(after["group"], "debug")

    def test_whiteouts_in_added_apt_payload_are_rejected(self):
        self.update_payload("new-runtime", [("etc/.wh.base", b"")])
        with self.assertRaisesRegex(ValueError, "contains a whiteout"):
            self.select()

    def test_payload_cannot_traverse_an_inherited_directory_symlink(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            status = tarfile.TarInfo("var/lib/dpkg/status")
            status.size = len(self.status)
            archive.addfile(status, io.BytesIO(self.status))
            link = tarfile.TarInfo("lib")
            link.type, link.linkname = tarfile.SYMTYPE, "usr/lib"
            archive.addfile(link)
        layer = output.getvalue()
        config = {"architecture": "amd64", "os": "linux",
                  "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
        write_layout(self.base, oci_files(json.dumps(config).encode(), [layer]))
        self.update_payload("new-runtime", [("lib/new.so", self.base_elf)])
        with self.assertRaisesRegex(ValueError, "crosses a non-directory"):
            self.select()

    def test_unknown_variant_and_foreign_base_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported orchagent APT variant"):
            subject.select(self.base, self.lock, self.policy, self.mapping, variant="other")
        layer = tar_bytes([("var/lib/dpkg/status", self.status)])
        config = {"architecture": "arm64", "os": "linux",
                  "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
        write_layout(self.base, oci_files(json.dumps(config).encode(), [layer]))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.select()

    def test_executable_hardlink_and_library_symlink_cannot_be_replaced(self):
        elf = {"kind": "file", "elf_machine": 62, "sha256": "a" * 64}
        for kind, target in (("hardlink", "usr/lib/library.so.1"),
                             ("symlink", "library.so.1"),
                             ("symlink", "/usr/lib/library.so.1")):
            with self.subTest(kind=kind, target=target):
                link = {"kind": kind, "linkname": target}
                base = {"usr/lib/library.so.1": elf, "usr/lib/library.so": link}
                assert_overlay_paths({"usr/lib/library.so": link}, base)
                with self.assertRaisesRegex(ValueError, "changes a base ELF link"):
                    assert_overlay_paths({"usr/lib/library.so": {**elf, "sha256": "b" * 64}}, base)
                with self.assertRaisesRegex(ValueError, "changes a base ELF link"):
                    assert_overlay_paths({"usr/lib/library.so": {"kind": "symlink", "linkname": "other.so"}}, base)

    def test_elf_link_resolution_handles_directory_aliases_and_cycles(self):
        base = {"lib": {"kind": "symlink", "linkname": "usr/lib"},
                "usr/lib/library.so.1": {"kind": "file", "elf_machine": 62},
                "usr/lib/library.so": {"kind": "symlink", "linkname": "/lib/library.so.1"},
                "etc/cycle": {"kind": "symlink", "linkname": "cycle"}}
        with self.assertRaisesRegex(ValueError, "changes a base ELF link"):
            assert_overlay_paths({"usr/lib/library.so": {"kind": "symlink", "linkname": "other.so"}}, base)
        assert_overlay_paths({"etc/cycle": {"kind": "file"}}, base)

    def test_runtime_library_directories_and_directory_links_cannot_be_redirected(self):
        base = {"lib": {"kind": "symlink", "linkname": "usr/lib"},
                "usr/lib": {"kind": "directory"},
                "usr/lib/library.so.1": {"kind": "file", "elf_machine": 62}}
        assert_overlay_paths({"lib": base["lib"], "usr/lib": base["usr/lib"]}, base)
        with self.assertRaisesRegex(ValueError, "changes a base directory link"):
            assert_overlay_paths({"lib": {"kind": "symlink", "linkname": "opt/lib"}}, base)
        for kind in ("symlink", "file"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "hides a base ELF directory"):
                assert_overlay_paths({"usr/lib": {"kind": kind, "linkname": "elsewhere"}}, base)


if __name__ == "__main__":
    unittest.main()
