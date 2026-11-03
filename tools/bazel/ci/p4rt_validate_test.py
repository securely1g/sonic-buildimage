"""Reject stale stage receipts and undeclared changes to installed image files."""

import ast
import copy
from pathlib import Path
import unittest

from tools.bazel.ci import p4rt_validate as validation


class DebugSaiTest(unittest.TestCase):
    def setUp(self):
        self.payloads = {}
        self.debug = {}
        self.metadata = {'source_packages': []}
        self.hashes = {'libsairedis': 'a' * 64, 'libsaimetadata': 'b' * 64}
        self.owner = {'schema': 1, 'module': 'sonic-sairedis', 'architecture': 'amd64', 'packages': {}}
        for package, target, names in (
                ('libsairedis', '//lib:libsairedis_pkg', ['libsairedis.so.0.0.0']),
                ('libsaimetadata', '//meta:libsaimetadata_pkg', ['libsaimeta.so.0.0.0', 'libsaimetadata.so.0.0.0'])):
            files = {'usr/lib/x86_64-linux-gnu/' + name: {
                'kind': 'file', 'uid': 0, 'gid': 0, 'mode': 0o644, 'sha256': name, 'elf_machine': 62}
                for name in names}
            self.payloads[package] = files
            self.debug.update(copy.deepcopy(files))
            self.metadata['source_packages'].append({'package': package, 'target': target,
                'source_revision': 'a' * 40, 'payload_sha256': self.hashes[package], 'version': '1.0.0',
                'native_files': [{'path': name, **row} for name, row in files.items()]})
            self.owner['packages'][package] = {'runtime_target': target,
                'payload_sha256': self.hashes[package], 'control_fields': {'Version': '1.0.0'}}

    def verify(self, runtime=None):
        validation.verify_debug_sai(runtime or {}, {}, self.debug, self.metadata, self.payloads, self.hashes, self.owner)

    def test_declared_debug_closure_and_owner_metadata_are_required(self):
        self.verify()
        self.metadata['source_packages'].pop()
        with self.assertRaisesRegex(ValueError, 'missing source-owned'):
            self.verify()

    def test_owner_metadata_is_bound_to_the_exact_retained_tars(self):
        self.verify()
        self.owner['packages']['libsairedis']['payload_sha256'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, 'differs from retained source TAR'):
            self.verify()
        self.owner['packages']['libsairedis']['payload_sha256'] = self.hashes['libsairedis']
        self.owner['packages']['syncd-vs'] = {}
        with self.assertRaisesRegex(ValueError, 'only the two SAI library packages'):
            self.verify()

    def test_missing_changed_or_runtime_sai_libraries_are_rejected(self):
        name = next(iter(self.debug))
        original = self.debug[name]
        with self.assertRaisesRegex(ValueError, 'leaked into an earlier stage'):
            self.verify({name: original})
        self.debug[name] = {**original, 'mode': 0o777}
        with self.assertRaisesRegex(ValueError, 'payload changed'):
            self.verify()
        del self.debug[name]
        with self.assertRaisesRegex(ValueError, 'payload changed'):
            self.verify()

    def test_extra_native_library_and_duplicate_metadata_are_rejected(self):
        first = self.metadata['source_packages'][0]
        self.metadata['source_packages'].append(copy.deepcopy(first))
        with self.assertRaisesRegex(ValueError, 'duplicate debug source'):
            self.verify()
        self.metadata['source_packages'].pop()
        self.payloads['libsairedis']['usr/lib/x86_64-linux-gnu/unexpected.so'] = next(iter(self.debug.values()))
        with self.assertRaisesRegex(ValueError, 'native inventory differs'):
            self.verify()


class LoadedSaiSelectionTest(unittest.TestCase):
    def test_debug_profile_requires_all_three_sai_libraries_and_matching_pairs(self):
        path = Path(__file__).resolve().parents[3] / 'dockers/docker-sonic-p4rt/bazel/runtime_probe.py'
        tree = ast.parse(path.read_text())
        selected = [item for item in tree.body if isinstance(item, ast.FunctionDef) and
                    item.name in ('require', 'source_debug_pairs')]
        self.assertEqual(len(selected), 2)
        namespace = {}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
        choose = namespace['source_debug_pairs']
        common = 'usr/lib/x86_64-linux-gnu/libswsscommon.so.0.0.0'
        sai = ['usr/lib/x86_64-linux-gnu/' + name for name in
               ('libsairedis.so.0.0.0', 'libsaimeta.so.0.0.0', 'libsaimetadata.so.0.0.0')]
        report = {'source_debug_pairs': [{'path': name} for name in [common, *sai]]}
        self.assertEqual(choose(report, {common: {}}, False), [{'path': common}])
        native = {name: {} for name in [common, *sai]}
        self.assertEqual(len(choose(report, native, True)), 4)
        with self.assertRaisesRegex(AssertionError, 'missing debug SAI runtime'):
            choose(report, {common: {}}, True)
        report['source_debug_pairs'].pop()
        with self.assertRaisesRegex(AssertionError, 'missing or duplicate'):
            choose(report, native, True)
        report['source_debug_pairs'].append({'path': common})
        with self.assertRaisesRegex(AssertionError, 'missing or duplicate'):
            choose(report, native, True)


class StageBindingTest(unittest.TestCase):
    def setUp(self):
        self.apt = {'image': 'docker-sonic-p4rt', 'group': 'debug',
                    'base_manifest_digest': 'sha256:symbols', 'dependency_check': {'status': 'satisfied'}}
        self.metadata = {'architecture': 'amd64', 'base_manifest_digest': 'sha256:symbols',
                         'apt_selection_sha256': 'apt-hash', 'dependency_check': {'status': 'satisfied'}}

    def verify(self):
        validation.stage_binding('debug', self.apt, self.metadata, base_digest='sha256:symbols', apt_sha='apt-hash')

    def test_final_debug_requires_the_completed_symbols_base(self):
        self.verify()
        for record in (self.apt, self.metadata):
            original = record['base_manifest_digest']
            record['base_manifest_digest'] = 'sha256:runtime'
            with self.assertRaisesRegex(ValueError, 'different completed OCI base'):
                self.verify()
            record['base_manifest_digest'] = original

    def test_unrelated_apt_receipt_or_unsatisfied_dependencies_are_rejected(self):
        self.metadata['apt_selection_sha256'] = 'earlier-stage'
        with self.assertRaisesRegex(ValueError, 'checked APT selection'):
            self.verify()
        self.metadata['apt_selection_sha256'] = 'apt-hash'
        self.apt['dependency_check']['status'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'unsatisfied stage'):
            self.verify()

    def test_workspace_owner_cannot_be_relabelled_as_a_bzlmod_source(self):
        self.metadata['source_contract_sha256'] = 'contract'
        validation.stage_binding('symbols', self.apt, self.metadata, base_digest='sha256:symbols',
                                 apt_sha='apt-hash', contract_sha='contract')
        self.metadata['source_modules'] = {'sonic-pins': {'version': 'invented'}}
        with self.assertRaisesRegex(ValueError, 'real owner contract'):
            validation.stage_binding('symbols', self.apt, self.metadata, base_digest='sha256:symbols',
                                     apt_sha='apt-hash', contract_sha='contract')


class InheritanceTest(unittest.TestCase):
    def setUp(self):
        self.parent = {name: {'kind': 'file', 'uid': 0, 'gid': 0, 'mode': 0o644, 'sha256': name}
                       for name in ('etc/group', 'etc/gshadow', 'etc/passwd', 'usr/bin/service')}
        self.child = copy.deepcopy(self.parent)
        self.rows = ['root:x:0:', 'redis:x:103:']
        self.debug_rows = self.rows + ['_ssh:x:104:']

    def verify(self):
        validation.verify_groups(self.parent, self.child, self.rows, self.debug_rows)

    def test_only_the_declared_ssh_group_may_be_appended(self):
        self.verify()
        for rows in ([self.rows[1], self.rows[0], '_ssh:x:104:'],
                     self.debug_rows + ['root:x:900:'], ['root:x:0:', 'redis:x:103:other', '_ssh:x:104:']):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validation.verify_groups(self.parent, self.child, self.rows, rows)

    def test_group_metadata_and_shadow_state_cannot_be_exempted(self):
        for name, key, value in (('etc/group', 'mode', 0o777), ('etc/group', 'uid', 1000),
                                 ('etc/gshadow', 'sha256', 'changed')):
            with self.subTest(name=name, key=key):
                old = self.child[name][key]
                self.child[name][key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.child[name][key] = old

    def test_symbol_stage_preserves_every_runtime_file(self):
        validation.inherited_files(self.parent, self.child)
        self.child['usr/bin/service']['mode'] = 0o777
        with self.assertRaisesRegex(ValueError, 'undeclared inherited file change'):
            validation.inherited_files(self.parent, self.child)


class OwnerPackageTest(unittest.TestCase):
    def test_native_inventory_and_provenance_must_match_the_real_owner(self):
        payload = {'usr/local/bin/p4rt': {'elf_machine': 62, 'sha256': 'actual', 'mode': 0o755, 'uid': 0, 'gid': 0}}
        definition = {'control_fields': {'Version': '1.0'}, 'target': '//p4rt_app:p4rt_runtime',
                      'source_revision': 'a' * 40, 'payload_sha256': 'tar'}
        contract = {'packages': {'sonic-p4rt': definition}}
        row = {'package': 'sonic-p4rt', 'version': '1.0', **{k: definition[k] for k in
               ('target', 'source_revision', 'payload_sha256')},
               'native_files': [{'path': 'usr/local/bin/p4rt', **payload['usr/local/bin/p4rt']}]}
        metadata = {'source_packages': [row]}
        validation.owner_package_state(metadata, contract, payload)
        row['native_files'].append(copy.deepcopy(row['native_files'][0]))
        with self.assertRaisesRegex(ValueError, 'native inventory'):
            validation.owner_package_state(metadata, contract, payload)
        row['native_files'].pop()
        row['source_revision'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'provenance'):
            validation.owner_package_state(metadata, contract, payload)


class ConfigTest(unittest.TestCase):
    def test_only_declared_startup_environment_and_manifest_change(self):
        parent = {'User': '0', 'WorkingDir': '/', 'Cmd': ['/bin/sh'], 'Env': ['PATH=/usr/bin'],
                  'Labels': {'inherited': 'keep'}}
        runtime = {'User': '0', 'WorkingDir': '/', 'Entrypoint': ['/usr/local/bin/supervisord'],
                   'Env': ['PATH=/usr/bin', 'DEBIAN_FRONTEND=noninteractive'],
                   'Labels': {'inherited': 'keep', 'com.azure.sonic.manifest': '{"name":"p4rt"}'}}
        validation.verify_config(runtime, parent, runtime=True, manifest={'name': 'p4rt'})
        for key, changed in (('User', '1000'), ('WorkingDir', '/tmp'), ('Cmd', ['unexpected']),
                             ('Env', runtime['Env'] + ['LD_PRELOAD=/override.so'])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validation.verify_config({**runtime, key: changed}, parent, runtime=True, manifest={'name': 'p4rt'})
        validation.verify_config(runtime, runtime)


if __name__ == '__main__':
    unittest.main()
