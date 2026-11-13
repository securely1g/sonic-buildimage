#!/usr/bin/env python3
"""Mocked API boundary tests; no network or repository commands are executed."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import image_worker_receipt as owner
from tools.bazel.ci import observe_final_native_provenance as observe

HERE = Path(__file__).parent


class NativeProvenanceObservationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = json.loads((HERE / 'vs_native_api_provenance_inputs.json').read_text())
        self.original = json.loads((HERE / 'vs_native_api_provenance_test_inputs.json').read_text())
        self.source = self.root / 'source'
        contracts = self.source / 'tools/bazel/ci'
        contracts.mkdir(parents=True)
        (contracts / 'syncd_worker_contract.json').write_text(self.original['contract_text'])
        self.request = self.root / 'request.json'
        self.request.write_text(self.original['request_text'])
        self.output = self.root / 'output'
        self.operator = {'id': 123456, 'run_attempt': 1, 'head_sha': 'c' * 40,
            'event': 'workflow_dispatch', 'path': owner.WORKFLOW,
            'repository': {'full_name': owner.REPOSITORY},
            'head_repository': {'full_name': owner.REPOSITORY},
            'actor': {'login': 'securely1g'}, 'triggering_actor': {'login': 'securely1g'}}
        self.env = {'GITHUB_REPOSITORY': owner.REPOSITORY,
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_ACTOR': 'securely1g',
            'GITHUB_TRIGGERING_ACTOR': 'securely1g', 'GITHUB_RUN_ID': '123456',
            'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'vs_native_api_provenance',
            'GITHUB_SHA': 'c' * 40, 'GITHUB_REF': 'refs/heads/observer',
            'RUNNER_NAME': 'fixture-worker', 'GITHUB_OUTPUT': str(self.root / 'github-output'),
            'GH_TOKEN': 'fixture-secret-never-recorded'}
        self.environment = mock.patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.endpoints = []
        self.api = mock.patch.object(owner, 'api', side_effect=self.respond)
        self.api.start()
        self.addCleanup(self.api.stop)

    def respond(self, endpoint):
        self.endpoints.append(endpoint)
        selected = self.plan['families']['syncd']
        if endpoint == '/actions/runs/123456':
            return copy.deepcopy(self.operator)
        if endpoint == '/actions/artifacts/' + str(selected['request_artifact']['id']):
            return copy.deepcopy(self.original['origin_artifact'])
        if endpoint == '/actions/runs/' + str(selected['request']['normal_run']):
            return copy.deepcopy(self.original['origin_run'])
        if endpoint == '/actions/runs/' + str(selected['request']['normal_run']) + '/artifacts?per_page=100':
            return {'total_count': 1, 'artifacts': [copy.deepcopy(self.original['origin_artifact'])]}
        if endpoint.startswith('/actions/artifacts?per_page=100&name='):
            return {'total_count': 1, 'artifacts': [copy.deepcopy(self.original['artifact'])]}
        if endpoint == '/actions/runs/' + str(selected['producer_run']):
            return copy.deepcopy(self.original['run'])
        if endpoint == '/actions/runs/' + str(selected['producer_run']) + '/jobs?filter=latest&per_page=100':
            return {'total_count': 1, 'jobs': [copy.deepcopy(self.original['job'])]}
        self.fail('Unexpected endpoint: ' + endpoint)

    def origin(self):
        observe.authenticate_origin(owner, self.plan, 'syncd', self.output)

    def capture(self):
        observe.capture(owner, self.plan, 'syncd', self.source, self.request, self.output)

    def test_actual_manual_origin_and_original_request_remain_distinct(self):
        original_env = dict(os.environ)
        self.origin()
        value = owner.read(self.output / 'origin.json', public=False)
        self.assertEqual(value, {'artifact': self.original['origin_artifact'],
                                 'run': self.original['origin_run']})
        self.assertEqual(dict(os.environ), original_env)
        self.assertEqual(len(self.endpoints), 4)

    def test_capture_preserves_all_original_api_objects_and_request_bytes(self):
        self.origin()
        original_env = dict(os.environ)
        self.capture()
        expected = {'artifact': self.original['artifact'], 'run': self.original['run'],
            'job': self.original['job'], 'jobs': [self.original['job']],
            'origin': self.original['origin_artifact']}
        self.assertEqual(owner.read(self.output / 'provenance.json', public=False), expected)
        self.assertEqual((self.output / 'request.json').read_bytes(), self.request.read_bytes())
        observation = owner.read(self.output / 'observation.json', public=False)
        self.assertEqual(observation['operator']['run'], self.operator)
        self.assertEqual(observation['source_revision'], self.plan['source_revision'])
        self.assertFalse(observation['normal_consumer_job_passed'])
        self.assertFalse(observation['native_acceptance_executed'])
        self.assertNotIn('fixture-secret-never-recorded', json.dumps(observation))
        self.assertEqual(dict(os.environ), original_env)

    def test_changed_original_request_bytes_stop_before_api_capture(self):
        self.origin()
        self.endpoints.clear()
        self.request.write_text(self.original['request_text'] + ' ')
        with self.assertRaisesRegex(ValueError, 'size differs'):
            self.capture()
        self.assertEqual(self.endpoints, [])

    def test_other_acceptance_artifact_is_not_substituted(self):
        self.origin()
        self.original['artifact']['id'] += 1
        with self.assertRaisesRegex(ValueError, 'Fixed original artifact differs'):
            self.capture()
        self.assertFalse((self.output / 'provenance.json').exists())

    def test_failed_native_run_is_rejected_by_maintained_owner(self):
        self.origin()
        self.original['run']['conclusion'] = 'failure'
        with self.assertRaisesRegex(ValueError, 'Untrusted or incomplete worker run'):
            self.capture()
        self.assertFalse((self.output / 'provenance.json').exists())

    def test_wrong_actual_observer_revision_is_rejected(self):
        self.operator['head_sha'] = 'd' * 40
        with self.assertRaisesRegex(ValueError, 'Actual operator API context differs'):
            self.origin()
        self.assertFalse(self.output.exists())

    def test_actual_event_is_not_rewritten_to_normal_ci(self):
        with mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request'}):
            with self.assertRaisesRegex(ValueError, 'Unexpected actual observer context'):
                self.origin()
            self.assertEqual(os.environ['GITHUB_EVENT_NAME'], 'pull_request')
        self.assertEqual(self.endpoints, [])

    def test_changed_native_job_identity_is_not_accepted(self):
        self.origin()
        self.original['job']['id'] += 1
        with self.assertRaisesRegex(ValueError, 'Original producer job differs'):
            self.capture()

    def test_existing_provenance_is_never_replaced(self):
        self.origin()
        (self.output / 'provenance.json').write_text('original bytes')
        with self.assertRaises(FileExistsError):
            self.capture()
        self.assertEqual((self.output / 'provenance.json').read_text(), 'original bytes')

    def test_duplicate_original_request_is_rejected_by_maintained_owner(self):
        response = self.respond
        def duplicate(endpoint):
            value = response(endpoint)
            if endpoint.endswith('/artifacts?per_page=100'):
                value['artifacts'] *= 2
                value['total_count'] = 2
            return value
        with mock.patch.object(owner, 'api', side_effect=duplicate):
            with self.assertRaisesRegex(ValueError, 'Duplicate original CI request'):
                self.origin()

    def test_changed_owner_bytes_stop_before_import(self):
        path = self.source / 'tools/bazel/ci/image_worker_receipt.py'
        path.write_text('unreviewed owner')
        isolated_plan = {'owner_sources': {'tools/bazel/ci/image_worker_receipt.py':
            self.plan['owner_sources']['tools/bazel/ci/image_worker_receipt.py']}}
        with mock.patch.object(observe.importlib, 'import_module') as imported:
            with self.assertRaisesRegex(ValueError, 'Fixed input size differs'):
                observe.load_owner(self.source, isolated_plan)
            imported.assert_not_called()


if __name__ == '__main__':
    unittest.main()
