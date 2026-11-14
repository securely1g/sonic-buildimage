"""Selected callback boundaries; no real subprocess, Docker, network or user change."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(os.environ.get('COMPONENT_RUNTIME_TEST_SOURCE', str(ROOT)))
CALLER = ROOT / 'tools/bazel/ci/build_vs_coherent_installed_policies.py'
RUNTIME = ROOT / 'tools/bazel/ci/vs_component_runtime_owner.py'
REVISION = 'c02d93956919e4ea6886f347be4ac0ab6e3e36f0'


def selected_owner(path, names, namespace):
    tree = ast.parse(path.read_bytes())
    selected = []
    for node in tree.body:
        declared = ({node.name} if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else
                    {item.id for item in node.targets if isinstance(item, ast.Name)}
                    if isinstance(node, ast.Assign) else set())
        if declared.intersection(names):
            selected.append(node)
    found = set()
    for node in selected:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            found.add(node.name)
        else:
            found.update(item.id for item in node.targets if isinstance(item, ast.Name))
    if found != names:
        raise AssertionError('Missing exact selected owner declarations')
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class ComponentRuntimeCallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='component-runtime-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.private, self.state = [self.root / item for item in ('source', 'private', 'state')]
        for path in (self.source, self.private, self.state):
            path.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.docker = self.bin / 'docker'
        self.docker.write_text('fixture data only; never executed\n')
        self.docker.chmod(0o755)
        self.env = {'PATH': str(self.bin), 'DOCKER_HOST': 'unix:///fixture/private.sock',
                    'DOCKER_CONFIG': str(self.root / 'client'), 'LC_ALL': 'C'}
        if 'HOME' in os.environ:
            self.env['HOME'] = os.environ['HOME']
        scope = {name: globals()[name] for name in
                 ('hashlib', 'json', 'os', 'Path', 're', 'shutil', 'stat', 'sys')}
        self.caller = selected_owner(CALLER, {
            '_COMPONENT_RUNTIME_FIXED', '_COMPONENT_RUNTIME_INLINE', 'component_workload'}, scope)
        controls = selected_owner(RUNTIME, {'ProbeFailure', 'require', 'file_identity'}, {})
        self.error = controls['ProbeFailure']
        self.calls, self.outputs = [], set()
        self.output_uid = 1000
        actual_stat = Path.stat

        def fixed_worker_stat(path, *args, **kwargs):
            value = actual_stat(path, *args, **kwargs)
            if path in self.outputs:
                fields = {name: getattr(value, name) for name in
                          ('st_mode', 'st_uid', 'st_gid', 'st_dev', 'st_ino', 'st_size',
                           'st_mtime_ns', 'st_ctime_ns')}
                fields.update(st_uid=self.output_uid, st_gid=1000)
                return SimpleNamespace(**fields)
            return value

        patch = mock.patch.object(Path, 'stat', autospec=True, side_effect=fixed_worker_stat)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(os, 'fchown')
        self.chown = patch.start()
        self.addCleanup(patch.stop)
        self.owner = SimpleNamespace(require=controls['require'], file_identity=controls['file_identity'],
                                     bounded=self.bound)
        self.mode = None

    def seed(self, family, public_status='passed'):
        spec = self.caller['_COMPONENT_RUNTIME_FIXED'][family]
        directory = self.private / spec['directory']
        directory.mkdir()
        output = directory / 'runtime'
        output.mkdir()
        self.outputs.add(output)
        artifacts = {}
        for name in spec['images']:
            path = directory / (name + '.gz')
            path.write_bytes(('original-' + name).encode())
            artifacts[path.name] = {'bytes': path.stat().st_size,
                                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            if spec['per_image_validation']:
                (directory / (name + '.validation.json')).write_text(json.dumps({'status': 'passed'}))
        base = {'revision': REVISION, 'architecture': 'amd64', 'artifacts': artifacts}
        (directory / 'build-receipt.json').write_text(json.dumps({**base, 'status': 'built'}))
        (directory / 'validation.json').write_text(json.dumps({**base, 'status': 'passed'}))
        (directory / 'public-receipt.json').write_text(json.dumps({**base, 'status': public_status}))
        if family == 'base':
            policy = {'architecture': 'amd64',
                      'images': [{'name': name} for name in spec['images']]}
            policy_path = directory / 'installed-images-policy.json'
            policy_path.write_text(json.dumps(policy))
            receipt = {**base, 'status': 'passed', 'images': {},
                       'installed_images_policy_sha256': hashlib.sha256(policy_path.read_bytes()).hexdigest()}
            for index, name in enumerate(spec['images']):
                digest = 'sha256:' + str(index + 1) * 64
                receipt['images'][name] = {'manifest_digest': digest, 'source_elf_count': 1}
                (directory / (name + '.validation.json')).write_text(json.dumps({'manifest_digest': digest}))
            (directory / 'receipt.json').write_text(json.dumps(receipt))
            for name in ('build-receipt.json', 'validation.json', 'public-receipt.json'):
                (directory / name).unlink()
            for relative, expected in spec['source_owners'].items():
                data = (SOURCE / relative).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
                destination = self.source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        if 'module' in spec:
            original = SOURCE / spec['module']
            data = original.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), spec['owner_sha256'])
            destination = self.source / spec['module']
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        return directory

    def bound(self, argv, state, label, seconds, **options):
        self.assertEqual(state, self.state)
        self.assertEqual(seconds, 1200)
        self.assertTrue(options['worker_user'])
        self.assertEqual(options['working_directory'], str(self.source))
        self.assertEqual(options['environment'], {**self.env, **(
            {'EXPECTED_ARCH': 'amd64'} if label == 'component-base' else {})})
        self.calls.append((argv, label))
        if label.endswith('-swss'):
            directory = self.private / ('swss' if 'orchagent' in label else 'sflow')
            (directory / 'swss-layer.runtime-smoke.json').write_text('{"status":"passed"}\n')
        elif label == 'component-base':
            directory = self.private / 'base'
            self.assertIn('tools/bazel/ci/validate_runtime.py', argv[-1])
            self.assertIn('--images-policy', argv[-1])
            self.assertIn('tools/bazel/oci/base_runtime_smoke.py', argv[-1])
            self.assertIn('--network=none --cap-drop=ALL --security-opt=no-new-privileges', argv[-1])
            self.assertNotIn('$PWD/' + str(directory), argv[-1])
            images = {name: {'archive_sha256': hashlib.sha256(
                (directory / (name + '.gz')).read_bytes()).hexdigest()}
                for name in self.caller['_COMPONENT_RUNTIME_FIXED']['base']['images']}
            if self.mode == 'wrong-base-archive':
                images['docker-base-trixie']['archive_sha256'] = '0' * 64
            (directory / 'installed-images.json').write_text(json.dumps({
                'status': 'passed', 'architecture': 'amd64', 'images': images}))
            (directory / 'runtime-smoke.json').write_text('{"status":"passed"}\n')
        elif label == 'component-swss-layer':
            self.assertIn("receipt['status'] == 'passed'", argv[-1])
            self.assertNotIn('modprobe', argv[-1])
            self.assertIn(str(self.private / 'swss-layer'), argv[-1])
            (self.private / 'swss-layer/runtime-smoke.json').write_text('{"status":"passed"}\n')
        else:
            directory = Path(argv[-1])
            family = label.removeprefix('component-')
            spec = self.caller['_COMPONENT_RUNTIME_FIXED'][family]
            self.assertEqual(argv[1], '-B')
            self.assertEqual(argv[-2], '--artifacts')
            for name in spec['images']:
                archive = directory / (name + '.gz')
                value = {'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                         'config_and_layers_verified': True}
                if family == 'database':
                    value['profiles'] = {key: {'status': 'passed'} for key in ('single', 'multi')}
                else:
                    value['status'] = 'passed'
                if not spec['aggregate']:
                    value['revision'] = 'wrong' if self.mode == 'wrong-report-source' else REVISION
                (directory / (name + '.runtime.json')).write_text(json.dumps(value))
            if spec['aggregate']:
                (directory / 'loaded-validation.json').write_text(json.dumps({
                    'status': 'failed' if self.mode == 'failed-aggregate' else 'passed',
                    'revision': 'wrong' if self.mode == 'wrong-report-source' else REVISION,
                    'images': {name: json.loads((directory / (name + '.runtime.json')).read_bytes())
                               for name in spec['images']}}))
        if self.mode == 'tamper-archive':
            (self.private / 'swss-layer/docker-swss-layer-trixie.gz').write_bytes(b'changed')
        return b''

    def run_group(self, family):
        return self.caller['component_workload'](self.source, self.private, family,
            str(self.docker), self.env, {'state': self.state}, self.owner)

    def test_dict_paths_and_original_passed_swss_projection_retain_exact_reports(self):
        directory = self.seed('swss-layer')
        result = self.run_group('swss-layer')
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(result['native_acceptance_claimed'])
        record = result['reports']['runtime-smoke.json']
        self.assertEqual(Path(record['path']).read_bytes(), (directory / 'runtime-smoke.json').read_bytes())
        self.assertEqual(record['sha256'], hashlib.sha256(Path(record['path']).read_bytes()).hexdigest())
        self.assertEqual(len(self.calls), 1)
        self.chown.assert_called_once()

    def test_built_swss_receipt_stops_before_original_runtime(self):
        self.seed('swss-layer', public_status='built')
        with self.assertRaisesRegex(self.error, 'passed public receipt'):
            self.run_group('swss-layer')
        self.assertEqual(self.calls, [])

    def test_wrong_source_stops_before_original_runtime(self):
        directory = self.seed('swss-layer')
        path = directory / 'validation.json'
        value = json.loads(path.read_bytes())
        value['revision'] = 'wrong'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(self.error, 'validated runtime source'):
            self.run_group('swss-layer')
        self.assertEqual(self.calls, [])

    def test_changed_archive_stops_before_original_runtime(self):
        directory = self.seed('swss-layer')
        (directory / 'docker-swss-layer-trixie.gz').write_bytes(b'changed')
        with self.assertRaisesRegex(self.error, 'archive differs'):
            self.run_group('swss-layer')
        self.assertEqual(self.calls, [])

    def test_existing_original_report_is_preserved(self):
        directory = self.seed('swss-layer')
        original = directory / 'runtime-smoke.json'
        original.write_bytes(b'original evidence')
        with self.assertRaisesRegex(self.error, 'report already exists'):
            self.run_group('swss-layer')
        self.assertEqual(original.read_bytes(), b'original evidence')
        self.assertEqual(self.calls, [])

    def test_changed_archive_after_child_stops_before_report_copy(self):
        directory = self.seed('swss-layer')
        self.mode = 'tamper-archive'
        with self.assertRaisesRegex(self.error, 'input changed during'):
            self.run_group('swss-layer')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((directory / 'runtime').iterdir()), [])

    def test_home_change_is_rejected_without_mutating_actual_home(self):
        self.seed('swss-layer')
        original = dict(os.environ)
        self.env['HOME'] = '/different-fixture-home'
        with self.assertRaisesRegex(self.error, 'HOME differs'):
            self.run_group('swss-layer')
        self.assertEqual(dict(os.environ), original)
        self.assertEqual(self.calls, [])

    def test_output_worker_ownership_is_required(self):
        self.seed('swss-layer')
        self.output_uid = 0
        with self.assertRaisesRegex(self.error, 'worker-owned'):
            self.run_group('swss-layer')
        self.assertEqual(self.calls, [])

    def test_loaded_cli_report_must_bind_actual_source(self):
        directory = self.seed('database')
        self.mode = 'wrong-report-source'
        with self.assertRaisesRegex(self.error, 'aggregate differs'):
            self.run_group('database')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((directory / 'runtime').iterdir()), [])

    def test_sflow_executes_original_swss_dependency_inside_its_group_first(self):
        shared = self.seed('swss-layer')
        directory = self.seed('sflow')
        result = self.run_group('sflow')
        self.assertEqual([label for _, label in self.calls], ['component-sflow-swss', 'component-sflow'])
        self.assertTrue(result['shared_swss_dependency'])
        self.assertFalse((shared / 'runtime-smoke.json').exists())
        self.assertIn('swss-layer.runtime-smoke.json', result['reports'])
        self.assertEqual(len(list((directory / 'runtime').iterdir())), 3)

    def test_original_base_schema_and_two_reports_are_preserved(self):
        directory = self.seed('base')
        self.assertFalse((directory / 'build-receipt.json').exists())
        self.assertNotIn('status', json.loads((directory / 'docker-base-trixie.validation.json').read_bytes()))
        result = self.run_group('base')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(set(result['reports']), {'installed-images.json', 'runtime-smoke.json'})
        self.assertEqual(set(result['archives']), {'docker-base-trixie', 'docker-config-engine-trixie'})
        self.assertEqual([label for _, label in self.calls], ['component-base'])
        self.assertEqual(self.chown.call_count, 2)

    def test_changed_original_base_policy_stops_before_child(self):
        directory = self.seed('base')
        (directory / 'installed-images-policy.json').write_text('{}')
        with self.assertRaisesRegex(self.error, 'installed policy differs'):
            self.run_group('base')
        self.assertEqual(self.calls, [])

    def test_installed_base_report_must_bind_original_archive(self):
        directory = self.seed('base')
        self.mode = 'wrong-base-archive'
        with self.assertRaisesRegex(self.error, 'Installed base report differs'):
            self.run_group('base')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((directory / 'runtime').iterdir()), [])

    def test_database_uses_real_profiles_and_aggregate_source_without_invented_fields(self):
        directory = self.seed('database')
        self.assertFalse((directory / 'docker-database.validation.json').exists())
        result = self.run_group('database')
        per_image = result['original_owner_results']['docker-database.runtime.json']
        self.assertNotIn('status', per_image)
        self.assertNotIn('revision', per_image)
        self.assertEqual(set(per_image['profiles']), {'single', 'multi'})
        aggregate = result['original_owner_results']['loaded-validation.json']
        self.assertEqual(aggregate['revision'], REVISION)
        self.assertEqual(aggregate['images']['docker-database'], per_image)

    def test_failed_original_aggregate_stops_before_report_copies(self):
        directory = self.seed('database')
        self.mode = 'failed-aggregate'
        with self.assertRaisesRegex(self.error, 'aggregate differs'):
            self.run_group('database')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((directory / 'runtime').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
