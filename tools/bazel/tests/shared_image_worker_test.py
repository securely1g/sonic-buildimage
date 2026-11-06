#!/usr/bin/env python3
"""Pure checks for existing shared-image command reuse and public views."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import shared_image_worker as shared
from tools.bazel.ci import source_image_worker as worker
from tools.bazel.ci.public_artifacts import safe_json


class SharedImageWorkerTest(unittest.TestCase):
    def test_workflow_keeps_combined_interface_separate_from_service_phases(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition
        workflow = yaml.safe_load((ROOT / '.github/workflows/bazel-oci.yml').read_text())
        steps = {step['name']: step for step in workflow['jobs']['source_image']['steps'] if 'name' in step}
        for image in ('base-config', 'swss-layer', 'snmp', 'p4rt'):
            shared_mode = image in worker.SHARED_PROFILES
            self.assertEqual(condition(steps['Build and inspect the selected shared images']['if'], mode=image), shared_mode)
            for name in ('Build source runtime and debug images', 'Validate image contents and symbols'):
                self.assertEqual(condition(steps[name]['if'], mode=image), not shared_mode)

    def test_combined_base_interface_and_distinct_phase(self):
        workspace, artifacts = Path('/source'), Path('/receipts')
        commands = shared.commands('base-config', 'build-inspect', workspace, artifacts)
        self.assertEqual(commands, [[sys.executable, '-B', '/source/tools/bazel/ci/base_images.py',
            '--bazel', '/source/tools/bazel/ci/libteam_draft_bazel.sh', '--architecture', 'amd64',
            '--artifacts', '/receipts', '--config-engine-acceptance']])
        for phase in ('build', 'validate'):
            with self.assertRaises(ValueError):
                worker.phase_command('base-config', phase, workspace, artifacts)
        selected = worker.phase_command('base-config', 'build-inspect', workspace, artifacts)
        self.assertIn(str(ROOT / 'tools/bazel/ci/shared_image_worker.py'), selected)
        self.assertIn('build-inspect', selected)

    def test_swss_extra_audit_precedes_exact_existing_consumer_tests(self):
        commands = shared.commands('swss-layer', 'build-inspect', Path('/source'), Path('/receipts'))
        self.assertEqual(len(commands), 4)
        self.assertTrue(commands[1][2].endswith('/swss_layer.py'))
        self.assertEqual(commands[2][-5:], [*shared.SWSS_TESTS, shared.SWSS_PARENT_ARCHIVE])
        self.assertTrue(commands[2][2].endswith('/bazel_commands.py'))
        self.assertEqual(commands[3][-4:], shared.SWSS_TESTS)
        self.assertEqual(commands[3][1], 'test')
        self.assertIn('--nocache_test_results', commands[3])
        for flag in ('--jobs=2', '--local_resources=cpu=2', '--local_resources=memory=8000'):
            self.assertIn(flag, commands[3])
        self.assertNotIn(shared.SWSS_PARENT_ARCHIVE, commands[3])

    def test_original_incremental_interfaces_and_exact_parent(self):
        base = shared.commands('base-config', 'incremental', Path('/source'), Path('/receipts'))
        self.assertEqual(len(base), 1)
        self.assertTrue(base[0][2].endswith('/config_engine_rebuild.py'))
        self.assertEqual(base[0][-2:], ['--output', '/receipts/config-engine-rebuild.json'])
        swss = shared.commands('swss-layer', 'incremental', Path('/source'), Path('/receipts'))[0]
        self.assertTrue(swss[2].endswith('/swss_image_rebuild.py'))
        self.assertEqual(swss[swss.index('--unchanged-target') + 1], shared.SWSS_PARENT_ARCHIVE)
        self.assertEqual(swss[swss.index('--additional-runtime-target') + 1],
                         '//dockers/docker-swss-layer-trixie:source_runtime')

    def test_loaded_commands_keep_private_original_contract(self):
        for image, tag, probe in [('base-config', 'docker-config-engine-trixie:latest', '/probe/smoke.py'),
                                  ('swss-layer', 'docker-swss-layer-trixie:latest', '/probe/swss_layer_runtime.py')]:
            commands = shared.commands(image, 'loaded', Path('/source'), Path('/receipts'))
            self.assertEqual(commands[0][:3], [sys.executable, '-B', '-c'])
            self.assertIn("receipt['revision']", commands[0][3])
            command = commands[-1]
            for flag in ('--rm', '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                         '--read-only', '--pids-limit=128', '--memory=4g', '--cpus=2'):
                self.assertIn(flag, command)
            self.assertEqual(command[-4:], [tag, '-B', probe, '/probe/validation.json'])
            self.assertTrue(all(m.endswith(',readonly') for m in command if m.startswith('type=bind,')))
            self.assertNotIn('--privileged', command)
        base = shared.commands('base-config', 'loaded', Path('/source'), Path('/receipts'))
        self.assertTrue(base[1][2].endswith('/validate_runtime.py'))
        self.assertIn('--images-policy', base[1])

    def test_command_failure_stops_before_later_native_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = ['shared_image_worker.py', 'build-inspect', '--image', 'swss-layer',
                    '--workspace', str(root), '--artifacts', str(root / 'receipts')]
            with patch.object(sys, 'argv', argv), patch.object(shared.subprocess, 'run',
                    side_effect=subprocess.CalledProcessError(1, 'guarded recorder')) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    shared.main()
                self.assertEqual(run.call_count, 1)

    def test_env_projection_is_exact_named_nonmutating_and_digest_bound(self):
        value = {'config': {'Env': ['PRIVATE_TOKEN=do-not-publish'], 'WorkingDir': '/'},
                 'native': {'usr/bin/example': {'sha256': 'b' * 64}}}
        original = copy.deepcopy(value)
        projected = shared.public_view('docker-base-trixie.validation.json', value, 'a' * 64)
        self.assertEqual(value, original)
        self.assertNotIn('Env', projected['config'])
        self.assertEqual(projected['native'], original['native'])
        self.assertEqual(projected['public_projection']['original_file_sha256'], 'a' * 64)
        self.assertEqual(projected['public_projection']['omitted_fields'], ['/config/Env'])
        safe_json(projected)
        self.assertIsNone(shared.public_view('other.validation.json', value, 'a' * 64))
        with self.assertRaises(ValueError):
            safe_json(value)
        poisoned = {**value, 'password': 'must-still-fail'}
        with self.assertRaises(ValueError):
            safe_json(shared.public_view('docker-base-trixie.validation.json', poisoned, 'a' * 64))

    def test_policy_projection_omits_only_two_exact_env_paths(self):
        policy = {'images': [{'name': name, 'expected_config': {'Env': ['PRIVATE=1'], 'Cmd': []},
                              'archive_sha256': 'c' * 64} for name in shared.IMAGES]}
        original = copy.deepcopy(policy)
        result = shared.public_view('installed-images-policy.json', policy, 'a' * 64)
        self.assertEqual(policy, original)
        self.assertEqual(result['public_projection']['omitted_fields'],
                         ['/images/0/expected_config/Env', '/images/1/expected_config/Env'])
        safe_json(result)
        policy['images'].reverse()
        with self.assertRaises(ValueError):
            shared.public_view('installed-images-policy.json', policy, 'a' * 64)

    def test_publication_preserves_original_bytes_and_projects_only_public_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b'{"config":{"Env":["PRIVATE=1"],"WorkingDir":"/"},"native":{}}\n'
            name = 'docker-config-engine-trixie.validation.json'
            (root / name).write_bytes(original)
            (root / 'receipt.json').write_text(json.dumps({'status': 'passed', 'architecture': 'amd64',
                'revision': 'a' * 40, 'artifacts': {}, 'commands': ['private diagnostic command'],
                'installed_images_policy_sha256': 'b' * 64}))
            (root / 'worker-private.json').write_text('{"environment":{"PRIVATE":"1"}}')
            worker.publish(SimpleNamespace(image='base-config', artifacts=root))
            self.assertEqual((root / name).read_bytes(), original)
            public = json.loads((root / 'public' / name).read_text())
            self.assertEqual(public['public_projection']['original_file_sha256'], hashlib.sha256(original).hexdigest())
            self.assertNotIn('Env', public['config'])
            self.assertFalse((root / 'public/worker-private.json').exists())
            self.assertNotIn('commands', json.loads((root / 'public/public-receipt.json').read_text()))


if __name__ == '__main__':
    unittest.main()
