#!/usr/bin/env python3
"""Exercise the real prepared-host receipt and byte-verification boundary."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host
import prepared_host


class PreparedHostTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.identity = {
            'arch': 'amd64', 'platform': 'vs', 'machine': 'vs', 'image_type': 'onie',
            'distro': 'trixie', 'image_version': 'test', 'source_commit': 'abc123',
            'source_branch': 'test', 'source_date_epoch': '1',
        }
        self.request = {'image_store_backend': 'containerd'}
        for name in ('source', 'snapshot', 'config', 'execution_environment', 'build_script',
                     'extension_template', 'runtime_overlay', 'offline_host'):
            path = self.inputs / name
            path.write_text('declared-' + name)
            self.request[name] = str(path)
        self.config = {
            'schema': 1, 'identity': self.identity,
            'environment': {
                'SONIC_IMAGE_VERSION': 'test',
                'installer_images': 'swss|dockers/swss||target/docker-orchagent.gz:test',
                'sonic_local_packages': 'macsec|target/docker-macsec.gz|local|y',
            },
        }
        Path(self.request['config']).write_text(json.dumps(self.config))
        metadata = self.inputs / 'swss.json'
        metadata.write_text(json.dumps(host.project({
            'architecture': 'amd64', 'os': 'linux',
            'config': {'Labels': {'com.azure.sonic.manifest': '{"service":{"name":"swss"}}'}},
        })))
        local = self.inputs / 'macsec.oci'
        local.mkdir()
        (local / 'manifest.json').write_text('{"schema":1}')
        (local / 'image.tar').write_bytes(b'local package OCI contents')
        self.request.update(metadata={'docker-orchagent.gz': str(metadata)},
                            local_images={'docker-macsec.gz': str(local)})
        self.prepared = {}
        self.outputs = {}
        for key, filename in prepared_host.OUTPUT_NAMES.items():
            path = self.root / filename
            path.write_bytes(('prepared-' + key).encode())
            self.prepared[key] = path
            self.outputs[key] = self.root / 'verified' / filename
        self.receipt = {
            'schema': 1, 'status': 'passed', 'image_store_backend': 'containerd',
            'snapshot_identity': self.identity,
            'inputs': host.input_identity(self.request),
            'outputs': {key: host.file_identity(path) for key, path in self.prepared.items()},
        }
        self.receipt_path = self.root / 'receipt.json'
        self.write_receipt()

    def write_receipt(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

    def verify(self, request=None):
        return prepared_host.verify(request or self.request, self.prepared, self.receipt_path,
                                    self.outputs, self.root / 'verified/receipt.json')

    def test_valid_handoff_preserves_bytes_without_subprocesses(self):
        with mock.patch.object(host.subprocess, 'run', side_effect=AssertionError('unexpected subprocess')), \
                mock.patch.object(host.subprocess, 'check_output', side_effect=AssertionError('unexpected subprocess')):
            result = self.verify()
        self.assertTrue(result['prepared_host_verified'])
        for key, output in self.outputs.items():
            self.assertEqual(output.read_bytes(), self.prepared[key].read_bytes())
        self.assertEqual(json.loads((self.root / 'verified/receipt.json').read_text()), result)

    def test_identity_survives_relocation_between_jobs(self):
        relocated = self.root / 'another-job' / 'inputs'
        shutil.copytree(self.inputs, relocated)
        request = copy.deepcopy(self.request)
        for key, value in request.items():
            if key not in ('image_store_backend', 'metadata', 'local_images'):
                request[key] = str(relocated / Path(value).relative_to(self.inputs))
        for key in ('metadata', 'local_images'):
            request[key] = {name: str(relocated / Path(path).relative_to(self.inputs))
                            for name, path in request[key].items()}
        self.assertEqual(host.input_identity(self.request), host.input_identity(request))
        self.verify(request)

    def test_sandbox_tree_file_links_preserve_identity_and_check_target_bytes(self):
        original = Path(self.request['local_images']['docker-macsec.gz'])
        sandbox = self.root / 'another-job' / 'sandbox' / 'macsec.oci'
        sandbox.mkdir(parents=True)
        for path in original.iterdir():
            (sandbox / path.name).symlink_to(path)
        request = copy.deepcopy(self.request)
        request['local_images']['docker-macsec.gz'] = str(sandbox)
        self.assertEqual(host.input_identity(self.request), host.input_identity(request))
        self.verify(request)
        (original / 'image.tar').write_bytes(b'changed archive behind sandbox link')
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            self.verify(request)

    def test_every_direct_host_input_is_checked(self):
        for key in self.receipt['inputs']['files']:
            path = Path(self.request[key])
            original = path.read_bytes()
            with self.subTest(input=key):
                if key == 'config':
                    config = copy.deepcopy(self.config)
                    config['environment']['CHANGE_DEFAULT_PASSWORD'] = 'y'
                    path.write_text(json.dumps(config))
                else:
                    path.write_bytes(original + b'changed')
                with self.assertRaisesRegex(ValueError, 'inputs changed'):
                    self.verify()
                path.write_bytes(original)
        self.assertFalse((self.root / 'verified').exists())

    def test_changed_builtin_metadata_is_rejected(self):
        Path(self.request['metadata']['docker-orchagent.gz']).write_text('{"different":"metadata"}')
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            self.verify()

    def test_local_package_bytes_and_members_are_checked(self):
        directory = Path(self.request['local_images']['docker-macsec.gz'])
        archive = directory / 'image.tar'
        original = archive.read_bytes()
        for operation in ('change', 'add', 'remove'):
            with self.subTest(operation=operation):
                if operation == 'change':
                    archive.write_bytes(original + b'changed')
                elif operation == 'add':
                    (directory / 'extra').write_text('undeclared')
                else:
                    archive.unlink()
                with self.assertRaisesRegex(ValueError, 'inputs changed'):
                    self.verify()
                archive.write_bytes(original)
                (directory / 'extra').unlink(missing_ok=True)

    def test_unsafe_local_tree_is_rejected(self):
        directory = Path(self.request['local_images']['docker-macsec.gz'])
        for target in (self.inputs, self.inputs / 'missing'):
            with self.subTest(target=target):
                link = directory / 'linked'
                link.symlink_to(target)
                with self.assertRaisesRegex(ValueError, 'unsupported local host image'):
                    self.verify()
                link.unlink()

    def test_local_tree_special_file_is_rejected(self):
        directory = Path(self.request['local_images']['docker-macsec.gz'])
        os.mkfifo(directory / 'fifo')
        with self.assertRaisesRegex(ValueError, 'unsupported local host image'):
            self.verify()

    def test_changed_host_implementation_invalidates_preparation(self):
        self.receipt['inputs']['implementation']['host.py']['sha256'] = '0' * 64
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            self.verify()

    def test_missing_or_changed_service_inventory_is_rejected(self):
        request = dict(self.request, metadata={})
        with self.assertRaisesRegex(ValueError, 'declared image inputs'):
            self.verify(request)

    def test_each_corrupt_prepared_output_is_rejected_before_copy(self):
        for key, path in self.prepared.items():
            original = path.read_bytes()
            with self.subTest(output=key):
                path.write_bytes(original + b'corrupt')
                with self.assertRaisesRegex(ValueError, 'size/SHA256'):
                    self.verify()
                path.write_bytes(original)
        self.assertFalse((self.root / 'verified').exists())

    def test_legacy_failed_and_wrong_backend_receipts_are_rejected(self):
        original = copy.deepcopy(self.receipt)
        cases = [dict(original, status='failed'), dict(original, image_store_backend='docker')]
        legacy = copy.deepcopy(original)
        del legacy['inputs']
        cases.append(legacy)
        for value in cases:
            with self.subTest(receipt=value):
                self.receipt = value
                self.write_receipt()
                with self.assertRaisesRegex(ValueError, 'successful containerd host receipt'):
                    self.verify()

    def test_export_checks_and_preserves_all_outputs(self):
        destination = self.root / 'export'
        prepared_host.export(self.prepared, self.receipt_path, destination)
        self.assertEqual(set(path.name for path in destination.iterdir()),
                         {*prepared_host.OUTPUT_NAMES.values(), 'receipt.json'})
        for key, name in prepared_host.OUTPUT_NAMES.items():
            self.assertEqual((destination / name).read_bytes(), self.prepared[key].read_bytes())
        self.assertEqual(json.loads((destination / 'receipt.json').read_text()), self.receipt)
        with self.assertRaisesRegex(ValueError, 'must be fresh'):
            prepared_host.export(self.prepared, self.receipt_path, destination)

    def test_copy_detects_changed_bytes_and_preserves_old_output(self):
        source = self.prepared['fs']
        destination = self.root / 'old.squashfs'
        destination.write_bytes(b'previous output')
        def copy_changed(_incoming, outgoing, _length):
            outgoing.write(b'changed during transfer')
        with mock.patch.object(prepared_host.shutil, 'copyfileobj', side_effect=copy_changed):
            with self.assertRaisesRegex(ValueError, 'changed while copying'):
                prepared_host.copy_verified(source, destination, self.receipt['outputs']['fs'])
        self.assertEqual(destination.read_bytes(), b'previous output')

    def test_receipt_cannot_claim_another_snapshot_identity(self):
        self.receipt['snapshot_identity'] = dict(self.identity, image_version='another')
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
