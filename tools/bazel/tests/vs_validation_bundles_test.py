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
    def test_private_owner_fixture_uses_its_exact_module_mapping(self):
        """A private target is selected directly and matched to its owner's canonical output."""
        label = "@sonic_sysmgr//tests:reboot_dbus_mock"
        canonical = "@@sonic-sysmgr+//tests:reboot_dbus_mock"
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mock").touch()
            events = root / "events"
            events.write_text(json.dumps({"workspaceInfo": {"localExecRoot": str(root)}}))
            with patch.object(bundles.subprocess, "check_output", side_effect=[
                json.dumps({"sonic_sysmgr": "sonic-sysmgr+"}), canonical + "\tmock\n",
            ]) as bazel:
                self.assertEqual(bundles.configured_outputs("bazel", [], [label], events), {label: root / "mock"})
                self.assertIn("set(" + label + ")", bazel.call_args.args[0])

    def test_cross_repository_alias_binds_its_single_requested_output(self):
        """Bazel may print a rule from an entirely different repository for an owner alias."""
        alias = "@lldpd//:runtime"
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "lldpd.tar").touch()
            events = root / "events"
            events.write_text(json.dumps({"workspaceInfo": {"localExecRoot": str(root)}}))
            with patch.object(bundles.subprocess, "check_output", side_effect=[
                '{"lldpd":"lldpd+"}', "@@lldpd++_repo_rules+lldpd_source//:runtime\tlldpd.tar\n" * 2, "lldpd.tar\n",
            ]) as bazel:
                self.assertEqual(bundles.configured_outputs("bazel", [], [alias], events), {alias: root / "lldpd.tar"})
                self.assertEqual(bazel.call_args.args[0], ["bazel", "cquery", alias, "--output=files"])

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

    def test_validation_only_companion_follows_debug_without_entering_the_installer(self):
        """DASH engine's debug archive is an auxiliary output only in a debug VS run."""
        installer = {"docker-dash-engine.gz": "//platform/vs/docker-dash-engine:docker-dash-engine.gz"}
        debug = "docker-dash-engine-dbg.gz"
        with patch("tools.bazel.ci.vs_probe_policy.preparation_targets", return_value={}):
            runtime = bundles.validation_targets(installer, set(), False, [debug])["docker-dash-engine"]
            paired = bundles.validation_targets(installer, set(), True, [debug])["docker-dash-engine"]
            self.assertTrue(runtime["supports_debug"])
            self.assertEqual(set(runtime["members"]), {"runtime"})
            self.assertEqual(set(paired["members"]), {"runtime", "debug"})
            self.assertEqual(paired["members"]["debug"][1], "//platform/vs/docker-dash-engine:" + debug)
            self.assertNotIn(debug, installer)

    def test_matrix_comes_from_selected_targets_even_if_a_directory_is_lost(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "selection.json").write_text(json.dumps({
                "installer_archives": ["docker-unit.gz"], "registered_debug": [], "validation_only_debug": [], "debug_enabled": False,
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
