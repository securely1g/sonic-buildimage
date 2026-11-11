"""Synthetic metadata/byte fixtures only; no package, archive or process execution."""
import copy
import json
import unittest
from unittest import mock

from tools.bazel.ci import native_payload_recovery as recovery

REQUEST = b'{\n  "base": "806f0efb59164a1edfa6361da90c8a2fa8e424d9",\n  "catalog": "e6ef022c94dd2fdeeac364dfd591f9701cd37c29",\n  "contract_sha256": "1120ce7e974722a7430f4fd1416c6d6bd1dd375bf956c711a12bb4b41eecb7ea",\n  "controller": "87b459c50a4ee8c90679a1b3756a5bb724c6d765",\n  "head": "b89a8ab3c1a0aa5ff300207dcbef6ac2346ac941",\n  "image": "gearbox",\n  "merge": "ede0723988a4d032eba39928a35f561afd202cec",\n  "normal_run": 42823372355,\n  "pr": 196,\n  "repository": "securely1g/sonic-buildimage",\n  "request_attempt": 1,\n  "schema": 2,\n  "tree": "176dd1e81ee3e13764551d9d9ee5a5d3b67fcbe9",\n  "workflow": ".github/workflows/bazel-oci.yml"\n}\n'
CONTRACT = {'audit_labels': ['//dockers/docker-base-trixie:runtime_package_metadata',
                  '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
                  '//dockers/docker-config-engine-trixie:docker-config-engine-trixie.debug_symbols',
                  '//dockers/docker-config-engine-trixie:runtime_package_metadata',
                  '//dockers/docker-config-engine-trixie:source_runtime',
                  '//platform/vs/docker-gbsyncd-vs/config:files',
                  '//platform/vs/docker-gbsyncd-vs:debug_apt_selection',
                  '//platform/vs/docker-gbsyncd-vs:debug_installed_packages',
                  '//platform/vs/docker-gbsyncd-vs:debug_package_metadata',
                  '//platform/vs/docker-gbsyncd-vs:debug_tools',
                  '//platform/vs/docker-gbsyncd-vs:docker-gbsyncd-vs',
                  '//platform/vs/docker-gbsyncd-vs:docker-gbsyncd-vs-dbg',
                  '//platform/vs/docker-gbsyncd-vs:docker-gbsyncd-vs-dbg.gz',
                  '//platform/vs/docker-gbsyncd-vs:docker-gbsyncd-vs.gz',
                  '//platform/vs/docker-gbsyncd-vs:installed_packages',
                  '//platform/vs/docker-gbsyncd-vs:package_policy',
                  '//platform/vs/docker-gbsyncd-vs:runtime_apt_selection',
                  '//platform/vs/docker-gbsyncd-vs:runtime_layer',
                  '//platform/vs/docker-gbsyncd-vs:runtime_package_metadata',
                  '//platform/vs/docker-gbsyncd-vs:source_contract',
                  '//platform/vs/docker-gbsyncd-vs:source_package_receipt',
                  '//platform/vs/docker-gbsyncd-vs:source_runtime',
                  '//platform/vs/docker-gbsyncd-vs:test_redis',
                  '//platform/vs/docker-syncd-vs:installation_links',
                  '//platform/vs/docker-syncd-vs:source_debug_payloads'],
 'catalog': 'e6ef022c94dd2fdeeac364dfd591f9701cd37c29',
 'controller': '87b459c50a4ee8c90679a1b3756a5bb724c6d765',
 'fixture_sources': {},
 'image': 'gearbox',
 'outputs': {'base-debug.tar': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie.debug_symbols',
             'base-packages.json': '//dockers/docker-base-trixie:runtime_package_metadata',
             'config-engine-packages.json': '//dockers/docker-config-engine-trixie:runtime_package_metadata',
             'config-engine-source.tar': '//dockers/docker-config-engine-trixie:source_runtime',
             'config-files.tar': '//platform/vs/docker-gbsyncd-vs/config:files',
             'debug-apt.json': '//platform/vs/docker-gbsyncd-vs:debug_apt_selection',
             'debug-packages.json': '//platform/vs/docker-gbsyncd-vs:debug_package_metadata',
             'debug-tools.tar': '//platform/vs/docker-gbsyncd-vs:debug_tools',
             'debug.status': '//platform/vs/docker-gbsyncd-vs:debug_installed_packages',
             'docker-gbsyncd-vs-dbg.gz': '//platform/vs/docker-gbsyncd-vs:docker-gbsyncd-vs-dbg.gz',
             'docker-gbsyncd-vs.gz': '//platform/vs/docker-gbsyncd-vs:docker-gbsyncd-vs.gz',
             'installation-links.tar': '//platform/vs/docker-syncd-vs:installation_links',
             'policy.json': '//platform/vs/docker-gbsyncd-vs:package_policy',
             'runtime-apt.json': '//platform/vs/docker-gbsyncd-vs:runtime_apt_selection',
             'runtime-apt.tar': '//platform/vs/docker-gbsyncd-vs:runtime_layer',
             'runtime-packages.json': '//platform/vs/docker-gbsyncd-vs:runtime_package_metadata',
             'runtime.status': '//platform/vs/docker-gbsyncd-vs:installed_packages',
             'source-contract.json': '//platform/vs/docker-gbsyncd-vs:source_contract',
             'source-debug.tar': '//platform/vs/docker-syncd-vs:source_debug_payloads',
             'source-receipt.json': '//platform/vs/docker-gbsyncd-vs:source_package_receipt',
             'source-runtime.tar': '//platform/vs/docker-gbsyncd-vs:source_runtime',
             'test-redis.tar': '//platform/vs/docker-gbsyncd-vs:test_redis'},
 'schema': 2,
 'selected_modules': {'libnl3': '3.7.0.sonic.7',
                      'p4c': '1.2.4.2.sonic.3',
                      'p4lang-bmv2': '1.15.0.sonic.4',
                      'p4lang-pi': '0.1.3.sonic.5',
                      'rules_distroless': '0.9.4.sonic.19',
                      'sonic-build-infra': '0.0.28-9965823498ab54816de5addb6084638470dd04d7',
                      'sonic-dash': '0.0.1-2cbfdfa1205c0d03003444e5c80f89adb8c3df59',
                      'sonic-fips': '0.0.6-18a778db501d455f8a1b6d1e3ef8e17121661177',
                      'sonic-sairedis': '0.0.4-ee19458a7d1b417317aaf34fe48969da7293d670',
                      'sonic-swss-common': '0.0.9-334389eae54afd1f4f5ee69f89c3a7ad2062ee61'},
 'worker_pool': 'sonic-slave-container-k3s'}


class RecoveryInventoryTest(unittest.TestCase):
    def fixture(self):
        contract = copy.deepcopy(CONTRACT)
        request = json.loads(REQUEST)
        case = recovery.CASES['gearbox']
        run = {'id': case['run'], 'run_attempt': 1, 'head_sha': request['controller'],
               'head_branch': 'codex/source-image-worker-p4rt-feature', 'workflow_id': 380324045,
               'path': recovery.worker.WORKFLOW, 'event': 'workflow_dispatch', 'status': 'completed',
               'conclusion': 'failure', 'repository': {'full_name': recovery.worker.REPOSITORY},
               'head_repository': {'full_name': recovery.worker.REPOSITORY},
               'actor': {'login': 'securely1g'}, 'triggering_actor': {'login': 'securely1g'}}
        steps = [{'name': n, 'status': 'completed', 'conclusion': state}
                 for state, names in [('success', case['success_steps']),
                                      ('skipped', case['skipped_steps']),
                                      ('failure', [recovery.CLEANUP])] for n in names]
        job = {'id': case['job'], 'run_id': case['run'], 'run_attempt': 1,
               'head_sha': request['controller'], 'name': 'Source image (gearbox)',
               'status': 'completed', 'conclusion': 'failure', 'runner_id': 1,
               'runner_name': case['runner_name'], 'labels': ['sonic-slave-container-k3s'], 'steps': steps}
        artifact = {'id': case['artifact'], 'expired': False,
                    'name': 'source-image-gearbox-' + str(case['run']) + '-1',
                    'workflow_run': {'id': case['run'], 'head_sha': request['controller']},
                    'size_in_bytes': 100, 'digest': 'sha256:' + 'a' * 64}
        return [REQUEST, contract, run, job, artifact]

    def inventory_fixture(self):
        args = self.fixture()
        contract, artifact = args[1], args[4]
        public = {n: b'{}' for n in recovery.worker.public_json_names(json.loads(REQUEST))}
        payloads = {n: public.get(n, ('synthetic payload ' + n).encode()) for n in contract['outputs']}
        declared = {n: {'bytes': len(v), 'sha256': recovery.digest(v)} for n, v in payloads.items()}
        public['public-receipt.json'] = json.dumps({'artifacts': declared}, sort_keys=True).encode()
        data = {**payloads, **public}
        paths = {n: 'observed-prefix/' + n for n in data}
        rows = [{'name': paths[n], 'type': 'regular', 'bytes': len(v),
                 'sha256': recovery.digest(v), 'encrypted': False} for n, v in data.items()]
        inventory = {'schema': 1, 'artifact_id': artifact['id'], 'zip_sha256': artifact['digest'][7:],
                     'zip_bytes': 100, 'central_directory_complete': True,
                     'all_members_crc_verified': True, 'members': rows}
        return args + [inventory, public, paths]

    def test_failed_producer_is_inspection_only(self):
        result = recovery.admit_original(*self.fixture())
        self.assertEqual(result['original_conclusion'], 'failure')
        self.assertFalse(result['canonical_native_acceptance'])
        self.assertFalse(result['fresh_build_or_runtime'])

    def test_success_cannot_replace_original_failure(self):
        args = self.fixture()
        args[2]['conclusion'] = 'success'
        with self.assertRaisesRegex(ValueError, 'producer run'):
            recovery.admit_original(*args)

    def test_noncleanup_failure_and_missing_step_refused(self):
        for change in ('fail', 'remove'):
            args = self.fixture()
            if change == 'fail':
                args[3]['steps'][0]['conclusion'] = 'failure'
            else:
                args[3]['steps'].pop(0)
            with self.assertRaises(ValueError):
                recovery.admit_original(*args)

    def test_original_request_bytes_and_contract_stay_bound(self):
        args = self.fixture()
        args[0] += b'\n'
        with self.assertRaisesRegex(ValueError, 'request bytes changed'):
            recovery.admit_original(*args)
        args = self.fixture()
        args[1]['catalog'] = '0' * 40
        with self.assertRaisesRegex(ValueError, 'source contract'):
            recovery.admit_original(*args)

    def test_other_artifact_and_attempt_refused(self):
        args = self.fixture()
        args[4]['id'] += 1
        with self.assertRaisesRegex(ValueError, 'artifact identity'):
            recovery.admit_original(*args)
        args = self.fixture()
        args[2]['run_attempt'] = True
        with self.assertRaisesRegex(ValueError, 'producer run'):
            recovery.admit_original(*args)

    def test_inventory_keeps_original_identity_and_limits(self):
        args = self.inventory_fixture()
        with mock.patch.object(recovery, 'public_semantics', return_value={'synthetic': True}) as verify:
            result = recovery.review_public_upload(*args)
        verify.assert_called_once_with(json.loads(REQUEST), args[1], args[6])
        self.assertEqual(result['status'], 'PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED')
        self.assertEqual(result['original_conclusion'], 'failure')
        self.assertFalse(result['canonical_native_acceptance'])
        self.assertIn('private SDK/layout', result['limits'])
        self.assertEqual(result['logical_member_paths'], args[7])

    def test_payload_hash_conflict_is_not_accepted(self):
        args = self.inventory_fixture()
        row = next(r for r in args[5]['members'] if r['name'].endswith('docker-gbsyncd-vs.gz'))
        row['sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'payload differs'):
            recovery.review_public_upload(*args)

    def test_public_worker_metadata_cannot_be_restamped(self):
        args = self.inventory_fixture()
        args[6]['worker.json'] = b'{"schema":2,"cache_mode":"remote_only_run_attempt"}'
        with self.assertRaisesRegex(ValueError, 'public JSON bytes differ'):
            recovery.review_public_upload(*args)

    def test_incomplete_crc_or_duplicate_member_refused(self):
        for change in ('crc', 'duplicate'):
            args = self.inventory_fixture()
            if change == 'crc':
                args[5]['all_members_crc_verified'] = False
            else:
                args[5]['members'].append(copy.deepcopy(args[5]['members'][0]))
            with self.assertRaises(ValueError):
                recovery.review_public_upload(*args)

    def test_unsafe_paths_special_members_and_encryption_refused(self):
        for key, value in [('name', '../escape'), ('name', 'a\\b'), ('type', 'symlink'),
                           ('encrypted', True), ('bytes', recovery.MAX_ZIP_BYTES + 1)]:
            args = self.inventory_fixture()
            args[5]['members'][0][key] = value
            with self.assertRaises(ValueError):
                recovery.review_public_upload(*args)

    def test_missing_json_and_invented_ci_binding_refused(self):
        args = self.inventory_fixture()
        args[6].pop('worker.json')
        with self.assertRaisesRegex(ValueError, 'Public JSON inventory'):
            recovery.review_public_upload(*args)
        args = self.inventory_fixture()
        args[6]['ci-binding.json'] = b'{}'
        with self.assertRaisesRegex(ValueError, 'Public JSON inventory'):
            recovery.review_public_upload(*args)

    def test_mapping_cannot_alias_or_invent_member(self):
        for change in ('alias', 'missing'):
            args = self.inventory_fixture()
            names = list(args[7])
            args[7][names[0]] = args[7][names[1]] if change == 'alias' else 'absent/member'
            with self.assertRaises(ValueError):
                recovery.review_public_upload(*args)

    def test_existing_semantic_validator_receives_original_source(self):
        request = json.loads(REQUEST)
        observed = {'fixture_sources': CONTRACT['fixture_sources'],
                    'tested_revision_parents': [request['base'], request['head']]}
        with mock.patch.object(recovery.syncd_receipt_semantics, 'verify', return_value=observed) as verify:
            recovery.public_semantics(request, CONTRACT, {})
        self.assertEqual(verify.call_args.kwargs,
                         {'source': request['merge'], 'outputs': CONTRACT['outputs'], 'image': 'gearbox'})
        observed['tested_revision_parents'] = ['0' * 40, request['head']]
        with mock.patch.object(recovery.syncd_receipt_semantics, 'verify', return_value=observed):
            with self.assertRaisesRegex(ValueError, 'installed-service inputs'):
                recovery.public_semantics(request, CONTRACT, {})
