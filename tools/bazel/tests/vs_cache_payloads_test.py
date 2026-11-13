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


class WorkspaceHostDebTests(unittest.TestCase):
    def fixture(self, root):
        directory = root / 'target/debs/trixie'
        directory.mkdir(parents=True)
        path = directory / payloads.WORKSPACE_DEBS[0]
        path.write_bytes(b'literal fixture only, not a DEB')
        device = path.stat().st_dev
        mounts = [{'mountpoint': '/', 'device': f'{os.major(device)}:{os.minor(device)}',
                   'filesystem': 'fixture', 'mount_root_sha256': '0' * 64}]
        return path, mounts

    def test_exact_literal_hash_and_unrelated_paths_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path, mounts = self.fixture(root)
            before, info = path.read_bytes(), path.stat()
            with mock.patch.object(payloads.os, 'scandir', side_effect=AssertionError('No scan')):
                result = payloads.inspect_workspace_debs(str(root), mounts, payloads.Budget())
            first, second = result['paths']
            self.assertEqual(first['status'], 'CURRENT_WORKSPACE_BYTES_OBSERVED_NOT_ADMITTED')
            self.assertEqual(first['sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(first['workspace'], str(root))
            self.assertEqual(second['status'], 'missing_at_this_exact_path')
            self.assertFalse(result['missing_is_global_absence'])
            self.assertNotIn(before.decode(), json.dumps(result))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(payloads.stable(path.stat()), payloads.stable(info))

    def test_missing_workspace_and_missing_parent_are_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for workspace in (root, root / 'absent'):
                result = payloads.inspect_workspace_debs(str(workspace), [], payloads.Budget())
                # For an existing workspace, the covering mount must be known.
                if workspace == root:
                    device = root.stat().st_dev
                    mounts = [{'mountpoint': '/', 'device': f'{os.major(device)}:{os.minor(device)}'}]
                    result = payloads.inspect_workspace_debs(str(workspace), mounts, payloads.Budget())
                self.assertEqual([r['status'] for r in result['paths']],
                                 ['missing_at_this_exact_path'] * 2)
                self.assertTrue(all('sha256' not in r for r in result['paths']))

    def test_leaf_and_parent_symlinks_are_never_followed(self):
        for component in ('file', 'target'):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); path, mounts = self.fixture(root)
                selected = path if component == 'file' else root / 'target'
                saved = root / 'saved'; selected.rename(saved); selected.symlink_to(saved)
                with mock.patch.object(payloads.os, 'read', side_effect=AssertionError('No payload read')):
                    result = payloads.inspect_workspace_deb(str(root), payloads.WORKSPACE_DEBS[0], mounts, payloads.Budget())
                self.assertEqual(result['status'], 'symlink_rejected')
                self.assertNotIn('sha256', result)

    def test_nonregular_and_size_limit_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path, mounts = self.fixture(root)
            with mock.patch.object(payloads, 'MAX_ARCHIVE', 1), mock.patch.object(payloads.os, 'read', side_effect=AssertionError('No read')):
                result = payloads.inspect_workspace_deb(str(root), path.name, mounts, payloads.Budget())
            self.assertEqual(result['status'], 'size_limit')
            path.unlink(); path.mkdir()
            with mock.patch.object(payloads.os, 'read', side_effect=AssertionError('No read')):
                result = payloads.inspect_workspace_deb(str(root), path.name, mounts, payloads.Budget())
            self.assertEqual(result['status'], 'non_regular')
            self.assertNotIn('sha256', result)

    def test_changed_bytes_and_replaced_named_inode_discard_hash(self):
        for operation in ('append', 'replace'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); path, mounts = self.fixture(root)
                original_read = os.read
                def change(fd, size):
                    data = original_read(fd, size)
                    if operation == 'append':
                        with path.open('ab') as stream:
                            stream.write(b'changed')
                    else:
                        path.rename(root / 'old'); path.write_bytes(b'replaced')
                    return data
                with mock.patch.object(payloads.os, 'read', change):
                    result = payloads.inspect_workspace_deb(str(root), path.name, mounts, payloads.Budget())
                self.assertEqual(result['status'], 'not_fully_observed')
                self.assertNotIn('sha256', result)


class BootstrapBuildHooksTests(unittest.TestCase):
    def fixture(self, root):
        workspace = root / 'workspace'
        installed = root / 'installed'
        source = workspace / payloads.BUILD_HOOKS_WORKSPACE_PATH
        destination = installed / payloads.BUILD_HOOKS_DEB
        for path, data in ((source, b'workspace literal fixture'),
                           (destination, b'installed literal fixture')):
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
        device = root.stat().st_dev
        mounts = [{'mountpoint': '/', 'device': f'{os.major(device)}:{os.minor(device)}'}]
        return workspace, installed, source, destination, mounts

    def test_distinguishes_existing_copies_without_extracting_or_changing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, installed, source, destination, mounts = self.fixture(Path(temporary))
            originals = [(path, path.read_bytes(), payloads.stable(path.stat()))
                         for path in (source, destination)]
            with mock.patch.object(payloads, 'BUILD_HOOKS_INSTALLED_BASE', str(installed)), \
                 mock.patch.object(payloads.os, 'scandir', side_effect=AssertionError('No scan')):
                result = payloads.inspect_build_hook_debs(str(workspace), mounts, payloads.Budget())
            for key, (path, data, metadata) in zip(
                    ('workspace_copy', 'installed_buildinfo_copy'), originals):
                record = result[key]
                self.assertEqual(record['status'], 'CURRENT_BYTES_OBSERVED_NOT_ADMITTED')
                self.assertEqual(Path(record['base']) / record['relative_path'], path)
                self.assertEqual(record['sha256'], hashlib.sha256(data).hexdigest())
                self.assertNotIn(data.decode(), json.dumps(result))
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(payloads.stable(path.stat()), metadata)
            self.assertFalse(result['package_format_validated'])
            self.assertFalse(result['contents_copied_or_extracted'])
            self.assertEqual(result['source_compatibility'], 'not_established')

    def test_missing_workspace_copy_and_installed_parent_symlink_do_not_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, installed, source, destination, mounts = self.fixture(root)
            source.unlink()
            saved = root / 'saved'; installed.rename(saved); installed.symlink_to(saved)
            with mock.patch.object(payloads, 'BUILD_HOOKS_INSTALLED_BASE', str(installed)), \
                 mock.patch.object(payloads.os, 'read', side_effect=AssertionError('No read')):
                result = payloads.inspect_build_hook_debs(str(workspace), mounts, payloads.Budget())
            self.assertEqual(result['workspace_copy']['status'], 'missing_at_this_exact_path')
            self.assertEqual(result['installed_buildinfo_copy']['status'], 'symlink_rejected')
            self.assertFalse(result['missing_is_global_absence'])
            self.assertTrue(all('sha256' not in result[key]
                                for key in ('workspace_copy', 'installed_buildinfo_copy')))

    def test_undeclared_relative_paths_are_rejected_before_open(self):
        with mock.patch.object(payloads.os, 'open', side_effect=AssertionError('No open')):
            for path in ('../' + payloads.BUILD_HOOKS_DEB, '/etc/passwd',
                         'src/sonic-build-hooks/' + payloads.BUILD_HOOKS_DEB):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    payloads._inspect_declared_deb('/workspace', path, [], payloads.Budget())


class DockerObjectSelectionTests(unittest.TestCase):
    def test_real_original_size_preserves_object_and_batch_bounds(self):
        name = "docker-platform-monitor.gz-b75d1522d43834e57856897-426de01443dfc8081f8ec70.tgz"
        row = {"name": name, "prior_metadata": {"bytes": 191626678},
               "expected_paths": ["target/docker-platform-monitor.gz"]}
        value = {"schema": 1, "root": "/dpkg_cache", "objects": [row]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selection.json"
            def load():
                raw = json.dumps(value).encode()
                path.write_bytes(raw)
                return payloads.manifest(path, hashlib.sha256(raw).hexdigest())
            self.assertEqual(load()["objects"][0]["prior_metadata"]["bytes"], 191626678)
            row["prior_metadata"]["bytes"] = 256 * 1024**2 + 1
            with self.assertRaisesRegex(ValueError, "Invalid expected cache size"):
                load()
            row["prior_metadata"]["bytes"] = 191626678
            value["objects"] = [{**row, "name": name.replace("docker-platform-monitor", "docker-platform-monitor" + str(i))}
                                for i in range(3)]
            with self.assertRaisesRegex(ValueError, "Compressed archive selection limit"):
                load()


if __name__ == '__main__':
    unittest.main()
