"""Reject account corruption hidden by the runtime image's added SSH group."""

import ast
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.bazel.ci.syncd_image import ROOT, manifest_command, verify_environment, verify_inherited_accounts, verify_parent_files


class SyncdPreparationTest(unittest.TestCase):
    def test_retained_apt_payload_is_the_actual_installed_layer(self):
        from tools.bazel.ci import syncd_image
        owner = ROOT / "platform/vs/docker-syncd-vs/BUILD.bazel"
        declarations = ast.parse(owner.read_text())
        runtime = next(node.value for node in declarations.body
                       if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                       and isinstance(node.value.func, ast.Name) and node.value.func.id == "oci_image"
                       and any(key.arg == "name" and ast.literal_eval(key.value) == syncd_image.IMAGES[0]
                               for key in node.value.keywords))
        layers = ast.literal_eval(next(key.value for key in runtime.keywords if key.arg == "tars"))
        exported = syncd_image.TARGETS["runtime-apt.tar"]
        self.assertTrue(exported.startswith(syncd_image.OWNER))
        self.assertIn(":" + exported.removeprefix(syncd_image.OWNER), layers)
        # Preserve the existing full-image test's declared input contract too.
        tests = ast.parse((ROOT / "tools/bazel/tests/BUILD.bazel").read_text())
        mapping = next(node.value for node in tests.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "_SYNCD_P4_IMAGE_INPUTS"
                               for target in node.targets))
        apt = next(value for key, value in zip(mapping.keys, mapping.values)
                   if ast.literal_eval(key) == "apt_layer")
        self.assertIsInstance(apt, ast.BinOp)
        self.assertEqual(apt.left.id, "_SYNCD_OWNER")
        self.assertEqual(exported, syncd_image.OWNER.rstrip(":") + ast.literal_eval(apt.right))

    def test_actual_metadata_preparation_from_empty_target_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            subprocess.run([*manifest_command(), 'TARGET_PATH=' + str(target)], cwd=ROOT, check=True)
            paths = sorted(target.glob('bazel-manifests/*/manifest.json'))
            self.assertEqual([path.parent.name for path in paths], [
                'docker-base-trixie', 'docker-config-engine-trixie',
                'docker-syncd-vs', 'docker-syncd-vs-dbg'])
            for path in paths:
                self.assertIsInstance(json.loads(path.read_text()), dict)
            self.assertEqual(list(target.rglob('image.env')), [])

    def test_environment_preserves_every_parent_value_and_only_declared_override(self):
        parent = ['PATH=/usr/bin', 'IMAGE_VERSION=parent', 'DEBIAN_FRONTEND=dialog']
        installed = ['PATH=/usr/bin', 'IMAGE_VERSION=parent', 'DEBIAN_FRONTEND=noninteractive']
        verify_environment(parent, installed)
        for values in (installed[1:], installed + ['NEW=unexpected'],
                       ['PATH=/bin', *installed[1:]], installed + ['PATH=/usr/bin']):
            with self.subTest(values=values), self.assertRaises(ValueError):
                verify_environment(parent, values)


class SyncdAccountsTest(unittest.TestCase):
    def setUp(self):
        self.rows = ["root:x:0:", "adm:x:4:syslog", "redis:x:999:"]
        self.parent = {
            "etc/group": {"kind": "file", "uid": 0, "gid": 0, "mode": 0o644, "sha256": "parent-groups"},
            "etc/passwd": {"kind": "file", "uid": 0, "gid": 0, "mode": 0o644, "sha256": "passwords"},
            "etc/gshadow": {"kind": "file", "uid": 0, "gid": 42, "mode": 0o640, "sha256": "shadow"},
        }
        self.debug = copy.deepcopy(self.parent)
        self.debug["etc/group"]["sha256"] = "groups-with-ssh"

    def verify(self, rows, installed=None, parent_rows=None, add_ssh=True):
        verify_inherited_accounts(self.parent, self.debug if installed is None else installed,
                                  self.rows if parent_rows is None else parent_rows, rows, add_ssh=add_ssh)

    def test_runtime_and_single_debug_addition_preserve_accounts(self):
        self.verify(self.rows, installed=self.parent, add_ssh=False)
        self.verify(self.rows + ["_ssh:x:104:"])
        self.verify(self.rows + ["_ssh:x:104:"], parent_rows=self.rows + ["_ssh:x:104:"])

    def test_deleted_changed_reordered_or_extra_inherited_rows_fail(self):
        for rows in (self.rows[1:], ["root:x:1:", *self.rows[1:]], self.rows[::-1], self.rows + ["extra:x:42:"]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "inherited group rows"):
                self.verify(rows + ["_ssh:x:104:"])

    def test_duplicate_group_names_in_parent_or_debug_fail(self):
        for rows, parent in ((self.rows + ["redis:x:998:", "_ssh:x:104:"], self.rows),
                             (self.rows + ["_ssh:x:104:"], self.rows + ["redis:x:998:"])):
            with self.subTest(rows=rows, parent=parent), self.assertRaisesRegex(ValueError, "duplicate group name"):
                self.verify(rows, parent_rows=parent)

    def test_conflicting_or_missing_ssh_group_fails(self):
        for rows in (self.rows, self.rows + ["_ssh:x:105:"], self.rows + ["_ssh:!:104:"]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "inherited group rows"):
                self.verify(rows)

    def test_group_kind_ownership_and_mode_are_preserved(self):
        for key, value in (("kind", "symlink"), ("uid", 1), ("gid", 1), ("mode", 0o600)):
            changed = copy.deepcopy(self.debug)
            changed["etc/group"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "group file metadata"):
                self.verify(self.rows + ["_ssh:x:104:"], installed=changed)

    def test_password_and_shadow_payload_or_metadata_changes_fail(self):
        for path in ("etc/passwd", "etc/gshadow"):
            for key, value in (("sha256", "changed"), ("mode", 0o777), ("uid", 1)):
                changed = copy.deepcopy(self.debug)
                changed[path][key] = value
                with self.subTest(path=path, key=key), self.assertRaisesRegex(ValueError, "inherited account state"):
                    self.verify(self.rows + ["_ssh:x:104:"], installed=changed)

    def test_missing_account_files_fail(self):
        for path in ("etc/group", "etc/passwd", "etc/gshadow"):
            changed = copy.deepcopy(self.debug)
            del changed[path]
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.verify(self.rows + ["_ssh:x:104:"], installed=changed)


class SyncdParentTest(unittest.TestCase):
    def test_declared_vim_documentation_replacements_bind_exact_layer_records(self):
        paths = ["usr/share/vim/vim91/doc/help.txt", "usr/share/vim/vim91/doc/tags"]
        original = {"kind": "file", "sha256": "tiny-help", "size": 12, "mode": 0o644, "uid": 0, "gid": 0}
        installed = {**original, "sha256": "full-help", "size": 24}
        runtime = {path: dict(original) for path in paths}
        tools = {path: dict(installed) for path in paths}
        declared = [{"package": "vim-runtime", "path": path} for path in paths]
        result = verify_parent_files(runtime, runtime, tools, {}, [],
                                     debug_replacements=declared, debug_tools=tools)
        self.assertEqual(result["declared_debug_package_replacements"], paths)
        for key, value in (("sha256", "wrong"), ("size", 25), ("mode", 0o600),
                           ("uid", 1), ("gid", 1), ("kind", "symlink")):
            wrong = copy.deepcopy(tools)
            wrong[paths[0]][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "differs from declared payload"):
                verify_parent_files(runtime, runtime, wrong, {}, [],
                                    debug_replacements=declared, debug_tools=tools)
        with self.assertRaisesRegex(ValueError, "inherited runtime file"):
            verify_parent_files(runtime, runtime, tools, {}, [],
                                debug_replacements=declared[:1], debug_tools=tools)

    def test_debug_replacement_rejects_duplicate_missing_or_elf_paths(self):
        path = "usr/share/vim/vim91/doc/help.txt"
        original = {"kind": "file", "sha256": "tiny", "mode": 0o644, "uid": 0, "gid": 0}
        installed = {**original, "sha256": "full"}
        declared = [{"package": "vim-runtime", "path": path}]
        with self.assertRaisesRegex(ValueError, "duplicate debug replacement"):
            verify_parent_files({path: original}, {path: original}, {path: installed}, {}, [],
                                debug_replacements=declared * 2, debug_tools={path: installed})
        for runtime, tools in (({}, {path: installed}), ({path: original}, {}),
                               ({path: {**original, "elf_machine": 62}}, {path: installed}),
                               ({path: original}, {path: {**installed, "elf_machine": 62}})):
            with self.subTest(runtime=runtime, tools=tools), self.assertRaisesRegex(ValueError, "inherited non-ELF file"):
                verify_parent_files(runtime, runtime, {path: installed}, {}, [],
                                    debug_replacements=declared, debug_tools=tools)

    def test_checks_nonnative_parent_files_and_exact_metadata(self):
        parent = {"usr/share/owner/data": {"kind": "file", "sha256": "same", "mode": 0o644}}
        verify_parent_files(parent, parent, parent, {}, [])
        for key, value in (("sha256", "changed"), ("mode", 0o600), ("kind", "symlink")):
            changed = copy.deepcopy(parent)
            changed["usr/share/owner/data"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "inherited parent file"):
                verify_parent_files(parent, changed, changed, {}, [])
        with self.assertRaisesRegex(ValueError, "inherited runtime file"):
            verify_parent_files(parent, parent, {}, {}, [])

    def test_only_declared_configuration_and_alternatives_are_excepted(self):
        parent = {"usr/bin/start.sh": {"sha256": "old"}, "usr/bin/vim": {"kind": "hardlink"}}
        runtime = {**parent, "usr/bin/start.sh": {"sha256": "new"}}
        debug = {**runtime, "usr/bin/vim": {"kind": "symlink"}}
        verify_parent_files(parent, runtime, debug, {"usr/bin/start.sh": {}}, [{"paths": ["usr/bin/vim"]}])
        with self.assertRaisesRegex(ValueError, "inherited runtime file"):
            verify_parent_files(parent, runtime, debug, {"usr/bin/start.sh": {}}, [])


if __name__ == "__main__":
    unittest.main()
