"""Pure controls for immutable CI requests and trusted worker evidence."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.bazel.ci import p4rt_worker_receipt as receipt


def request():
    return {'schema': 1, 'repository': receipt.REPOSITORY, 'workflow': receipt.WORKFLOW,
            'normal_run': 123, 'request_attempt': 1, 'pr': 159, 'head': 'a' * 40,
            'base': 'b' * 40, 'merge': 'c' * 40, 'tree': 'd' * 40,
            'catalog': 'e' * 40, 'owner': 'f' * 40, 'controller': '1' * 40,
            'contract_sha256': '2' * 64}


def metadata():
    req = request()
    artifact = {'id': 300, 'name': receipt.acceptance_name(req), 'expired': False,
                'size_in_bytes': 4096, 'workflow_run': {'id': 200, 'head_sha': req['controller'],
                    'repository_id': 42, 'head_repository_id': 42}}
    run = {'id': 200, 'head_sha': req['controller'], 'run_attempt': 1,
           'repository': {'full_name': receipt.REPOSITORY, 'id': 42},
           'head_repository': {'full_name': receipt.REPOSITORY}, 'path': receipt.WORKFLOW,
           'event': 'workflow_dispatch', 'actor': {'login': 'securely1g'},
           'status': 'completed', 'conclusion': 'success'}
    job = {'id': 201, 'name': 'Source image (p4rt)', 'status': 'completed', 'conclusion': 'success',
           'run_id': 200, 'run_attempt': 1, 'head_sha': req['controller'],
           'runner_id': 12, 'runner_name': 'sonic-slave-lgh114-1',
           'labels': ['sonic-slave-container-dpool', 'sonic-slave-lgh114-1'],
           'steps': [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in receipt.STEPS]}
    return req, artifact, run, [job]


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.network = patch.object(receipt.urllib.request, 'urlopen', side_effect=AssertionError('No network'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.process = patch.object(receipt.subprocess, 'check_output', side_effect=AssertionError('No process'))
        self.process.start()
        self.addCleanup(self.process.stop)

    def test_rerun_keeps_original_attempt_and_exact_checkout(self):
        original = request()
        current = {**original, 'request_attempt': 3}
        receipt.same_checkout(original, current)
        self.assertEqual(original['request_attempt'], 1)
        for key in ('normal_run', 'head', 'base', 'merge', 'tree', 'owner', 'catalog', 'controller', 'contract_sha256'):
            other = copy.deepcopy(current)
            other[key] = 999 if key == 'normal_run' else '9' * len(other[key])
            with self.subTest(key=key), self.assertRaises(ValueError):
                receipt.same_checkout(original, other)


    def test_original_artifact_cannot_expire_duplicate_or_grow(self):
        artifact = {'id': 7, 'name': receipt.origin_name(123), 'expired': False, 'size_in_bytes': 1024}
        self.assertEqual(receipt.original_artifact([artifact], 123), artifact)
        for rows in ([{**artifact, 'expired': True}], [artifact, artifact],
                     [artifact, {**artifact, 'id': 8, 'expired': True}],
                     [{**artifact, 'size_in_bytes': receipt.MEMBER_LIMIT + 1}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                receipt.original_artifact(rows, 123, optional=True)

    def test_initial_failure_then_rerun_reuses_original_request(self):
        args = SimpleNamespace(contract=self.root / 'contract.json', workspace=self.root,
                               directory=self.root / 'request', existing_only=False)
        receipt.write(args.contract, {})
        event = self.root / 'event.json'; event.write_text('{}')
        env = {'GITHUB_REPOSITORY': receipt.REPOSITORY, 'GITHUB_EVENT_NAME': 'pull_request',
               'GITHUB_EVENT_PATH': str(event), 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
        with patch.dict(os.environ, env), patch.object(receipt, 'checkout_request', return_value=request()), \
                patch.object(receipt, 'api', return_value={'total_count': 0, 'artifacts': []}), \
                patch.object(receipt, 'output') as emit:
            receipt.normal_origin(args)
            emit.assert_called_once_with(new_request='true', request_name=receipt.origin_name(123))
        original_bytes = (args.directory / 'request.json').read_bytes()
        artifact = {'id': 7, 'name': receipt.origin_name(123), 'expired': False, 'size_in_bytes': 1024,
                    'workflow_run': {'id': 123, 'head_sha': request()['head']}}
        # The initial discover would fail without acceptance; the request survives.
        with self.assertRaises(ValueError):
            receipt.unique_artifact([], receipt.acceptance_name(request()))
        for attempt in (2, 3):
            with patch.dict(os.environ, {**env, 'GITHUB_RUN_ATTEMPT': str(attempt)}), \
                    patch.object(receipt, 'checkout_request', return_value={**request(), 'request_attempt': attempt}), \
                    patch.object(receipt, 'api', return_value={'total_count': 1, 'artifacts': [artifact]}), \
                    patch.object(receipt, 'output') as emit:
                receipt.normal_origin(args)
                emit.assert_called_once_with(new_request='false', request_artifact=7, request_run=123)
            self.assertEqual((args.directory / 'request.json').read_bytes(), original_bytes)
        args.existing_only = True
        with patch.dict(os.environ, env), patch.object(receipt, 'checkout_request', return_value=request()), \
                patch.object(receipt, 'api', return_value={'total_count': 0, 'artifacts': []}), self.assertRaises(ValueError):
            receipt.normal_origin(args)

    def test_cross_run_acceptance_name_and_all_cleanup_are_required(self):
        req, artifact, run, jobs = metadata()
        with self.assertRaises(ValueError):
            receipt.worker_metadata(req, {**artifact, 'name': receipt.acceptance_name({**req, 'normal_run': 999})}, run, jobs)
        for name in ("Stop only this job's private Docker daemon", 'Post Checkout the exact reviewed source',
                     'Post Checkout reviewed dispatcher', 'Complete job'):
            for conclusion in ('failure', 'skipped'):
                changed = copy.deepcopy(jobs)
                next(s for s in changed[0]['steps'] if s['name'] == name)['conclusion'] = conclusion
                with self.subTest(name=name, conclusion=conclusion), self.assertRaises(ValueError):
                    receipt.worker_metadata(req, artifact, run, changed)

    def test_attempt_cannot_move_backwards_or_use_boolean_identity(self):
        with self.assertRaises(ValueError):
            receipt.same_checkout({**request(), 'request_attempt': 2}, request())
        for key in ('normal_run', 'request_attempt', 'pr'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                receipt.validate_request({**request(), key: True})

    def test_checkout_requires_merge_and_selected_owner(self):
        req = request()
        contract = {k: req[k] for k in ('owner', 'catalog', 'controller')}
        event = {'pull_request': {'number': 159,
            'head': {'sha': req['head'], 'repo': {'full_name': receipt.REPOSITORY}},
            'base': {'sha': req['base'], 'repo': {'full_name': receipt.REPOSITORY}}}}
        answers = [req['merge'], 'tree ' + req['tree'] + '\nparent ' + req['base'] + '\nparent ' + req['head'] + '\n\nMerge',
                   '160000 commit ' + req['owner'] + '\tsrc/sonic-p4rt/sonic-pins', req['tree']]
        with patch.object(receipt, 'git', side_effect=answers):
            result = receipt.checkout_request(self.root, contract, event=event, run=123, attempt=1)
        self.assertEqual(result['merge'], req['merge'])
        for parents in (req['head'], req['head'] + ' ' + req['base'], '9' * 40 + ' ' + req['head']):
            with patch.object(receipt, 'git', side_effect=[req['merge'], '\n'.join('parent ' + p for p in parents.split()) + '\n\nMerge']), self.assertRaises(ValueError):
                receipt.checkout_request(self.root, contract, event=event, run=123, attempt=1)
        event['pull_request']['head']['repo']['full_name'] = 'someone/sonic-buildimage'
        with self.assertRaises(ValueError):
            receipt.checkout_request(self.root, contract, event=event, run=123, attempt=1)

    def test_rerun_without_origin_does_not_create_replacement(self):
        args = SimpleNamespace(contract=self.root / 'contract.json', workspace=self.root,
                               directory=self.root / 'request', existing_only=False)
        receipt.write(args.contract, {})
        event = self.root / 'event.json'
        event.write_text('{}')
        env = {'GITHUB_REPOSITORY': receipt.REPOSITORY, 'GITHUB_EVENT_NAME': 'pull_request',
               'GITHUB_EVENT_PATH': str(event), 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
        with patch.dict(os.environ, env), patch.object(receipt, 'checkout_request', return_value={**request(), 'request_attempt': 2}), \
                patch.object(receipt, 'api', return_value={'total_count': 0, 'artifacts': []}):
            with self.assertRaisesRegex(ValueError, 'cannot mint'):
                receipt.normal_origin(args)
        self.assertFalse(args.directory.exists())

    def test_exact_successful_job_metadata_is_accepted(self):
        req, artifact, run, jobs = metadata()
        self.assertEqual(receipt.worker_metadata(req, artifact, run, jobs)['id'], 201)

    def test_wrong_actor_source_controller_repository_or_attempt_rejected(self):
        for field in ('actor', 'head_sha', 'head_repository', 'event', 'run_attempt', 'conclusion'):
            req, artifact, run, jobs = metadata()
            values = {'actor': {'login': 'someone'}, 'head_sha': '9' * 40,
                      'head_repository': {'full_name': 'someone/fork'}, 'event': 'pull_request',
                      'run_attempt': 2, 'conclusion': 'failure'}
            run[field] = values[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipt.worker_metadata(req, artifact, run, jobs)

    def test_partial_cleanup_missing_phase_unknown_failure_and_duplicate_job_rejected(self):
        for kind in ('cleanup', 'missing', 'unknown', 'duplicate', 'labels', 'busy'):
            req, artifact, run, jobs = metadata()
            if kind == 'cleanup':
                jobs[0]['steps'][-3]['status'] = 'in_progress'
            elif kind == 'missing':
                jobs[0]['steps'] = jobs[0]['steps'][1:]
            elif kind == 'unknown':
                jobs[0]['steps'].append({'name': 'Unexpected failure', 'status': 'completed', 'conclusion': 'failure'})
            elif kind == 'duplicate':
                jobs.append(copy.deepcopy(jobs[0]))
            elif kind == 'labels':
                jobs[0]['labels'] = ['ubuntu-latest']
            else:
                jobs[0]['status'] = 'in_progress'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                receipt.worker_metadata(req, artifact, run, jobs)

    def test_duplicate_expired_or_oversized_artifact_rejected(self):
        _, artifact, _, _ = metadata()
        self.assertEqual(receipt.unique_artifact([artifact], artifact['name'])['id'], 300)
        for rows in ([artifact, copy.deepcopy(artifact)], [{**artifact, 'expired': True}],
                     [{**artifact, 'size_in_bytes': receipt.TOTAL_LIMIT + 1}]):
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                receipt.unique_artifact(rows, artifact['name'])
        with self.assertRaises(ValueError):
            receipt.inventory({'total_count': 101, 'artifacts': []}, 'artifacts')

    def test_json_duplicate_keys_secret_paths_symlinks_and_size_rejected(self):
        target = self.root / 'data.json'
        for data in ('{"schema":1,"schema":2}', '{"authorization":"Bearer hidden"}'):
            target.write_text(data)
            with self.assertRaises(ValueError):
                receipt.read(target)
        target.write_bytes(b' ' * (receipt.MEMBER_LIMIT + 1))
        with self.assertRaises(ValueError):
            receipt.read(target)
        target.write_text('{}')
        link = self.root / 'link.json'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            receipt.read(link)

    def test_public_bundle_rejects_unknown_receipt_before_copying(self):
        req = request()
        private = self.root / 'request'
        public = self.root / 'public'
        public.mkdir()
        receipt.write(private / 'payload' / 'request.json', req)
        receipt.write(private / 'binding.json', {'request': req, 'request_artifact': 12,
            'request_sha256': receipt.sha((private / 'payload' / 'request.json').read_bytes())})
        receipt.write(public / 'worker.json', {'source_revision': req['merge'], 'dispatcher_revision': req['controller'],
            'review_catalog': req['catalog'], 'image': 'p4rt'})
        for phase in receipt.PHASES:
            receipt.write(public / ('worker-' + phase + '.json'), {'phase': phase, 'status': 'passed', 'exit_code': 0,
                'source_revision': req['merge'], 'image': 'p4rt', 'no_oom_events': True})
        receipt.write(public / 'unexpected.json', {})
        args = SimpleNamespace(directory=private, public=public, output=self.root / 'acceptance')
        with self.assertRaisesRegex(ValueError, 'Unsafe receipt'):
            receipt.bundle(args)
        self.assertFalse(args.output.exists())


if __name__ == '__main__':
    unittest.main()
