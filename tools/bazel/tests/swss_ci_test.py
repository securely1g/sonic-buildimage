"""Keep SWSS package policy in its configuration for the shared CI runner."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import container

ci = container.load_module(ROOT / "dockers/docker-orchagent/bazel/ci_config.py")


class SourceDirectoryTest(unittest.TestCase):
    """Apply SWSS source policy after the shared helper resolves a directory."""

    def test_resolved_swss_source_must_supply_its_install_declarations(self):
        """Use the SWSS package target and reject a source tree lacking install declarations."""
        with tempfile.TemporaryDirectory() as temporary:
            output_base = Path(temporary)
            source = output_base / "external/sonic-swss+"
            (source / "dist").mkdir(parents=True)
            (source / "dist/BUILD.bazel").write_text("# Source install declarations\n")

            with mock.patch.object(ci.build, "source_directory", return_value=source) as lookup:
                self.assertEqual(ci.source_directory(ROOT, "bazel", [], output_base, {}), source)
                self.assertEqual(lookup.call_args.args[0], ROOT)
                self.assertEqual(lookup.call_args.args[3], "@sonic_swss//dist:swss_pkg")
                (source / "dist/BUILD.bazel").unlink()
                with self.assertRaisesRegex(ValueError, "SWSS source lacks its install declarations"):
                    ci.source_directory(ROOT, "bazel", [], output_base, {})

    def test_archive_hook_validates_the_resolved_owner_source(self):
        """The shared runner's callback must still enforce SWSS's source/package contract."""
        source = ROOT / "sample-source"
        paths = {"swss.tar": ROOT / "sample-swss.tar"}
        artifacts = ROOT / "sample-artifacts"
        receipt = {}
        with mock.patch.object(ci, "source_directory", return_value=source) as locate, \
             mock.patch.object(ci, "verify_packages", return_value={"programs": ["usr/bin/orchagent"]}) as verify:
            result = ci.CONFIG.validate_archives(ROOT, paths, artifacts, receipt,
                                                bazel="chosen-bazel", options=["--jobs=2"])
        locate.assert_called_once_with(ROOT, "chosen-bazel", ["--jobs=2"], artifacts, receipt)
        verify.assert_called_once_with(paths, source)
        self.assertEqual(result["programs"], ["usr/bin/orchagent"])


if __name__ == "__main__":
    unittest.main()
