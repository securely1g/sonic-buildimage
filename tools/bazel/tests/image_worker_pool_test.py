"""Pure JSON/mocked admission tests; these fixtures assert no native acceptance."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.bazel.ci import image_worker_receipt as receipt, syncd_receipt_semantics


DPOOL = 'sonic-slave-container-dpool'
K3S = 'sonic-slave-container-k3s'


def selected_case(family='p4rt', pool=K3S):
    value = {'schema': 1 if family == 'p4rt' else 2, 'repository': receipt.REPOSITORY,
        'workflow': receipt.WORKFLOW, 'normal_run': 123, 'request_attempt': 1, 'pr': 196,
        'head': '1' * 40, 'base': '2' * 40, 'merge': '3' * 40, 'tree': '4' * 40,
        'catalog': '5' * 40, 'controller': '6' * 40}
    value.update({'owner': '7' * 40} if family == 'p4rt' else {'image': family})
    contract = {key: value[key] for key in ('catalog', 'controller')}
    contract.update({'owner': value['owner']} if family == 'p4rt' else {'image': family})
    if pool is not None:
        contract['worker_pool'] = pool
    value['contract_sha256'] = receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode())
    name = 'ks-source-worker-1' if pool == K3S else 'sonic-slave-lgh114-1'
    job = {'id': 201, 'run_id': 789, 'run_attempt': 1, 'head_sha': value['controller'],
        'name': 'Source image (' + family + ')', 'status': 'completed', 'conclusion': 'success',
        'runner_id': 31, 'runner_name': name,
        'labels': [K3S] if pool == K3S else [DPOOL, name],
        'steps': [{'name': name, 'status': 'completed', 'conclusion': 'success'}
                  for name in receipt.required_steps(value)]}
    run = {'id': 789, 'run_attempt': 1, 'head_sha': value['controller'],
        'repository': {'id': 1, 'full_name': receipt.REPOSITORY},
        'head_repository': {'full_name': receipt.REPOSITORY}, 'path': receipt.WORKFLOW,
        'event': 'workflow_dispatch', 'actor': {'login': 'securely1g'},
        'triggering_actor': {'login': 'securely1g'}, 'status': 'completed', 'conclusion': 'success'}
    artifact = {'name': receipt.acceptance_name(value), 'expired': False, 'size_in_bytes': 1,
        'workflow_run': {'id': 789, 'head_sha': value['controller'], 'repository_id': 1,
                         'head_repository_id': 1}}
    return value, contract, job, run, artifact


def resource_case(job, pool=K3S):
    profile = {'jobs': 2, 'cpu': 2, 'jvm_heap_gib': 3, 'scheduler_memory_mb': 8000,
               'loaded_container_memory_gib': 4}
    preflight = {'build_profile': dict(profile), 'checks': {
        'cache_mount': True, 'cache_storage': True, 'cache_writable': True},
        'facts': {'cpu_limit': 6 if pool == K3S else 4, 'memory_limit_bytes': 20 * 1024**3,
            'pool': pool, 'worker': job['runner_name'], 'cache_root': {
                'namespace': 'actions_work_source_images',
                'worker_directory': 'source-images/' + job['runner_name'],
                'backing_mount': 'actions', 'shared_with_workspace': True,
                'lifetime': 'current_worker_mount', 'cross_pod_reuse': 'not_established',
                'work_root_uid': 0, 'work_root_gid': 0, 'work_root_mode': '0o755',
                'uid': 1000, 'gid': 1000, 'mode': '0o700', 'writable': True,
                'write_probe_closed': True}, 'cache_storage': {
                'filesystem': 'ext4', 'free_bytes': 50 * 1024**3,
                'backing_mount': 'actions', 'shared_with_workspace': True}}}
    return {'build_profile': profile}, preflight


class WorkerPoolReceiptTest(unittest.TestCase):
    def setUp(self):
        for target, name in ((receipt.urllib.request, 'urlopen'), (receipt.subprocess, 'check_output')):
            guard = patch.object(target, name, side_effect=AssertionError('No network or process'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_legacy_missing_pool_keeps_exact_dpool_name_and_labels(self):
        value, contract, job, run, artifact = selected_case(pool=None)
        self.assertEqual(receipt.reviewed_worker_pool(value, contract), DPOOL)
        self.assertEqual(receipt.worker_metadata(value, artifact, run, [job], contract), job)
        for changes in ({'runner_name': 'ks-source-worker-1'}, {'labels': [DPOOL]},
                        {'runner_name': 'sonic-slave-lgh114-5'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                receipt.worker_metadata(value, artifact, run, [{**job, **changes}], contract)

    def test_k3s_all_families_use_actual_job_without_per_name_label(self):
        for family in ('p4rt', 'syncd', 'gearbox'):
            value, contract, job, run, artifact = selected_case(family)
            with self.subTest(family=family):
                self.assertNotIn(job['runner_name'], job['labels'])
                self.assertEqual(receipt.worker_metadata(value, artifact, run, [job], contract), job)

    def test_entire_contract_is_bound_before_metadata_or_pool_selection(self):
        value, contract, _, _, _ = selected_case()
        for changed in ({**contract, 'worker_pool': DPOOL}, {**contract, 'unreviewed': True},
                        {**contract, 'controller': '8' * 40}):
            with self.subTest(changed=changed), patch.object(receipt, 'worker_metadata') as metadata:
                with self.assertRaisesRegex(ValueError, 'Reviewed source contract differs'):
                    receipt.verify_evidence(Path('.'), value, {}, changed)
                metadata.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Reviewed source contract differs'):
            receipt.worker_metadata(value, {}, {}, [], {**contract, 'worker_pool': DPOOL})

    def test_pool_enum_is_closed_even_with_a_matching_contract_hash(self):
        for pool in ('k3s', 'private', True, 3):
            value, contract, _, _, _ = selected_case(pool=pool)
            with self.subTest(pool=pool), self.assertRaisesRegex(ValueError, 'Unsupported reviewed worker pool'):
                receipt.reviewed_worker_pool(value, contract)

    def test_artifact_hints_and_request_extras_cannot_select_k3s(self):
        value, contract, job, run, artifact = selected_case(pool=None)
        artifact['worker_pool'] = K3S
        job.update(runner_name='ks-source-worker-1', labels=[K3S])
        with self.assertRaises(ValueError):
            receipt.worker_metadata(value, artifact, run, [job], contract)
        with self.assertRaisesRegex(ValueError, 'Unexpected request fields'):
            receipt.reviewed_worker_pool({**value, 'worker_pool': K3S}, contract)

    def test_actual_job_identity_attempt_state_and_pool_are_required(self):
        value, contract, job, run, artifact = selected_case()
        changes = [{'runner_id': 0}, {'runner_id': True}, {'id': 0}, {'run_id': 790},
            {'run_attempt': 2}, {'head_sha': '9' * 40}, {'labels': [DPOOL]},
            {'runner_name': 'ks-bad-'}, {'runner_name': 'ks-' + 'a' * 61},
            {'status': 'queued'}, {'conclusion': 'cancelled'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                receipt.worker_metadata(value, artifact, run, [{**job, **change}], contract)
        for jobs in ([], [job, job]):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                receipt.worker_metadata(value, artifact, run, jobs, contract)

    def test_both_actual_run_actors_must_match(self):
        value, contract, job, run, artifact = selected_case()
        for field in ('actor', 'triggering_actor'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipt.worker_metadata(value, artifact, {**run, field: {'login': 'other'}}, [job], contract)

    def test_dpool_and_k3s_keep_distinct_exact_resource_profiles(self):
        for pool in (DPOOL, K3S):
            _, _, job, _, _ = selected_case(pool=pool)
            worker, preflight = resource_case(job, pool)
            self.assertEqual(receipt.verify_worker_profile(worker, preflight, job, pool), worker['build_profile'])
            for field, value in (('cpu_limit', 4 if pool == K3S else 6), ('memory_limit_bytes', 21 * 1024**3)):
                changed = copy.deepcopy(preflight); changed['facts'][field] = value
                with self.subTest(pool=pool, field=field), self.assertRaises(ValueError):
                    receipt.verify_worker_profile(worker, changed, job, pool)
            changed = copy.deepcopy(preflight); changed['build_profile']['jobs'] = 3
            with self.assertRaises(ValueError):
                receipt.verify_worker_profile(worker, changed, job, pool)

    def test_syncd_and_gearbox_capacity_matches_the_authenticated_pool(self):
        for family in ('syncd', 'gearbox'):
            for pool in (DPOOL, K3S):
                value, contract, job, _, _ = selected_case(family, pool)
                verified = receipt.reviewed_worker_pool(value, contract)
                worker, preflight = resource_case(job, pool)
                receipt.verify_worker_profile(worker, preflight, job, verified)
                cpus = receipt.expected_worker_cpus(verified)
                syncd_receipt_semantics.verify_pool_resources(preflight['facts'], cpus)
                other = 4 if pool == K3S else 6
                with self.assertRaises(AssertionError):
                    syncd_receipt_semantics.verify_pool_resources({**preflight['facts'], 'cpu_limit': other}, cpus)

    def test_k3s_cache_directory_cannot_claim_other_storage_or_cross_pod_reuse(self):
        _, _, job, _, _ = selected_case(); worker, preflight = resource_case(job)
        for field, value in (('namespace', 'other'), ('worker_directory', 'source-images/ks-other'),
                ('cross_pod_reuse', 'established'), ('shared_with_workspace', False), ('uid', 0),
                ('mode', '0o777'), ('writable', False), ('write_probe_closed', False),
                ('work_root_uid', True), ('private_path', '/private')):
            changed = copy.deepcopy(preflight); changed['facts']['cache_root'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipt.verify_worker_profile(worker, changed, job, K3S)

    def test_k3s_cache_mount_space_and_measured_worker_must_pass(self):
        _, _, job, _, _ = selected_case(); worker, preflight = resource_case(job)
        for field, value in (('filesystem', 'tmpfs'), ('free_bytes', 50 * 1024**3 - 1),
                             ('backing_mount', 'other'), ('shared_with_workspace', False)):
            changed = copy.deepcopy(preflight); changed['facts']['cache_storage'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipt.verify_worker_profile(worker, changed, job, K3S)
        for section, field, value in (('facts', 'worker', 'ks-other'), ('facts', 'pool', DPOOL),
                                     ('checks', 'cache_writable', False)):
            changed = copy.deepcopy(preflight); changed[section][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipt.verify_worker_profile(worker, changed, job, K3S)

    def bind_case(self, family='p4rt', *, environment_change=None, job_change=None, contract_change=None):
        value, contract, job, _, _ = selected_case(family)
        job.update(status='in_progress', conclusion=None)
        job.update(job_change or {})
        contract.update(contract_change or {})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt.write(root / 'payload/request.json', value)
            original = (root / 'payload/request.json').read_bytes()
            receipt.write(root / ('tools/bazel/ci/' + family + '_worker_contract.json'), contract)
            receipt.write(root / 'origin.json', {'run': {'id': 123, 'head_sha': value['head'], 'run_attempt': 1},
                'artifact': {'id': 456, 'name': receipt.origin_name(123, family)}}, public=False)
            env = {'SELECTED_IMAGE': family, 'SOURCE_REVISION': value['merge'],
                'SOURCE_REGISTRY_REVISION': value['catalog'], 'GITHUB_SHA': value['controller'],
                'GITHUB_RUN_ID': '789', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_ACTOR': 'securely1g',
                'GITHUB_TRIGGERING_ACTOR': 'securely1g', 'WORKER_POOL': K3S, 'RUNNER_NAME': 'ks-source-worker-1'}
            env.update(environment_change or {})
            def api(endpoint):
                if endpoint == '/pulls/196':
                    return {'state': 'open', 'head': {'sha': value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
                        'base': {'sha': value['base'], 'ref': 'main', 'repo': {'full_name': receipt.REPOSITORY}}}
                if endpoint == '/git/ref/heads/main':
                    return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': value['base']}}
                if endpoint == '/actions/runs/789/jobs?filter=latest&per_page=100':
                    return {'total_count': 1, 'jobs': [job]}
                self.fail('Unexpected metadata request: ' + endpoint)
            answers = [value['merge'], value['tree'],
                'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge']
            with patch.dict(os.environ, env), patch.object(receipt, 'git', side_effect=answers), \
                    patch.object(receipt, 'api', side_effect=api) as metadata:
                try:
                    receipt.bind_worker(SimpleNamespace(directory=root, workspace=root, image=family))
                except Exception:
                    self.assertFalse((root / 'binding.json').exists())
                    if environment_change and 'WORKER_POOL' in environment_change or contract_change:
                        metadata.assert_not_called()
                    raise
            binding = receipt.read(root / 'binding.json')
            self.assertEqual((root / 'payload/request.json').read_bytes(), original)
            self.assertEqual(binding, {'request': value, 'request_artifact': 456,
                                       'request_sha256': receipt.sha(original)})

    def test_prebuild_binding_keeps_original_bytes_and_schema_for_every_family(self):
        for family in ('p4rt', 'syncd', 'gearbox'):
            with self.subTest(family=family):
                self.bind_case(family)

    def test_prebuild_pool_mismatch_or_contract_drift_cannot_query_or_bind(self):
        with self.assertRaisesRegex(ValueError, 'Dispatched pool differs'):
            self.bind_case(environment_change={'WORKER_POOL': DPOOL})
        with self.assertRaisesRegex(ValueError, 'Reviewed source contract differs'):
            self.bind_case(contract_change={'worker_pool': DPOOL})

    def test_prebuild_assigned_runner_and_exact_attempt_are_required(self):
        for change in ({'runner_name': 'ks-other'}, {'run_attempt': 2}, {'runner_id': 0},
                       {'labels': [DPOOL]}, {'status': 'completed', 'conclusion': 'success'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.bind_case(job_change=change)


if __name__ == '__main__':
    unittest.main()
