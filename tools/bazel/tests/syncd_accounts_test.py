"""Reject account corruption hidden by the runtime image's added SSH group."""

import copy
import unittest

from tools.bazel.ci.syncd_image import verify_inherited_accounts, verify_parent_files


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
