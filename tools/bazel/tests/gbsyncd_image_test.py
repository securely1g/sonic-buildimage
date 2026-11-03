"""Reject account loss while retaining the declared gearbox OpenSSH group."""

import unittest
from unittest.mock import patch

from tools.bazel.ci import gbsyncd_image


BASE = "root:x:0:\nusers:x:100:sonic\nredis:x:103:\n"
RUNTIME = BASE + "_ssh:x:104:\n"


class GearboxGroups(unittest.TestCase):
    def validate(self, base=BASE, runtime=RUNTIME, debug=RUNTIME, mode=0o644):
        layouts = [(base, 0o644), (runtime, mode), (debug, 0o644)]

        def inventory(layout):
            return {"etc/group": {"kind": "file", "mode": layout[1], "uid": 0, "gid": 0}}, {}

        def extract(layout, files, origins, selected, root):
            self.assertEqual(selected, {"etc/group"})
            (root / "etc").mkdir()
            (root / "etc/group").write_text(layout[0])

        with patch.object(gbsyncd_image.image_validation, "image_inventory", inventory), \
                patch.object(gbsyncd_image.source_base_validation, "extract_selected", extract):
            return gbsyncd_image.validate_groups(*layouts)

    def test_declared_addition_preserves_parent_and_debug_groups(self):
        self.assertEqual(self.validate(), {"base_groups": 3, "runtime_groups": 4,
                                          "debug_groups": 4, "declared_addition": "_ssh:x:104:"})

    def test_missing_or_modified_parent_rows_are_rejected(self):
        for changed in (RUNTIME.replace("redis:x:103:\n", ""),
                        RUNTIME.replace("users:x:100:sonic", "users:x:100:")):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "existing groups"):
                self.validate(runtime=changed, debug=changed)

    def test_extra_or_wrong_ssh_group_is_rejected(self):
        for changed in (RUNTIME + "unexpected:x:105:\n", RUNTIME.replace("_ssh:x:104:", "_ssh:x:105:")):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "existing groups"):
                self.validate(runtime=changed, debug=changed)

    def test_debug_cannot_drop_or_add_runtime_group(self):
        for changed in (BASE, RUNTIME + "unexpected:x:105:\n"):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "runtime groups"):
                self.validate(debug=changed)

    def test_duplicate_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate group names"):
            self.validate(runtime=RUNTIME + "users:x:100:sonic\n")

    def test_permissions_are_preserved(self):
        with self.assertRaisesRegex(ValueError, "ownership or permissions"):
            self.validate(mode=0o666)


if __name__ == "__main__":
    unittest.main()
