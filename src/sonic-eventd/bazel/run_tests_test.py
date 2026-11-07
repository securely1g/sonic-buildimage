"""Keep the Redis compatibility exception confined to emulated ARM fixtures."""
import unittest
from unittest.mock import mock_open, patch

import run_tests


class RedisEmulationTest(unittest.TestCase):
    def test_native_and_unrecognized_memory_maps_keep_the_kernel_check(self):
        for machine, descriptor, expected in [
            ('aarch64', '/proc/123/smaps', False),
            ('x86_64', '/proc/123/smaps', False),
            ('x86_64', '/memfd:qemu-open (deleted)', False),
            ('aarch64', '/memfd:unrelated (deleted)', False),
            ('aarch64', '/memfd:qemu-open (deleted)', True),
        ]:
            with self.subTest(machine=machine, descriptor=descriptor):
                with patch.object(run_tests.platform, 'machine', return_value=machine), \
                        patch('builtins.open', mock_open()), \
                        patch.object(run_tests.os, 'readlink', return_value=descriptor):
                    self.assertEqual(run_tests.qemu_arm64_smaps(), expected)

    def test_unavailable_proc_information_keeps_the_kernel_check(self):
        with patch.object(run_tests.platform, 'machine', return_value='aarch64'), \
                patch('builtins.open', side_effect=PermissionError):
            self.assertFalse(run_tests.qemu_arm64_smaps())


if __name__ == '__main__':
    unittest.main()
