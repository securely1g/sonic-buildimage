#!/usr/bin/env python3
"""Check native package validation with a real linked ELF and detached symbols.

Run directly with Python; the test uses the host C compiler and binutils and
creates no Debian packages.
"""

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

OWNER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OWNER / "bazel"))
import validate_native_packages as subject
import validate_payloads


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

    def compile(self, value, stem, *, executable=False):
        source = self.root / (stem + ".c")
        function = "main" if executable else "sample"
        source.write_text(f"int {function}(void) {{ return {value}; }}\n")
        original = self.root / (stem + ".unstripped")
        flags = [] if executable else ["-shared", "-fPIC", "-Wl,-soname,libsample.so.0"]
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

    def handoffs(self, *, library=None, symbols=None, symlink=True, package="libsairedis"):
        runtime_entries = [(self.library_path, (library or self.runtime).read_bytes(), None)]
        if symlink:
            runtime_entries.append((self.soname_path, None, "libsample.so.0.0.0"))
        runtime = self.handoff("runtime", package, runtime_entries)
        debug_entries = ([(self.debug_path, (symbols or self.symbols).read_bytes(), None)] if symbols is not False else
                         [("usr/share/dummy/data", b"no symbols\n", None)])
        debug = self.handoff("debug", package + "-dbgsym", debug_entries, runtime)
        return runtime, debug

    def validate(self, *, required={"libsairedis"}, sonames={"libsairedis": "libsample.so.0"}, gaps=set()):
        return subject.validate_native(self.runtime_manifest, self.debug_manifest,
                                       required_packages=required, required_sonames=sonames,
                                       gap_packages=gaps | {"openssh-client"})

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
