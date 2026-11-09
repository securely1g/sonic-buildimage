"""Keep image profiles separate while sharing provenance checks."""
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
    selected = 'syncd'

    def image_request(self):
        return {**syncd_request(), 'image': self.selected}

    def test_policy_keeps_original_bytes_and_allows_only_the_public_constant(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'policy.json'
            value = {'image_validation': {'environment': {'DEBIAN_FRONTEND': 'noninteractive'}}}
            content = json.dumps(value).encode()
            path.write_bytes(content)
            self.assertEqual(receipt.read_evidence(path, self.image_request()), value)
            self.assertEqual(path.read_bytes(), content)
            with self.assertRaises(ValueError):
                receipt.read_evidence(path, request())
            for environment in ({'DEBIAN_FRONTEND': 'interactive'}, {'TOKEN': 'secret'}):
                path.write_text(json.dumps({'image_validation': {'environment': environment}}))
                with self.subTest(environment=environment), self.assertRaises(ValueError):
                    receipt.read_evidence(path, self.image_request())

    def test_profiles_cannot_substitute_requests_or_artifacts(self):
        value = self.image_request()
        self.assertEqual(receipt.validate_request(value), value)
        self.assertEqual(receipt.phases(value), ('build', 'validate', 'incremental', 'loaded'))
        self.assertNotEqual(receipt.acceptance_name(value), receipt.acceptance_name(request()))
        self.assertNotEqual(receipt.origin_name(123, self.selected), receipt.origin_name(123))
        for changed in ({**value, 'image': 'p4rt'}, {**value, 'schema': 1},
                        {**value, 'image': 'unreviewed'}, {**value, 'owner': 'a' * 40},
                        {**request(), 'image': self.selected}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                receipt.validate_request(changed)
        with self.assertRaises(ValueError):
            receipt.same_checkout(value, request())
        other = {**value, 'image': 'gearbox' if self.selected == 'syncd' else 'syncd'}
        with self.assertRaises(ValueError):
            receipt.same_checkout(value, other)
        self.assertNotEqual(receipt.acceptance_name(value), receipt.acceptance_name(other))
        artifact = {'id': 7, 'name': receipt.origin_name(123), 'expired': False, 'size_in_bytes': 1024}
        with self.assertRaises(ValueError):
            receipt.original_artifact([artifact], 123, selected=self.selected)

    def test_syncd_requires_its_job_all_four_phases_and_cleanup(self):
        _, artifact, run, jobs = metadata()
        value = self.image_request()
        artifact['name'] = receipt.acceptance_name(value)
        jobs[0]['name'] = 'Source image (' + self.selected + ')'
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
        value = self.image_request()
        contract = {key: value[key] for key in ('image', 'controller', 'catalog')}
        event = {'pull_request': {'number': 196,
                 'head': {'sha': value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
                 'base': {'sha': value['base'], 'repo': {'full_name': receipt.REPOSITORY}}}}
        answers = [value['merge'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge', value['tree']]
        with patch.object(receipt, 'git', side_effect=answers) as git:
            actual = receipt.checkout_request(Path('.'), contract, event=event, run=123, attempt=1)
        self.assertEqual(actual['image'], self.selected)
        self.assertNotIn('owner', actual)
        self.assertEqual(git.call_count, 3)
        self.assertEqual(actual['contract_sha256'], receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()))

    def test_worker_binds_the_selected_syncd_contract_and_rejects_profile_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = self.image_request()
            contract = {key: value[key] for key in ('image', 'controller', 'catalog')}
            value['contract_sha256'] = receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode())
            receipt.write(root / 'payload/request.json', value)
            receipt.write(root / ('tools/bazel/ci/' + self.selected + '_worker_contract.json'), contract)
            receipt.write(root / 'origin.json', {
                'run': {'id': 123, 'head_sha': value['head'], 'run_attempt': 1},
                'artifact': {'id': 456, 'name': receipt.origin_name(123, self.selected)}} , public=False)
            args = SimpleNamespace(directory=root, workspace=root, image=self.selected)
            environment = {'SELECTED_IMAGE': self.selected, 'SOURCE_REVISION': value['merge'],
                           'SOURCE_REGISTRY_REVISION': value['catalog'], 'GITHUB_SHA': value['controller']}
            pull = {'state': 'open', 'head': {'sha': value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
                    'base': {'sha': value['base'], 'ref': 'main', 'repo': {'full_name': receipt.REPOSITORY}}}
            base_ref = {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': value['base']}}
            with patch.dict(os.environ, environment), patch.object(receipt, 'git',
                    side_effect=[value['merge'], value['tree'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge']), \
                    patch.object(receipt, 'api', side_effect=[pull, base_ref]):
                receipt.bind_worker(args)
            self.assertEqual(receipt.read(root / 'binding.json')['request'], value)
            with patch.dict(os.environ, {**environment, 'SELECTED_IMAGE': 'p4rt'}), \
                    self.assertRaisesRegex(ValueError, 'Dispatch image differs'):
                receipt.bind_worker(args)


class GearboxReceiptTest(ImageReceiptTest):
    selected = 'gearbox'


class WorkerBaseRefTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.value = request()
        self.contract = {key: self.value[key] for key in ('owner', 'controller', 'catalog')}
        self.value['contract_sha256'] = receipt.sha(json.dumps(
            self.contract, sort_keys=True, separators=(',', ':')).encode())
        self.request_path = self.root / 'payload/request.json'
        receipt.write(self.request_path, self.value)
        self.original_bytes = self.request_path.read_bytes()
        receipt.write(self.root / 'tools/bazel/ci/p4rt_worker_contract.json', self.contract)
        receipt.write(self.root / 'origin.json', {
            'run': {'id': 123, 'head_sha': self.value['head'], 'run_attempt': 1},
            'artifact': {'id': 456, 'name': receipt.origin_name(123)}}, public=False)
        self.args = SimpleNamespace(directory=self.root, workspace=self.root, image='p4rt')
        self.environment = {'SELECTED_IMAGE': 'p4rt', 'SOURCE_REVISION': self.value['merge'],
            'SOURCE_REGISTRY_REVISION': self.value['catalog'], 'GITHUB_SHA': self.value['controller']}
        self.git_answers = [self.value['merge'], self.value['tree'],
            'parent ' + self.value['base'] + '\nparent ' + self.value['head'] + '\n\nMerge']
        self.pull = {'state': 'open',
            'head': {'sha': self.value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
            'base': {'sha': '0' * 40, 'ref': 'codex/review+base',
                     'repo': {'full_name': receipt.REPOSITORY}}}
        self.base_ref = {'ref': 'refs/heads/codex/review+base',
            'object': {'type': 'commit', 'sha': self.value['base']}}
        for target, name in ((receipt.urllib.request, 'urlopen'), (receipt.subprocess, 'check_output')):
            guard = patch.object(target, name, side_effect=AssertionError('No network or process'))
            guard.start(); self.addCleanup(guard.stop)
        api = patch.object(receipt, 'api')
        self.api = api.start(); self.addCleanup(api.stop)

    def bind(self):
        self.api.reset_mock()
        self.api.side_effect = [self.pull, self.base_ref]
        with patch.dict(os.environ, self.environment), \
                patch.object(receipt, 'git', side_effect=self.git_answers):
            receipt.bind_worker(self.args)

    def test_stale_snapshot_uses_exact_live_ref_and_keeps_original_request(self):
        self.bind()
        self.assertEqual([call.args for call in self.api.call_args_list], [
            ('/pulls/159',), ('/git/ref/heads/codex%2Freview%2Bbase',)])
        self.assertEqual(self.request_path.read_bytes(), self.original_bytes)
        binding = receipt.read(self.root / 'binding.json')
        self.assertEqual(binding['request'], self.value)
        self.assertEqual(binding['request_sha256'], receipt.sha(self.original_bytes))

    def test_same_base_commit_on_another_live_branch_is_not_a_new_policy(self):
        self.pull['base'].update(sha=self.value['base'], ref='another-base')
        self.base_ref['ref'] = 'refs/heads/another-base'
        self.bind()
        self.assertEqual(self.api.call_args.args, ('/git/ref/heads/another-base',))

    def test_missing_or_malformed_branch_fails_before_ref_lookup(self):
        for branch in (None, '', False, 12, [], {}, ' ', 'two words', 'base\nother', 'base\x00'):
            self.pull['base']['ref'] = branch
            with self.subTest(branch=branch), self.assertRaisesRegex(ValueError, 'PR base branch'):
                self.bind()
            self.assertEqual(self.api.call_count, 1)
            self.assertFalse((self.root / 'binding.json').exists())
        self.pull['base'].pop('ref')
        with self.assertRaisesRegex(ValueError, 'PR base branch'):
            self.bind()
        self.assertEqual(self.api.call_count, 1)

    def test_advanced_base_or_malformed_ref_response_cannot_bind(self):
        correct = copy.deepcopy(self.base_ref)
        responses = [None, [], {}, {'ref': correct['ref']},
            {**correct, 'ref': 'refs/heads/another-base'},
            {**correct, 'object': None}, {**correct, 'object': 'commit'},
            {**correct, 'object': {'sha': self.value['base']}},
            {**correct, 'object': {'type': 'tag', 'sha': self.value['base']}},
            {**correct, 'object': {'type': 'commit', 'sha': '9' * 40}},
            {**correct, 'object': {'type': 'commit', 'sha': None}}]
        for response in responses:
            self.base_ref = response
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, 'PR base ref changed'):
                self.bind()
            self.assertEqual(self.api.call_count, 2)
            self.assertFalse((self.root / 'binding.json').exists())

    def test_closed_pr_changed_head_or_foreign_repo_fails_before_ref_lookup(self):
        original = copy.deepcopy(self.pull)
        for change in ('closed', 'head', 'head_repo', 'base_repo'):
            self.pull = copy.deepcopy(original)
            if change == 'closed':
                self.pull['state'] = 'closed'
            elif change == 'head':
                self.pull['head']['sha'] = '9' * 40
            else:
                self.pull[change.removesuffix('_repo')]['repo']['full_name'] = 'other/repository'
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'PR head/base changed'):
                self.bind()
            self.assertEqual(self.api.call_count, 1)
            self.assertFalse((self.root / 'binding.json').exists())

    def test_dispatch_and_exact_checkout_checks_still_precede_metadata(self):
        for key in ('SOURCE_REVISION', 'SOURCE_REGISTRY_REVISION', 'GITHUB_SHA'):
            with self.subTest(key=key), patch.dict(self.environment, {key: '9' * 40}), \
                    self.assertRaisesRegex(ValueError, 'Dispatch differs'):
                self.bind()
            self.assertEqual(self.api.call_count, 0)
        original = self.git_answers[:]
        for index in range(3):
            self.git_answers = original[:]
            self.git_answers[index] = ('9' * 40 if index < 2 else
                'parent ' + self.value['head'] + '\nparent ' + self.value['base'] + '\n\nMerge')
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'Worker checkout differs'):
                self.bind()
            self.assertEqual(self.api.call_count, 0)
            self.assertFalse((self.root / 'binding.json').exists())

if __name__ == '__main__':
    unittest.main()
