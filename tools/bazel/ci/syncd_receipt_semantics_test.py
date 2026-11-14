"""Pure receipt consistency and rejection tests; no source programs or downloads."""
import copy
import hashlib
import json
import socket
import subprocess
import unittest
from unittest.mock import patch

from tools.bazel.ci import syncd_receipt_semantics as semantics

SOURCE = 'a' * 40
OUTPUTS = {'docker-syncd-vs.gz': '//platform/vs/docker-syncd-vs:docker-syncd-vs.gz', 'docker-syncd-vs-dbg.gz': '//platform/vs/docker-syncd-vs:docker-syncd-vs-dbg.gz', 'source-runtime.tar': '//platform/vs/docker-syncd-vs:source_runtime_payloads', 'source-debug.tar': '//platform/vs/docker-syncd-vs:source_debug_payloads', 'source-contract.json': '//platform/vs/docker-syncd-vs:source_contract', 'source-receipt.json': '//platform/vs/docker-syncd-vs:source_package_receipt', 'policy.json': '//platform/vs/docker-syncd-vs:package_policy', 'config-files.tar': '//platform/vs/docker-syncd-vs/config:files', 'runtime-apt.tar': '//platform/vs/docker-syncd-vs:apt_deps', 'runtime-apt.json': '//platform/vs/docker-syncd-vs:runtime_apt_selection', 'debug-tools.tar': '//platform/vs/docker-syncd-vs:debug_tools', 'debug-apt.json': '//platform/vs/docker-syncd-vs:debug_apt_selection', 'installation-links.tar': '//platform/vs/docker-syncd-vs:installation_links', 'runtime-packages.json': '//platform/vs/docker-syncd-vs:runtime_package_metadata', 'runtime.status': '//platform/vs/docker-syncd-vs:installed_packages', 'debug-packages.json': '//platform/vs/docker-syncd-vs:debug_package_metadata', 'debug.status': '//platform/vs/docker-syncd-vs:debug_installed_packages', 'base-debug.tar': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie.debug_symbols', 'base-packages.json': '//dockers/docker-base-trixie:runtime_package_metadata', 'config-engine-packages.json': '//dockers/docker-config-engine-trixie:runtime_package_metadata', 'config-engine-source.tar': '//dockers/docker-config-engine-trixie:source_runtime', 'test-redis.tar': '//platform/vs/docker-gbsyncd-vs:test_redis'}


def encoded(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def fixture():
    # Compact synthetic JSON records, distinct from retained native acceptance.
    h = '1' * 64
    owner, parent = semantics.OWNER, semantics.PARENT
    artifacts = {n: {'target': t, 'bytes': 1, 'sha256': h} for n, t in OUTPUTS.items()}
    public = {'status': 'built', 'architecture': 'amd64', 'revision': SOURCE,
              'tested_revision_parents': ['b' * 40], 'artifacts': artifacts}
    profile = dict(semantics.BUILD_PROFILE)
    profile.update(name='two_actions', status='pilot',
                   minimum_memory_bytes=20 * 1024**3, minimum_cpu=4,
                   measured_memory_limit_bytes=20 * 1024**3, measured_cpu_limit=4.0)
    memory = {'limit': str(20 * 1024**3), 'events': dict.fromkeys(('oom', 'oom_kill', 'oom_group_kill'), 0)}
    docs = {'public-receipt.json': public,
            'worker.json': {'status': 'prepared', 'image': 'syncd', 'source_revision': SOURCE,
                            'architecture': 'amd64', 'distribution': 'trixie', 'docker_network': 'none',
                            'cache_mode': 'persistent_worker_local', 'build_profile': profile},
            'worker-preflight.json': {'status': 'passed', 'checks': {'bounded_resources': True},
                'build_profile': profile, 'facts': {'memory_limit_bytes': 20 * 1024**3, 'cpu_limit': 4, 'cpu_max': '400000 100000'},
                'image_build_executed': False, 'deb_generation_executed': False},
            'worker-tap.json': {'status': 'passed', 'private_network_namespace': True,
                               'tap_created_and_closed': True, 'device_major': 10, 'device_minor': 200},
            'build-summary.json': {'build-events.jsonl': {'finished': {'success': True, 'exit_code': 0},
                 'bazel_version': '8.5.1', 'truncated': False, 'targets': dict.fromkeys(OUTPUTS.values(), True)}},
            'action-audit.json': {'targets': list(OUTPUTS.values()) + [parent, *(owner+n for n in semantics.IMAGES)],
                                  'deb_outputs': [], 'packaging_wrappers': [], 'action_count': 100}}
    for phase in semantics.PHASES:
        docs['worker-' + phase + '.json'] = {'phase': phase, 'status': 'passed', 'exit_code': 0,
            'source_revision': SOURCE, 'image': 'syncd', 'no_oom_events': True,
            'build_profile': profile, 'memory_before': memory, 'memory_after': memory}
    packages = [{'package': name, 'architecture': 'amd64', 'control_fields': {'Package': name},
                 'version': '1', 'source': {'module': name, 'version': '1.0'}}
                for name in sorted(semantics.SOURCE_MODULES)]
    contract = {'architecture': 'amd64', 'distribution': 'trixie', 'image': semantics.IMAGES[0], 'packages': packages}
    docs['source-contract.json'] = contract
    receipt = {**contract, 'kind': 'bazel_source', 'contract_sha256': hashlib.sha256(encoded(contract)).hexdigest(),
               'base_manifest_digest': 'sha256:' + '2' * 64,
               'module_file_sha256': dict.fromkeys(semantics.SOURCE_MODULES, h)}
    docs['source-receipt.json'] = receipt
    docs['module-graph.json'] = {'dependencies': [{'name': n, 'version': '1.0'} for n in sorted(semantics.SOURCE_MODULES)]}
    docs['policy.json'] = {'image': semantics.IMAGES[0], 'architecture': 'amd64', 'distribution': 'trixie',
        'native': {'require_source': True, 'gap_packages': [], 'required_packages': [p['package'] for p in packages]},
        'image_validation': {'environment': {'DEBIAN_FRONTEND': 'noninteractive'}}}
    for name in OUTPUTS:
        if name.endswith('.json') and name not in docs: docs[name] = {'fixture': name}
    # Only the exported small-JSON byte bindings are regenerated by this fixture.
    for name, item in artifacts.items():
        if name.endswith('.json'):
            content = encoded(docs[name]); item.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    native = {p: {'elf_machine': 62, 'sha256': h} for p in sorted(semantics.DEBUG_PATHS)}
    pairs = [{'path': p, 'machine': 62, 'build_id': h[:40], 'has_debuglink': True,
              'debug_path': 'usr/lib/debug/.build-id/' + h[:2] + '/' + h[2:40] + '.debug'} for p in native]
    images = {n: {'manifest_digest': 'sha256:' + str(i+3) * 64, 'config_digest': 'sha256:' + h,
                  'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + h] * (i+1)},
                  'native_paths': list(native)} for i, n in enumerate(semantics.IMAGES)}
    validation = {**public, 'status': 'passed', 'native': native, 'source_debug_pairs': pairs, 'images': images,
        'source_native': {'source_packages': receipt, 'source_receipt_sha256': artifacts['source-receipt.json']['sha256']},
        'p4': {'contract_sha256': artifacts['source-contract.json']['sha256'], 'receipt_sha256': artifacts['source-receipt.json']['sha256']},
        'accounts': {'ordered_rows_and_metadata_preserved': True, 'declared_addition': '_ssh:x:104:'}}
    docs['validation.json'] = validation
    baseline = {parent: receipt['base_manifest_digest'], owner+'source_runtime_payloads': h, owner+'debug_packages': h}
    for name in semantics.IMAGES:
        baseline[owner+name] = images[name]['manifest_digest']; baseline[owner+name+'.gz'] = h
    inc = {phase: {'seconds': 1, 'sha256': copy.deepcopy(baseline)} for phase in ('baseline', 'noop', 'edit', 'restore')}
    for name in semantics.IMAGES:
        inc['edit']['sha256'][owner+name] = 'sha256:' + '9' * 64
        inc['edit']['sha256'][owner+name+'.gz'] = '9' * 64
    docs['incremental.json'] = inc
    loaded = {'status': 'passed', 'revision': SOURCE, 'images': {}}
    for name in semantics.IMAGES:
        record = {'status': 'passed', 'variant': name, 'native_files': len(native), 'archive_sha256': h,
            'private_redis': {'loopback_tcp_and_unix_same_process': True, 'sha256': h, 'database_config_sha256': h},
            'service_states': {n: {'exitstatus': 0, 'state': 'EXITED' if n in ('start','dependent-startup') else 'RUNNING'}
                               for n in ('start','dependent-startup','supervisor-proc-exit-listener','rsyslogd','syncd')},
            'fixture_absent': ['coreportindexmap.ini'], 'fixture_sources': dict.fromkeys(semantics.FIXTURE_NAMES, h),
            'startup_arguments': ['/usr/bin/syncd','-u','-s','-B','null','-l','-p','/usr/share/sonic/hwsku/sai.profile','-w','30000000'],
            'debug_checks': []}
        for key in ('config_and_layers_verified','actual_entrypoint','actual_syncd','original_core_port_map_absent','profile_copy_and_render','stable_process'):
            record[key] = True
        if name.endswith('-dbg'):
            record['debug_checks'] = [{'path': '/'+p['path'], 'debug_path': '/'+p['debug_path'],
                'without_symbols': {'found': False}, 'with_symbols': {'found': True, 'line': 1, 'source': 'probe.cc', 'type': 'int'}} for p in pairs]
        docs[name+'.runtime.json'] = record; loaded['images'][name] = record
    docs['loaded-validation.json'] = loaded
    return docs


def gearbox_fixture():
    # Same shared source payloads, but the Gearbox producer has distinct labels
    # and its installed probe reports a manager/context contract without TAP.
    def convert(value):
        if isinstance(value, str):
            return value.replace('docker-syncd-vs', 'docker-gbsyncd-vs')
        if isinstance(value, list): return [convert(v) for v in value]
        if isinstance(value, dict): return {convert(k): convert(v) for k, v in value.items()}
        return value
    docs = convert(fixture())
    owner = '//platform/vs/docker-gbsyncd-vs:'
    outputs = convert(OUTPUTS)
    outputs.update({'source-runtime.tar': owner+'source_runtime',
                    'source-debug.tar': '//platform/vs/docker-syncd-vs:source_debug_payloads',
                    'installation-links.tar': '//platform/vs/docker-syncd-vs:installation_links',
                    'runtime-apt.tar': owner+'runtime_layer'})
    images = ('docker-gbsyncd-vs', 'docker-gbsyncd-vs-dbg')
    docs.pop('worker-tap.json')
    docs['worker.json']['image'] = 'gearbox'
    for phase in semantics.PHASES: docs['worker-'+phase+'.json']['image'] = 'gearbox'
    docs['build-summary.json']['build-events.jsonl']['targets'] = dict.fromkeys(outputs.values(), True)
    docs['action-audit.json']['targets'] = list(outputs.values()) + [semantics.PARENT, *(owner+n for n in images)]
    artifacts = docs['public-receipt.json']['artifacts']
    for name, label in outputs.items(): artifacts[name]['target'] = label
    docs['source-receipt.json']['contract_sha256'] = hashlib.sha256(encoded(docs['source-contract.json'])).hexdigest()
    for name, item in artifacts.items():
        if name.endswith('.json'):
            content = encoded(docs[name]); item.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    validation = docs['validation.json']
    validation['artifacts'] = artifacts
    validation['source_native']['source_packages'] = docs['source-receipt.json']
    validation['source_native']['source_receipt_sha256'] = artifacts['source-receipt.json']['sha256']
    validation['p4'] = {'contract_sha256': artifacts['source-contract.json']['sha256'],
                        'receipt_sha256': artifacts['source-receipt.json']['sha256']}
    validation['accounts'] = {'base_groups': 39, 'runtime_groups': 40, 'debug_groups': 40, 'declared_addition': '_ssh:x:104:'}
    validation['image_version'] = 'fixture-version'
    for state in docs['incremental.json'].values():
        hashes = state['sha256']
        hashes[owner+'source_runtime'] = hashes.pop(owner+'source_runtime_payloads')
        hashes[owner+'all_debug_symbols'] = hashes.pop(owner+'debug_packages')
    for index, name in enumerate(images):
        record = docs[name+'.runtime.json']
        for key in ('original_core_port_map_absent', 'stable_process', 'fixture_absent', 'fixture_sources', 'startup_arguments'):
            record.pop(key)
        record.update(actual_manager=True, main_database_unchanged=True, child_exit_observed=True,
                      context=1, gb_databases=[13, 15], full_vim={'returncode': 0, 'huge_version': True} if index else None)
        record['private_redis'].pop('loopback_tcp_and_unix_same_process')
        record['private_redis'].update(unix_socket='/var/run/redis/redis.sock', tcp_disabled=False,
                                      tcp_port=6379, tcp_bind='127.0.0.1', process_id=100+index)
        record['service_states'].pop('start')
        record['service_states']['gbsyncdmgrd'] = record['service_states'].pop('syncd')
        docs['loaded-validation.json']['images'][name] = record
    return docs, outputs


class SyncdReceiptTest(unittest.TestCase):
    def setUp(self):
        self.docs = fixture()
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('No network'))
        self.process = patch.object(subprocess, 'Popen', side_effect=AssertionError('No native process'))
        self.network.start(); self.process.start()
        self.addCleanup(self.network.stop); self.addCleanup(self.process.stop)

    def verify(self, outputs=None, source=SOURCE):
        return semantics.verify(lambda n: self.docs[n], lambda n: encoded(self.docs[n]),
                                source=source, outputs=outputs or OUTPUTS)

    def test_consistent_receipts_and_dynamic_native_count(self):
        result = self.verify()
        self.assertEqual((result['exports'], result['audit_roots'], result['native_files']), (22, 25, 4))
        self.assertEqual(result['fixture_sources'], self.docs[semantics.IMAGES[0]+'.runtime.json']['fixture_sources'])
        self.assertEqual(len(semantics.PUBLIC_JSON_NAMES), 28)
        self.assertNotIn('worker-private.json', semantics.PUBLIC_JSON_NAMES)

    def test_wrong_source_declared_label_or_export_hash_rejected(self):
        with self.assertRaises(AssertionError): self.verify(source='c'*40)
        outputs = dict(OUTPUTS); outputs['source-runtime.tar'] = '//wrong:runtime'
        with self.assertRaises(AssertionError): self.verify(outputs=outputs)
        self.docs['debug-packages.json']['changed'] = True
        with self.assertRaises(AssertionError): self.verify()

    def test_native_machine_missing_pair_and_bad_debug_link_rejected(self):
        for kind in ('machine', 'missing', 'path', 'debuglink'):
            self.docs = fixture(); pairs = self.docs['validation.json']['source_debug_pairs']
            if kind == 'machine': self.docs['validation.json']['native'][pairs[0]['path']]['elf_machine'] = 183
            elif kind == 'missing': pairs.pop()
            elif kind == 'path': pairs[0]['debug_path'] = 'usr/lib/debug/wrong.debug'
            else: pairs[0]['has_debuglink'] = False
            with self.subTest(kind=kind), self.assertRaises(AssertionError): self.verify()

    def test_audit_and_build_cannot_omit_or_add_targets(self):
        for kind in ('audit', 'duplicate', 'deb', 'build', 'truncated'):
            self.docs = fixture()
            if kind == 'audit': self.docs['action-audit.json']['targets'].pop()
            elif kind == 'duplicate': self.docs['action-audit.json']['targets'][-1] = self.docs['action-audit.json']['targets'][0]
            elif kind == 'deb': self.docs['action-audit.json']['deb_outputs'] = ['package.deb']
            elif kind == 'build': self.docs['build-summary.json']['build-events.jsonl']['targets'][next(iter(OUTPUTS.values()))] = False
            else: self.docs['build-summary.json']['build-events.jsonl']['truncated'] = True
            with self.subTest(kind=kind), self.assertRaises(AssertionError): self.verify()

    def test_failed_skipped_phase_oom_or_resource_drift_rejected(self):
        for kind in ('phase', 'exit', 'oom', 'resource'):
            self.docs = fixture(); record = self.docs['worker-loaded.json']
            if kind == 'phase': record['status'] = 'skipped'
            elif kind == 'exit': record['exit_code'] = 1
            elif kind == 'oom': record['memory_after']['events']['oom_kill'] = 1
            else: self.docs['worker.json']['build_profile']['loaded_container_memory_gib'] = 8
            with self.subTest(kind=kind), self.assertRaises(AssertionError): self.verify()

    def test_foreign_or_ambiguous_source_graph_rejected(self):
        for kind in ('version', 'duplicate'):
            self.docs = fixture(); deps = self.docs['module-graph.json']['dependencies']
            if kind == 'version': deps[0]['version'] = '2.0'
            else: deps.append({**deps[0], 'version': '2.0'})
            with self.subTest(kind=kind), self.assertRaises(AssertionError): self.verify()

    def test_incremental_must_restore_all_seven_labels_and_change_only_images(self):
        for phase, label in [('noop', semantics.PARENT), ('restore', semantics.OWNER+'source_runtime_payloads'),
                             ('edit', semantics.OWNER+'debug_packages')]:
            self.docs = fixture(); self.docs['incremental.json'][phase]['sha256'][label] = '8'*64
            with self.subTest(phase=phase), self.assertRaises(AssertionError): self.verify()

    def test_loaded_services_fixture_and_debug_negative_controls_required(self):
        for kind in ('service', 'argv', 'fixture', 'debug', 'private', 'tap'):
            self.docs = fixture(); record = self.docs[semantics.IMAGES[1]+'.runtime.json']
            if kind == 'service': record['service_states']['syncd']['state'] = 'EXITED'
            elif kind == 'argv': record['startup_arguments'].append('--different')
            elif kind == 'fixture': record['fixture_sources']['sai.profile'] = '7'*64
            elif kind == 'debug': record['debug_checks'][0]['without_symbols']['found'] = True
            elif kind == 'private': record['private_redis']['loopback_tcp_and_unix_same_process'] = False
            else: self.docs['worker-tap.json']['private_network_namespace'] = False
            with self.subTest(kind=kind), self.assertRaises(AssertionError): self.verify()


class GearboxReceiptTest(unittest.TestCase):
    def setUp(self):
        self.docs, self.outputs = gearbox_fixture()
        network = patch.object(socket, 'socket', side_effect=AssertionError('No network'))
        process = patch.object(subprocess, 'Popen', side_effect=AssertionError('No native process'))
        network.start(); process.start()
        self.addCleanup(network.stop); self.addCleanup(process.stop)

    def verify(self, image='gearbox'):
        return semantics.verify(lambda n: self.docs[n], lambda n: encoded(self.docs[n]),
                                source=SOURCE, outputs=self.outputs, image=image)

    def test_gearbox_has_distinct_runtime_and_allowlist(self):
        result = self.verify()
        self.assertEqual((result['exports'], result['audit_roots'], result['native_files']), (22, 25, 4))
        self.assertEqual(result['fixture_sources'], {})
        self.assertEqual(len(semantics.public_json_names('gearbox')), 27)
        self.assertNotIn('worker-tap.json', semantics.public_json_names('gearbox'))
        self.assertEqual(semantics.public_json_names(), semantics.PUBLIC_JSON_NAMES)
        with self.assertRaises(AssertionError): self.verify(image='syncd')
        with self.assertRaises(AssertionError): semantics.public_json_names('other')
        with self.assertRaises(AssertionError): self.verify(image='other')

    def test_manager_context_database_redis_and_vim_contracts_required(self):
        mutations = [lambda r: r.update(actual_manager=False),
                     lambda r: r.update(child_exit_observed=False),
                     lambda r: r.update(main_database_unchanged=False),
                     lambda r: r.update(context=0), lambda r: r.update(gb_databases=[0, 4]),
                     lambda r: r['service_states']['gbsyncdmgrd'].update(state='EXITED'),
                     lambda r: r['private_redis'].update(tcp_bind='0.0.0.0'),
                     lambda r: r['private_redis'].update(tcp_disabled=True),
                     lambda r: r['private_redis'].update(tcp_port=6380),
                     lambda r: r['private_redis'].update(unix_socket='/tmp/redis.sock'),
                     lambda r: r['private_redis'].update(process_id=0),
                     lambda r: r['private_redis'].update(sha256='7'*64),
                     lambda r: r['full_vim'].update(huge_version=False),
                     lambda r: r['full_vim'].update(returncode=1),
                     lambda r: r['debug_checks'][0]['without_symbols'].update(found=True)]
        for index, mutate in enumerate(mutations):
            self.docs, self.outputs = gearbox_fixture()
            mutate(self.docs['docker-gbsyncd-vs-dbg.runtime.json'])
            with self.subTest(case=index), self.assertRaises(AssertionError): self.verify()

    def test_gearbox_retains_source_audit_phase_and_incremental_checks(self):
        mutations = [lambda d: d['worker-loaded.json'].update(status='skipped'),
                     lambda d: d['worker-build.json'].update(image='syncd'),
                     lambda d: d['action-audit.json']['targets'].pop(),
                     lambda d: d['validation.json']['accounts'].update(debug_groups=41),
                     lambda d: d['validation.json']['accounts'].update(runtime_groups=38),
                     lambda d: d['validation.json'].update(image_version='bad\nversion'),
                     lambda d: d['module-graph.json']['dependencies'][0].update(version='2.0'),
                     lambda d: d['incremental.json']['restore']['sha256'].update({'//platform/vs/docker-gbsyncd-vs:all_debug_symbols': '8'*64})]
        for index, mutate in enumerate(mutations):
            self.docs, self.outputs = gearbox_fixture(); mutate(self.docs)
            with self.subTest(case=index), self.assertRaises(AssertionError): self.verify()


class WorkerCacheCompatibilityTest(unittest.TestCase):
    """Family semantics use the same reviewed cache authority as canonical receipts."""

    def setUp(self):
        network = patch.object(socket, 'socket', side_effect=AssertionError('No network'))
        process = patch.object(subprocess, 'Popen', side_effect=AssertionError('No native process'))
        network.start(); process.start()
        self.addCleanup(network.stop); self.addCleanup(process.stop)

    def documents(self, image, schema=3):
        from tools.bazel.ci import image_worker_receipt as reader
        docs, outputs = (fixture(), OUTPUTS) if image == 'syncd' else gearbox_fixture()
        policy = (copy.deepcopy(reader.K3S_REMOTE_CACHE_POLICY) if schema == 2 else
                  dict(reader.K3S_MANAGED_REMOTE_CACHE_POLICY,
                       endpoint='https://cache.example/', host='cache.example'))
        docs['worker.json'].update(schema=schema, cache_mode='remote_only_run_attempt',
                                  remote_cache=policy)
        return docs, outputs

    def verify(self, image, docs, outputs, pool=None):
        return semantics.verify(lambda n: docs[n], lambda n: encoded(docs[n]),
                                source=SOURCE, outputs=outputs, image=image, worker_pool=pool)

    def test_remote_schema2_and3_are_accepted_in_reviewed_pool(self):
        for image in ('syncd', 'gearbox'):
            for schema in (2, 3):
                with self.subTest(image=image, schema=schema):
                    docs, outputs = self.documents(image, schema)
                    result = self.verify(image, docs, outputs, 'sonic-slave-container-k3s')
                    self.assertEqual((result['exports'], result['audit_roots']), (22, 25))

    def test_remote_requires_explicit_reviewed_pool(self):
        for image in ('syncd', 'gearbox'):
            for schema in (2, 3):
                for pool in (None, 'sonic-buildimage-worker', 'unreviewed-pool'):
                    with self.subTest(image=image, schema=schema, pool=pool):
                        docs, outputs = self.documents(image, schema)
                        with self.assertRaises(ValueError): self.verify(image, docs, outputs, pool)

    def test_remote_rejects_disk_and_repository_cache(self):
        for image in ('syncd', 'gearbox'):
            for schema in (2, 3):
                for key in ('action_disk_cache', 'repository_cache'):
                    with self.subTest(image=image, schema=schema, key=key):
                        docs, outputs = self.documents(image, schema)
                        docs['worker.json']['remote_cache'][key] = 'enabled'
                        with self.assertRaises(ValueError):
                            self.verify(image, docs, outputs, 'sonic-slave-container-k3s')

    def test_remote_rejects_untrusted_policy_or_origin(self):
        changes = ({'access_probe': 'failed'}, {'credential_helper_sha256': '0' * 64},
                   {'remote_executor': 'enabled'}, {'endpoint': 'http://cache.example/'},
                   {'endpoint': 'https://cache.example/path'}, {'host': 'other.example'},
                   {'endpoint': 'https://user:password@cache.example/'})
        for image in ('syncd', 'gearbox'):
            for change in changes:
                with self.subTest(image=image, changed=sorted(change)):
                    docs, outputs = self.documents(image)
                    docs['worker.json']['remote_cache'].update(change)
                    with self.assertRaises(ValueError):
                        self.verify(image, docs, outputs, 'sonic-slave-container-k3s')

    def test_unknown_schema_or_mode_is_rejected(self):
        changes = ({'schema': 4}, {'schema': True}, {'schema': '3'},
                   {'cache_mode': 'anything'}, {'cache_mode': 'persistent_worker_local'})
        for image in ('syncd', 'gearbox'):
            for change in changes:
                with self.subTest(image=image, changed=change):
                    docs, outputs = self.documents(image)
                    docs['worker.json'].update(change)
                    with self.assertRaises(ValueError):
                        self.verify(image, docs, outputs, 'sonic-slave-container-k3s')

    def test_legacy_scope_and_remote_family_checks_preserved(self):
        for image in ('syncd', 'gearbox'):
            docs, outputs = (fixture(), OUTPUTS) if image == 'syncd' else gearbox_fixture()
            self.assertEqual(self.verify(image, docs, outputs)['exports'], 22)
            docs['worker.json']['remote_cache'] = {}
            with self.subTest(image=image, case='legacy-policy-injection'), self.assertRaises(ValueError):
                self.verify(image, docs, outputs)
            for case in ('source', 'failed-phase', 'architecture'):
                docs, outputs = self.documents(image)
                if case == 'source': docs['worker.json']['source_revision'] = 'b' * 40
                elif case == 'failed-phase': docs['worker-loaded.json']['status'] = 'skipped'
                else: docs['worker.json']['architecture'] = 'arm64'
                with self.subTest(image=image, case=case), self.assertRaises(AssertionError):
                    self.verify(image, docs, outputs, 'sonic-slave-container-k3s')


if __name__ == '__main__':
    unittest.main()
