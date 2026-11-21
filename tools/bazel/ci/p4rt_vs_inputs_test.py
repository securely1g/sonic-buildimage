"""Exercise the VS-to-P4RT handoff independently of Bazel or a shared filesystem."""

from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.bazel.ci import p4rt_image, p4rt_vs_inputs as handoff


class VSOwnerInputsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.make = self.root / 'make'
        self.bundle = self.root / 'vs-image'
        self.transfer = self.root / 'artifact'
        self.revision = '1' * 40
        self.owner_revision = '2' * 40
        self.bundle.mkdir()
        prep, owner = self.make / 'owner-preparation', self.make / 'owner-output'
        prep.mkdir(parents=True)
        owner.mkdir()
        files = {}
        for name, target in handoff.PREPARATION_TARGETS.items():
            path = prep / name
            path.write_bytes(name.encode())
            files[name] = dict(target=target, bytes=path.stat().st_size, sha256=handoff.check.sha(path))
        output = {}
        for name in handoff.p4rt_handoff.FILES:
            path = owner / name
            path.write_bytes(b'{}' if name.endswith('.json') else name.encode())
            output[name] = dict(bytes=path.stat().st_size, sha256=handoff.check.sha(path))
        audit = {'schema': 1, 'targets': list(handoff.PREPARATION_TARGETS.values()),
                 'deb_outputs': [], 'packaging_wrappers': [], 'action_count': 24, 'output_count': 37}
        p4rt_image.write(prep / 'action-audit.json', audit)
        p4rt_image.write(owner / 'action-audit.json', {})
        p4rt_image.write(owner / 'source.json', {
            'commit': self.owner_revision, 'bazel': '6.4.0', 'architecture': 'amd64',
            'test_targets': ['//p4rt_app:runtime_package_test'], 'sdk_inventory': {},
            'tar_sha256': {k: v['sha256'] for k, v in output.items() if k.endswith('.tar')},
            'contract_sha256': {k: v['sha256'] for k, v in output.items() if k.endswith('.json')},
            'options': ['--output_base=/private/worker'],
        })
        # These may be present on disk from earlier work. Neither is proof for this test job.
        p4rt_image.write(owner / 'test-results.json', {'//p4rt_app:runtime_package_test': 'PASSED'})
        p4rt_image.write(self.make / 'owner-tests.json', {'status': 'passed'})
        self.owner_handoff = {'files': output}
        self.parent = {'parent_source_tar_sha256': files['config-engine.source.tar']['sha256']}
        p4rt_image.write(self.make / 'owner-preparation.json', {
            'schema': 1, 'architecture': 'amd64', 'revision': self.revision,
            'owner_revision': self.owner_revision, 'status': 'prepared-for-make',
            'owner_handoff': self.owner_handoff, 'artifacts': files, 'parent_common': self.parent,
            'action_audit': audit, 'commands': ['private-command'],
        })
        p4rt_image.write(self.bundle / 'handoff.json', self.owner_handoff)
        p4rt_image.write(self.bundle / 'bundle.json', {
            'architecture': 'amd64', 'image': 'docker-sonic-p4rt', 'revision': self.revision,
            'fixtures': {'config-engine.source.tar': files['config-engine.source.tar']['sha256'],
                         'handoff.json': handoff.check.sha(self.bundle / 'handoff.json')},
        })
        handoff.export_inputs(self.make, self.bundle, self.transfer)

    def test_relocated_inputs_verify_without_building_or_importing_old_test_results(self):
        """A fresh worker verifies the VS packages and SDK before recording its own preparation."""
        download = self.root / 'download'
        shutil.copytree(self.transfer, download)
        shutil.rmtree(self.make)
        shutil.rmtree(self.bundle)
        shutil.rmtree(self.transfer)
        args = SimpleNamespace(directory=self.root / 'consumer', vs_owner=download,
                               owner_source=self.root / 'pins', owner_revision=self.owner_revision)
        with patch.object(p4rt_image, 'native'), \
                patch.object(p4rt_image, 'source_identity', return_value=self.revision), \
                patch.object(p4rt_image.p4rt_handoff, 'source_identity'), \
                patch.object(p4rt_image, 'prepare_sdk', return_value=self.parent) as sdk, \
                patch.object(p4rt_image.p4rt_handoff, 'stage', return_value=self.owner_handoff) as stage, \
                patch.object(p4rt_image, 'run_owner', side_effect=AssertionError('rebuilding the owner')):
            p4rt_image.restore_vs_owner(args)
        sdk.assert_called_once()
        stage.assert_called_once()
        self.assertFalse((args.directory / 'owner-tests.json').exists())
        self.assertFalse((args.directory / 'owner-output/test-results.json').exists())
        self.assertNotIn('options', handoff.read(args.directory / 'owner-output/source.json'))
        record = handoff.read(args.directory / 'owner-preparation.json')
        self.assertNotIn('commands', record)
        self.assertEqual((record['input_source'], record['status']), ('vs-installer', 'prepared'))
        self.assertEqual(handoff.read(download / 'vs-preparation.json')['status'], 'prepared-for-make')

    def test_checkout_revision_owner_and_actual_selected_image_must_match(self):
        """A valid transfer for another checkout, PINS revision or selected parent must fail."""
        for revision, owner in [('3' * 40, self.owner_revision), (self.revision, '4' * 40)]:
            with self.subTest(revision=revision, owner=owner), self.assertRaisesRegex(ValueError, 'different VS'):
                handoff.read_inputs(self.transfer, revision, owner)
        bundle = handoff.read(self.transfer / 'vs-bundle.json')
        bundle['fixtures']['config-engine.source.tar'] = 'f' * 64
        p4rt_image.write(self.transfer / 'vs-bundle.json', bundle)
        with self.assertRaisesRegex(ValueError, 'selected VS image'):
            handoff.read_inputs(self.transfer, self.revision, self.owner_revision)

    def test_tampered_sdk_tool_and_owner_package_fail_before_execution(self):
        """Changing a transferred executable or sealed owner TAR invalidates the producer digest."""
        for path in ('owner-preparation/runtime-patchelf', 'owner-output/p4rt_runtime.tar'):
            with self.subTest(path=path):
                file = self.transfer / path
                original = file.read_bytes()
                file.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'Changed VS owner'):
                    handoff.read_inputs(self.transfer, self.revision, self.owner_revision)
                file.write_bytes(original)
