#!/usr/bin/env python3
"""Check native package validation with a real linked ELF and detached symbols.

Run directly with Python; the test uses the host C compiler and binutils and
creates no Debian packages.
"""

import copy
import hashlib
import io
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

OWNER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OWNER / "bazel"))
import validate_native_packages as subject
import validate_payloads
import source_packages
import base_debug_symbols
from tools.bazel.tests.oci_base_fixture import digest, oci_files, tar_entries, write_layout


@unittest.skipUnless(platform.machine() == "x86_64" and all(shutil.which(name) for name in ("cc", "readelf", "objcopy")),
                     "native AMD64 C compiler and binutils are required")
class ValidateNativePackagesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-native-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original, self.runtime, self.symbols, self.identifier = self.compile(42, "first")
        _, self.ssh_runtime, _, _ = self.compile(0, "ssh", executable=True)
        self.library_path = "usr/lib/x86_64-linux-gnu/libsample.so.0.0.0"
        self.soname_path = "usr/lib/x86_64-linux-gnu/libsample.so.0"
        self.debug_path = "usr/lib/debug/.build-id/" + self.identifier[:2] + "/" + self.identifier[2:] + ".debug"
        self.runtime_manifest, self.debug_manifest = self.handoffs()

    def compile(self, value, stem, *, executable=False, soname="libsample.so.0"):
        source = self.root / (stem + ".c")
        function = "main" if executable else "sample"
        source.write_text(f"int {function}(void) {{ return {value}; }}\n")
        original = self.root / (stem + ".unstripped")
        flags = [] if executable else ["-shared", "-fPIC", "-Wl,-soname," + soname]
        subprocess.run(["cc", *flags, "-g", "-O0", "-Wl,--build-id=sha1",
                        str(source), "-o", str(original)], capture_output=True, check=True)
        notes = subprocess.check_output(["readelf", "-n", str(original)], text=True)
        identifier = re.search(r"Build ID: ([0-9a-f]+)", notes)[1]
        symbols = self.root / stem / (identifier[2:] + ".debug")
        symbols.parent.mkdir()
        subprocess.run(["objcopy", "--only-keep-debug", str(original), str(symbols)], capture_output=True, check=True)
        runtime = self.root / (stem + ".runtime")
        subprocess.run(["objcopy", "--strip-debug", "--add-gnu-debuglink=" + str(symbols), str(original), str(runtime)],
                       capture_output=True, check=True)
        return original, runtime, symbols, identifier

    def handoff(self, variant, package, entries, runtime=None):
        directory = self.root / (variant + "-handoff")
        directory.mkdir(parents=True, exist_ok=True)
        payload = directory / "payload.tar"
        def tar_bytes(members):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for name, data, link in members:
                    entry = tarfile.TarInfo(name)
                    if link is not None:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = link
                        entry.mode = 0o777
                        archive.addfile(entry)
                    else:
                        entry.size = len(data)
                        entry.mode = 0o755 if name.startswith("usr/bin/") else 0o644
                        archive.addfile(entry, io.BytesIO(data))
            return stream.getvalue()
        packages = [(package, "1.0", entries)]
        if variant == "runtime":
            packages.append(("openssh-client", "1.0+fips", [("usr/bin/ssh", self.ssh_runtime.read_bytes(), None)]))
        records, combined = [], []
        for name, version, members in packages:
            data = tar_bytes(members)
            records.append({"package": name, "version": version, "architecture": "amd64",
                "source_deb": name + "_" + version + "_amd64.deb", "source_size": 1,
                "source_sha256": hashlib.sha256(name.encode()).hexdigest(),
                "control_sha256": hashlib.sha256((name + " control").encode()).hexdigest(),
                "control_fields": {"Package": name, "Version": version, "Architecture": "amd64"},
                "payload_sha256": hashlib.sha256(data).hexdigest(), "payload_size": len(data), "payload_members": len(members)})
            combined.extend(members)
        payload.write_bytes(tar_bytes(combined))
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        value = {"schema": 1, "image": "docker-syncd-vs", "variant": variant, "architecture": "amd64",
                 "distribution": "trixie", "features": dict(validate_payloads.FEATURES),
                 "required_packages": [record["package"] for record in records], "debug_apt_packages": [], "packages": records,
                 "payload": {"path": "payload.tar", "sha256": digest, "size": payload.stat().st_size, "members": len(combined)}}
        if runtime:
            value["runtime_manifest_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
            value["debug_apt_packages"] = ["gdb", "gdbserver", "sshpass", "strace", "vim"]
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        return manifest

    def handoffs(self, *, library=None, symbols=None, symlink=True, package="libyang3"):
        runtime_entries = [(self.library_path, (library or self.runtime).read_bytes(), None)]
        if symlink:
            runtime_entries.append((self.soname_path, None, "libsample.so.0.0.0"))
        runtime = self.handoff("runtime", package, runtime_entries)
        debug_entries = ([(self.debug_path, (symbols or self.symbols).read_bytes(), None)] if symbols is not False else
                         [("usr/share/dummy/data", b"no symbols\n", None)])
        debug = self.handoff("debug", package + "-dbgsym", debug_entries, runtime)
        return runtime, debug

    def validate(self, *, required={"libyang3"}, sonames={"libyang3": "libsample.so.0"}, gaps=set(), **kwargs):
        return subject.validate_native(self.runtime_manifest, self.debug_manifest,
                                       required_packages=required, required_sonames=sonames,
                                       gap_packages=gaps | {"openssh-client"}, fixture=True, **kwargs)

    def source_archives(self, *, symbols_mutation=None, overlap=False):
        """Build the three owner tars and a real receipt around compiled sample ELFs."""
        records = copy.deepcopy(json.loads((OWNER / "bazel/source_packages.json").read_bytes())["packages"])
        runtime_entries, debug_entries = [], []
        source_dir = self.root / "source"
        source_dir.mkdir()

        def write_tar(path, entries):
            with tarfile.open(path, "w") as archive:
                for name, data, link in entries:
                    member = tarfile.TarInfo(name)
                    member.mode = 0o644 if link is None else 0o777
                    if link is not None:
                        member.type, member.linkname = tarfile.SYMTYPE, link
                        archive.addfile(member)
                    else:
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))

        for index, record in enumerate(records):
            package = record["package"]
            soname = package + ".so.0"
            _, runtime, symbols, identifier = self.compile(100 + index, package, soname=soname)
            name = "usr/lib/x86_64-linux-gnu/" + soname + ".0.0"
            if overlap and index == 0:
                name = self.library_path
            symbol_bytes = symbols.read_bytes()
            if symbols_mutation and index == 0:
                symbol_bytes = symbols_mutation(symbol_bytes)
            runtime_members = [(name, runtime.read_bytes(), None),
                               ("usr/lib/x86_64-linux-gnu/" + soname, None, Path(name).name)]
            debug_members = [("usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug",
                              symbol_bytes, None)]
            runtime_path, debug_path = source_dir / (package + ".tar"), source_dir / (package + "-debug.tar")
            write_tar(runtime_path, runtime_members)
            write_tar(debug_path, debug_members)
            record.pop("required_paths")
            record.update(input_tar_sha256=subject.sha(runtime_path), files=source_packages.inventory(runtime_path))
            runtime_entries.extend(runtime_members)
            debug_entries.extend(debug_members)
        runtime_tar, debug_tar = source_dir / "runtime.tar", source_dir / "debug.tar"
        write_tar(runtime_tar, runtime_entries)
        write_tar(debug_tar, debug_entries)
        receipt = {**source_packages.IDENTITY, "schema": source_packages.RECEIPT_SCHEMA,
                   "kind": "bazel_source", "packages": records,
                   "contract_sha256": subject.sha(OWNER / "bazel/source_packages.json"),
                   "base_manifest_digest": "sha256:" + "b" * 64,
                   "module_file_sha256": {name: "a" * 64 for name in ("sonic-swss-common", "sonic-sairedis")}}
        receipt["payload"] = {"sha256": subject.sha(runtime_tar), "size": runtime_tar.stat().st_size,
                              "members": len(source_packages.inventory(runtime_tar))}
        receipt_path = source_dir / "receipt.json"
        receipt_path.write_text(json.dumps(receipt))
        return {"source_runtime_tar": runtime_tar, "source_debug_tar": debug_tar, "source_receipt": receipt_path}

    def source_validate(self, inputs):
        required = {"libyang3", *source_packages.PACKAGES}
        sonames = {name: name + ".so.0" for name in source_packages.PACKAGES}
        return self.validate(required=required, sonames={"libyang3": "libsample.so.0", **sonames}, **inputs)

    def test_source_libraries_keep_owner_provenance_and_matching_symbols(self):
        """The reused owner outputs join Make ELFs without losing source or symbol identity."""
        inputs = self.source_archives()
        result = self.source_validate(inputs)
        source_pairs = [item for item in result["paired_symbols"] if item.get("origin") == "bazel_source"]
        self.assertEqual({item["package"] for item in source_pairs}, set(source_packages.PACKAGES))
        self.assertEqual(result["source_receipt_sha256"], subject.sha(inputs["source_receipt"]))
        for pair in source_pairs:
            self.assertEqual(pair["source"]["commit"], next(item["source"]["commit"] for item in
                             result["source_packages"]["packages"] if item["package"] == pair["package"]))

    def test_source_symbol_crc_is_checked_even_with_a_matching_receipt(self):
        """A receipt with updated hashes cannot disguise mismatched compiler debug output."""
        inputs = self.source_archives(symbols_mutation=lambda value: value + b"changed bytes")
        with self.assertRaisesRegex(ValueError, "debuglink CRC differs"):
            self.source_validate(inputs)

    def test_source_symbols_require_the_same_runtime_build_id(self):
        """An unrelated symbol ELF cannot satisfy a reused library's build-ID path."""
        inputs = self.source_archives(symbols_mutation=lambda value: self.symbols.read_bytes())
        with self.assertRaisesRegex(ValueError, "does not match runtime build ID"):
            self.source_validate(inputs)

    def test_source_tar_hash_and_make_path_ownership_are_checked(self):
        """Reject tampered owner archives and source paths already supplied by a Make package."""
        inputs = self.source_archives(overlap=True)
        with self.assertRaisesRegex(ValueError, "source package overlaps a Make payload"):
            self.source_validate(inputs)
        inputs["source_runtime_tar"].write_bytes(inputs["source_runtime_tar"].read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "source receipt archive hash differs"):
            self.source_validate(inputs)

    def test_production_native_validation_requires_all_source_inputs(self):
        """Complete validation cannot accidentally report only the remaining Make packages."""
        with self.assertRaisesRegex(ValueError, "native validation requires source"):
            subject.validate_native(self.runtime_manifest, self.debug_manifest)
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            self.validate(source_runtime_tar=self.runtime)

    def inherited_base_fixture(self, *, wrong_alternate_id=False):
        """Construct an OCI base ELF and its real debuglink/DWZ companion relationship."""
        _, _, dwz, dwz_id = self.compile(91, "dwz")
        dwz_name = "usr/lib/debug/.dwz/x86_64-linux-gnu/libswsscommon.debug"
        alternate = self.root / "alternate-link"
        alternate.write_bytes(b"../../.dwz/x86_64-linux-gnu/libswsscommon.debug\0" +
                              bytes.fromhex("0" * 40 if wrong_alternate_id else dwz_id))
        linked = self.symbols.with_suffix(".linked")
        subprocess.run(["objcopy", "--add-section", ".gnu_debugaltlink=" + str(alternate), str(self.symbols), str(linked)],
                       capture_output=True, check=True)
        linked.replace(self.symbols)
        subprocess.run(["objcopy", "--strip-debug", "--add-gnu-debuglink=" + str(self.symbols),
                        str(self.original), str(self.runtime)], capture_output=True, check=True)
        data = tar_entries([(self.library_path, self.runtime.read_bytes(), 0o644)])
        base_tar = self.root / "base.tar"
        base_tar.write_bytes(data)
        base = self.root / "base.oci"
        config = {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": [digest(data)]}}
        write_layout(base, oci_files(json.dumps(config).encode(), [data]))
        receipt = {"base_manifest_digest": json.loads((base / "index.json").read_bytes())["manifests"][0]["digest"],
                   "inherited_files": source_packages.inventory(base_tar)}
        record = dict(json.loads(self.debug_manifest.read_bytes())["packages"][0], package="libswsscommon-dbgsym")
        contract = {"schema": 1, "package": {key: value for key, value in record.items() if not key.startswith("payload_")},
                    "original_payload": {key: record["payload_" + key] for key in ("sha256", "size", "members")},
                    "runtime": {"path": self.library_path, "sha256": subject.sha(self.runtime), "build_id": self.identifier},
                    "files": {}}
        for name, file, identifier in ((self.debug_path, self.symbols, self.identifier), (dwz_name, dwz, dwz_id)):
            contract["files"][name] = {"sha256": subject.sha(file), "size": file.stat().st_size,
                                        "mode": 0o644, "uid": 0, "gid": 0, "build_id": identifier}
        contract_path = self.root / "base-symbol-contract.json"
        contract_path.write_text(json.dumps(contract))
        record.update({"original_payload_" + key: value for key, value in contract["original_payload"].items()})
        record.update(payload_members=2, base_debug_symbols=base_debug_symbols.descriptor(contract_path))
        manifest = self.root / "base-symbol-manifest.json"
        manifest.write_text(json.dumps({"packages": [record]}))
        debug = {name: {"kind": "file", "file": file, "package": base_debug_symbols.PACKAGE}
                 for name, file in ((self.debug_path, self.symbols), (dwz_name, dwz))}
        return base, receipt, debug, manifest, contract_path

    def test_inherited_base_symbol_pair_keeps_its_dwz_companion(self):
        """The retained base helper keeps complete symbols without retaining old source-library companions."""
        base, receipt, debug, manifest, contract = self.inherited_base_fixture()
        with mock.patch.object(base_debug_symbols, "CONTRACT", contract):
            pairs = subject.validate_inherited_base(base, receipt, debug, manifest, self.root,
                                                    readelf="readelf", objcopy="objcopy")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["build_id"], self.identifier)
        self.assertEqual(pairs[0]["origin"], "inherited_base")
        self.assertTrue(pairs[0]["dwz_build_id"])

    def test_inherited_base_symbols_reject_a_wrong_dwz_link(self):
        """Even valid archive hashes and debuglink CRC must not admit an unrelated DWZ identifier."""
        base, receipt, debug, manifest, contract = self.inherited_base_fixture(wrong_alternate_id=True)
        with mock.patch.object(base_debug_symbols, "CONTRACT", contract):
            with self.assertRaisesRegex(ValueError, "alternate link differs"):
                subject.validate_inherited_base(base, receipt, debug, manifest, self.root,
                                                 readelf="readelf", objcopy="objcopy")

    def test_real_runtime_and_symbols_match(self):
        result = self.validate()
        self.assertEqual(result["elf_count"], 2)
        self.assertEqual(len(result["paired_symbols"]), 1)
        self.assertEqual(result["paired_symbols"][0]["build_id"], self.identifier)
        self.assertEqual(result["paired_symbols"][0]["soname"], "libsample.so.0")
        self.assertIn("openssh-client", subject.GAP_PACKAGES)
        self.assertEqual([item["package"] for item in result["preserved_make_debug_gaps"]], ["openssh-client"])
        ssh = result["preserved_make_debug_gaps"][0]
        self.assertFalse(ssh["has_dwarf"])
        self.assertTrue(ssh["has_debuglink"])
        self.assertIn("no matching symbol package", ssh["reason"])

    def test_missing_soname_link_is_rejected(self):
        self.runtime_manifest, self.debug_manifest = self.handoffs(symlink=False)
        with self.assertRaisesRegex(ValueError, "missing library or debug link target"):
            self.validate()

    def test_different_debug_build_id_is_rejected(self):
        _, _, other, _ = self.compile(43, "second")
        self.runtime_manifest, self.debug_manifest = self.handoffs(symbols=other)
        with self.assertRaisesRegex(ValueError, "does not match runtime build ID"):
            self.validate()

    def test_debuglink_checksum_is_required(self):
        altered = self.root / "altered.debug"
        altered.write_bytes(self.symbols.read_bytes() + b"changed debug bytes")
        self.runtime_manifest, self.debug_manifest = self.handoffs(symbols=altered)
        with self.assertRaisesRegex(ValueError, "debuglink CRC differs"):
            self.validate()

    def test_required_symbols_cannot_be_omitted(self):
        self.runtime_manifest, self.debug_manifest = self.handoffs(symbols=False)
        with self.assertRaisesRegex(ValueError, "missing matching debug symbols"):
            self.validate()

    def test_embedded_and_existing_make_gap_coverage_are_distinguished(self):
        self.runtime_manifest, self.debug_manifest = self.handoffs(library=self.original, symbols=False, package="p4lang-pi")
        result = self.validate(required=set(), sonames={}, gaps={"p4lang-pi"})
        self.assertEqual(len(result["embedded_symbols"]), 1)
        self.assertEqual([item["package"] for item in result["preserved_make_debug_gaps"]], ["openssh-client"])
        self.runtime_manifest, self.debug_manifest = self.handoffs(symbols=False, package="p4lang-pi")
        result = self.validate(required=set(), sonames={}, gaps={"p4lang-pi"})
        self.assertEqual({item["package"] for item in result["preserved_make_debug_gaps"]}, {"p4lang-pi", "openssh-client"})
        self.assertEqual(result["embedded_symbols"], [])


if __name__ == "__main__":
    unittest.main()
