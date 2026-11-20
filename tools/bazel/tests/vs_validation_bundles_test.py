"""Exercise VS bundle selection at the producer/consumer boundary."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import vs_validation_bundles as bundles


class BundleSelectionTest(unittest.TestCase):
    def test_registered_companion_only_requested_when_debug_is_enabled(self):
        runtime = {"docker-unit.gz": "//unit:docker-unit.gz"}
        debug = {"docker-unit-dbg.gz": "//unit:docker-unit-dbg.gz"}
        registered = set(debug)
        with patch("tools.bazel.ci.vs_probe_policy.preparation_targets", return_value={}) as inputs:
            families = bundles.validation_targets(runtime, registered, False)
            self.assertEqual(set(families), {"docker-unit", *bundles.PARENTS})
            self.assertEqual(set(families["docker-unit"]["members"]), {"runtime"})
            self.assertTrue(families["docker-unit"]["supports_debug"])
            self.assertTrue(all(not call.args[1] for call in inputs.call_args_list))
            self.assertNotIn("//unit:docker-unit-dbg.gz", bundles.extra_targets(families))
            with self.assertRaisesRegex(ValueError, "batch differs from debug selection"):
                bundles.validation_targets(runtime, registered, True)
            paired = bundles.validation_targets({**runtime, **debug}, registered, True)
            self.assertEqual(set(paired["docker-unit"]["members"]), {"runtime", "debug"})
            for parent in bundles.PARENTS:
                self.assertEqual(set(paired[parent]["members"]), {"runtime"})

    def test_matrix_comes_from_selected_targets_even_if_a_directory_is_lost(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "selection.json").write_text(json.dumps({
                "installer_archives": ["docker-unit.gz"], "registered_debug": [], "debug_enabled": False,
            }))
            for image in ("docker-unit", *bundles.PARENTS):
                (directory / image).mkdir()
                (directory / image / "bundle.json").write_text(json.dumps({"archives": {"runtime": {}}}))
            self.assertEqual([entry["image"] for entry in bundles.matrix(directory, "n")["include"]],
                             sorted(["docker-unit", *bundles.PARENTS]))
            (directory / "docker-unit").rename(directory / "unexpected")
            with self.assertRaisesRegex(ValueError, "differs from the VS build"):
                bundles.matrix(directory, "n")


if __name__ == "__main__":
    unittest.main()
