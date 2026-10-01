#!/usr/bin/env python3
"""Worker reuse must preserve the declared environment and refuse collisions."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('image_run', Path(__file__).with_name('run.py'))
image_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_run)


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'MODULE.bazel').touch()
        self.spec = self.root / 'worker.json'
        self.spec.write_text(json.dumps({'schema': 1, 'platform': 'linux/amd64',
                                        'worker_image': 'sha256:' + 'a' * 64}))
        self.args = argparse.Namespace(
            mount_root=str(self.root), workspace=str(self.workspace), worker_spec=str(self.spec),
            output_user_root=str(self.root / 'outputs'), bazel='/usr/local/bin/bazel',
            repository_cache=None, command=['--', 'build', '//:image'],
            persistent_worker='test-worker', worker_action='run')

    def plan(self):
        return image_run.build_plan(self.args)

    def worker(self, plan):
        identity = plan['identity']
        return {'Id': 'b' * 64, 'Name': '/' + identity['name'], 'Image': identity['image'],
                'Config': {'Image': identity['image'], 'Labels': {image_run.LABEL: plan['digest']},
                           'Entrypoint': ['/bin/bash'],
                           'Cmd': ['-ec', plan['bootstrap'], 'sonic-image-worker'],
                           'WorkingDir': identity['workspace'], 'User': '0:0'},
                'HostConfig': {'Init': True, 'Privileged': True, 'NanoCpus': 8_000_000_000,
                               'Memory': image_run.MEMORY, 'MemorySwap': image_run.MEMORY,
                               'NetworkMode': 'bridge', 'PidMode': '', 'IpcMode': 'private',
                               'AutoRemove': False, 'PublishAllPorts': False, 'PortBindings': {},
                               'RestartPolicy': {'Name': 'no'}},
                'Mounts': [{'Type': 'bind', 'Source': source, 'Destination': target,
                            'RW': writable, 'Propagation': 'rprivate'}
                           for source, target, writable in plan['mounts']],
                'State': {'Running': True, 'Status': 'running'}}

    def test_disposable_default_preserved(self):
        self.args.persistent_worker = None
        command = image_run.build_command(self.args)
        self.assertEqual(command[:5], ['docker', 'run', '--rm', '--init', '--pull=never'])
        self.assertIn('--batch', command)
        self.assertIn('--jobs=8', command)
        self.assertIn('--memory=24g', command)

    def test_persistent_server_and_isolation(self):
        plan = self.plan()
        self.assertIn('--init', plan['create'])
        self.assertNotIn('--rm', plan['create'])
        self.assertIn('--network=bridge', plan['create'])
        self.assertNotIn('--batch', plan['bazel_command'])
        self.assertEqual(image_run.validate_worker(self.worker(plan), plan), 'b' * 64)

    def test_explicit_user_home_preserves_action_environment(self):
        original = self.plan()
        self.args.worker_user = 'lgh'
        self.args.worker_home = '/var/lgh'
        plan = self.plan()
        self.assertNotEqual(original['digest'], plan['digest'])
        self.assertIn('useradd -u 1000 -g 1000 -d /var/lgh', plan['bootstrap'])
        self.assertIn('lgh:/var/lgh', plan['bootstrap'])
        self.assertNotIn('sonic-builder', plan['bootstrap'])
        self.assertEqual(image_run.validate_worker(self.worker(plan), plan), 'b' * 64)

    def test_explicit_account_fields_preserve_builder_substrings(self):
        for user, home in [('alice', '/var/sonic-builder'),
                           ('alice', '/srv/sonic-builder/home'),
                           ('sonic-builder-ci', '/var/custom'),
                           ('sonic-builder-ci', '/srv/sonic-builder/home')]:
            with self.subTest(user=user, home=home):
                self.args.worker_user, self.args.worker_home = user, home
                bootstrap = self.plan()['bootstrap']
                commands = [shlex.split(line) for line in bootstrap.splitlines()
                            if line.strip().startswith(('groupadd ', 'useradd '))]
                groupadd, useradd = commands
                self.assertEqual(groupadd[-1], user)
                self.assertEqual(useradd[-1], user)
                self.assertEqual(useradd[useradd.index('-d') + 1], home)
                self.assertIn('= "' + user + ':' + home + '" ]', bootstrap)

    def test_invalid_or_partial_user_identity_rejected(self):
        for user, home in [('lgh', None), (None, '/var/lgh'), ('bad;cmd', '/var/lgh'),
                           ('lgh', '/var/../lgh'), ('lgh', '/tmp/$(cmd)')]:
            self.args.worker_user, self.args.worker_home = user, home
            with self.subTest(user=user, home=home), self.assertRaises(ValueError):
                self.plan()

    def test_persistent_digest_cache_is_bounded_and_overridable(self):
        self.args.command += ['--cache_computed_file_digests=10000']
        command = self.plan()['bazel_command']
        self.assertLess(command.index('--cache_computed_file_digests=200000'),
                        command.index('--cache_computed_file_digests=10000'))
        self.args.persistent_worker = None
        self.assertNotIn('--cache_computed_file_digests=200000', self.plan()['disposable'])

    def test_lifecycle_without_bazel_command(self):
        self.args.command = []
        self.args.worker_action = 'start'
        self.assertEqual(self.plan()['identity']['name'], 'test-worker')
        self.args.persistent_worker = None
        with self.assertRaisesRegex(ValueError, 'requires --persistent-worker'):
            self.plan()

    def test_lifecycle_rejects_accidental_bazel_command(self):
        self.args.worker_action = 'stop'
        with self.assertRaisesRegex(ValueError, 'do not take a Bazel command'):
            self.plan()

    def test_server_output_directory_and_mode_cannot_be_overridden(self):
        for option in ['--batch', '--nobatch', '--output_base=/tmp/other', '--output_user_root=/tmp/other']:
            with self.subTest(option=option):
                self.args.command = [option, 'build', '//:image']
                with self.assertRaisesRegex(ValueError, 'controls Bazel startup option'):
                    self.plan()

    def test_other_startup_options_reach_bazel_for_normal_restart(self):
        original = self.plan()
        self.args.command = ['--host_jvm_args=-Xmx2g', 'build', '//:image']
        updated = self.plan()
        self.assertEqual(original['digest'], updated['digest'])
        self.assertLess(updated['bazel_command'].index('--host_jvm_args=-Xmx2g'),
                        updated['bazel_command'].index('build'))

    def test_image_spec_content_and_host_bazel_are_identity_inputs(self):
        first = self.plan()['digest']
        value = json.loads(self.spec.read_text())
        value['docker_version'] = 'different'
        self.spec.write_text(json.dumps(value))
        second = self.plan()['digest']
        self.assertNotEqual(first, second)
        bazel = self.root / 'bazel'
        bazel.write_bytes(b'first')
        self.args.bazel = str(bazel)
        third = self.plan()['digest']
        bazel.write_bytes(b'second')
        self.assertNotEqual(third, self.plan()['digest'])

    def test_mismatched_worker_rejected_before_exec(self):
        plan = self.plan()
        cases = [
            ('Config', 'Image', 'sha256:' + 'c' * 64),
            ('Config', 'Labels', {}), ('Config', 'WorkingDir', '/elsewhere'),
            ('Config', 'User', '0'), ('Config', 'Cmd', ['sh']),
            ('HostConfig', 'NanoCpus', 4_000_000_000),
            ('HostConfig', 'Memory', 123), ('HostConfig', 'MemorySwap', -1),
            ('HostConfig', 'Privileged', False), ('HostConfig', 'Init', False),
            ('HostConfig', 'NetworkMode', 'host'), ('HostConfig', 'PidMode', 'host'),
            ('HostConfig', 'IpcMode', 'host'), ('HostConfig', 'PublishAllPorts', True),
            ('HostConfig', 'PortBindings', {'22/tcp': [{'HostPort': '2222'}]}),
        ]
        for section, field, value in cases:
            with self.subTest(field=field):
                worker = self.worker(plan)
                worker[section][field] = value
                with self.assertRaisesRegex(ValueError, 'mismatched persistent worker'):
                    image_run.validate_worker(worker, plan)

    def test_host_docker_socket_or_mount_change_rejected(self):
        plan = self.plan()
        worker = self.worker(plan)
        worker['Mounts'].append({'Type': 'bind', 'Source': '/var/run/docker.sock',
                                 'Destination': '/var/run/docker.sock', 'RW': True, 'Propagation': 'rprivate'})
        with self.assertRaisesRegex(ValueError, 'bind mounts'):
            image_run.validate_worker(worker, plan)
        for field, value in [('Source', '/different'), ('RW', False), ('Propagation', 'rshared')]:
            worker = self.worker(plan)
            worker['Mounts'][0][field] = value
            with self.assertRaisesRegex(ValueError, 'bind mounts'):
                image_run.validate_worker(worker, plan)

    def test_existing_bad_worker_is_never_started_or_removed(self):
        plan = self.plan()
        worker = self.worker(plan)
        worker['Config']['Labels'] = {}
        with mock.patch.object(image_run, 'inspect_worker', return_value=worker), \
                mock.patch.object(image_run.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                image_run.persistent_action(self.args, plan, self.root / 'owner.json')
        run.assert_not_called()

    def test_name_creation_race_validates_winner(self):
        plan = self.plan()
        worker = self.worker(plan)
        worker['Config']['Labels'] = {}
        with mock.patch.object(image_run, 'inspect_worker', side_effect=[None, worker]), \
                mock.patch.object(image_run.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'name conflict')) as run:
            with self.assertRaises(ValueError):
                image_run.persistent_action(self.args, plan, self.root / 'owner.json')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1], 'create')

    def test_reuse_executes_immutable_container_id(self):
        plan = self.plan()
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker'), \
                mock.patch.object(image_run.subprocess, 'run', return_value=subprocess.CompletedProcess([], 17)) as run:
            self.assertEqual(image_run.persistent_action(self.args, plan, self.root / 'owner.json'), 17)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['docker', 'exec'])
        self.assertIn('b' * 64, command)
        self.assertNotIn('test-worker', command)
        self.assertNotIn('--batch', command)

    def test_stop_removes_only_verified_container_after_shutdown(self):
        self.args.command = []
        self.args.worker_action = 'stop'
        plan = self.plan()
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker'), \
                mock.patch.object(image_run, 'capture', return_value='') as capture, \
                mock.patch.object(image_run.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(image_run.persistent_action(self.args, plan, self.root / 'owner.json'), 0)
        self.assertEqual(run.call_args.args[0][-1], 'shutdown')
        self.assertEqual(capture.call_args_list, [mock.call(['docker', 'stop', '--time', '30', 'b' * 64]),
                                                mock.call(['docker', 'rm', 'b' * 64])])

    def test_stop_failure_preserves_worker(self):
        self.args.command = []
        self.args.worker_action = 'stop'
        plan = self.plan()
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker'), \
                mock.patch.object(image_run, 'capture') as capture, \
                mock.patch.object(image_run.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2)):
            self.assertEqual(image_run.persistent_action(self.args, plan, self.root / 'owner.json'), 2)
        capture.assert_not_called()

    def test_output_root_cannot_be_shared_with_other_worker_or_disposable(self):
        plan = self.plan()
        owner = self.root / 'owner.json'
        owner.write_text(json.dumps({'name': 'another', 'container_id': 'f' * 64}))
        with mock.patch.object(image_run, 'inspect_worker', return_value={'Id': 'f' * 64}):
            with self.assertRaisesRegex(ValueError, 'stop it first'):
                image_run.check_owner(owner, plan)
            self.args.persistent_worker = None
            with self.assertRaisesRegex(ValueError, 'stop it first'):
                image_run.check_owner(owner, self.plan())

    def test_renamed_previous_worker_cannot_be_replaced_on_same_output_root(self):
        plan = self.plan()
        owner = self.root / 'owner.json'
        owner.write_text(json.dumps({'name': 'test-worker', 'container_id': 'f' * 64}))
        with mock.patch.object(image_run, 'inspect_worker', side_effect=[self.worker(plan), {'Id': 'f' * 64}]), \
                mock.patch.object(image_run.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'previous worker still exists'):
                image_run.persistent_action(self.args, plan, owner)
        run.assert_not_called()

    def test_live_owner_receipt_identity_must_match(self):
        plan = self.plan()
        owner = self.root / 'owner.json'
        owner.write_text(json.dumps({'name': 'test-worker', 'container_id': 'b' * 64, 'identity': 'different'}))
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'ownership identity'):
                image_run.persistent_action(self.args, plan, owner)
        run.assert_not_called()

    def test_stale_owner_is_released_only_after_container_is_absent(self):
        owner = self.root / 'owner.json'
        owner.write_text(json.dumps({'name': 'another', 'container_id': 'f' * 64}))
        with mock.patch.object(image_run, 'inspect_worker', return_value=None):
            image_run.check_owner(owner, self.plan())
        self.assertFalse(owner.exists())

    def test_lock_serializes_processes_and_releases_after_exit(self):
        root = self.root / 'lock-test'
        with image_run.output_lock(root):
            code = '''import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(23)
'''
            command = [os.sys.executable, '-c', code, str(root / '.sonic-image-worker.lock')]
            self.assertEqual(subprocess.run(command).returncode, 23)
        self.assertEqual(subprocess.run(command).returncode, 0)

    def test_lock_and_owner_symlinks_rejected(self):
        root = self.root / 'lock-test'
        root.mkdir()
        (root / '.sonic-image-worker.lock').symlink_to(self.spec)
        with self.assertRaises(OSError), image_run.output_lock(root):
            pass
        owner = root / 'owner.json'
        owner.symlink_to(self.spec)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            image_run.check_owner(owner, self.plan())


if __name__ == '__main__':
    unittest.main()
