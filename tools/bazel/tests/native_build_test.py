"""Check the real Make launcher preserves arguments and accessible cache paths."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.oci import native_build
from tools.bazel.ci import bazel_commands


class NativeBuildTest(unittest.TestCase):
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
        with patch.object(bazel_commands, 'audit_targets') as audit:
            native_build.audit_command(['bazel', 'aquery', 'deps(//:image)'], Path.cwd())
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
