"""Keep Syncd's shared-runner profile limited to its existing contract-test scope."""

from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import container

CONFIG_PATH = ROOT / "dockers/docker-syncd-vs/bazel/ci_config.py"


class ContractProfileTest(unittest.TestCase):
    """Check the common entrypoint preserves Syncd's targets and input provenance."""

    def test_cli_runs_the_owner_profile_and_hashes_shared_inputs(self):
        """The common CLI must retain all eight contracts without enabling image production."""
        with mock.patch.object(container, "run") as run:
            container.main(["--config", str(CONFIG_PATH), "--bazel", "selected-bazel",
                            "--bazel-arg=--jobs=2", "--artifacts", "artifacts/example"])
        config = run.call_args.args[0]
        self.assertEqual(set(config.tests), {
            "//tools/bazel/tests:syncd_manifest_labels_test",
            "//tools/bazel/tests:syncd_package_state_layer_test",
            "//tools/bazel/tests:syncd_select_apt_payloads_test",
            "//tools/bazel/tests:syncd_package_contract_test",
            "//tools/bazel/tests:syncd_validate_image_test",
            "//tools/bazel/tests:syncd_validate_native_packages_test",
            "//tools/bazel/tests:syncd_validate_payloads_test",
            "//tools/bazel/tests:apt_selection_test",
        })
        self.assertFalse(config.archives)
        self.assertIsNone(config.validate_archives)
        self.assertFalse(config.retain_test_events)
        self.assertIn("dockers/docker-syncd-vs/bazel/ci_config.py", config.source_files)
        self.assertIn("tools/bazel/ci/container.py", config.source_files)
        self.assertIn("dockers/docker-syncd-vs/bazel/apt.lock.json", config.source_files)
        self.assertIn("dockers/docker-syncd-vs/config/runtime_package_state.json", config.source_files)
        self.assertEqual(len(config.source_files), len(set(config.source_files)))
        self.assertEqual(run.call_args.kwargs["bazel"], "selected-bazel")
        self.assertEqual(run.call_args.kwargs["options"], [*container.OPTIONS, "--jobs=2"])


if __name__ == "__main__":
    unittest.main()
