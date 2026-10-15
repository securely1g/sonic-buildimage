"""Exercise real alternatives registration and removal without building DEBs."""

import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.oci import alternatives_state
from tools.bazel.oci.oci_layout import OciLayout


def archive(path, files):
    with tarfile.open(path, "w") as output:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                member.type, member.linkname = tarfile.SYMTYPE, value[0]
                output.addfile(member)
            else:
                content = value.encode() if isinstance(value, str) else value
                member.size, member.mode = len(content), 0o644
                output.addfile(member, io.BytesIO(content))
    return path


@unittest.skipUnless(shutil.which("update-alternatives"), "requires Debian's update-alternatives")
class AlternativesStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.command = [shutil.which("update-alternatives")]
        self.base_files = {}
        self.name, self.target, self.priority = "editor", "/usr/bin/vim.tiny", 15

    def run_build(self, suffix="", script=None):
        group = {"name": self.name, "link": "/usr/bin/" + self.name,
                 "path": self.target, "priority": self.priority}
        controls = archive(self.root / ("controls" + suffix + ".tar"), {
            "control": "Package: example\nVersion: 1\nArchitecture: amd64\n",
            "postinst": script or "update-alternatives --install {link} {name} {path} {priority}\n".format(**group),
            "prerm": "update-alternatives --remove {name} {path}\n".format(**group),
        })
        payload = archive(self.root / ("payload" + suffix + ".tar"), {self.target[1:]: "actual executable bytes"})
        base = archive(self.root / ("base" + suffix + ".tar"), self.base_files)
        layout = OciLayout({"digest": "sha256:base"}, {}, {}, (base,))
        output = self.root / ("out" + suffix)
        mtree = self.root / ("layer" + suffix + ".mtree")
        with patch.object(alternatives_state, "validate_layout", return_value=layout):
            report = alternatives_state.build(self.root / "base", {"architecture": "amd64", "packages": [{
                "package": "example", "payload": str(payload), "control": str(controls), "groups": [group],
            }]}, self.command, output, mtree, self.root / ("receipt" + suffix + ".json"))
        return output, mtree, report

    def materialize(self, output, receipt):
        root = self.root / "installed"
        root.mkdir()
        for name, metadata in receipt["files"].items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if "link" in metadata:
                path.symlink_to(metadata["link"])
            else:
                path.write_bytes((output / name).read_bytes())
        target = root / self.target[1:]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        return root

    def test_real_tool_registers_state_and_removes_both_links(self):
        self.base_files = {"usr/bin/editor": ("/etc/alternatives/editor",),
                           "etc/alternatives/editor": ("/usr/bin/vim.tiny",)}
        output, mtree, receipt = self.run_build()
        root = self.materialize(output, receipt)
        query = alternatives_state.invoke(self.command, root, ["--query", "editor"])
        self.assertIn("Value: /usr/bin/vim.tiny", query)
        self.assertFalse((output / "usr/bin/vim.tiny").exists())
        alternatives_state.invoke(self.command, root, ["--remove", "editor", self.target])
        self.assertFalse((root / "usr/bin/editor").is_symlink())
        self.assertFalse((root / "etc/alternatives/editor").is_symlink())
        self.assertFalse((root / "var/lib/dpkg/alternatives/editor").exists())
        self.assertIn("type=link", mtree.read_text())
        self.assertFalse(receipt["maintainer_scripts_executed"])

    def pager_base(self, manual=False):
        seed = self.root / "seed"
        for directory in ("usr/bin", "bin", "etc/alternatives", "var/lib/dpkg/alternatives"):
            (seed / directory).mkdir(parents=True, exist_ok=True)
        (seed / "bin/more").touch()
        alternatives_state.invoke(self.command, seed, ["--install", "/usr/bin/pager", "pager", "/bin/more", "50"])
        if manual:
            alternatives_state.invoke(self.command, seed, ["--set", "pager", "/bin/more"])
        self.base_files = {"bin": ("usr/bin",), "usr/bin/more": "more", "usr/bin/pager": ("/etc/alternatives/pager",),
                           "etc/alternatives/pager": ("/bin/more",),
                           "var/lib/dpkg/alternatives/pager": (seed / "var/lib/dpkg/alternatives/pager").read_bytes()}
        self.name, self.target, self.priority = "pager", "/usr/bin/less", 77

    def test_registers_less_and_retains_more_fallback(self):
        self.pager_base()
        output, _, report = self.run_build()
        self.assertIn("Alternative: /bin/more", report["queries"]["pager"])
        self.assertIn("Value: /usr/bin/less", report["queries"]["pager"])
        root = self.materialize(output, report)
        (root / "bin").mkdir()
        (root / "bin/more").touch()
        alternatives_state.invoke(self.command, root, ["--remove", "pager", "/usr/bin/less"])
        self.assertIn("Value: /bin/more", alternatives_state.invoke(self.command, root, ["--query", "pager"]))

    def test_preserves_inherited_manual_selection(self):
        self.pager_base(manual=True)
        _, _, report = self.run_build()
        self.assertIn("Status: manual", report["queries"]["pager"])
        self.assertIn("Value: /bin/more", report["queries"]["pager"])
        self.assertIn("Alternative: /usr/bin/less", report["queries"]["pager"])

    def test_metadata_is_reproducible_across_temporary_roots(self):
        output1, _, report1 = self.run_build("-first")
        output2, _, report2 = self.run_build("-second")
        self.assertEqual(report1, report2)
        self.assertEqual((output1 / "var/lib/dpkg/alternatives/editor").read_bytes(),
                         (output2 / "var/lib/dpkg/alternatives/editor").read_bytes())

    def test_changed_package_script_requires_review(self):
        with self.assertRaisesRegex(ValueError, "differs from reviewed package scripts"):
            self.run_build(script="update-alternatives --install /usr/bin/editor editor /usr/bin/vim.tiny 25\n")

    def test_existing_real_command_cannot_be_silently_replaced(self):
        self.base_files = {"usr/bin/editor": "existing executable"}
        with self.assertRaisesRegex(ValueError, "existing command prevents"):
            self.run_build()

    def test_file_overlay_hides_previous_directory_contents(self):
        lower = archive(self.root / "lower.tar", {"usr/bin/tool": "old program"})
        upper = archive(self.root / "upper.tar", {"usr/bin": "new regular file"})
        files, _ = alternatives_state.image_inventory([lower, upper])
        self.assertNotIn("usr/bin/tool", files)


if __name__ == "__main__":
    unittest.main()
