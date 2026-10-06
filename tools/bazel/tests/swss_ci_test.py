"""Keep SWSS source policy in the caller of shared Bazel CI helpers."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "dockers/docker-orchagent/bazel"))

import ci


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
                self.assertEqual(ci.source_directory("bazel", [], output_base, {}), source)
                self.assertEqual(lookup.call_args.args[3], "@sonic_swss//dist:swss_pkg")
                (source / "dist/BUILD.bazel").unlink()
                with self.assertRaisesRegex(ValueError, "SWSS source lacks its install declarations"):
                    ci.source_directory("bazel", [], output_base, {})


if __name__ == "__main__":
    unittest.main()
