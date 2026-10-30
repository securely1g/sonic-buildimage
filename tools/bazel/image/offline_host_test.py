#!/usr/bin/env python3
"""Check extraction boundaries for host CLI plugins from local OCI packages."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('offline_host', Path(__file__).with_name('offline_host.py'))
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


class OfflineHostTest(unittest.TestCase):
    def package(self, *paths, name='dhcp-relay'):
        return SimpleNamespace(name=name, manifest={'cli': {'show': list(paths)}})

    def test_plugin_names_follow_the_package_manager_for_multiple_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'image'
            plugins = root / 'cli/show/plugins'
            plugins.mkdir(parents=True)
            (plugins / 'show_dhcp_relay.py').write_text('def cli(): pass\n')
            (plugins / 'extra.py').write_text('# extra plugin\n')
            target = Path(temp) / 'host'
            target.mkdir()
            naming = lambda package, index, command: target / (package.name + (f'_{index}' if index else '') + '.py')
            offline.copy_cli_plugins(self.package('/cli/show/plugins/show_dhcp_relay.py', '/cli/show/plugins/extra.py'), root, ['show'], naming)
            self.assertEqual((target / 'dhcp-relay.py').read_text(), 'def cli(): pass\n')
            self.assertEqual((target / 'dhcp-relay_1.py').read_text(), '# extra plugin\n')

    def test_plugin_cannot_escape_image_root_or_overwrite_through_host_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'image'
            root.mkdir()
            outside = Path(temp) / 'outside.py'
            outside.write_text('unchanged')
            (root / 'escape.py').symlink_to(outside)
            (root / 'safe.py').write_text('plugin')
            destination = Path(temp) / 'host.py'
            naming = lambda *_: destination
            for path in ('relative.py', '/../outside.py', '/escape.py'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    offline.copy_cli_plugins(self.package(path), root, ['show'], naming)
            destination.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'destination'):
                offline.copy_cli_plugins(self.package('/safe.py'), root, ['show'], naming)
            self.assertEqual(outside.read_text(), 'unchanged')


if __name__ == '__main__':
    unittest.main()
