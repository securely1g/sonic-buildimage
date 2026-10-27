"""Check the real Make launcher preserves arguments and accessible cache paths."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.bazel.oci import native_build
from tools.bazel.ci import bazel_commands


class NativeBuildTest(unittest.TestCase):
    def setUp(self):
        # Command tests must not inherit the CI host's /bazel_cache mount.
        self.shared_cache = False
        is_dir = Path.is_dir
        cache_mount = patch.object(Path, 'is_dir',
            lambda path: self.shared_cache if path == Path('/bazel_cache') else is_dir(path))
        cache_mount.start()
        self.addCleanup(cache_mount.stop)

    def test_private_cache_and_arguments_remain_literal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'checkout with spaces'
            workspace.mkdir()
            cache = root / 'cache'
            cache.mkdir()
            bazel = root / 'bazel'
            bazel.touch()
            repository = root / 'repository-cache'
            extra = '--define=value=$(touch injected);literal'
            arguments = ['cquery', '--repository_cache=' + str(repository), extra, '//:image']
            with patch.object(native_build.subprocess, 'check_output', return_value=str(workspace / '.git') + '\n'):
                command = native_build.command(arguments, workspace, bazel, cache)
            self.assertEqual(command[-4:], arguments)
            self.assertIn('--output_user_root=' + str(cache / 'bazel'), command)
            self.assertIn('type=bind,src=' + str(repository) + ',dst=' + str(repository), command)
            self.assertIn('type=bind,src=' + str(bazel) + ',dst=' + str(bazel) + ',readonly', command)
            self.assertIn(native_build.ENVIRONMENT, command)
            self.assertFalse(any('docker.sock' in item for item in command))
            bootstrap = command[command.index('sh') + 2]
            self.assertNotIn(extra, bootstrap)
            self.assertIn('exec "$@"', bootstrap)
            self.assertLess(bootstrap.index('audit_command(sys.argv[1:]'), bootstrap.index('exec "$@"'))
            self.assertTrue(repository.is_dir())

    def test_executable_version_probe_uses_selected_bazel_without_build_state(self):
        """Container CI can probe this executable before Docker or native setup."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bazel = root / 'selected bazel'
            bazel.write_text('#!' + sys.executable + '\n'
                             'import os, sys\n'
                             'assert sys.argv[1:] == ["--version"]\n'
                             'print("bazel 8.5.1")\n'
                             'sys.exit(int(os.environ["VERSION_STATUS"]))\n')
            bazel.chmod(0o755)
            cache = root / 'native-cache'
            environment = dict(os.environ, BAZEL=str(bazel), BAZEL_NATIVE_CACHE_DIR=str(cache),
                               BAZEL_NATIVE_STARTUP_ARGS='not needed for a version probe')
            for status in (0, 23):
                with self.subTest(status=status):
                    result = subprocess.run([str(Path(native_build.__file__).resolve()), '--version'],
                                            cwd=root, env=dict(environment, VERSION_STATUS=str(status)),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(result.stdout, 'bazel 8.5.1\n')
                    self.assertEqual(result.stderr, '')
                    self.assertFalse(cache.exists())

    def test_native_container_retains_shared_cache_configuration_when_mounted(self):
        """Root must retain Make's mounted inputs without writing its ordinary caches."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            cache = workspace / 'private-cache'
            cache.mkdir()
            bazel = workspace / 'bazel'
            bazel.touch()
            rc = workspace / 'tools/bazel/slave.bazelrc'
            rc.parent.mkdir(parents=True)
            rc.write_text('common --repository_cache=/bazel_cache/repository_cache\n'
                          'common --disk_cache=/bazel_cache/disk_cache\n')
            arguments = ['build', '--disk_cache=' + str(workspace / 'explicit-disk'), '//:image']
            for present in (False, True):
                with self.subTest(shared_cache=present):
                    self.shared_cache = present
                    with patch.object(native_build.subprocess, 'check_output', return_value=str(workspace / '.git') + '\n'), \
                         patch.object(Path, 'mkdir'):
                        argv = native_build.command(arguments, workspace, bazel, cache)
                    rc_mount = 'type=bind,src=' + str(rc) + ',dst=/etc/bazel.bazelrc,readonly'
                    self.assertEqual(rc_mount in argv, present)
                    self.assertEqual('type=bind,src=/bazel_cache,dst=/bazel_cache' in argv, present)
                    expected = '/bazel_cache/native-root/bazelisk' if present else str(cache / 'bazelisk')
                    self.assertIn('BAZELISK_HOME=' + expected, argv)
                    expected_arguments = arguments
                    if present:
                        expected_arguments = ['build',
                            '--repository_cache=/bazel_cache/native-root/repository_cache',
                            '--disk_cache=/bazel_cache/native-root/disk_cache', *arguments[1:]]
                    self.assertEqual(argv[argv.index('build'):], expected_arguments)

    def test_explicit_cache_choices_override_native_defaults_and_keep_distdir(self):
        """A caller can choose or disable each cache; source downloads stay shared."""
        self.shared_cache = True
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            cache = workspace / 'private-cache'
            cache.mkdir()
            bazel = workspace / 'bazel'
            bazel.touch()
            rc = workspace / 'tools/bazel/slave.bazelrc'
            rc.parent.mkdir(parents=True)
            rc.touch()
            for value in ('', str(workspace / 'explicit-cache')):
                with self.subTest(cache=value):
                    arguments = ['build', '--repository_cache=' + value, '--disk_cache=' + value,
                                 '--distdir=/bazel_cache/distdir', '//:image']
                    with patch.object(native_build.subprocess, 'check_output', return_value=str(workspace / '.git') + '\n'), \
                         patch.object(Path, 'mkdir'):
                        argv = native_build.command(arguments, workspace, bazel, cache)
                    selected = argv[argv.index('build'):]
                    # Bazel gives later scalar flags precedence over RC/defaults.
                    for name in ('repository_cache', 'disk_cache'):
                        prefix = '--' + name + '='
                        values = [item[len(prefix):] for item in selected if item.startswith(prefix)]
                        self.assertEqual(values, ['/bazel_cache/native-root/' + name, value])
                    self.assertIn('--distdir=/bazel_cache/distdir', selected)
                    self.assertIn('type=bind,src=/bazel_cache,dst=/bazel_cache', argv)
                    self.assertEqual(selected[-len(arguments[1:]):], arguments[1:])

    def test_root_direct_execution_audits_and_uses_the_same_cache_choices(self):
        """Entering an existing native container must not bypass writer isolation."""
        original = ['run', '--repository_cache=', '--disk_cache=/custom/disk',
                    '--distdir=/bazel_cache/distdir', '//:export', '--', '--output', '/tmp/image']
        startup = ['--output_user_root=/private/output']
        for present in (False, True):
            with self.subTest(shared_cache=present):
                self.shared_cache = present
                environment = {'APT_INSTALL_BUILD_CONTAINER': native_build.ENVIRONMENT,
                               'BAZEL_NATIVE_STARTUP_ARGS': json.dumps(startup),
                               'BAZELISK_HOME': '/bazel_cache/bazelisk'}
                executed = {}

                def execute(executable, argv):
                    executed.update(executable=executable, argv=argv,
                                    bazelisk_home=native_build.os.environ['BAZELISK_HOME'])
                    raise SystemExit(0)

                with patch.object(native_build.sys, 'argv', ['native_build.py', *original]), \
                     patch.object(native_build.os, 'geteuid', return_value=0), \
                     patch.dict(native_build.os.environ, environment), \
                     patch.object(native_build.shutil, 'which', return_value='/usr/bin/bazel'), \
                     patch.object(native_build, 'audit_command') as audit, \
                     patch.object(native_build.os, 'execv', side_effect=execute):
                    with self.assertRaises(SystemExit) as result:
                        native_build.main()
                self.assertEqual(result.exception.code, 0)
                expected = ['/usr/bin/bazel', *startup, 'run']
                if present:
                    expected += ['--repository_cache=/bazel_cache/native-root/repository_cache',
                                 '--disk_cache=/bazel_cache/native-root/disk_cache']
                expected += original[1:]
                self.assertEqual(executed['executable'], '/usr/bin/bazel')
                self.assertEqual(executed['argv'], expected)
                self.assertEqual(audit.call_args.args[0], expected)
                self.assertEqual(executed['bazelisk_home'],
                    '/bazel_cache/native-root/bazelisk' if present else '/bazel_cache/bazelisk')

    def test_mod_graph_keeps_native_cache_and_output_context_without_build_audit(self):
        """Provenance collection uses the same root cache and Bazel server as builds."""
        self.shared_cache = True
        arguments = ['mod', 'graph', '--output=json', '--extension_info=hidden', '--lockfile_mode=off']
        startup = ['--output_base=/private/selected-output']
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            cache = workspace / 'private-cache'
            bazel = workspace / 'bazel'
            rc = workspace / 'tools/bazel/slave.bazelrc'
            rc.parent.mkdir(parents=True)
            rc.touch()
            with patch.object(native_build.subprocess, 'check_output', return_value=str(workspace / '.git') + '\n'), \
                 patch.object(Path, 'mkdir'):
                docker = native_build.command(arguments, workspace, bazel, cache, startup)
            expected = [str(bazel), '--output_user_root=' + str(cache / 'bazel'), *startup, 'mod',
                        '--repository_cache=/bazel_cache/native-root/repository_cache',
                        '--disk_cache=/bazel_cache/native-root/disk_cache', *arguments[1:]]
            self.assertEqual(docker[docker.index('native-bazel') + 1:], expected)
            self.assertIn('APT_INSTALL_BUILD_CONTAINER=' + native_build.ENVIRONMENT, docker)
            self.assertIn('BAZELISK_HOME=/bazel_cache/native-root/bazelisk', docker)
            self.assertIn('type=bind,src=' + str(rc) + ',dst=/etc/bazel.bazelrc,readonly', docker)
            with patch.object(native_build.sys, 'argv', ['native_build.py', *arguments]), \
                 patch.object(native_build.os, 'geteuid', return_value=0), \
                 patch.dict(native_build.os.environ, {
                     'APT_INSTALL_BUILD_CONTAINER': native_build.ENVIRONMENT,
                     'BAZEL_NATIVE_STARTUP_ARGS': json.dumps(startup),
                 }), \
                 patch.object(native_build.shutil, 'which', return_value=str(bazel)), \
                 patch.object(bazel_commands, 'audit_targets') as audit, \
                 patch.object(native_build.os, 'execv', side_effect=SystemExit(0)) as execute:
                with self.assertRaises(SystemExit) as result:
                    native_build.main()
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(execute.call_args.args,
                             (str(bazel), [str(bazel), *startup, *expected[expected.index('mod'):]]))
            audit.assert_not_called()

    def test_shared_output_containers_wait_and_release_after_failure(self):
        """Real launcher processes cannot overlap Docker or strand the lock on failure.

        The fake Docker process holds a marker while its container is alive. A
        second launcher has reached flock before we check that it cannot enter.
        Symlink aliases and explicit startup overrides must select the same lock.
        """
        child = '''
import importlib.util, json, os, pathlib, subprocess, sys
from unittest.mock import patch
source, workspace, control, name, cache, startup, status = sys.argv[1:]
spec = importlib.util.spec_from_file_location('native_build_under_test', source)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
os.chdir(workspace)
os.environ['BAZEL_NATIVE_CACHE_DIR'] = cache
os.environ['BAZEL_NATIVE_STARTUP_ARGS'] = startup
sys.argv = ['native_build.py', 'build', '//:' + name]
original_flock = native.fcntl.flock
def observe_flock(fd, operation):
    pathlib.Path(control, name + '.waiting').touch()
    original_flock(fd, operation)
docker = [sys.executable, str(pathlib.Path(control, 'docker.py')), control, name, status]
with patch.object(native.os, 'geteuid', return_value=1000), \\
     patch.object(native.shutil, 'which', return_value='/usr/bin/bazel'), \\
     patch.object(native, 'command', return_value=docker), \\
     patch.object(native.fcntl, 'flock', side_effect=observe_flock):
    native.main()
'''
        docker = '''
import os, pathlib, sys, time
root, name, status = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
active = root / 'active'
fd = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.close(fd)
(root / (name + '.entered')).touch()
deadline = time.monotonic() + 15
while not (root / (name + '.release')).exists():
    if time.monotonic() > deadline:
        raise RuntimeError('Test did not release fake Docker')
    time.sleep(0.01)
active.unlink()
sys.exit(status)
'''
        for override in (None, '--output_base=', '--output_user_root='):
            with self.subTest(startup=override), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                cache = root / 'cache'
                cache.mkdir()
                alias = root / 'cache-alias'
                alias.symlink_to(cache, target_is_directory=True)
                (root / 'docker.py').write_text(docker)
                first_startup = [override + str(cache / 'output')] if override else []
                second_startup = [override + str(alias / 'output')] if override else []
                second_cache = root / 'different-cache' if override else alias
                processes = []

                def launch(name, selected_cache, startup, status):
                    process = subprocess.Popen([sys.executable, '-c', child, str(Path(native_build.__file__).resolve()),
                        str(root), str(root), name, str(selected_cache), json.dumps(startup), str(status)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    processes.append(process)
                    return process

                def wait_for(name, process):
                    deadline = time.monotonic() + 10
                    while not (root / name).exists():
                        if process.poll() is not None:
                            self.fail('Launcher did not reach ' + name + ': ' + str(process.communicate(timeout=1)))
                        if time.monotonic() > deadline:
                            self.fail('Timed out waiting for launcher to reach ' + name)
                        time.sleep(0.01)

                try:
                    first = launch('first', cache, first_startup, 7)
                    wait_for('first.entered', first)
                    second = launch('second', second_cache, second_startup, 0)
                    wait_for('second.waiting', second)
                    with self.assertRaises(subprocess.TimeoutExpired):
                        second.wait(timeout=0.2)
                    self.assertFalse((root / 'second.entered').exists())
                    (root / 'first.release').touch()
                    self.assertEqual(first.wait(timeout=5), 7)
                    wait_for('second.entered', second)
                    (root / 'second.release').touch()
                    self.assertEqual(second.wait(timeout=5), 0)
                finally:
                    # Unblock fake containers even if an assertion fails, so
                    # their child processes do not outlive the test fixture.
                    for name in ('first', 'second'):
                        (root / (name + '.release')).touch()
                    for process in processes:
                        try:
                            process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate(timeout=5)

    def test_worktree_common_git_metadata_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, cache, gitdir = [root / name for name in ('worktree', 'cache', 'common.git')]
            for path in (workspace, cache, gitdir):
                path.mkdir()
            bazel = root / 'bazel'
            bazel.touch()
            with patch.object(native_build.subprocess, 'check_output', return_value=str(gitdir) + '\n'):
                command = native_build.command(['info', 'release'], workspace, bazel, cache)
            self.assertIn('type=bind,src=' + str(gitdir) + ',dst=' + str(gitdir) + ',readonly', command)

    def test_external_reviewed_source_overrides_are_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, cache, prepared = [root / name for name in ('worktree', 'cache', 'reviewed source')]
            for path in (workspace, cache, prepared):
                path.mkdir()
            bazel = root / 'bazel'
            bazel.touch()
            (workspace / '.bazelrc.user').write_text("# generated source inputs\ncommon '--override_module=library=" + str(prepared) + "'\n")
            with patch.object(native_build.subprocess, 'check_output', return_value=str(workspace / '.git') + '\n'):
                command = native_build.command(['build', '//:image'], workspace, bazel, cache)
            self.assertIn('type=bind,src=' + str(prepared) + ',dst=' + str(prepared) + ',readonly', command)
            self.assertNotIn('--override_module=library=', command)

    def test_comma_in_a_bind_path_fails_before_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / 'comma,path'
            workspace.mkdir()
            with patch.object(native_build.subprocess, 'check_output', return_value=str(workspace / '.git') + '\n'):
                with self.assertRaisesRegex(ValueError, 'cannot contain commas'):
                    native_build.command(['build', '//:image'], workspace, workspace / 'bazel', workspace / 'cache')

    def test_build_and_test_audit_the_same_targets_and_configuration(self):
        for command in ('build', 'test'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                arguments = ['bazel', '--output_user_root=/private/cache', command,
                             '--config=syncd-dash', '//dockers/example:image',
                             '--override_module=component=/reviewed/source', ':debug']
                with patch.object(bazel_commands, 'audit_targets') as audit:
                    native_build.audit_command(arguments, workspace)
                self.assertEqual(audit.call_args.args, ('bazel',
                    ['--config=syncd-dash', '--override_module=component=/reviewed/source'],
                    ['//dockers/example:image', ':debug']))
                self.assertEqual(audit.call_args.kwargs['startup'], ['--output_user_root=/private/cache'])
                self.assertEqual(audit.call_args.kwargs['workspace'], workspace)

    def test_queries_do_not_execute_a_build_or_extra_audit(self):
        for command, argument in (('query', 'deps(//:image)'), ('aquery', 'deps(//:image)'),
                                  ('cquery', '//:image'), ('info', 'output_path'), ('mod', 'graph')):
            with self.subTest(command=command), patch.object(bazel_commands, 'audit_targets') as audit:
                native_build.audit_command(['bazel', command, argument], Path.cwd())
                audit.assert_not_called()

    def test_run_audits_only_bazel_flags_and_the_program_target(self):
        arguments = ['bazel', 'run', '--config=syncd-dash', '//dockers/example:export',
                     '--', '--destination', '/tmp/image.gz', '//:program-argument']
        with patch.object(bazel_commands, 'audit_targets') as audit:
            native_build.audit_command(arguments, Path.cwd())
        self.assertEqual(audit.call_args.args, ('bazel', ['--config=syncd-dash'], ['//dockers/example:export']))
        self.assertEqual(arguments[-4:], ['--', '--destination', '/tmp/image.gz', '//:program-argument'])

    def test_native_run_does_not_execute_when_the_graph_audit_fails(self):
        arguments = ['native_build.py', 'run', '//:export', '--', '--destination', '/tmp/image.gz']
        with patch.object(native_build.sys, 'argv', arguments), \
             patch.object(native_build.os, 'geteuid', return_value=0), \
             patch.dict(native_build.os.environ, {'APT_INSTALL_BUILD_CONTAINER': native_build.ENVIRONMENT}), \
             patch.object(native_build.shutil, 'which', return_value='/usr/bin/bazel'), \
             patch.object(native_build, 'audit_command', side_effect=ValueError('DEB action found')) as audit, \
             patch.object(native_build.os, 'execv') as execute:
            with self.assertRaisesRegex(ValueError, 'DEB action found'):
                native_build.main()
        self.assertEqual(audit.call_args.args[0][-5:], arguments[-5:])
        execute.assert_not_called()

    def test_ambiguous_value_or_implicit_target_fails_before_execution(self):
        for arguments in (['build'], ['build', 'image'], ['build', '--jobs', '2', '//:image']):
            with self.subTest(arguments=arguments), patch.object(bazel_commands, 'audit_targets') as audit:
                with self.assertRaisesRegex(ValueError, 'explicit Bazel target labels'):
                    native_build.audit_command(['bazel', *arguments], Path.cwd())
                audit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
