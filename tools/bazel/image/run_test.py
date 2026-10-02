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
                'HostConfig': {'Init': True, 'Privileged': True, 'NanoCpus': identity['cpu_count'] * 1_000_000_000,
                               'Memory': identity['memory_bytes'], 'MemorySwap': identity['memory_bytes'],
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

    def test_git_transport_is_explicit_readonly_and_part_of_worker_identity(self):
        original = self.plan()
        config = self.workspace / 'source.gitconfig'
        config.write_text('[url "file:///source/"]\n insteadOf = https://example.test/\n')
        self.args.git_config = str(config)
        plan = self.plan()
        self.assertNotEqual(original['digest'], plan['digest'])
        self.assertIn((str(config), '/run/sonic-source.gitconfig', False), plan['mounts'])
        for key in ('create', 'disposable'):
            self.assertIn('GIT_CONFIG_SYSTEM=/run/sonic-source.gitconfig', plan[key])
        self.assertIn('--repo_env=GIT_CONFIG_SYSTEM=/run/sonic-source.gitconfig', plan['bazel_command'])
        worker = self.worker(plan)
        with self.assertRaisesRegex(ValueError, 'Git transport environment'):
            image_run.validate_worker(worker, plan)
        worker['Config']['Env'] = ['GIT_CONFIG_SYSTEM=/run/sonic-source.gitconfig', 'GIT_CONFIG_NOSYSTEM=0', 'CARGO_NET_GIT_FETCH_WITH_CLI=true']
        self.assertEqual(image_run.validate_worker(worker, plan), 'b' * 64)
        config.write_text(config.read_text() + '# changed policy\n')
        self.assertNotEqual(plan['digest'], self.plan()['digest'])
        self.args.git_config = '/etc/passwd'
        with self.assertRaisesRegex(ValueError, 'inside --mount-root'):
            self.plan()

    def test_persistent_server_and_isolation(self):
        plan = self.plan()
        self.assertIn('--init', plan['create'])
        self.assertNotIn('--rm', plan['create'])
        self.assertIn('--network=bridge', plan['create'])
        self.assertNotIn('--batch', plan['bazel_command'])
        self.assertEqual(image_run.validate_worker(self.worker(plan), plan), 'b' * 64)

    def test_trust_comes_from_declared_worker_not_ambient_host_mount(self):
        plan = self.plan()
        self.assertEqual(plan['mounts'], [
            (str(self.root), str(self.root), True),
            (str(self.spec), '/run/sonic-image-worker.json', False),
        ])
        worker = self.worker(plan)
        worker['Mounts'].append({'Type': 'bind', 'Source': '/etc/ssl/certs',
                                 'Destination': '/etc/ssl/certs', 'RW': False,
                                 'Propagation': 'rprivate'})
        with self.assertRaisesRegex(ValueError, 'bind mounts'):
            image_run.validate_worker(worker, plan)

    def test_explicit_default_resources_preserve_existing_worker_identity(self):
        original = self.plan()
        self.args.worker_cpus, self.args.worker_memory_gib = 8, 24
        plan = self.plan()
        self.assertEqual(plan['identity'], original['identity'])
        self.assertEqual(plan['digest'], original['digest'])
        self.assertEqual(plan['create'], original['create'])
        self.assertEqual(plan['bazel_command'], original['bazel_command'])

    def test_hosted_worker_resources_match_docker_identity_and_bazel_defaults(self):
        original = self.plan()
        self.args.worker_cpus, self.args.worker_memory_gib = 4, 12
        plan = self.plan()
        self.assertNotEqual(plan['digest'], original['digest'])
        for argument in ('--cpus=4', '--memory=12g', '--memory-swap=12g'):
            self.assertIn(argument, plan['create'])
        self.assertIn('--jobs=4', plan['bazel_command'])
        self.assertIn('--local_resources=cpu=4', plan['bazel_command'])
        self.assertNotIn('--jobs=8', plan['bazel_command'])
        self.assertEqual(image_run.validate_worker(self.worker(plan), plan), 'b' * 64)
        for field, incorrect in [('NanoCpus', 8_000_000_000), ('Memory', 24 * 1024 ** 3),
                                 ('MemorySwap', 24 * 1024 ** 3)]:
            with self.subTest(field=field):
                worker = self.worker(plan)
                worker['HostConfig'][field] = incorrect
                with self.assertRaisesRegex(ValueError, 'mismatched persistent worker'):
                    image_run.validate_worker(worker, plan)

    def test_worker_resources_reject_nonpositive_and_noninteger_values(self):
        for field in ('worker_cpus', 'worker_memory_gib'):
            for value in (0, -1, 2.5, '4', True, None):
                with self.subTest(field=field, value=value):
                    self.args.worker_cpus, self.args.worker_memory_gib = 8, 24
                    setattr(self.args, field, value)
                    with self.assertRaisesRegex(ValueError, 'positive integers'):
                        self.plan()

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
        for action in ('run', 'stop'):
            with self.subTest(action=action):
                self.args.worker_action = action
                self.args.command = ['--', 'build', '//:image'] if action == 'run' else []
                plan = self.plan()
                worker = self.worker(plan)
                worker['Config']['Labels'] = {}
                with mock.patch.object(image_run, 'inspect_worker', return_value=worker), \
                        mock.patch.object(image_run, 'capture') as capture, \
                        mock.patch.object(image_run.subprocess, 'run') as run:
                    with self.assertRaises(ValueError):
                        image_run.persistent_action(self.args, plan, self.root / 'owner.json')
                run.assert_not_called()
                capture.assert_not_called()

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
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertEqual(capture.call_args_list, [mock.call(['docker', 'stop', '--time', '30', 'b' * 64], timeout=40),
                                                mock.call(['docker', 'rm', 'b' * 64], timeout=10)])

    def test_failed_bazel_shutdown_still_removes_worker_and_propagates_status(self):
        self.args.command = []
        self.args.worker_action = 'stop'
        plan = self.plan()
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker'), \
                mock.patch.object(image_run, 'capture') as capture, \
                mock.patch.object(image_run.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2)):
            self.assertEqual(image_run.persistent_action(self.args, plan, self.root / 'owner.json'), 2)
        self.assertEqual(capture.call_args_list, [mock.call(['docker', 'stop', '--time', '30', 'b' * 64], timeout=40),
                                                mock.call(['docker', 'rm', 'b' * 64], timeout=10)])
        self.assertFalse((self.root / 'owner.json').exists())

    def test_unready_worker_is_still_removed_after_identity_validation(self):
        self.args.command = []
        self.args.worker_action = 'stop'
        plan = self.plan()
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker', side_effect=RuntimeError('bootstrap failed')), \
                mock.patch.object(image_run, 'capture') as capture, \
                mock.patch.object(image_run.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'worker removed.*bootstrap failed'):
                image_run.persistent_action(self.args, plan, self.root / 'owner.json')
        run.assert_not_called()
        self.assertEqual(capture.call_args_list, [mock.call(['docker', 'stop', '--time', '30', 'b' * 64], timeout=40),
                                                mock.call(['docker', 'rm', 'b' * 64], timeout=10)])
        self.assertFalse((self.root / 'owner.json').exists())

    def test_bazel_shutdown_timeout_does_not_bypass_worker_cleanup(self):
        self.args.command = []
        self.args.worker_action = 'stop'
        plan = self.plan()
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker'), \
                mock.patch.object(image_run, 'capture') as capture, \
                mock.patch.object(image_run.subprocess, 'run', side_effect=subprocess.TimeoutExpired('shutdown', 30)):
            with self.assertRaisesRegex(RuntimeError, 'worker removed.*TimeoutExpired'):
                image_run.persistent_action(self.args, plan, self.root / 'owner.json')
        self.assertEqual(capture.call_count, 2)
        self.assertFalse((self.root / 'owner.json').exists())

    def test_docker_stop_failure_attempts_exact_id_kill_and_removal_then_reports_error(self):
        self.args.command = []
        self.args.worker_action = 'stop'
        plan = self.plan()
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker'), \
                mock.patch.object(image_run, 'capture', side_effect=[RuntimeError('daemon stop failed'), '', '']) as capture, \
                mock.patch.object(image_run.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(RuntimeError, 'cleanup reported errors.*stop: daemon stop failed'):
                image_run.persistent_action(self.args, plan, self.root / 'owner.json')
        self.assertEqual(capture.call_args_list, [mock.call(['docker', 'stop', '--time', '30', 'b' * 64], timeout=40),
                                                mock.call(['docker', 'kill', 'b' * 64], timeout=10),
                                                mock.call(['docker', 'rm', 'b' * 64], timeout=10)])
        self.assertFalse((self.root / 'owner.json').exists())

    def test_docker_removal_failure_preserves_owner_and_reports_shutdown_error_separately(self):
        self.args.command = []
        self.args.worker_action = 'stop'
        plan = self.plan()
        owner = self.root / 'owner.json'
        with mock.patch.object(image_run, 'inspect_worker', return_value=self.worker(plan)), \
                mock.patch.object(image_run, 'ready_worker'), \
                mock.patch.object(image_run, 'capture', side_effect=['', RuntimeError('daemon remove failed')]), \
                mock.patch.object(image_run.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2)):
            with self.assertRaisesRegex(RuntimeError, 'cleanup reported errors') as raised:
                image_run.persistent_action(self.args, plan, owner)
        self.assertIn('Bazel shutdown exited with status 2', str(raised.exception))
        self.assertIn('remove: daemon remove failed', str(raised.exception))
        self.assertEqual(json.loads(owner.read_text())['container_id'], 'b' * 64)

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
