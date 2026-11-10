"""Small byte fixtures only; no real cache, package tool, or DEB generation."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import vs_cache_payloads as payloads


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, body, kind in entries:
            info = tarfile.TarInfo(name)
            info.size = len(body) if kind == tarfile.REGTYPE else 0
            info.type = kind
            if kind == tarfile.SYMTYPE:
                info.linkname = '/outside'
            archive.addfile(info, io.BytesIO(body) if info.size else None)
    return output.getvalue()


class CachePayloadTests(unittest.TestCase):
    def fixture(self, entries=None):
        return archive_bytes(entries or [('target/example.bin', b'original bytes', tarfile.REGTYPE)])

    def test_selected_bytes_and_log_hash_without_raw_contents(self):
        secret = b'private log content that must not appear in JSON'
        blob = self.fixture([('target/example.bin', b'original bytes', tarfile.REGTYPE),
                             ('target/example.cached.log', secret, tarfile.REGTYPE)])
        result = payloads.inspect_stream(io.BytesIO(blob), ['target/example.bin'], payloads.Budget())
        self.assertEqual(result['missing_expected_paths'], [])
        self.assertEqual(result['members'][0]['sha256'], hashlib.sha256(b'original bytes').hexdigest())
        self.assertEqual(result['members'][1]['sha256'], hashlib.sha256(secret).hexdigest())
        self.assertNotIn(secret.decode(), json.dumps(result))

    def test_missing_member_is_reported_not_claimed_present(self):
        result = payloads.inspect_stream(io.BytesIO(self.fixture()), ['target/missing.bin'], payloads.Budget())
        self.assertEqual(result['missing_expected_paths'], ['target/missing.bin'])

    def test_traversal_and_absolute_members_rejected(self):
        for name in ('/target/example.bin', 'target/../example.bin', 'other/example.bin'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                payloads.inspect_stream(io.BytesIO(self.fixture([(name, b'x', tarfile.REGTYPE)])), [], payloads.Budget())

    def test_duplicate_and_link_members_rejected(self):
        regular = ('target/example.bin', b'x', tarfile.REGTYPE)
        for entries in ([regular, regular], [('target/example.bin', b'', tarfile.SYMTYPE)],
                        [('target/example.bin', b'', tarfile.LNKTYPE)]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                payloads.inspect_stream(io.BytesIO(self.fixture(entries)), [], payloads.Budget())

    def test_expanded_budget_and_deadline_enforced(self):
        for budget in (payloads.Budget(expanded=10), payloads.Budget(seconds=-1)):
            with self.assertRaises((ValueError, TimeoutError)):
                payloads.inspect_stream(io.BytesIO(self.fixture()), [], budget)

    def test_bad_gzip_trailer_rejected(self):
        broken = bytearray(self.fixture()); broken[-8] ^= 0xff
        with self.assertRaises((OSError, EOFError)):
            payloads.inspect_stream(io.BytesIO(broken), [], payloads.Budget())

    def test_buffered_and_large_nonzero_tar_tails_rejected(self):
        original = gzip.decompress(self.fixture())
        second = gzip.decompress(self.fixture([('target/second.bin', b'other', tarfile.REGTYPE)]))
        # Short padding places the tail inside tarfile's first read buffer;
        # normal padding and a large gap cover later underlying reads too.
        first_end = original[:2048]
        for raw in (first_end + b'not-padding', original + second,
                    original + b'\0' * (2 * payloads.CHUNK) + b'not-padding'):
            with self.subTest(size=len(raw)), self.assertRaisesRegex(ValueError, 'Nonzero data'):
                payloads.inspect_stream(io.BytesIO(gzip.compress(raw)), [], payloads.Budget())

    def test_invalid_later_header_and_missing_end_marker_rejected(self):
        first_member = gzip.decompress(self.fixture())[:1024]
        for raw in (first_member, first_member + b'X' * 512 + b'\0' * 1024,
                    first_member + b'partial-header'):
            with self.subTest(size=len(raw)), self.assertRaisesRegex(ValueError, 'TAR header'):
                payloads.inspect_stream(io.BytesIO(gzip.compress(raw)), [], payloads.Budget())

    def object_fixture(self, root):
        path = root / ('sample-' + 'a' * 23 + '-' + 'b' * 23 + '.tgz')
        path.write_bytes(self.fixture())
        return path, {'name': path.name, 'expected_paths': ['target/example.bin'],
                      'prior_metadata': payloads.observation.metadata(path.stat())}

    def test_object_observation_does_not_rewrite_cache(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path, row = self.object_fixture(root)
            before, info = path.read_bytes(), path.stat()
            fd = os.open(root, payloads.observation.DIRECTORY_FLAGS)
            try:
                result = payloads.inspect_object(fd, row, payloads.Budget())
            finally:
                os.close(fd)
            self.assertEqual(result['status'], 'CURRENT_BYTES_OBSERVED_NOT_ADMITTED')
            self.assertEqual(result['sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(payloads.stable(path.stat()), payloads.stable(info))

    def test_symlink_object_is_not_followed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path, row = self.object_fixture(root)
            saved = root / 'saved'; path.rename(saved); path.symlink_to(saved)
            fd = os.open(root, payloads.observation.DIRECTORY_FLAGS)
            try:
                result = payloads.inspect_object(fd, row, payloads.Budget())
            finally:
                os.close(fd)
            self.assertEqual(result['status'], 'not_fully_observed')
            self.assertNotIn('sha256', result)

    def test_mutation_during_observation_discards_partial_result(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path, row = self.object_fixture(root)
            original = payloads.inspect_stream
            def changed(*args):
                result = original(*args)
                with path.open('ab') as stream:
                    stream.write(b'changed')
                return result
            fd = os.open(root, payloads.observation.DIRECTORY_FLAGS)
            try:
                with mock.patch.object(payloads, 'inspect_stream', changed):
                    result = payloads.inspect_object(fd, row, payloads.Budget())
            finally:
                os.close(fd)
            self.assertEqual(result['status'], 'not_fully_observed')
            self.assertNotIn('members', result)
            self.assertNotIn('sha256', result)

    def test_manifest_hash_and_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            payloads.strict_json('{"objects":[],"objects":[]}')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); _, row = self.object_fixture(root)
            path = root / 'selection.json'
            raw = json.dumps({'schema': 1, 'root': '/dpkg_cache', 'objects': [row]}).encode()
            path.write_bytes(raw)
            selected = payloads.manifest(path, hashlib.sha256(raw).hexdigest())
            self.assertEqual(selected['objects'][0]['name'], row['name'])
            with self.assertRaises(ValueError):
                payloads.manifest(path, '0' * 64)


if __name__ == '__main__':
    unittest.main()
