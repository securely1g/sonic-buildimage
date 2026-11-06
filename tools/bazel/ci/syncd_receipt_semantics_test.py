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
    memory = {'limit': str(20 * 1024**3), 'events': dict.fromkeys(('oom', 'oom_kill', 'oom_group_kill'), 0)}
    docs = {'public-receipt.json': public,
            'worker.json': {'status': 'prepared', 'image': 'syncd', 'source_revision': SOURCE,
                            'architecture': 'amd64', 'distribution': 'trixie', 'docker_network': 'none',
                            'cache_mode': 'persistent_worker_local', 'build_profile': profile},
            'worker-preflight.json': {'status': 'passed', 'checks': {'bounded_resources': True},
                'build_profile': profile, 'facts': {'memory_limit_bytes': 20 * 1024**3, 'cpu_limit': 4},
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


if __name__ == '__main__':
    unittest.main()
