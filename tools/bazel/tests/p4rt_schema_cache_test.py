#!/usr/bin/env python3
"""Check that P4RT TARs retain the declared schema and independent cache contract."""

import unittest

from tools.bazel.tests import p4rt_tar_test


class P4rtSchemaCacheTest(unittest.TestCase):
    setUp = p4rt_tar_test.P4rtMakeTarTest.setUp
    make = p4rt_tar_test.P4rtMakeTarTest.make
    recorded = p4rt_tar_test.P4rtMakeTarTest.recorded

    def test_schema_and_caches_are_explicit_and_noop_preserves_outputs(self):
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.schema_repo / 'cfg_schema.h').read_bytes(), self.schema.read_bytes())
        for operation in ('aquery', 'build', 'test'):
            for args in self.recorded(operation):
                self.assertIn('--override_repository=swss_cfg_schema=' + str(self.schema_repo), args)
                self.assertIn('--repository_cache=' + str(self.cache / 'repository_cache'), args)
                self.assertIn('--disk_cache=' + str(self.cache / 'disk_cache'), args)
                self.assertFalse(any(arg.startswith('--output_user_root') for arg in args))
        before = [path.stat().st_mtime_ns for path in (self.runtime, self.debug)]
        call_count = len(self.calls.read_text().splitlines())
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, [path.stat().st_mtime_ns for path in (self.runtime, self.debug)])
        self.assertEqual(call_count, len(self.calls.read_text().splitlines()))

    def test_missing_schema_stops_before_bazel(self):
        self.schema.unlink()
        result = self.make()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not found: is libswsscommon-dev installed?', result.stderr)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
