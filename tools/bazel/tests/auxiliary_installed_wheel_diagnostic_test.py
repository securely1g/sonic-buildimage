"""Focused boundary tests for the auxiliary capture; no product installs or commands."""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(os.environ.get("AUXILIARY_INSTALL_SOURCE",
                             str(Path(__file__).with_name("auxiliary_install.py"))))
SPEC = importlib.util.spec_from_file_location("auxiliary_capture_under_test", SOURCE)
AUX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUX)


class AuxiliaryInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def file(self, name, data=b"data", mode=0o600):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def identity(self, path):
        raw = path.read_bytes()
        return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    def installer(self):
        root = self.root / "site-packages"
        contents = {
            "installer/__init__.py": b"# original package\n",
            "installer/destinations.py": b"# destination\n",
            "installer/sources.py": b"# sources\n",
            "installer/_core.py": b"# core\n",
            "installer/py.typed": b"",
            "installer-0.7.0.dist-info/METADATA": b"Name: installer\nVersion: 0.7.0\n\n",
            "installer-0.7.0.dist-info/RECORD": b"original metadata\n",
        }
        files = []
        for name, raw in contents.items():
            path = self.file("site-packages/" + name, raw)
            files.append(dict(relative_name=name, bytes=len(raw), sha256=AUX.sha(raw)))
        return dict(root=str(root), version="0.7.0", files=files)

    def fixture(self, *, missing=False, different_python=False):
        test = self
        roots, calls = [], []
        @contextmanager
        def installed_wheels(archives, *, exclude_source_roots):
            calls.append((list(archives), list(exclude_source_roots)))
            with tempfile.TemporaryDirectory(dir=test.root) as temporary:
                root = Path(temporary)
                roots.append(root)
                environment = root / "environment"
                scripts = environment / "bin"
                scripts.mkdir(parents=True)
                python = scripts / "python"
                target = (test.file("different-python", b"not the selected interpreter")
                          if different_python else Path(sys.executable).resolve())
                python.symlink_to(target)
                for _, _, name in AUX.GROUPS:
                    if not missing:
                        path = scripts / name
                        path.write_bytes(b"#!/a/private/auxiliary/python\nwhole original body\n")
                        path.chmod(0o755)
                yield SimpleNamespace(root=root, environment=environment,
                                      scripts=scripts, python=python)
        return SimpleNamespace(installed_wheels=installed_wheels), roots, calls

    def capture(self, fixture):
        output = self.root / "output"
        output.mkdir()
        wheels = {role: {"resolved_path": str(self.file(role + ".whl"))}
                  for role in ("yang", "common", "config", "supervisor")}
        records = AUX.capture_installations(fixture, wheels, [self.root], output)
        return output, records

    def test_capture_occurs_inside_live_context_and_keeps_original_bytes(self):
        fixture, roots, calls = self.fixture()
        with mock.patch("subprocess.run", side_effect=AssertionError("product command invoked")), \
             mock.patch("subprocess.check_output", side_effect=AssertionError("product command invoked")):
            output, records = self.capture(fixture)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(not path.exists() for path in roots))
        for row in records:
            raw = (output / row["copied_relative_name"]).read_bytes()
            self.assertEqual(raw, b"#!/a/private/auxiliary/python\nwhole original body\n")
            header = (output / row["first_line_relative_name"]).read_bytes()
            self.assertEqual(header, raw.splitlines(keepends=True)[0])
            self.assertEqual(row["first_line_sha256"], AUX.sha(header))
            self.assertTrue(row["fixture_context_was_live"])
            self.assertFalse(row["original_failed_execution_script"])
            self.assertFalse(row["script_executed_by_this_diagnostic"])
        self.assertEqual([[p.name for p in call[0]] for call in calls],
                         [["yang.whl", "common.whl", "config.whl"],
                          ["common.whl", "supervisor.whl"], ["yang.whl"]])

    def test_public_summary_has_no_private_paths_or_original_acceptance_claim(self):
        fixture, _, _ = self.fixture()
        _, records = self.capture(fixture)
        result = AUX.public_summary(records)
        encoded = json.dumps(result)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("/a/private", encoded)
        self.assertNotIn("venv_python_symlink_text", encoded)
        self.assertFalse(result["original_test_evidence"])
        self.assertFalse(result["image_acceptance"])
        self.assertFalse(result["product_scripts_executed"])
        self.assertEqual(result["kind"], "auxiliary-installed-wheel-reproduction")

    def test_missing_script_is_a_failure_with_fixture_cleanup(self):
        fixture, roots, _ = self.fixture(missing=True)
        with self.assertRaises(FileNotFoundError):
            self.capture(fixture)
        self.assertTrue(all(not path.exists() for path in roots))
        self.assertFalse((self.root / "output/summary.json").exists())

    def test_different_venv_interpreter_is_rejected(self):
        fixture, roots, _ = self.fixture(different_python=True)
        with self.assertRaisesRegex(ValueError, "different interpreter"):
            self.capture(fixture)
        self.assertTrue(all(not path.exists() for path in roots))

    def test_symlink_nonexecutable_empty_and_oversized_scripts_rejected(self):
        script = self.file("script", b"#!/python\n", 0o755)
        link = self.root / "link"
        link.symlink_to(script)
        with self.assertRaises(OSError):
            AUX.snapshot(link, 1024, executable=True)
        for data, mode, limit in [(b"abc", 0o600, 1024),
                                  (b"", 0o755, 1024), (b"12345", 0o755, 4)]:
            script.write_bytes(data)
            script.chmod(mode)
            with self.assertRaises(ValueError):
                AUX.snapshot(script, limit, executable=True)

    def test_regular_file_changed_during_read_is_rejected(self):
        script = self.file("script", b"original", 0o755)
        original_read = AUX.os.read
        changed = False
        def read(fd, size):
            nonlocal changed
            raw = original_read(fd, size)
            if not changed:
                changed = True
                script.write_bytes(b"replacement bytes")
            return raw
        with mock.patch.object(AUX.os, "read", side_effect=read):
            with self.assertRaisesRegex(ValueError, "changed during capture"):
                AUX.snapshot(script, 1024, executable=True)

    def test_installer_manifest_accepts_sealed_empty_data_file(self):
        config = self.installer()
        _, inventory, identities = AUX.installer_inventory(config)
        self.assertEqual(inventory["installer/py.typed"][0], b"")
        self.assertEqual(len(identities), 7)

    def test_installer_manifest_rejects_extra_code_escape_and_wrong_version(self):
        config = self.installer()
        extra = self.file("site-packages/installer/unsealed.py", b"raise RuntimeError\n")
        with self.assertRaisesRegex(ValueError, "unsealed"):
            AUX.installer_inventory(config)
        extra.unlink()
        outside = self.file("outside.py", b"outside")
        extra.symlink_to(outside)
        with self.assertRaises(ValueError):
            AUX.installer_inventory(config)
        extra.unlink()
        bad = dict(config, version="0.8.0")
        with self.assertRaisesRegex(ValueError, "version"):
            AUX.installer_inventory(bad)
        changed = dict(config, files=[dict(config["files"][0], relative_name="../escape"),
                                      *config["files"][1:]])
        with self.assertRaisesRegex(ValueError, "outside fixed package"):
            AUX.installer_inventory(changed)

    def test_sealed_input_rejects_changed_bytes_and_duplicate_json(self):
        path = self.file("input", b"sealed")
        expected = self.identity(path)
        path.write_bytes(b"altered")
        with self.assertRaisesRegex(ValueError, "identity differs"):
            AUX.checked(expected)
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.assertRaises(ValueError):
                AUX.strict_json(raw)

    def test_existing_destination_cannot_be_overwritten(self):
        destination = self.file("original", b"kept")
        with self.assertRaises(FileExistsError):
            AUX.write_original(destination, b"changed")
        self.assertEqual(destination.read_bytes(), b"kept")


if __name__ == "__main__":
    unittest.main()

