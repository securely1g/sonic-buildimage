"""Keep the installed fixture faithful to the original tracked SONiC-VM data."""

import ast
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.bazel.ci import syncd_loaded as loaded


def fixture_function():
    # Exercise the real pure fixture helper without installing image-only Redis
    # and Supervisor modules into the host test environment.
    path = loaded.ROOT / "platform/vs/docker-syncd-vs/bazel/runtime_probe.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {"require", "prepare_hwsku"}]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["prepare_hwsku"]


class OriginalVsFixtureTest(unittest.TestCase):
    def setUp(self):
        self.original = loaded.ROOT / "device/virtual/x86_64-kvm_x86_64-r0/SONiC-VM"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.names = ("sai.profile", "lanemap.ini", "fabriclanemap.ini")
        for name in self.names:
            shutil.copyfile(self.original / name, self.source / name)
        self.prepare = fixture_function()

    def test_actual_tracked_fixture_preserves_default_map_and_all_bytes(self):
        self.assertEqual(loaded.profile_inputs(self.original), self.names)
        destination = self.root / "installed"
        profile = self.prepare(self.original, destination)
        self.assertEqual(profile, (self.original / "sai.profile").read_text())
        self.assertIn("SAI_VS_CORE_PORT_INDEX_MAP_FILE=/usr/share/sonic/hwsku/coreportindexmap.ini", profile)
        self.assertEqual(sorted(path.name for path in destination.iterdir()), sorted(self.names))
        for name in self.names:
            self.assertEqual((destination / name).read_bytes(), (self.original / name).read_bytes())

    def test_missing_or_symlinked_required_file_is_rejected(self):
        for name in self.names:
            for linked in (False, True):
                with self.subTest(name=name, linked=linked):
                    path = self.source / name
                    path.unlink()
                    if linked:
                        path.symlink_to(self.original / name)
                    with self.assertRaises(ValueError):
                        loaded.profile_inputs(self.source)
                    with self.assertRaises(AssertionError):
                        self.prepare(self.source, self.root / "installed")
                    if linked:
                        path.unlink()
                    shutil.copyfile(self.original / name, path)

    def test_added_optional_file_or_broken_link_is_rejected(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                path = self.source / "coreportindexmap.ini"
                if linked:
                    path.symlink_to("missing")
                else:
                    path.write_text("invented map")
                with self.assertRaises(ValueError):
                    loaded.profile_inputs(self.source)
                with self.assertRaises(AssertionError):
                    self.prepare(self.source, self.root / "installed")
                path.unlink()

    def test_preexisting_installed_map_is_rejected(self):
        destination = self.root / "installed"
        destination.mkdir()
        (destination / "coreportindexmap.ini").write_text("unexpected map")
        with self.assertRaises(AssertionError):
            self.prepare(self.original, destination)


if __name__ == "__main__":
    unittest.main()
