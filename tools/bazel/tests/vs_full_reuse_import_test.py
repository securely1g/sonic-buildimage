"""The maintained VS caller imports without running a process or cache probe."""
import importlib
import sys
import unittest
from unittest import mock


class FullVsImportClosureTest(unittest.TestCase):
    def test_real_composed_modules_import_without_execution(self):
        names = ("vs_reuse_plan", "vs_reuse_stages", "vs_worker_inspection",
                 "vs_runtime_check", "vs_reuse_stage")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("No child at import")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("No network at import")):
            modules = {name: importlib.import_module("tools.bazel.ci." + name) for name in names}
        self.assertIs(modules["vs_runtime_check"].inspection, modules["vs_worker_inspection"])
        self.assertIs(modules["vs_reuse_stage"].policy, modules["vs_reuse_plan"])
        self.assertTrue(callable(modules["vs_reuse_stages"].full_vs_workload))


if __name__ == "__main__":
    unittest.main()
