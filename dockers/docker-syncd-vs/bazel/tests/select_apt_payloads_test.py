#!/usr/bin/env python3
"""Check that syncd APT assembly preserves locked content and base ELF files."""

import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

OWNER = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(OWNER.parents[1]))
sys.path.insert(0, str(OWNER / "bazel"))
from tools.bazel.tests.oci_base_fixture import digest, oci_files, tar_entries, write_layout
import apt_lock
import select_apt_payloads as subject
import validate_payloads


def tar_bytes(entries):
    return tar_entries((name, data, 0o644) for name, data in entries)


def write_oci(path, entries):
    layer = tar_bytes(entries)
    config = {"architecture": "amd64", "os": "linux",
              "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
    write_layout(path, oci_files(json.dumps(config).encode(), [layer]))


class SelectAptPayloadsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-apt-test-")
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
        packages, roots, mapping = {}, [], []
        self.paths, self.controls = {}, {}
        for name, version in (("libssl3t64", "3.5.6"), ("libnl-3-200", "3.7.0-2"), ("new-runtime", "1.0")):
            key = "/trixie/" + name + ":amd64=" + version
            path = self.root / (name + ".tar")
            data = ([("usr/lib/libssl.so.3", bytes(header) + b"Debian runtime")] if name == "libssl3t64" else
                    [("usr/share/" + name + "/data", name.encode())])
            path.write_bytes(tar_bytes(data))
            control = self.root / (name + ".control")
            control.write_text("Package: " + name + "\nVersion: " + version + "\n")
            packages[key] = {"name": name, "version": version, "architecture": "amd64", "suite": "trixie",
                             "sha256": hashlib.sha256(name.encode()).hexdigest(), "size": 1,
                             "filename": "pool/" + name + ".deb", "depends_on": [],
                             "payload_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "payload_size": path.stat().st_size,
                             "control_sha256": hashlib.sha256(control.read_bytes()).hexdigest(), "control_size": control.stat().st_size}
            roots.append(key)
            mapping.append({"key": key, "payload": str(path), "control": str(control)})
            self.paths[name], self.controls[name] = path, control
        self.lock = self.root / "apt.lock.json"
        self.lock.write_text(json.dumps({"schema": 1, "bazel_version": apt_lock.BAZEL_VERSION,
            "rules_distroless_version": apt_lock.DISTROLESS_VERSION, "roots": {"runtime": roots, "debug": roots},
            "sources": {"trixie": {"uris": ["https://snapshot.debian.org/archive/debian/20260727T143429Z"]}},
            "packages": packages}))
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(json.dumps({"locked": mapping, "hub_paths": [str(path) for path in self.paths.values()]}))
        self.runtime = self.root / "runtime.json"
        self.runtime.write_text(json.dumps({"schema": 1, "image": "docker-syncd-vs", "variant": "runtime",
            "architecture": "amd64", "distribution": "trixie", "features": dict(validate_payloads.FEATURES),
            "packages": [{"package": "libnl-3-200", "source_sha256": "a" * 64}]}))

    def write_base(self, *, status=True):
        entries = [("usr/lib/libssl.so.3", self.base_elf), ("etc/base", b"base config")]
        if status:
            entries.append(("var/lib/dpkg/status", self.status))
        write_oci(self.base, entries)

    def update_payload(self, name, entries):
        path = self.paths[name]
        path.write_bytes(tar_bytes(entries))
        lock = json.loads(self.lock.read_bytes())
        package = next(value for value in lock["packages"].values() if value["name"] == name)
        package.update(payload_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), payload_size=path.stat().st_size)
        self.lock.write_text(json.dumps(lock))

    def select(self):
        return subject.select(self.base, self.lock, self.runtime, self.mapping, variant="runtime")

    def test_base_and_make_packages_are_retained(self):
        paths, receipt = self.select()
        self.assertEqual(paths, [self.paths["new-runtime"]])
        self.assertEqual([item["package"] for item in receipt["selected"]], ["new-runtime"])
        self.assertEqual(receipt["skipped_base"][0]["base_version"], "3.5.7+fips")
        self.assertEqual(receipt["skipped_base"][0]["selected_version"], "3.5.6")
        self.assertEqual(receipt["skipped_make"][0]["package"], "libnl-3-200")
        self.assertEqual(receipt["base_elf_count"], 1)

    def test_another_package_cannot_replace_a_base_elf(self):
        self.update_payload("new-runtime", [("usr/lib/libssl.so.3", self.base_elf + b"changed")])
        with self.assertRaisesRegex(ValueError, "would replace a base ELF"):
            self.select()

    def test_identical_base_elf_bytes_are_allowed(self):
        self.update_payload("new-runtime", [("usr/lib/libssl.so.3", self.base_elf)])
        paths, receipt = self.select()
        self.assertEqual(paths, [self.paths["new-runtime"]])
        self.assertEqual(receipt["changed_non_elf_base_paths"], [])

    def test_non_elf_base_changes_are_reported_for_review(self):
        self.update_payload("new-runtime", [("etc/base", b"changed config")])
        _, receipt = self.select()
        self.assertEqual(receipt["changed_non_elf_base_paths"], [{"package": "new-runtime", "path": "etc/base"}])

    def test_changed_data_and_control_bytes_are_rejected(self):
        payload = self.paths["new-runtime"]
        original = payload.read_bytes()
        payload.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "changed locked APT payload"):
            self.select()
        payload.write_bytes(original)
        self.controls["new-runtime"].write_text("changed control\n")
        with self.assertRaisesRegex(ValueError, "changed locked APT control"):
            self.select()

    def test_missing_base_status_and_wrong_feature_configuration_are_rejected(self):
        self.write_base(status=False)
        with self.assertRaisesRegex(ValueError, "lacks dpkg status"):
            self.select()
        self.write_base()
        value = json.loads(self.runtime.read_bytes())
        value["features"]["include_fips"] = "n"
        self.runtime.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "invalid syncd Make package manifest"):
            self.select()

    def test_current_hub_and_declared_content_must_match_the_lock(self):
        value = json.loads(self.mapping.read_bytes())
        value["hub_paths"].pop()
        self.mapping.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "current Distroless package set differs"):
            self.select()
        value["locked"].pop()
        self.mapping.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "declared syncd APT payloads differ"):
            self.select()

    def test_foreign_locked_package_is_rejected(self):
        lock = json.loads(self.lock.read_bytes())
        lock["roots"]["runtime"].append("/trixie/foreign:arm64=1.0")
        with self.assertRaisesRegex(ValueError, "foreign architecture"):
            apt_lock.closure(lock, "runtime")

    def test_identical_package_sources_are_deduplicated(self):
        lock = json.loads(self.lock.read_bytes())
        original_key = next(key for key, value in lock["packages"].items() if value["name"] == "new-runtime")
        duplicate_key = "/trixie-security/new-runtime:amd64=1.0"
        duplicate = dict(lock["packages"][original_key])
        duplicate.update(suite="trixie-security", filename="pool/updates/new-runtime.deb")
        lock["packages"][duplicate_key] = duplicate
        lock["roots"]["runtime"].append(duplicate_key)
        lock["roots"]["debug"].append(duplicate_key)
        lock["sources"]["trixie-security"] = {"uris": ["https://snapshot.debian.org/archive/debian-security/20260726T121236Z"]}
        self.lock.write_text(json.dumps(lock))
        payload = self.root / "duplicate.tar"
        payload.write_bytes(self.paths["new-runtime"].read_bytes())
        control = self.root / "duplicate.control"
        control.write_bytes(self.controls["new-runtime"].read_bytes())
        mapping = json.loads(self.mapping.read_bytes())
        mapping["locked"].append({"key": duplicate_key, "payload": str(payload), "control": str(control)})
        mapping["hub_paths"].append(str(payload))
        self.mapping.write_text(json.dumps(mapping))
        paths, receipt = self.select()
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(receipt["duplicate_sources"]), 1)
        self.assertEqual(receipt["duplicate_sources"][0]["package"], "new-runtime")
        lock["packages"][duplicate_key]["sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "different versions or content"):
            apt_lock.closure(lock, "runtime")


if __name__ == "__main__":
    unittest.main()
