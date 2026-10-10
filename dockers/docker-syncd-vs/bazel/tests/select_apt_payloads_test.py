#!/usr/bin/env python3
"""Check that syncd APT assembly preserves locked content and base ELF files."""

import argparse
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
from tools.bazel.oci import apt_selection as subject
from sonic_apt import dependencies
import validate_payloads
from sonic_apt.inputs import declarations


def tar_bytes(entries):
    return tar_entries((name, data, 0o644) for name, data in entries)


def write_oci(path, entries):
    layer = tar_bytes(entries)
    config = {"architecture": "amd64", "os": "linux",
              "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
    write_layout(path, oci_files(json.dumps(config).encode(), [layer]))


class SelectAptPayloadsTest(unittest.TestCase):
    policy_path: Path

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-apt-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = self.policy_path
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        self.base_elf = bytes(header) + b"FIPS runtime"
        self.status = ("Package: libssl3t64\nVersion: 3.5.7+fips\nArchitecture: amd64\n"
                       "Status: install ok installed\n\n").encode()
        self.base = self.root / "base.oci"
        self.write_base()
        self.paths, self.controls, self.provided = {}, {}, {}
        self.lock = self.root / "apt.lock.json"
        self.lock.write_text(json.dumps({"version": 2, "facts": {},
            "dependency_sets": {"runtime": {"sets": {"amd64": {}}}}, "sources": {"trixie": {
                "uris": ["https://snapshot.debian.org/archive/debian/20260727T143429Z"]}},
            "packages": {}}))
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(json.dumps({"architecture": "amd64", "locked": [], "dependency_set": "runtime"}))
        self.add_package("new-runtime", depends="libssl3t64 (>= 3.0.0)")
        self.runtime = self.root / "runtime.json"
        self.runtime.write_text(json.dumps({"schema": 1, "image": "docker-syncd-vs", "variant": "runtime",
            "architecture": "amd64", "distribution": "trixie", "features": dict(validate_payloads.FEATURES),
            "packages": [{"package": "libnl-3-200", "version": "3.7.0-2sonic1", "architecture": "amd64",
                          "control_fields": {"Package": "libnl-3-200", "Version": "3.7.0-2sonic1",
                                             "Architecture": "amd64"}, "source_sha256": "a" * 64}]}))

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
        if not provided:
            lock["dependency_sets"]["runtime"]["sets"]["amd64"][key.split("=")[0]] = version
            mapping = json.loads(self.mapping.read_bytes())
            mapping["locked"].append({"key": key, "payload": str(path), "control": str(control)})
            self.mapping.write_text(json.dumps(mapping))
        else:
            self.provided[name] = dependencies.control_fields(
                dependencies.package_from_control(fields, origin="test"))
        self.lock.write_text(json.dumps(lock))

    def update_payload(self, name, entries):
        path = self.paths[name]
        path.write_bytes(tar_bytes(entries))
        lock = json.loads(self.lock.read_bytes())
        package = next(value for value in lock["packages"].values() if value["name"] == name)
        package.update(payload_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), payload_size=path.stat().st_size)
        self.lock.write_text(json.dumps(lock))

    def select(self):
        return subject.select(self.base, self.lock, self.policy, self.mapping,
                              retained_manifest=self.runtime, variant="runtime")[1]

    def test_apt_declarations_match_reviewed_lock(self):
        module, _ = declarations(json.loads((OWNER / "bazel/apt.lock.json").read_bytes()))
        self.assertEqual((OWNER / "bazel/apt_inputs.MODULE.bazel").read_text(), module)

    def test_base_openssl_satisfies_new_package_without_being_added(self):
        receipt = self.select()
        self.assertEqual([item["package"] for item in receipt["selected"]], ["new-runtime"])
        self.assertEqual(receipt["base_elf_count"], 1)

    def test_base_package_is_retained_instead_of_old_candidate(self):
        self.add_package("libssl3t64", version="3.5.6")
        receipt = self.select()
        self.assertEqual(receipt["skipped_base"][0]["base_version"], "3.5.7+fips")

    def test_base_openssl_too_old_is_rejected(self):
        self.status = self.status.replace(b"3.5.7+fips", b"2.9.0")
        self.write_base()
        with self.assertRaisesRegex(ValueError, "libssl3t64"):
            self.select()

    def test_runtime_overlay_can_satisfy_debug_dependency(self):
        self.add_package("runtime-lib", version="2.0", provided=True)
        self.add_package("debug-tool", depends="runtime-lib (>= 2.0)")
        value = json.loads(self.runtime.read_bytes())
        value["variant"] = "debug"
        self.runtime.write_text(json.dumps(value))
        receipt = self.select_debug()
        self.assertEqual({item["package"] for item in receipt["selected"]}, {"new-runtime", "debug-tool"})

    def debug_replacement(self, *, name="openssh-client", version="1:10.0p1-7+fips",
                          depends="libssl3t64 (>= 3.0.0)"):
        self.add_package(name, version="1:10.0p1-7+deb13u4", provided=True,
                         depends="libssl3t64 (>= 3.0.0)")
        value = json.loads(self.runtime.read_bytes())
        value["variant"] = "debug"
        replacement = {"package": name, "version": version, "architecture": "amd64",
                       "source_sha256": "b" * 64, "payload_sha256": "c" * 64,
                       "control_sha256": "d" * 64, "control_fields": {"Depends": depends,
                           "Package": name, "Version": version, "Architecture": "amd64"}}
        value["packages"].append(replacement)
        self.runtime.write_text(json.dumps(value))
        return replacement

    def select_debug(self):
        installed = dependencies.installed_packages(self.status, origin="test")
        fields = {name: dependencies.control_fields(record) for name, record in sorted(installed.items())}
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        metadata = self.root / "base-metadata.json"
        metadata.write_text(json.dumps({"variant": "runtime", "make_manifest_sha256": "e" * 64,
            "dependency_check": {"schema": 1, "status": "satisfied",
                "base_package_inventory_sha256": digest, "packages": {**fields, **self.provided}}}))
        value = json.loads(self.runtime.read_bytes())
        value["runtime_manifest_sha256"] = "e" * 64
        self.runtime.write_text(json.dumps(value))
        return subject.select(self.base, self.lock, self.policy, self.mapping,
                              retained_manifest=self.runtime, variant="debug",
                              base_package_metadata=metadata)[1]

    def test_debug_fips_openssh_replaces_only_its_runtime_overlay_record(self):
        replacement = self.debug_replacement()
        self.add_package("debug-tool", depends="openssh-client (= 1:10.0p1-7+fips)")
        receipt = self.select_debug()
        self.assertEqual(receipt["provided_package_replacements"], [{
            "package": "openssh-client", "runtime_version": "1:10.0p1-7+deb13u4",
            "debug_version": replacement["version"], "source_sha256": "b" * 64,
            "payload_sha256": "c" * 64, "control_sha256": "d" * 64}])
        self.assertEqual({r["package"] for r in receipt["selected"]}, {"new-runtime", "debug-tool"})

        with self.subTest(debug_replacements="not declared"):
            policy = json.loads(self.policy.read_bytes())
            policy["debug_replacements"] = []
            self.policy = self.root / "without-debug-replacements.json"
            self.policy.write_text(json.dumps(policy))
            with self.assertRaisesRegex(ValueError, "conflicts with inherited|installed package"):
                self.select_debug()

    def test_runtime_cannot_override_its_openssh_record(self):
        self.debug_replacement()
        value = json.loads(self.runtime.read_bytes())
        value["variant"] = "runtime"
        self.runtime.write_text(json.dumps(value))
        metadata = self.root / "base-metadata.json"
        metadata.write_text("{}")
        with self.assertRaisesRegex(ValueError, "runtime cannot consume"):
            subject.select(self.base, self.lock, self.policy, self.mapping,
                           retained_manifest=self.runtime, variant="runtime",
                           base_package_metadata=metadata)

    def test_debug_cannot_override_another_runtime_package(self):
        self.debug_replacement(name="another-client")
        with self.assertRaisesRegex(ValueError, "conflicts with inherited|installed package"):
            self.select_debug()

    def test_debug_non_fips_openssh_conflict_is_rejected(self):
        self.debug_replacement(version="1:10.0p1-8")
        with self.assertRaisesRegex(ValueError, "conflicts with inherited|installed package"):
            self.select_debug()

    def test_debug_fips_replacement_cannot_change_dependency_requirements(self):
        self.debug_replacement(depends="libssl3t64 (>= 9.0)")
        with self.assertRaisesRegex(ValueError, "changes runtime package relationships"):
            self.select_debug()

    def test_debug_fips_replacement_needs_make_content_identity(self):
        self.debug_replacement()
        value = json.loads(self.runtime.read_bytes())
        value["packages"][-1].pop("payload_sha256")
        self.runtime.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "lacks Make package identity"):
            self.select_debug()

    def test_debug_fips_replacement_does_not_remove_installed_base_record(self):
        self.debug_replacement()
        self.status += ("Package: openssh-client\nVersion: 1:10.0p1-7+deb13u4\n"
                        "Architecture: amd64\nStatus: install ok installed\n\n").encode()
        self.write_base()
        with self.assertRaisesRegex(ValueError, "conflicts with inherited|installed package"):
            self.select_debug()

    def test_debug_fips_replacement_cannot_change_multi_arch(self):
        self.debug_replacement()
        value = json.loads(self.runtime.read_bytes())
        value["packages"][-1]["control_fields"]["Multi-Arch"] = "foreign"
        self.runtime.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "changes runtime package relationships"):
            self.select_debug()

    def test_retained_package_requires_full_original_identity(self):
        value = json.loads(self.runtime.read_bytes())
        value["packages"][0]["control_fields"].pop("Package")
        self.runtime.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "full original control metadata"):
            self.select()

    def test_debug_requires_the_runtime_selection_receipt(self):
        self.debug_replacement()
        with self.assertRaisesRegex(ValueError, "debug requires the runtime"):
            subject.select(self.base, self.lock, self.policy, self.mapping,
                           retained_manifest=self.runtime, variant="debug")

    def test_debug_rejects_a_receipt_for_a_different_make_handoff(self):
        self.debug_replacement()
        self.select_debug()
        value = json.loads(self.runtime.read_bytes())
        value["runtime_manifest_sha256"] = "f" * 64
        self.runtime.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "does not match the runtime Make"):
            subject.select(self.base, self.lock, self.policy, self.mapping,
                           retained_manifest=self.runtime, variant="debug",
                           base_package_metadata=self.root / "base-metadata.json")

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

    def test_missing_base_status_and_wrong_feature_configuration_are_rejected(self):
        self.write_base(status=False)
        with self.assertRaisesRegex(ValueError, "lacks dpkg status"):
            self.select()
        self.write_base()
        value = json.loads(self.runtime.read_bytes())
        value["features"]["include_fips"] = "n"
        self.runtime.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "invalid Make package manifest"):
            self.select()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    SelectAptPayloadsTest.policy_path = args.policy
    unittest.main(argv=[sys.argv[0], *remaining])
