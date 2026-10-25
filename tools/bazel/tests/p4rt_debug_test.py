#!/usr/bin/env python3
"""Exercise real GDB split-DWARF lookup; these fixtures do not build DEBs."""

from pathlib import Path
import io
import json
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.bazel.ci.verify_p4rt_debug import elf_sections, extract_member, verify_inventory, verify_pair


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "x86_64"
                     and all(shutil.which(tool) for tool in ("gcc", "dwp", "gdb", "objcopy")),
                     "Requires native AMD64 GCC, binutils and GDB")
class P4rtDebugTest(unittest.TestCase):
    """A populated DWP must independently supply matching function debug data."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "p4rt_app/p4rt.cc"
        self.source.parent.mkdir()
        self.source.write_text("int main(int argc, char** argv) {\n"
                               "  return argc + (argv[0][0] == 'x');\n}\n")
        self.binary, self.symbols, self.loose = self.compile(self.root / "build")

    def tool(self, *arguments):
        subprocess.run(arguments, cwd=self.root, check=True, capture_output=True, timeout=30)

    def compile(self, directory, indexed=False):
        directory.mkdir()
        binary, symbols, loose = directory / "p4rt", directory / "p4rt.dwp", directory / "p4rt.dwo"
        self.tool("gcc", "-x", "c++", "-O2", "-g", "-gdwarf-4", "-gsplit-dwarf",
                  *(["-ggnu-pubnames"] if indexed else []),
                  "-c", str(self.source), "-o", str(directory / "p4rt.o"))
        self.tool("gcc", str(directory / "p4rt.o"), "-o", str(binary),
                  *(["-fuse-ld=gold", "-Wl,--gdb-index"] if indexed else []))
        self.tool("dwp", str(loose), "-o", str(symbols))
        return binary, symbols, loose

    def test_matching_dwp_supplies_function_types_and_source_line(self):
        """Resolve main with the DWP after removing all loose DWO files."""
        self.loose.unlink()
        receipt = verify_pair(self.binary, self.symbols, self.source)
        self.assertEqual(receipt["status"], "passed")
        self.assertFalse(receipt["without_dwp"]["found"])
        self.assertTrue(receipt["with_dwp"]["file"].endswith("p4rt_app/p4rt.cc"))
        self.assertGreater(receipt["compilation_units"], 0)

    def test_existing_loose_dwo_cannot_make_check_pass(self):
        """Reject debug lookup through the absolute compilation directory."""
        with self.assertRaisesRegex(ValueError, "succeeds without DWP"):
            verify_pair(self.binary, self.symbols, self.source)

    def test_gnu_index_can_resolve_cpp_main_by_source_address(self):
        """Support Bazel's GNU index, which can store main under its C++ signature."""
        self.loose.unlink()
        binary, symbols, loose = self.compile(self.root / "indexed", indexed=True)
        loose.unlink()
        self.assertGreater(elf_sections(binary).get(".gdb_index", (0, 0))[1], 0)
        receipt = verify_pair(binary, symbols, self.source)
        self.assertTrue(receipt["with_dwp"]["found"])
        self.assertFalse(receipt["without_dwp"]["found"])

    def test_empty_dwp_is_not_a_valid_debug_package(self):
        """Catch the original zero-byte P4RT DWP regression."""
        self.symbols.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "nonempty AMD64 ELF"):
            verify_pair(self.binary, self.symbols, self.source)

    def test_unrelated_nonempty_dwp_cannot_supply_matching_symbols(self):
        """Matching sections alone do not prove the DWP belongs to this runtime."""
        self.loose.unlink()
        self.source.write_text("int main(int count, char** values) {\n"
                               "  volatile long different = 42;\n  return count + different;\n}\n")
        _, other_symbols, other_loose = self.compile(self.root / "other")
        other_loose.unlink()
        with self.assertRaisesRegex(ValueError, "DWP cannot resolve main"):
            verify_pair(self.binary, other_symbols, self.source)

    def test_missing_cu_index_is_rejected(self):
        """Require an index that lets GDB locate the packaged compilation units."""
        self.tool("objcopy", "--remove-section=.debug_cu_index", str(self.symbols))
        with self.assertRaisesRegex(ValueError, "populated .debug_cu_index"):
            verify_pair(self.binary, self.symbols, self.source)

    def test_empty_cu_index_header_is_rejected(self):
        """A named index section without indexed compilation units is unusable."""
        offset, _ = elf_sections(self.symbols)[".debug_cu_index"]
        with self.symbols.open("r+b") as stream:
            stream.seek(offset + 8)
            stream.write(struct.pack("<I", 0))
        with self.assertRaisesRegex(ValueError, "index is empty or invalid"):
            verify_pair(self.binary, self.symbols, self.source)

    def test_wrong_elf_architecture_is_rejected(self):
        """The existing P4RT package contract is AMD64."""
        with self.symbols.open("r+b") as stream:
            stream.seek(18)
            stream.write(struct.pack("<H", 183))
        with self.assertRaisesRegex(ValueError, "AMD64 ELF"):
            verify_pair(self.binary, self.symbols, self.source)

    def test_tar_cli_checks_payload_inventory_and_isolated_gdb(self):
        """Exercise TAR loading through the public CLI with real matching symbols."""
        from tools.bazel.ci.verify_p4rt_debug import RUNTIME_FILES, DEBUG_FILES

        self.loose.unlink()
        runtime, debug = self.root / "runtime.tar.bz2", self.root / "debug.tar.bz2"
        for archive_path, names, source in ((runtime, RUNTIME_FILES, self.binary),
                                            (debug, DEBUG_FILES, self.symbols)):
            with tarfile.open(archive_path, "w:bz2") as archive:
                for name in sorted(names):
                    member = archive.gettarinfo(source, arcname="./" + name)
                    member.mode, member.uid, member.gid = 0o755, 0, 0
                    with source.open("rb") as stream:
                        archive.addfile(member, stream)
        output = self.root / "receipt.json"
        self.tool(sys.executable, str(Path(__file__).resolve().parents[1] / "ci/verify_p4rt_debug.py"),
                  "--runtime-tar", str(runtime), "--debug-tar", str(debug),
                  "--source-root", str(self.root), "--output", str(output))
        receipt = json.loads(output.read_text())
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["payloads"]["runtime"], sorted(RUNTIME_FILES))
        self.assertFalse(receipt["without_dwp"]["found"])
        self.assertTrue(receipt["with_dwp"]["found"])


class TarPayloadTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / 'payload.tar.bz2'

    def archive(self, *members):
        with tarfile.open(self.package, 'w:bz2') as archive:
            for name, kind, mode in members:
                member = tarfile.TarInfo(name)
                member.mode, member.type = mode, kind
                if kind == tarfile.REGTYPE:
                    member.size = 7
                archive.addfile(member, io.BytesIO(b'payload') if member.isfile() else None)

    def test_copies_regular_owner_payload_without_archive_extraction(self):
        self.archive(('./usr/local/bin/p4rt', tarfile.REGTYPE, 0o755))
        self.assertEqual(verify_inventory(self.package, {'usr/local/bin/p4rt'}), ['usr/local/bin/p4rt'])
        destination = self.root / 'p4rt'
        extract_member(self.package, 'usr/local/bin/p4rt', destination)
        self.assertEqual(destination.read_bytes(), b'payload')

    def test_rejects_missing_duplicate_link_and_wrong_mode(self):
        for members in [(), (('usr/local/bin/p4rt', tarfile.REGTYPE, 0o644),),
                        (('usr/local/bin/p4rt', tarfile.SYMTYPE, 0o755),),
                        (('../p4rt', tarfile.REGTYPE, 0o755),),
                        (('usr/local/bin/p4rt', tarfile.REGTYPE, 0o755),) * 2]:
            with self.subTest(members=members):
                self.archive(*members)
                with self.assertRaises(ValueError):
                    verify_inventory(self.package, {'usr/local/bin/p4rt'})

    def test_legacy_deb_cli_keeps_its_decoder_and_receipt(self):
        from tools.bazel.ci import verify_p4rt_debug as verifier

        runtime, debug = self.root / 'runtime.input', self.root / 'debug.input'
        runtime.write_bytes(b'existing runtime input')
        debug.write_bytes(b'existing debug input')
        receipt = self.root / 'receipt.json'
        arguments = ['verify_p4rt_debug.py', '--runtime-deb', str(runtime),
                     '--debug-deb', str(debug), '--source-root', str(self.root),
                     '--output', str(receipt)]
        with mock.patch.object(sys, 'argv', arguments), \
                mock.patch.object(verifier, 'extract_deb_member') as decode_deb, \
                mock.patch.object(verifier, 'extract_member') as decode_tar, \
                mock.patch.object(verifier, 'verify_pair', return_value={'status': 'passed'}), \
                mock.patch('builtins.print'):
            verifier.main()
        self.assertEqual([call.args[:2] for call in decode_deb.call_args_list],
                         [(runtime, 'usr/local/bin/p4rt'), (debug, 'usr/local/bin/p4rt.dwp')])
        decode_tar.assert_not_called()
        self.assertEqual(json.loads(receipt.read_text()), {
            'status': 'passed', 'packages': {
                'runtime_sha256': verifier.sha256(runtime), 'debug_sha256': verifier.sha256(debug)}})

    def test_debug_cli_rejects_mixed_formats_before_reading_payloads(self):
        result = subprocess.run([sys.executable, str(ROOT / 'tools/bazel/ci/verify_p4rt_debug.py'),
                                 '--runtime-tar', 'missing.tar', '--debug-deb', 'missing.deb',
                                 '--source-root', '.', '--output', 'unused.json'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('must use the same archive format', result.stderr)


if __name__ == "__main__":
    unittest.main()
