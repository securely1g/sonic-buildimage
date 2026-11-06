"""Keep Syncd and P4RT evidence separate while sharing provenance checks."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.bazel.ci import image_worker_receipt as receipt
from tools.bazel.ci.p4rt_worker_receipt_test import metadata, request


def syncd_request():
    value = request()
    value.pop('owner')
    return {**value, 'schema': 2, 'image': 'syncd'}


class ImageReceiptTest(unittest.TestCase):
    def test_policy_keeps_original_bytes_and_allows_only_the_public_constant(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'policy.json'
            value = {'image_validation': {'environment': {'DEBIAN_FRONTEND': 'noninteractive'}}}
            content = json.dumps(value).encode()
            path.write_bytes(content)
            self.assertEqual(receipt.read_evidence(path, syncd_request()), value)
            self.assertEqual(path.read_bytes(), content)
            with self.assertRaises(ValueError):
                receipt.read_evidence(path, request())
            for environment in ({'DEBIAN_FRONTEND': 'interactive'}, {'TOKEN': 'secret'}):
                path.write_text(json.dumps({'image_validation': {'environment': environment}}))
                with self.subTest(environment=environment), self.assertRaises(ValueError):
                    receipt.read_evidence(path, syncd_request())

    def test_profiles_cannot_substitute_requests_or_artifacts(self):
        value = syncd_request()
        self.assertEqual(receipt.validate_request(value), value)
        self.assertEqual(receipt.phases(value), ('build', 'validate', 'incremental', 'loaded'))
        self.assertNotEqual(receipt.acceptance_name(value), receipt.acceptance_name(request()))
        self.assertNotEqual(receipt.origin_name(123, 'syncd'), receipt.origin_name(123))
        for changed in ({**value, 'image': 'p4rt'}, {**value, 'schema': 1},
                        {**value, 'image': 'unreviewed'}, {**value, 'owner': 'a' * 40},
                        {**request(), 'image': 'syncd'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                receipt.validate_request(changed)
        with self.assertRaises(ValueError):
            receipt.same_checkout(value, request())
        artifact = {'id': 7, 'name': receipt.origin_name(123), 'expired': False, 'size_in_bytes': 1024}
        with self.assertRaises(ValueError):
            receipt.original_artifact([artifact], 123, selected='syncd')

    def test_syncd_requires_its_job_all_four_phases_and_cleanup(self):
        _, artifact, run, jobs = metadata()
        value = syncd_request()
        artifact['name'] = receipt.acceptance_name(value)
        jobs[0]['name'] = 'Source image (syncd)'
        jobs[0]['steps'] = [{'name': name, 'status': 'completed', 'conclusion': 'success'}
                            for name in receipt.required_steps(value)]
        self.assertEqual(receipt.worker_metadata(value, artifact, run, jobs)['id'], 201)
        for step in receipt.required_steps(value):
            altered = copy.deepcopy(jobs)
            next(row for row in altered[0]['steps'] if row['name'] == step)['conclusion'] = 'skipped'
            with self.subTest(step=step), self.assertRaises(ValueError):
                receipt.worker_metadata(value, artifact, run, altered)
        jobs[0]['name'] = 'Source image (p4rt)'
        with self.assertRaises(ValueError):
            receipt.worker_metadata(value, artifact, run, jobs)

    def test_syncd_checkout_binds_the_whole_merge_without_a_p4rt_gitlink(self):
        value = syncd_request()
        contract = {key: value[key] for key in ('image', 'controller', 'catalog')}
        event = {'pull_request': {'number': 196,
                 'head': {'sha': value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
                 'base': {'sha': value['base'], 'repo': {'full_name': receipt.REPOSITORY}}}}
        answers = [value['merge'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge', value['tree']]
        with patch.object(receipt, 'git', side_effect=answers) as git:
            actual = receipt.checkout_request(Path('.'), contract, event=event, run=123, attempt=1)
        self.assertEqual(actual['image'], 'syncd')
        self.assertNotIn('owner', actual)
        self.assertEqual(git.call_count, 3)
        self.assertEqual(actual['contract_sha256'], receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()))

    def test_worker_binds_the_selected_syncd_contract_and_rejects_profile_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = syncd_request()
            contract = {key: value[key] for key in ('image', 'controller', 'catalog')}
            value['contract_sha256'] = receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode())
            receipt.write(root / 'payload/request.json', value)
            receipt.write(root / 'tools/bazel/ci/syncd_worker_contract.json', contract)
            receipt.write(root / 'origin.json', {
                'run': {'id': 123, 'head_sha': value['head'], 'run_attempt': 1},
                'artifact': {'id': 456, 'name': receipt.origin_name(123, 'syncd')}} , public=False)
            args = SimpleNamespace(directory=root, workspace=root, image='syncd')
            environment = {'SELECTED_IMAGE': 'syncd', 'SOURCE_REVISION': value['merge'],
                           'SOURCE_REGISTRY_REVISION': value['catalog'], 'GITHUB_SHA': value['controller']}
            pull = {'state': 'open', 'head': {'sha': value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
                    'base': {'sha': value['base'], 'repo': {'full_name': receipt.REPOSITORY}}}
            with patch.dict(os.environ, environment), patch.object(receipt, 'git',
                    side_effect=[value['merge'], value['tree'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge']), \
                    patch.object(receipt, 'api', return_value=pull):
                receipt.bind_worker(args)
            self.assertEqual(receipt.read(root / 'binding.json')['request'], value)
            with patch.dict(os.environ, {**environment, 'SELECTED_IMAGE': 'p4rt'}), \
                    self.assertRaisesRegex(ValueError, 'Dispatch image differs'):
                receipt.bind_worker(args)


if __name__ == '__main__':
    unittest.main()
