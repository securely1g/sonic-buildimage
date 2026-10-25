#!/usr/bin/env python3
"""Check P4RT runtime/debug payload validation and the legacy DEB interface."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.verify_p4rt_debug import extract_member, verify_inventory


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


if __name__ == '__main__':
    unittest.main()
