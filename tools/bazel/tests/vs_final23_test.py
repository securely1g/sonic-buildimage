"""Validate final23 archive contracts using synthetic TARs; never invoke a builder."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import vs_final23 as final
from tools.bazel.ci import vs_archive_reuse as reuse
from tools.bazel.tests.vs_archive_reuse_test import encoded, tar_bytes


class Final23Test(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.target = self.workspace / 'target'
        (self.target / '.container-build-method').mkdir(parents=True)
        (self.workspace / '.arch').write_text('amd64\n')
        (self.workspace / '.platform').write_text('vs\n')
        self.configs, self.layers, self.manifests = {}, {}, {}
        base = {'Entrypoint': [], 'Cmd': [], 'Env': ['PATH=/usr/bin:/bin'],
                'WorkingDir': '/', 'Labels': {'private': 'never-publish-me'}}
        self.add(final.BASE, base)
        self.add(final.CONFIG, base, final.BASE)
        config = copy.deepcopy(base)
        config.pop('Cmd')
        config.update(Entrypoint=[final.SUPERVISOR], Env=base['Env'] + ['DEBIAN_FRONTEND=noninteractive'])
        self.add(final.SWSS, config, final.CONFIG)
        for name, (parent, entrypoint, keys) in final.RUNTIMES.items():
            config = copy.deepcopy(self.configs[parent])
            config.pop('Cmd', None)
            config['Entrypoint'] = [entrypoint]
            environment = final.environment(config['Env'])
            environment['DEBIAN_FRONTEND'] = 'noninteractive'
            overrides = {'DEBIAN_FRONTEND': 'noninteractive'}
            for key in keys:
                overrides[key] = 'build.42' if key == 'IMAGE_VERSION' else 'y' if key == 'INCLUDE_SYSTEM_EVENTD' else '1'
            environment.update(overrides)
            config['Env'] = [key + '=' + value for key, value in environment.items()]
            self.add(name, config, parent)
            if keys and name != 'docker-snmp':
                (self.target / 'bazel-manifests' / name / 'image.env').write_text(
                    ''.join(key + '=' + value + '\n' for key, value in overrides.items()))
            if name != final.ENGINE:
                self.add(name + '-dbg', config, name, debug=True)
        self.env = {'VS_ARCHITECTURE': 'amd64', 'VS_PLATFORM': 'vs', 'VS_DISTRIBUTION': 'trixie',
                    'VS_BUILDER': 'y', 'VS_FIPS': 'y', 'VS_DASH': 'y', 'VS_ASAN': 'n', 'VS_SYNCD_RPC': 'n',
                    'SONIC_IMAGE_VERSION': 'build.42', 'include_system_eventd': 'y', 'install_debug_image': 'n'}
        self.select()

    def add(self, name, config, parent=None, debug=False):
        self.configs[name] = copy.deepcopy(config)
        manifest = {'package': {'name': name.removesuffix('-dbg'), 'version': '1.0+dbg' if debug else '1.0'}}
        self.manifests[name] = manifest
        layers = list(self.layers[parent]) if parent else []
        # P4RT's extra intermediate symbol OCI is valid prefix ancestry.
        if name == 'docker-sonic-p4rt-dbg':
            layers.append(tar_bytes([('intermediate', b'symbols')]))
        layers.append(tar_bytes([('layer-' + name, name.encode())]))
        self.layers[name] = layers
        self.write(name)

    def write(self, name):
        directory = self.target / 'bazel-manifests' / name
        directory.mkdir(parents=True, exist_ok=True)
        directory.joinpath('manifest.json').write_bytes(encoded(self.manifests[name]))
        (self.target / '.container-build-method' / (name + '.gz')).write_text('bazel\n')
        config = copy.deepcopy(self.configs[name])
        config.setdefault('Labels', {})[reuse.LABEL] = json.dumps(self.manifests[name])
        layers = self.layers[name]
        digests = [hashlib.sha256(value).hexdigest() for value in layers]
        doc = encoded({'architecture': 'amd64', 'os': 'linux', 'config': config,
                       'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + value for value in digests]}})
        config_path = hashlib.sha256(doc).hexdigest() + '.json'
        layer_names = [value + '.tar' for value in digests]
        data = tar_bytes([(config_path, doc), *zip(layer_names, layers), ('manifest.json', encoded([
            {'RepoTags': [name + ':latest'], 'Config': config_path, 'Layers': layer_names}]))])
        (self.target / (name + '.gz')).write_bytes(gzip.compress(data, mtime=0))

    def select(self, debug=False, legacy=False):
        self.env['install_debug_image'] = 'y' if debug else 'n'
        paths = ['target/' + name + ('-dbg' if debug and name != final.ENGINE else '') + '.gz'
                 for name in final.RUNTIMES if name != final.LOCAL]
        if legacy:
            paths.append('target/docker-nat.gz')
            (self.target / 'docker-nat.gz').write_bytes(b'retained legacy bytes')
        self.env['installer_images'] = ' '.join(
            Path(path).stem.removesuffix('-dbg') + '|dockers/source||' + path + ':1.0' for path in paths)
        local = 'docker-macsec' + ('-dbg' if debug else '') + '.gz'
        self.env['sonic_local_packages'] = local + '|target/' + local + '|local|enabled'
        (self.target / 'sonic-vs.bin.dockers').write_text(''.join(path + '\n' for path in paths))
        return final.record_selection(self.workspace, 'sonic-vs.bin', 'vs', self.env)

    def test_complete_profile_and_post_validation_identity(self):
        self.select(legacy=True)
        receipt = final.capture(self.workspace)
        self.assertEqual(len(receipt['archives']), 42)
        self.assertNotIn('docker-dash-engine-dbg', receipt['archives'])
        self.assertEqual(receipt['legacy']['target/docker-nat.gz']['builder'], 'make')
        self.assertNotIn('never-publish-me', json.dumps(receipt))
        final.verify(self.workspace, receipt)
        (self.target / 'docker-nat.gz').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed after capture'):
            final.verify(self.workspace, receipt)

    def test_opt_in_cli_captures_and_verifies_schema_two(self):
        receipt = self.workspace / 'receipts/final23.json'
        for action in ('capture-pairs', 'verify-pairs'):
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/bazel/ci/vs_archive_reuse.py'),
                                     action, '--profile', 'final23', '--workspace', str(self.workspace),
                                     '--receipt', str(receipt)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(receipt.read_bytes())
        self.assertEqual(data['schema'], 2)
        self.assertEqual(len(data['archives']), 42)
        self.assertEqual(set(data['validation_seconds']), {'capture', 'verify'})

    def test_debug_installer_preserves_runtime_only_engine_and_local_channel(self):
        self.select(debug=True)
        receipt = final.capture(self.workspace, debug=True)
        self.assertIn('target/docker-dash-engine.gz', [x['path'] for x in receipt['selection']['installer_images']])
        self.assertEqual(receipt['selection']['local_packages'][0]['path'], 'target/docker-macsec-dbg.gz')

    def test_missing_environment_duplicate_keys_and_inherited_mutation_fail(self):
        for name, key in [('docker-eventd', 'IMAGE_VERSION'), ('docker-database', 'INCLUDE_SYSTEM_EVENTD'),
                          ('docker-snmp', 'PYTHONOPTIMIZE'), ('docker-orchagent', 'PATH')]:
            original = copy.deepcopy(self.configs[name])
            for env in ([v for v in original['Env'] if not v.startswith(key + '=')],
                        original['Env'] + [key + '=bad']):
                with self.subTest(name=name, env=env):
                    self.configs[name]['Env'] = env
                    self.write(name)
                    with self.assertRaisesRegex(ValueError, 'environment'):
                        final.capture(self.workspace)
            self.configs[name] = original
            self.write(name)

    def test_parent_and_debug_mutations_fail(self):
        name = 'docker-sonic-p4rt-dbg'
        self.layers[name][0] = tar_bytes([('wrong-parent', b'changed')])
        self.write(name)
        with self.assertRaisesRegex(ValueError, 'exact parent layers'):
            final.capture(self.workspace)

    def test_undeclared_configuration_and_builder_fallback_fail(self):
        self.configs['docker-database']['User'] = 'root'
        self.write('docker-database')
        with self.assertRaisesRegex(ValueError, 'undeclared image configuration'):
            final.capture(self.workspace)
        self.configs['docker-database'].pop('User')
        self.write('docker-database')
        (self.target / '.container-build-method/docker-macsec.gz').write_text('make\n')
        with self.assertRaisesRegex(ValueError, 'builder differs'):
            final.capture(self.workspace)

    def test_missing_family_wrong_channel_and_unknown_family_fail(self):
        selection = self.select()
        for change in ('missing', 'channel', 'unknown', 'variant'):
            value = copy.deepcopy(selection)
            if change == 'missing':
                value['local_packages'] = []
            elif change == 'channel':
                value['installer_images'] += value['local_packages']
                value['local_packages'] = []
            elif change == 'unknown':
                value['local_packages'].append({'path': 'target/docker-unreviewed.gz'})
            else:
                value['local_packages'][0]['path'] = 'target/docker-macsec-dbg.gz'
            with self.subTest(change=change), self.assertRaises(ValueError):
                final.validate_selection(self.workspace, value)

    def test_post_capture_manifest_environment_parent_and_local_changes_fail(self):
        receipt = final.capture(self.workspace)
        for relative in ('target/bazel-manifests/docker-snmp/manifest.json',
                         'target/bazel-manifests/docker-eventd/image.env',
                         'target/docker-base-trixie.gz', 'target/docker-macsec.gz'):
            path = self.workspace / relative
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(path=relative), self.assertRaisesRegex(ValueError, 'changed after capture'):
                final.verify(self.workspace, receipt)
            path.write_bytes(original)

    def test_mismatched_package_metadata_and_missing_receipt_input_fail(self):
        self.manifests['docker-database']['package']['version'] = '2.0'
        self.write('docker-database')
        with self.assertRaisesRegex(ValueError, 'package metadata'):
            final.capture(self.workspace)
        self.manifests['docker-database']['package']['version'] = '1.0'
        self.write('docker-database')
        receipt = final.capture(self.workspace)
        receipt['files'].pop('target/bazel-manifests/docker-eventd/image.env')
        with self.assertRaisesRegex(ValueError, 'omits an archive or declared input'):
            final.verify(self.workspace, receipt)

    def test_native_profile_rejects_cross_build_and_wrong_architecture(self):
        for key, value in [('VS_CROSS_BUILD', 'y'), ('VS_MULTIARCH_QEMU', 'y'), ('VS_ARCHITECTURE', 'arm64')]:
            env = {**self.env, key: value}
            final.record_selection(self.workspace, 'sonic-vs.bin', 'vs', env)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'unsupported final23'):
                final.capture(self.workspace)

    def test_make_records_incomplete_selection_and_capture_alone_rejects_it(self):
        self.env['sonic_local_packages'] = ''
        selection = final.record_selection(self.workspace, 'sonic-vs.bin', 'vs', self.env)
        self.assertEqual(selection['local_packages'], [])
        self.assertEqual(json.loads((self.target / 'sonic-vs.bin.selection.json').read_bytes()), selection)
        with self.assertRaisesRegex(ValueError, 'omits an in-scope runtime family'):
            final.capture(self.workspace)

    def test_make_records_invalid_environment_for_separate_validation(self):
        for key, value, message in [('SONIC_IMAGE_VERSION', 'bad$version', 'image version'),
                                    ('include_system_eventd', 'invalid', 'eventd'),
                                    ('install_debug_image', 'invalid', 'debug')]:
            final.record_selection(self.workspace, 'sonic-vs.bin', 'vs', {**self.env, key: value})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, message):
                final.capture(self.workspace)

    def test_malformed_record_format_preserves_previous_receipt(self):
        self.env['installer_images'] = 'malformed|record'
        previous = (self.target / 'sonic-vs.bin.selection.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'invalid installer image record'):
            final.record_selection(self.workspace, 'sonic-vs.bin', 'vs', self.env)
        self.assertEqual((self.target / 'sonic-vs.bin.selection.json').read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
