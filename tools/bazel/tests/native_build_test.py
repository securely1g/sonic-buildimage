"""Check the real Make launcher preserves arguments and accessible cache paths."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.oci import native_build


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


if __name__ == '__main__':
    unittest.main()
