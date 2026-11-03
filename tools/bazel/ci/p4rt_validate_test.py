"""Reject stale stage receipts and undeclared changes to installed image files."""

import copy
import unittest

from tools.bazel.ci import p4rt_validate as validation


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
