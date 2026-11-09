"""Keep image profiles separate while sharing provenance checks."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.bazel.ci import image_worker_receipt as receipt
from tools.bazel.ci.p4rt_worker_receipt_test import metadata, pr_environment, pr_event, request


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
        event = pr_event(value)
        answers = [value['merge'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge',
                   value['tree'], value['merge']]
        with patch.dict(os.environ, pr_environment(value)), patch.object(receipt, 'git', side_effect=answers) as git:
            actual = receipt.checkout_request(Path('.'), contract, event=event, run=123, attempt=1)
        self.assertEqual(actual['image'], self.selected)
        self.assertNotIn('owner', actual)
        self.assertEqual(git.call_count, 4)
        self.assertFalse(any('ls-tree' in call.args for call in git.call_args_list))
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
                    'base': {'sha': value['base'], 'repo': {'full_name': receipt.REPOSITORY}}}
            with patch.dict(os.environ, environment), patch.object(receipt, 'git',
                    side_effect=[value['merge'], value['tree'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge']), \
                    patch.object(receipt, 'api', return_value=pull):
                receipt.bind_worker(args)
            self.assertEqual(receipt.read(root / 'binding.json')['request'], value)
            with patch.dict(os.environ, {**environment, 'SELECTED_IMAGE': 'p4rt'}), \
                    self.assertRaisesRegex(ValueError, 'Dispatch image differs'):
                receipt.bind_worker(args)


class GearboxReceiptTest(ImageReceiptTest):
    selected = 'gearbox'


class SourceContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.value = request()
        self.event = pr_event(self.value)
        self.env = pr_environment(self.value)
        self.parents = [self.value['base'], self.value['head']]
        self.ancestor_error = None
        self.calls = []
        self.head_reads = 0
        self.move_checkout = False
        for target in ('api',):
            mocked = patch.object(receipt, target, side_effect=AssertionError('No API'))
            mocked.start(); self.addCleanup(mocked.stop)
        for target, attribute in ((receipt.urllib.request, 'urlopen'), (receipt.subprocess, 'check_output')):
            mocked = patch.object(target, attribute, side_effect=AssertionError('No network or process'))
            mocked.start(); self.addCleanup(mocked.stop)

    def git(self, workspace, *args):
        self.calls.append(args)
        if args == ('rev-parse', 'HEAD'):
            self.head_reads += 1
            return '9' * 40 if self.move_checkout and self.head_reads > 1 else self.value['merge']
        if args == ('cat-file', 'commit', self.value['merge']):
            return 'tree ' + self.value['tree'] + '\n' + '\n'.join('parent ' + x for x in self.parents) + '\n\nMerge'
        if args == ('rev-parse', self.value['merge'] + '^{tree}'):
            return self.value['tree']
        if args == ('merge-base', '--is-ancestor', self.event['pull_request']['base']['sha'], self.parents[0]):
            if self.ancestor_error:
                raise self.ancestor_error
            return ''
        if args == ('ls-tree', self.value['merge'], 'src/sonic-p4rt/sonic-pins'):
            return '160000 commit ' + self.value['owner'] + '\tsrc/sonic-p4rt/sonic-pins'
        raise AssertionError('Unexpected Git operation: ' + repr(args))

    def context(self):
        with patch.object(receipt, 'git', side_effect=self.git):
            return receipt.normal_pr_context(self.root, self.event, environ=self.env)

    def stale_snapshot(self):
        self.event['pull_request']['base']['sha'] = '8' * 40
        self.env['CI_BASE_SHA'] = '8' * 40

    def test_identical_snapshot_and_stale_ancestor_keep_distinct_provenance(self):
        same = self.context()
        self.assertEqual(same['snapshot_relation'], 'identical')
        self.assertFalse(any(call[0] == 'merge-base' for call in self.calls))
        self.stale_snapshot()
        original = copy.deepcopy(self.event)
        stale = self.context()
        self.assertEqual(stale['snapshot_relation'], 'ancestor')
        self.assertEqual(stale['pr_base_snapshot'], '8' * 40)
        self.assertEqual(stale['tested_base'], self.value['base'])
        self.assertEqual(stale['tested_parents'], self.parents)
        self.assertEqual(self.event, original)
        # A preceding preflight may already have exported the verified base.
        self.env['CI_BASE_SHA'] = self.value['base']
        self.assertEqual(self.context(), stale)
        self.assertTrue(all('fetch' not in call and 'ls-remote' not in call for call in self.calls))

    def test_platform_bindings_and_requested_settings_fail_closed(self):
        changes = {'GITHUB_SHA': '9' * 40, 'GITHUB_REF': 'refs/pull/999/merge',
            'GITHUB_REPOSITORY': 'other/repository', 'GITHUB_BASE_REF': 'other-base',
            'GITHUB_HEAD_REF': 'other-head', 'CI_HEAD_SHA': '9' * 40, 'CI_BASE_SHA': '9' * 40}
        for key, value in changes.items():
            with self.subTest(key=key), patch.dict(self.env, {key: value}), self.assertRaises(ValueError):
                self.context()
        for name in ('push', 'workflow_dispatch', 'pull_request_target'):
            with self.subTest(event=name), patch.dict(self.env, {'GITHUB_EVENT_NAME': name}), self.assertRaises(ValueError):
                self.context()

    def test_wrong_event_repository_number_and_sha_are_rejected(self):
        for field in ('repository', 'number', 'head', 'base'):
            original = copy.deepcopy(self.event)
            if field == 'repository':
                self.event['repository']['full_name'] = 'other/repository'
            elif field == 'number':
                self.event['number'] = 999
            else:
                self.event['pull_request'][field]['sha'] = '--not-a-revision'
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.context()
            self.event = original

    def test_parents_must_be_exactly_two_and_end_with_the_requested_head(self):
        for parents in ([], [self.value['head']], list(reversed(self.parents)),
                        [self.value['base'], '9' * 40], [*self.parents, '9' * 40],
                        [self.value['head'], self.value['head']]):
            self.parents = parents
            with self.subTest(parents=parents), self.assertRaises(ValueError):
                self.context()

    def test_unrelated_missing_shallow_and_timed_out_history_fail_closed(self):
        self.stale_snapshot()
        for error in (receipt.subprocess.CalledProcessError(1, ['git', 'merge-base']),
                      receipt.subprocess.CalledProcessError(128, ['git', 'merge-base']),
                      receipt.subprocess.TimeoutExpired(['git', 'merge-base'], 30)):
            self.ancestor_error = error
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(ValueError, 'verified ancestor'):
                self.context()

    def test_queue_time_base_changes_do_not_replace_the_immutable_tested_base(self):
        self.stale_snapshot()
        # No live branch ref or API appears in the resolver's allowed Git calls.
        self.env['UNRELATED_CURRENT_BASE_TIP'] = '7' * 40
        self.assertEqual(self.context()['tested_base'], self.parents[0])
        self.env['UNRELATED_CURRENT_BASE_TIP'] = '6' * 40
        self.assertEqual(self.context()['tested_base'], self.parents[0])
        self.head_reads = 0
        self.move_checkout = True
        with self.assertRaisesRegex(ValueError, 'Checkout changed'):
            self.context()

    def test_verified_output_satisfies_the_unchanged_framework_identity_guard(self):
        self.stale_snapshot()
        context = self.context()
        # Execute only the real identity function, with all Git reads mocked.
        path = Path(importlib.util.find_spec('tools.bazel.ci.mgmt_framework_image').origin)
        method = next(node for node in ast.parse(path.read_text()).body
                      if isinstance(node, ast.FunctionDef) and node.name == 'identity')
        def check_output(arguments, **kwargs):
            if arguments == ['git', 'rev-parse', 'HEAD']:
                return self.value['merge']
            if arguments == ['git', 'cat-file', '-p', 'HEAD']:
                return '\n'.join('parent ' + p for p in self.parents) + '\n\nMerge'
            raise AssertionError('Unexpected process request')
        namespace = {'ROOT': self.root, 'os': os, 'check': SimpleNamespace(require=receipt.require),
                     'subprocess': SimpleNamespace(check_output=check_output)}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
        with patch.dict(os.environ, {'CI_HEAD_SHA': self.value['head'], 'CI_BASE_SHA': '8' * 40}), \
                self.assertRaisesRegex(ValueError, 'requested PR base'):
            namespace['identity']()
        with patch.dict(os.environ, {'CI_HEAD_SHA': self.value['head'], 'CI_BASE_SHA': context['tested_base']}):
            self.assertEqual(namespace['identity']()['tested_revision_parents'], self.parents)

    def test_hosted_fork_context_does_not_relax_worker_admission(self):
        self.event['pull_request']['head']['repo']['full_name'] = 'contributor/sonic-buildimage'
        self.assertEqual(self.context()['head_repository'], 'contributor/sonic-buildimage')
        with self.assertRaisesRegex(ValueError, 'Fork source'):
            receipt.checkout_request(self.root, {}, event=self.event, run=123, attempt=1)

    def test_context_cli_writes_separate_evidence_before_workflow_outputs(self):
        self.stale_snapshot()
        event = self.root / 'event.json'; event.write_text(json.dumps(self.event))
        original = event.read_bytes()
        args = SimpleNamespace(workspace=self.root, directory=self.root / 'source-context')
        seen = []
        def emitted(**values):
            self.assertTrue((args.directory / 'context.json').is_file())
            seen.append(values)
        with patch.dict(os.environ, {**self.env, 'GITHUB_EVENT_PATH': str(event)}), \
                patch.object(receipt, 'git', side_effect=self.git), patch.object(receipt, 'output', side_effect=emitted):
            receipt.source_context(args)
            with self.assertRaises(FileExistsError):
                receipt.source_context(args)
        self.assertEqual(event.read_bytes(), original)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['tested_base'], self.value['base'])
        self.assertEqual(seen[0]['snapshot_base'], '8' * 40)
        self.assertEqual(receipt.read(args.directory / 'context.json')['pr_base_snapshot'], '8' * 40)

    def test_failed_context_never_exposes_outputs_or_writes_evidence(self):
        self.stale_snapshot(); self.ancestor_error = receipt.subprocess.CalledProcessError(128, ['git'])
        event = self.root / 'event.json'; event.write_text(json.dumps(self.event))
        args = SimpleNamespace(workspace=self.root, directory=self.root / 'source-context')
        with patch.dict(os.environ, {**self.env, 'GITHUB_EVENT_PATH': str(event)}), \
                patch.object(receipt, 'git', side_effect=self.git), patch.object(receipt, 'output') as emit, \
                self.assertRaises(ValueError):
            receipt.source_context(args)
        emit.assert_not_called()
        self.assertFalse(args.directory.exists())

    def test_normal_request_paths_use_verified_base_without_restamping_old_requests(self):
        self.stale_snapshot()
        event = self.root / 'event.json'; event.write_text(json.dumps(self.event))
        original_event = event.read_bytes()
        args = SimpleNamespace(workspace=self.root, directory=self.root / 'request',
            contract=self.root / 'contract.json', existing_only=False, public=self.root / 'public',
            output=self.root / 'verified.json')
        contract = {key: self.value[key] for key in ('owner', 'catalog', 'controller')}
        receipt.write(args.contract, contract)
        env = {**self.env, 'GITHUB_EVENT_PATH': str(event), 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
        with patch.dict(os.environ, env), patch.object(receipt, 'git', side_effect=self.git), \
                patch.object(receipt, 'api', return_value={'total_count': 0, 'artifacts': []}), \
                patch.object(receipt, 'output'):
            receipt.normal_origin(args)
        current = receipt.read(args.directory / 'request.json')
        self.assertEqual(current['base'], self.value['base'])
        self.assertEqual(set(current), set(self.value))
        self.assertEqual({p.name for p in args.directory.iterdir()}, {'request.json'})
        before = (args.directory / 'request.json').read_bytes()
        with patch.dict(os.environ, env), patch.object(receipt, 'git', side_effect=self.git), \
                patch.object(receipt, 'api', side_effect=RuntimeError('Reached discovery boundary')), \
                self.assertRaisesRegex(RuntimeError, 'discovery boundary'):
            receipt.discover(args)
        receipt.write(args.directory / 'provenance.json', {}, public=False)
        with patch.dict(os.environ, env), patch.object(receipt, 'git', side_effect=self.git), \
                patch.object(receipt, 'verify_evidence', return_value={'status': 'passed'}) as verify:
            receipt.verify(args)
            self.assertEqual(verify.call_args.args[1]['base'], self.value['base'])
        self.assertEqual((args.directory / 'request.json').read_bytes(), before)
        self.assertEqual(event.read_bytes(), original_event)
        old = {**current, 'base': '8' * 40}
        (args.directory / 'request.json').write_text(json.dumps(old))
        with patch.dict(os.environ, env), patch.object(receipt, 'git', side_effect=self.git), \
                patch.object(receipt, 'api') as api, self.assertRaisesRegex(ValueError, 'another CI checkout'):
            receipt.discover(args)
        api.assert_not_called()
        self.assertEqual(receipt.read(args.directory / 'request.json'), old)


if __name__ == '__main__':
    unittest.main()
