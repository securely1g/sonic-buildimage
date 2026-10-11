"""Exercise source/base validation with real native ELF pairs and OCI layers."""

import copy
import hashlib
import io
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel.oci import source_base_validation as check
from tools.bazel.tests import oci_base_fixture as fixture


def sha(data):
    return hashlib.sha256(data).hexdigest()


def layer(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, value in entries.items():
            data, attrs = value if isinstance(value, tuple) else (value, {})
            item = tarfile.TarInfo(name)
            item.mode = 0o644
            item.size = len(data)
            for key, value in attrs.items():
                setattr(item, key, value)
            archive.addfile(item, io.BytesIO(data) if item.isfile() else None)
    return stream.getvalue()


class SourceBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
        cls.triplet = check.MACHINES[cls.native][1]
        cls.temporary = tempfile.TemporaryDirectory(prefix="source-base-test-elf-")
        cls.build = Path(cls.temporary.name)
        cls.pairs = {}
        for name, linked in (("ordinary", True), ("sealed", False), ("replacement", True)):
            source = cls.build / (name + ".c")
            source.write_text('#include <stdio.h>\nint ' + name + '(void) {return puts("' + name + '");}\n')
            binary = cls.build / (name + ".so")
            subprocess.run(["cc", "-g", "-shared", "-fPIC", "-Wl,--build-id=sha1",
                            "-Wl,-soname," + name + ".so", str(source), "-o", str(binary)], check=True)
            notes = subprocess.check_output(["readelf", "-n", binary], text=True)
            identifier = re.search(r"Build ID: ([0-9a-f]+)", notes)[1]
            symbols = cls.build / (identifier[2:] + ".debug")
            subprocess.run(["objcopy", "--only-keep-debug", binary, symbols], check=True)
            subprocess.run(["strip", "--strip-unneeded", binary], check=True)
            if linked:
                subprocess.run(["objcopy", "--add-gnu-debuglink=" + str(symbols), binary], check=True)
            cls.pairs[name] = (binary.read_bytes(), symbols.read_bytes(), identifier)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-base-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ordinary = "usr/lib/" + self.triplet + "/libswsscommon.so.0"
        self.sealed = "usr/lib/" + self.triplet + "/libsymcrypt.so.103.13.0"
        self.files, self.symbols = {}, {}
        packages = []
        for key, name in (("ordinary", self.ordinary), ("sealed", self.sealed)):
            binary, symbols, identifier = self.pairs[key]
            self.files[name] = binary
            self.symbols["usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug"] = symbols
            packages.append({"package": "symcrypt-openssl" if key == "sealed" else "libswsscommon",
                             "version": "1.11.0" if key == "sealed" else "1.0",
                             "target": "@sonic_fips//symcrypt:runtime" if key == "sealed" else "@common//:runtime",
                             "source_revision": "a" * 40, "payload_sha256": "b" * 64,
                             "native_files": [{"path": name, "sha256": sha(binary), "mode": 0o644, "uid": 0, "gid": 0}]})
        self.reports = [{"schema": 1, "architecture": self.native,
                         "dependency_check": {"status": "satisfied"}, "source_packages": [p]} for p in packages]
        self.runtime_tail = []
        self.debug_tail = []
        self.replacing = None

    def run_validation(self):
        metadata = []
        files = copy.deepcopy(self.files)
        for index, (name, report) in enumerate(zip(check.METADATA_NAMES, self.reports)):
            raw = json.dumps(report).encode()
            path = self.root / (str(index) + ".json")
            path.write_bytes(raw)
            metadata.append(path)
            files[name] = raw
        runtime_layers = [layer(files), *self.runtime_tail]
        debug_layers = [*runtime_layers, layer(self.symbols), *self.debug_tail]
        for name, layers in (("runtime", runtime_layers), ("debug", debug_layers)):
            config = {"architecture": self.native, "os": "linux", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": [fixture.digest(x) for x in layers]}}
            fixture.write_layout(self.root / name, fixture.oci_files(json.dumps(config).encode(), layers))
        symbols = self.root / "symbols.tar"
        symbols.write_bytes(layer(self.symbols))
        replacing = None
        if self.replacing is not None:
            replacing = self.root / "replacement.tar"
            replacing.write_bytes(layer(self.replacing))
        return check.validate_source_base(self.root / "runtime", self.root / "debug",
                                          metadata_paths=metadata, source_debug_tar=symbols,
                                          architecture=self.native, replacing_runtime_tar=replacing)

    def test_native_pairs_and_declared_dependencies(self):
        result = self.run_validation()
        self.assertEqual(result["elf_count"], 2)
        self.assertEqual({p["path"] for p in result["paired_symbols"]}, {self.ordinary, self.sealed})
        self.assertIn("libc.so.6", result["external_needed"])
        self.assertRegex(result["runtime_manifest_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(result["debug_manifest_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_read_only_source_elf(self):
        self.files[self.ordinary] = (self.files[self.ordinary], {"mode": 0o444})
        self.reports[0]["source_packages"][0]["native_files"][0]["mode"] = 0o444
        self.assertEqual(self.run_validation()["status"], "passed")

    def test_foreign_registry_revision(self):
        self.reports[0]["source_packages"][0]["source_revision"] = "3.7.0.sonic.3"
        self.assertEqual(self.run_validation()["status"], "passed")

    def test_runtime_hash_and_metadata_changes(self):
        for attrs in ({"mode": 0o755}, {"uid": 123}, {"gid": 456}):
            with self.subTest(attrs=attrs):
                self.runtime_tail = [layer({self.ordinary: (self.pairs["ordinary"][0], attrs)})]
                with self.assertRaisesRegex(ValueError, "changed declared source"):
                    self.run_validation()
        self.runtime_tail = [layer({self.ordinary: self.pairs["ordinary"][0] + b"tamper"})]
        with self.assertRaisesRegex(ValueError, "changed declared source"):
            self.run_validation()

    def test_debug_image_cannot_change_inherited_runtime(self):
        self.debug_tail = [layer({self.ordinary: self.pairs["replacement"][0]})]
        with self.assertRaisesRegex(ValueError, "changed declared source"):
            self.run_validation()

    def test_embedded_receipt_matches_declared_input(self):
        self.runtime_tail = [layer({check.METADATA_NAMES[0]: b"{}"})]
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            self.run_validation()

    def test_whiteout_removes_source_elf(self):
        name = str(Path(self.ordinary).parent / (".wh." + Path(self.ordinary).name))
        self.runtime_tail = [layer({name: b""})]
        with self.assertRaisesRegex(ValueError, "changed declared source"):
            self.run_validation()

    def test_parent_symlink_cannot_hide_source_elf(self):
        self.runtime_tail = [layer({"usr/lib/" + self.triplet: (b"", {"type": tarfile.SYMTYPE, "linkname": "/other"})})]
        with self.assertRaisesRegex(ValueError, "path is redirected"):
            self.run_validation()

    def test_missing_debug_file(self):
        self.symbols.pop(next(iter(self.symbols)))
        with self.assertRaisesRegex(ValueError, "lacks collected symbols"):
            self.run_validation()

    def test_symbol_architecture(self):
        name = next(iter(self.symbols))
        raw = bytearray(self.symbols[name]); raw[18:20] = b"\x28\x00"
        self.symbols[name] = bytes(raw)
        with self.assertRaisesRegex(ValueError, "debug ELF architecture differs"):
            self.run_validation()

    def test_debuglink_crc_rejects_changed_symbols_even_with_same_build_id(self):
        name = next(iter(self.symbols))
        self.symbols[name] += b"changed bytes with original build ID"
        with self.assertRaisesRegex(ValueError, "CRC differs"):
            self.run_validation()

    def test_ordinary_elf_requires_debuglink(self):
        self.files[self.ordinary] = self.pairs["sealed"][0]
        self.reports[0]["source_packages"][0]["native_files"][0]["sha256"] = sha(self.files[self.ordinary])
        with self.assertRaisesRegex(ValueError, "ordinary source ELF lacks"):
            self.run_validation()

    def test_sealed_exception_requires_exact_source_identity(self):
        package = self.reports[1]["source_packages"][0]
        for key in ("package", "target", "version", "source_revision"):
            with self.subTest(key=key):
                old = package[key]; package[key] = "1.0.sonic.1" if key == "source_revision" else "wrong"
                with self.assertRaisesRegex(ValueError, "sealed core lacks"):
                    self.run_validation()
                package[key] = old

    def test_sealed_runtime_bytes_are_checked(self):
        self.runtime_tail = [layer({self.sealed: self.pairs["sealed"][0] + b"bad digest"})]
        with self.assertRaisesRegex(ValueError, "changed declared source"):
            self.run_validation()

    def test_replacement_is_explicit_and_constrained(self):
        self.replacing = {self.ordinary: self.pairs["replacement"][0]}
        self.runtime_tail = [layer(self.replacing)]
        result = self.run_validation()
        self.assertEqual(result["replaced_source_files"][0]["path"], self.ordinary)
        self.assertEqual(result["elf_count"], 1)
        self.replacing = {self.sealed: self.pairs["sealed"][0]}
        with self.assertRaisesRegex(ValueError, "outside the three reviewed"):
            self.run_validation()


if __name__ == "__main__":
    unittest.main()
