"""Keep the P4RT SDK handoff bound to the Common selected by the image."""

import ast
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import p4rt_image as image


class CommonSelectionTest(unittest.TestCase):
    def test_sdk_identity_matches_both_root_common_selections(self):
        selections = []
        for node in ast.walk(ast.parse((image.ROOT / 'MODULE.bazel').read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ('bazel_dep', 'single_version_override'):
                continue
            values = {item.arg: ast.literal_eval(item.value) for item in node.keywords}
            if values.get('name', values.get('module_name')) == 'sonic-swss-common':
                selections.append(values['version'])
        self.assertEqual(selections, [image.COMMON_VERSION, image.COMMON_VERSION])
        self.assertEqual(image.COMMON_VERSION.split('-', 1)[1], image.COMMON_REVISION)

    def prepare_until_sdk(self, version):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            directory = Path(folder)
            preparation = directory / 'preparation'
            args = SimpleNamespace(owner_source=directory / 'owner', owner_revision='a' * 40,
                                   bazel='unused-bazel')
            stack.enter_context(mock.patch.object(image, 'native'))
            stack.enter_context(mock.patch.object(image, 'locations', return_value=(
                directory, preparation, directory / 'sdk', directory / 'output')))
            stack.enter_context(mock.patch.object(image, 'source_identity', return_value='b' * 40))
            stack.enter_context(mock.patch.object(image.p4rt_handoff, 'source_identity'))
            stack.enter_context(mock.patch.object(image.command_log, 'execute', return_value='bazel 8.5.1'))
            stack.enter_context(mock.patch.object(image, 'root_options', return_value=[]))
            stack.enter_context(mock.patch.object(image, 'prepare_manifests'))
            stack.enter_context(mock.patch.object(image.bazel_commands, 'audit_targets', return_value={}))

            def collect(*args, **kwargs):
                (preparation / 'common.MODULE.bazel').write_text(
                    'module(name="sonic-swss-common", version="' + version + '")\n')

            stack.enter_context(mock.patch.object(image.build, 'collect_archives', side_effect=collect))
            sdk = stack.enter_context(mock.patch.object(image, 'prepare_repository',
                                                       side_effect=RuntimeError('SDK gate reached')))
            owner = stack.enter_context(mock.patch.object(image, 'run_owner'))
            try:
                image.prepare_owner(args)
            except (RuntimeError, ValueError) as error:
                result = str(error)
            else:
                self.fail('Preparation unexpectedly executed past the SDK test boundary')
            owner.assert_not_called()
            return result, sdk.call_args

    def test_selected_module_reaches_sdk_with_exact_source_identity(self):
        error, sdk_call = self.prepare_until_sdk(image.COMMON_VERSION)
        self.assertEqual(error, 'SDK gate reached')
        self.assertEqual(sdk_call.args[3]['common_source_commit'], image.COMMON_REVISION)
        self.assertEqual(sdk_call.args[3]['image_source_revision'], 'b' * 40)

    def test_old_or_mismatched_module_is_rejected_before_owner_build(self):
        for version in ('0.0.8-43598143ef4a46431eae0859915cd7d951dad1ed',
                        '0.0.9-' + '0' * 40, image.COMMON_VERSION + '-unexpected'):
            with self.subTest(version=version):
                error, sdk_call = self.prepare_until_sdk(version)
                self.assertIn('Common owner differs', error)
                self.assertIsNone(sdk_call)


if __name__ == '__main__':
    unittest.main()
