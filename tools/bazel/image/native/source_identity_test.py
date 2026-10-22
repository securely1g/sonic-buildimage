#!/usr/bin/env python3
"""Exercise native source provenance with real, small local Git histories."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import source_identity


class NativeSourceIdentityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name).resolve() / "source"
        self.source.mkdir()
        self.init(self.source)
        self.frr = self.source / source_identity.FRR
        self.frr.mkdir(parents=True)
        self.init(self.frr)
        (self.frr / "debian").mkdir()
        (self.frr / "code.c").write_text("int original;\n")
        (self.frr / "debian/changelog").write_text("frr (10.5.4-1) unstable; urgency=medium\n\n  * Upstream.\n")
        self.commit(self.frr, "upstream baseline")
        self.base = self.head(self.frr)
        self.git(self.frr, "tag", "frr-10.5.4")
        patches = self.source / source_identity.PATCHES
        patches.mkdir()
        self.patch_commits = []
        (self.frr / "code.c").write_text("int sonic;\n")
        self.commit(self.frr, "SONiC code change")
        self.patch_commits.append(self.head(self.frr))
        (patches / "0001-code.patch").write_bytes(self.git(self.frr, "format-patch", "-1", "--stdout"))
        (self.frr / "feature.h").write_text("#define SONIC 1\n")
        self.commit(self.frr, "SONiC header")
        self.patch_commits.append(self.head(self.frr))
        (patches / "0002-header.patch").write_bytes(self.git(self.frr, "format-patch", "-1", "--stdout"))
        (patches / "series").write_text("# applies to upstream\n0001-code.patch\n0002-header.patch\n")
        (self.source / "rules").mkdir()
        (self.source / "rules/frr.mk").write_text(
            "FRR_VERSION = 10.5.4\nFRR_SUBVERSION = 0\nFRR_TAG = frr-$(FRR_VERSION)\n")
        (self.source / "src/sonic-frr/Makefile").write_text("# captured native packaging recipe\n")
        module = self.source / source_identity.C_MODULE
        module.parent.mkdir()
        module.write_text("int sonic_dplane;\n")
        self.git(self.frr, "checkout", "--detach", self.base)

        self.other = self.source / "src/other"
        self.other.mkdir()
        self.init(self.other)
        self.nested = self.other / "nested"
        self.nested.mkdir()
        self.init(self.nested)
        (self.nested / "data").write_text("nested source\n")
        self.commit(self.nested, "nested source")
        (self.other / ".gitmodules").write_text('[submodule "nested"]\n path = nested\n url = https://github.com/example/nested\n')
        self.git(self.other, "add", ".gitmodules")
        self.link(self.other, "nested", self.head(self.nested))
        self.git(self.other, "commit", "-qm", "record nested source")
        (self.source / ".gitmodules").write_text(
            '[submodule "frr"]\n path = src/sonic-frr/frr\n url = https://github.com/FRRouting/frr\n'
            '[submodule "other"]\n path = src/other\n url = https://github.com/example/other\n')
        self.git(self.source, "add", ".gitmodules", "rules", "src/sonic-frr/Makefile",
                 "src/sonic-frr/patch", source_identity.C_MODULE)
        self.link(self.source, source_identity.FRR, self.base)
        self.link(self.source, "src/other", self.head(self.other))
        self.git(self.source, "commit", "-qm", "native source declaration")
        self.baseline = source_identity.source_identity(self.source)

    def git(self, directory, *arguments):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME="Native provenance fixture", GIT_COMMITTER_NAME="Native provenance fixture",
                   GIT_AUTHOR_EMAIL="securely1g@gmail.com", GIT_COMMITTER_EMAIL="securely1g@gmail.com",
                   GIT_TERMINAL_PROMPT="0")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(directory),
                                        "-C", str(directory), *arguments], env=env, stderr=subprocess.PIPE)

    def init(self, directory):
        self.git(directory, "init", "--quiet", "--template=")

    def commit(self, directory, message):
        self.git(directory, "add", ".")
        self.git(directory, "commit", "-qm", message)

    def link(self, directory, path, commit):
        self.git(directory, "update-index", "--add", "--cacheinfo", "160000," + commit + "," + path)

    def head(self, directory):
        return self.git(directory, "rev-parse", "HEAD").decode().strip()

    def transform(self, version="10.5.4-sonic-0", final_extra=False):
        self.git(self.frr, "checkout", "-B", "frr-10.5.4-patched", self.patch_commits[-1])
        changelog = self.frr / "debian/changelog"
        changelog.write_text("frr (" + version + ") UNRELEASED; urgency=medium\n\n  * SONiC build.\n\n" + changelog.read_text())
        if final_extra:
            (self.frr / "code.c").write_text("int unreviewed;\n")
        self.commit(self.frr, "Update changelog")
        (self.frr / "zebra").mkdir(exist_ok=True)
        (self.frr / "zebra/dplane_fpm_sonic.c").write_bytes((self.source / source_identity.C_MODULE).read_bytes())

    def identity(self):
        return source_identity.source_identity(self.source)

    def test_clean_and_valid_transformation_keep_same_declared_source_graph(self):
        self.assertEqual(self.baseline["native_transformations"], {})
        self.transform()
        before_index = (self.frr / ".git/index").read_bytes()
        before_head = self.head(self.frr)
        proof = self.identity()
        for key in ("source_commit", "source_branch", "source_submodules"):
            self.assertEqual(proof[key], self.baseline[key])
        transformation = proof["native_transformations"][source_identity.FRR]
        self.assertEqual(transformation["recorded_commit"], self.base)
        self.assertEqual(transformation["actual_commit"], before_head)
        self.assertEqual(transformation["patch_count"], 2)
        self.assertEqual(transformation["version"], "10.5.4-sonic-0")
        self.assertEqual(transformation["changelog_sha256"], hashlib.sha256((self.frr / "debian/changelog").read_bytes()).hexdigest())
        self.assertEqual(set(transformation["inputs"]), {"rules/frr.mk", "src/sonic-frr/Makefile", source_identity.C_MODULE,
                                                       source_identity.PATCHES + "series", source_identity.PATCHES + "0001-code.patch",
                                                       source_identity.PATCHES + "0002-header.patch"})
        self.assertEqual((self.frr / ".git/index").read_bytes(), before_index)
        self.assertEqual(self.head(self.frr), before_head)

    def test_unrelated_native_working_file_dirt_is_allowed(self):
        (self.nested / "data").write_text("native generated content\n")
        (self.source / "native-output").write_text("artifact\n")
        self.assertEqual(self.identity(), self.baseline)

    def test_non_frr_head_change_is_rejected(self):
        (self.nested / "data").write_text("unrecorded new revision\n")
        self.commit(self.nested, "unexpected native commit")
        with self.assertRaisesRegex(ValueError, "unexpected native component HEAD"):
            self.identity()

    def test_uninitialized_or_missing_recorded_component_is_rejected(self):
        metadata = self.nested / ".git"
        metadata.rename(self.source.parent / "preserved-git")
        with self.assertRaisesRegex(ValueError, "uninitialized"):
            self.identity()
        self.nested.rename(self.source.parent / "preserved-component")
        with self.assertRaisesRegex(ValueError, "missing"):
            self.identity()

    def test_extra_and_missing_index_gitlinks_are_rejected(self):
        self.link(self.source, "src/extra", self.base)
        with self.assertRaisesRegex(ValueError, "extra, missing, or changed gitlinks"):
            self.identity()
        self.git(self.source, "update-index", "--force-remove", "src/extra")
        self.git(self.source, "update-index", "--force-remove", source_identity.FRR)
        with self.assertRaisesRegex(ValueError, "extra, missing, or changed gitlinks"):
            self.identity()

    def test_conflicted_index_is_rejected(self):
        self.git(self.source, "update-index", "--force-remove", source_identity.FRR)
        # An unresolved gitlink is represented by nonzero index stages.
        env = os.environ.copy()
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        subprocess.run(["git", "-C", str(self.source), "update-index", "--index-info"],
                       input=("160000 " + self.base + " 1\t" + source_identity.FRR + "\n").encode(),
                       env=env, check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.identity()

    def test_changed_gitmodules_is_rejected(self):
        with (self.source / ".gitmodules").open("a") as stream:
            stream.write('[submodule "extra"]\n path = extra\n url = https://github.com/example/extra\n')
        with self.assertRaisesRegex(ValueError, "differs from recorded source"):
            self.identity()

    def test_wrong_tag_and_missing_tag_are_rejected(self):
        self.transform()
        self.git(self.frr, "tag", "-f", "frr-10.5.4", self.patch_commits[0])
        with self.assertRaisesRegex(ValueError, "tag does not match"):
            self.identity()
        self.git(self.frr, "tag", "-d", "frr-10.5.4")
        with self.assertRaises(ValueError):
            self.identity()

    def test_each_modified_recipe_input_is_rejected_under_same_root_revision(self):
        self.transform()
        for name in ("rules/frr.mk", "src/sonic-frr/Makefile", source_identity.C_MODULE,
                     source_identity.PATCHES + "series", source_identity.PATCHES + "0001-code.patch"):
            path = self.source / name
            original = path.read_bytes()
            path.write_bytes(original + b"unexpected change\n")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "differs from recorded source"):
                self.identity()
            path.write_bytes(original)

    def test_committed_patch_change_cannot_validate_old_applied_tree(self):
        self.transform()
        patch = self.source / source_identity.PATCHES / "0001-code.patch"
        patch.write_bytes(patch.read_bytes().replace(b"+int sonic;", b"+int revised;"))
        self.git(self.source, "add", str(patch))
        self.git(self.source, "commit", "-qm", "change expected patch")
        with self.assertRaisesRegex(ValueError, "patch tree differs"):
            self.identity()

    def test_changed_series_order_cannot_validate_old_history(self):
        self.transform()
        series = self.source / source_identity.PATCHES / "series"
        series.write_text("0002-header.patch\n0001-code.patch\n")
        self.git(self.source, "add", str(series))
        self.git(self.source, "commit", "-qm", "reorder expected patches")
        with self.assertRaisesRegex(ValueError, "patch tree differs"):
            self.identity()

    def test_extra_commit_and_incomplete_transformation_are_rejected(self):
        self.git(self.frr, "checkout", "-B", "frr-10.5.4-patched", self.patch_commits[-1])
        with self.assertRaisesRegex(ValueError, "one commit per patch"):
            self.identity()

    def test_merge_commit_is_rejected_even_with_expected_trees_and_commit_count(self):
        self.transform()
        tree = self.git(self.frr, "rev-parse", "HEAD^{tree}").decode().strip()
        merge = self.git(self.frr, "commit-tree", tree, "-p", self.patch_commits[-1],
                         "-p", self.base, "-m", "changelog merge").decode().strip()
        self.git(self.frr, "checkout", "-B", "frr-10.5.4-patched", merge)
        with self.assertRaisesRegex(ValueError, "not a linear chain"):
            self.identity()

    def test_extra_commit_is_rejected(self):
        self.transform()
        (self.frr / "extra").write_text("extra commit\n")
        self.commit(self.frr, "unexpected extra commit")
        with self.assertRaisesRegex(ValueError, "one commit per patch"):
            self.identity()

    def test_final_commit_cannot_change_compiled_source(self):
        self.transform(final_extra=True)
        with self.assertRaisesRegex(ValueError, "only debian/changelog"):
            self.identity()

    def test_wrong_changelog_version_is_rejected(self):
        self.transform(version="10.5.4-sonic-99")
        with self.assertRaisesRegex(ValueError, "wrong package version"):
            self.identity()

    def test_missing_or_changed_untracked_c_module_is_rejected(self):
        self.transform()
        copied = self.frr / "zebra/dplane_fpm_sonic.c"
        copied.write_text("int wrong_module;\n")
        with self.assertRaisesRegex(ValueError, "copied C module"):
            self.identity()
        copied.unlink()
        with self.assertRaisesRegex(ValueError, "copied C module"):
            self.identity()

    def test_replacement_refs_and_inherited_git_configuration_are_ignored(self):
        self.transform()
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.nested / ".git"),
                                          "GIT_WORK_TREE": str(self.nested),
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
                                          "GIT_CONFIG_VALUE_0": str(self.nested)}):
            self.assertEqual(self.identity()["source_commit"], self.baseline["source_commit"])
        self.git(self.frr, "replace", self.base, self.patch_commits[0])
        self.assertEqual(self.identity()["native_transformations"][source_identity.FRR]["patch_count"], 2)


if __name__ == "__main__":
    unittest.main()
