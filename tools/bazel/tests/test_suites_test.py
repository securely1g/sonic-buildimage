"""Keep Docker checks package-free without losing explicit packaging coverage."""

import ast
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import test_suites


def probe(module, name, calls):
    cls = type(name, (unittest.TestCase,), {
        "__module__": module,
        "test_probe": lambda self: calls.append((module, name)),
    })
    return cls("test_probe")


class TestSuitesTest(unittest.TestCase):
    def inventory(self, calls):
        return unittest.TestSuite([
            probe("kernel_test", "KernelBundleTest", calls),
            unittest.TestSuite([
                probe("kernel_sbom_test", "KernelSbomTest", calls),
                probe("tools.bazel.tests.kernel_artifacts_test", "KernelArtifactsTest", calls),
            ]),
            probe("kernel_test", "KernelConfigurationTest", calls),
            probe("kernel_test", "KernelWorkflowTest", calls),
            probe("setup_bazel_cache_test", "SetupBazelCacheTest", calls),
        ])

    def test_runtime_excludes_only_package_producers_before_setup(self):
        calls = []
        suite = test_suites.select_unit_tests(self.inventory(calls), "tar-runtime")
        self.assertEqual(suite.countTestCases(), 3)
        self.assertTrue(suite.run(unittest.TestResult()).wasSuccessful())
        self.assertEqual([name for _, name in calls], [
            "KernelConfigurationTest", "KernelWorkflowTest", "SetupBazelCacheTest",
        ])

    def test_explicit_fixtures_retain_the_complete_suite(self):
        calls = []
        suite = test_suites.select_unit_tests(self.inventory(calls), "deb-fixtures")
        self.assertEqual(suite.countTestCases(), 6)
        self.assertTrue(suite.run(unittest.TestResult()).wasSuccessful())
        self.assertEqual(len(calls), 6)

    def test_import_errors_cannot_be_hidden_by_suite_selection(self):
        discovered = unittest.TestLoader().loadTestsFromName("no_such_sonic_test_module")
        selected = test_suites.select_unit_tests(discovered, "tar-runtime")
        result = selected.run(unittest.TestResult())
        self.assertEqual(len(result.errors), 1)

    def test_package_class_inventory_names_real_tests(self):
        for module, name in test_suites.DEB_FIXTURE_CLASSES:
            tree = ast.parse((ROOT / "tools/bazel/tests" / (module + ".py")).read_text())
            classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
            self.assertIn(name, classes)

    def test_invalid_environment_suite_fails_before_discovery(self):
        with patch.dict(os.environ, SONIC_BAZEL_CI_SUITE="typo"), \
                patch.object(unittest.TestLoader, "discover") as discover, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            test_suites.main(["unit"])
        self.assertEqual(error.exception.code, 2)
        discover.assert_not_called()

    def test_default_is_tar_runtime(self):
        calls = []
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(unittest.TestLoader, "discover", return_value=self.inventory(calls)):
            self.assertEqual(test_suites.main(["unit"]), 0)
        self.assertEqual(len(calls), 3)

    def test_image_architecture_and_fixture_coverage(self):
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture):
                runtime = test_suites.image_targets("tar-runtime", architecture)
                fixtures = test_suites.image_targets("deb-fixtures", architecture)
                self.assertNotIn(test_suites.KERNEL_DEB_TEST, runtime)
                self.assertEqual(set(fixtures) - set(runtime), {test_suites.KERNEL_DEB_TEST})
                self.assertEqual(test_suites.DISK_TEST in runtime, architecture == "amd64")
                self.assertEqual(len(runtime), len(set(runtime)))
        with self.assertRaises(ValueError):
            test_suites.image_targets("tar-runtime", "unknown")

    def test_workflows_select_tar_runtime_on_automatic_events(self):
        for filename in ("bazel-oci.yml", "bazel-image-rules.yml"):
            document = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
            events = document.get("on", document.get(True))
            choices = events["workflow_dispatch"]["inputs"]["test_suite"]
            self.assertEqual(choices["default"], "tar-runtime")
            self.assertEqual(choices["options"], list(test_suites.SUITES))
            env = document.get("env", document["jobs"].get("contracts", {}).get("env"))
            self.assertEqual(env["SONIC_BAZEL_CI_SUITE"],
                             "${{ github.event_name == 'workflow_dispatch' && inputs.test_suite || 'tar-runtime' }}")
        document = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        for job in ("archive", "checks"):
            steps = document["jobs"][job]["steps"]
            commands = "\n".join(step.get("run", "") for step in steps)
            self.assertEqual(commands.count("tools/bazel/ci/test_suites.py unit"), 1)
            self.assertNotIn("unittest discover -s tools/bazel/tests", commands)

    def test_kernel_production_remains_a_complete_explicit_workflow(self):
        document = yaml.safe_load((ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text())
        events = document.get("on", document.get(True))
        self.assertEqual(set(events), {"workflow_dispatch"})
        job = document["jobs"]["kernel-bundle"]
        self.assertEqual(job["if"], "github.event_name == 'workflow_dispatch'")
        self.assertEqual({item["arch"] for item in job["strategy"]["matrix"]["include"]},
                         {"amd64", "arm64"})
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        for file in ("kernel_test.py", "kernel_sbom_test.py", "kernel_artifacts_test.py"):
            self.assertIn("tools/bazel/tests/" + file, commands)
        self.assertIn("tools/bazel/kernel/prepare.py ensure", commands)
        self.assertIn("Verify a fresh output directory reuses the kernel action",
                      [step.get("name") for step in job["steps"]])

    def test_image_evidence_matches_selected_targets_on_both_architectures(self):
        document = yaml.safe_load((ROOT / ".github/workflows/bazel-image-rules.yml").read_text())
        steps = document["jobs"]["contracts"]["steps"]
        action = next(step["run"] for step in steps if step.get("name") == "Run public Bazel rule actions")
        self.assertIn('bazel test "${image_targets[@]}"', action)
        # Exercise the actual evidence-copy loop with harmless text receipts.
        start = action.index('for target in "${image_targets[@]}"; do')
        end = action.index('test -s artifacts/image-rule-actions/MODULE.bazel.lock', start)
        for suite in test_suites.SUITES:
            for architecture in ("amd64", "arm64"):
                with self.subTest(suite=suite, architecture=architecture), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    targets = test_suites.image_targets(suite, architecture)
                    for target in targets:
                        log = root / "bazel-testlogs" / target[2:].replace(":", "/")
                        log.mkdir(parents=True)
                        for name in ("test.log", "test.xml"):
                            (log / name).write_text(target)
                    output = root / "bazel-testlogs/tools/bazel/image/action_test/action_test/test.outputs"
                    output.mkdir()
                    (output / "action-receipt.json").write_text("{}")
                    command = "image_targets=(" + " ".join(targets) + ")\n" + action[start:end]
                    subprocess.run(["bash", "-euc", command], cwd=root, check=True, capture_output=True)
                    logs = list((root / "artifacts/image-rule-actions").glob("*/test.log"))
                    self.assertEqual({p.read_text() for p in logs}, set(targets))
                    self.assertEqual(len(logs), len(targets))


if __name__ == "__main__":
    unittest.main()
