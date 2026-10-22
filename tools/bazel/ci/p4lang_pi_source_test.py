#!/usr/bin/env python3
"""Exercise PI source extraction and the real Make selector without network/builds."""

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = (Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
        if "TEST_SRCDIR" in os.environ else Path(__file__).resolve().parents[3])
SOURCE = ROOT / "src/p4lang"
SPEC = importlib.util.spec_from_file_location("pi_source", SOURCE / "pi_source.py")
pi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pi)


class SourceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "p4lang-pi-0.1.3"
        self.manifest = json.loads((SOURCE / "pi_source.json").read_text())
        self.payloads = {}
        for number, item in enumerate(self.manifest["archives"]):
            entries = [("file", "source.cc", b"source bytes", 0o664)]
            if item["destination"] == ".":
                entries += [("file", "autogen.sh", b"#!/bin/sh\n", 0o775),
                            ("symlink", "source-link", "source.cc", 0o777)]
                entries += [("directory", name, b"", 0o775)
                            for name in pi.COMPONENTS if name not in (".", "packaging")]
            elif item["destination"] == "packaging":
                entries = [("file", "p4lang-pi/changelog", b"p4lang-pi (0.1.3-2) unstable; urgency=medium\n", 0o664),
                           ("file", "p4lang-pi/rules", b"#!/usr/bin/make -f\n", 0o775)]
            data = self.archive(entries, item["prefix"])
            item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            self.payloads[item["destination"]] = data

    def archive(self, entries, prefix="owner-repo-abcdef0"):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as target:
            root = tarfile.TarInfo(prefix)
            root.type = tarfile.DIRTYPE
            target.addfile(root)
            for kind, name, value, mode in entries:
                member = tarfile.TarInfo(prefix + "/" + name)
                member.mode = mode
                if kind == "file":
                    member.size = len(value)
                    target.addfile(member, io.BytesIO(value))
                else:
                    member.type = {"directory": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE,
                                   "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
                    if kind in ("symlink", "hardlink"):
                        member.linkname = value
                    target.addfile(member)
        return stream.getvalue()

    def fetch(self, item, destination):
        destination.write_bytes(self.payloads[item["destination"]])

    def extract(self, entries):
        archive = self.root / "input.tar.gz"
        archive.write_bytes(self.archive(entries))
        pi.extract(archive, self.root / "extracted", "owner-repo-abcdef0")

    def test_stage_preserves_content_links_and_modes_without_git_metadata(self):
        receipt = pi.stage(self.manifest, self.output, self.fetch)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["archives"], self.manifest["archives"])
        self.assertEqual((self.output / "source.cc").read_bytes(), b"source bytes")
        self.assertEqual(os.readlink(self.output / "source-link"), "source.cc")
        self.assertEqual((self.output / "autogen.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.output / "source.cc").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.output / "debian/rules").stat().st_mode & 0o777, 0o755)
        for name in pi.COMPONENTS:
            if name not in (".", "packaging"):
                self.assertEqual((self.output / name / "source.cc").read_bytes(), b"source bytes")
        for path in self.output.rglob("*"):
            self.assertNotEqual(path.name, ".git")
            self.assertEqual(path.lstat().st_mtime, 0)
        self.assertEqual(list(self.root.glob(".pi-source-*")), [])

    def test_hash_failure_publishes_nothing_and_keeps_siblings(self):
        sibling = self.root / "keep"
        sibling.write_bytes(b"retained")
        self.payloads["."] += b"changed"
        with self.assertRaisesRegex(ValueError, "size/SHA256"):
            pi.stage(self.manifest, self.output, self.fetch)
        self.assertFalse(self.output.exists())
        self.assertEqual(sibling.read_bytes(), b"retained")
        self.assertEqual(list(self.root.glob(".pi-source-*")), [])

    def test_existing_file_directory_and_broken_symlink_are_rejected_before_fetch(self):
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                if kind == "file":
                    self.output.write_text("keep")
                elif kind == "directory":
                    self.output.mkdir()
                else:
                    self.output.symlink_to("missing")
                fetch = mock.Mock()
                with self.assertRaisesRegex(ValueError, "already exists"):
                    pi.stage(self.manifest, self.output, fetch)
                fetch.assert_not_called()
                if kind == "directory":
                    self.output.rmdir()
                else:
                    self.output.unlink()

    def test_linked_parent_and_unexpected_destination_are_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        for output in (link / self.output.name, self.root / "unexpected"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                pi.stage(self.manifest, output, mock.Mock())

    def test_manifest_rejects_unknown_missing_reordered_or_mutable_inputs(self):
        variants = []
        for key, value in (("url", "http://github.com/p4lang/PI/archive/main.tar.gz"),
                           ("commit", "main"), ("prefix", "unexpected"),
                           ("sha256", ""), ("bytes", True), ("destination", "../../outside")):
            item = copy.deepcopy(self.manifest)
            item["archives"][0][key] = value
            variants.append(item)
        missing = copy.deepcopy(self.manifest)
        missing["archives"].pop()
        variants.append(missing)
        reordered = copy.deepcopy(self.manifest)
        reordered["archives"].reverse()
        variants.append(reordered)
        for item in variants:
            with self.subTest(item=item), self.assertRaises(ValueError):
                pi.validate_manifest(item)

    def test_archive_rejects_escaping_paths_git_metadata_and_wrong_prefix(self):
        for name in ("../outside", "foo/../../outside", ".git/config", "nested/.git", "./alias"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "input.tar.gz"
                archive.write_bytes(self.archive([("file", name, b"bad", 0o644)]))
                with self.assertRaises(ValueError):
                    pi.extract(archive, Path(tmp) / "out", "owner-repo-abcdef0")
        archive = self.root / "absolute.tar.gz"
        archive.write_bytes(self.archive([("file", "file", b"bad", 0o644)], "/absolute"))
        with self.assertRaises(ValueError):
            pi.extract(archive, self.root / "absolute-out", "owner-repo-abcdef0")
        archive.write_bytes(self.archive([("file", "file", b"bad", 0o644)]))
        with self.assertRaisesRegex(ValueError, "prefix"):
            pi.extract(archive, self.root / "wrong-prefix", "other")

    def test_archive_rejects_symlink_escape_and_symlink_parent(self):
        variants = [
            [("symlink", "escape", "/tmp", 0o777)],
            [("symlink", "escape", "../outside", 0o777)],
            [("symlink", "escape", ".git/config", 0o777)],
            [("symlink", "alias", "directory", 0o777), ("file", "alias/file", b"bad", 0o644)],
        ]
        for entries in variants:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "input.tar.gz"
                archive.write_bytes(self.archive(entries))
                with self.assertRaises(ValueError):
                    pi.extract(archive, Path(tmp) / "out", "owner-repo-abcdef0")

    def test_archive_rejects_hardlinks_special_files_duplicates_and_setuid(self):
        variants = [
            [("hardlink", "hard", "file", 0o644)], [("fifo", "pipe", b"", 0o644)],
            [("file", "same", b"one", 0o644), ("file", "same", b"two", 0o644)],
            [("file", "setuid", b"bad", 0o4755)],
        ]
        for entries in variants:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "input.tar.gz"
                archive.write_bytes(self.archive(entries))
                with self.assertRaises(ValueError):
                    pi.extract(archive, Path(tmp) / "out", "owner-repo-abcdef0")

    def test_archive_rejects_composed_symlink_escape_and_cycles(self):
        for entries in [
            [("symlink", "b", ".", 0o777), ("symlink", "a", "b/..", 0o777)],
            [("symlink", "a", "b", 0o777), ("symlink", "b", "a", 0o777)],
        ]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "input.tar.gz"
                archive.write_bytes(self.archive(entries))
                with self.assertRaisesRegex(ValueError, "symlink chain"):
                    pi.extract(archive, Path(tmp) / "out", "owner-repo-abcdef0")

    def test_packaging_version_and_nonempty_gitlink_placeholder_fail_closed(self):
        for component, entries in [
            ("packaging", [("file", "p4lang-pi/changelog", b"p4lang-pi (wrong) unstable\n", 0o644)]),
            (".", [("file", "proto/openconfig/gnmi/unexpected", b"not a placeholder", 0o644)]),
        ]:
            with self.subTest(component=component):
                manifest = copy.deepcopy(self.manifest)
                item = next(item for item in manifest["archives"] if item["destination"] == component)
                data = self.archive(entries, item["prefix"])
                item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                saved = self.payloads[component]
                self.payloads[component] = data
                with self.assertRaises(ValueError):
                    pi.stage(manifest, self.output, self.fetch)
                self.assertFalse(self.output.exists())
                self.payloads[component] = saved

    def test_download_is_bounded_and_keeps_https_verification(self):
        item = self.manifest["archives"][0]
        with mock.patch.object(pi.subprocess, "run") as run:
            pi.download(item, self.root / "download")
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/curl", "--disable"])
        self.assertEqual(command[command.index("--proto") + 1], "=https")
        self.assertEqual(command[command.index("--proto-redir") + 1], "=https")
        self.assertEqual(command[command.index("--max-filesize") + 1], str(item["bytes"]))
        self.assertEqual(run.call_args.kwargs, {"check": True, "timeout": 360})
        self.assertNotIn("--insecure", command)
        self.assertNotIn("--netrc", command)

    def make(self, mode=None, changes=None, package="pi"):
        values = {"P4LANG_PI_VERSION": "0.1.3", "P4LANG_PI_VERSION_FULL": "0.1.3-2",
                  "P4LANG_BMV2_VERSION": "1.15.0", "P4LANG_BMV2_VERSION_FULL": "1.15.0-9",
                  "P4LANG_P4C_VERSION": "1.2.4.2", "P4LANG_P4C_VERSION_FULL": "1.2.4.2-3",
                  "CONFIGURED_ARCH": "amd64", "SONIC_CONFIG_MAKE_JOBS": "2",
                  "SONIC_DPKG_ADMINDIR": "/tmp/test-dpkg", "CROSS_BUILD_ENVIRON": "",
                  "DEST": str(self.root / "debs")}
        values.update(changes or {})
        if mode is not None:
            values["P4LANG_PI_SOURCE_METHOD"] = mode
        target = values["DEST"] + "/p4lang-" + package + "_" + values["P4LANG_" + package.upper() + "_VERSION_FULL"] + "_amd64.deb"
        environment = dict(os.environ)
        environment.pop("P4LANG_PI_SOURCE_METHOD", None)
        return subprocess.run(["make", "--no-print-directory", "-n", "-f", str(SOURCE / "Makefile"),
                               *[key + "=" + value for key, value in values.items()], target],
                              env=environment, capture_output=True, text=True, check=False)

    def test_real_make_default_and_github_rejoin_identical_patch_and_build_steps(self):
        obs = self.make()
        github = self.make("github")
        self.assertEqual(obs.returncode, 0, obs.stderr)
        self.assertEqual(github.returncode, 0, github.stderr)
        self.assertIn("dget -u p4lang-pi_0.1.3-2.dsc", obs.stdout)
        self.assertNotIn("pi_source.py", obs.stdout)
        self.assertIn("python3 pi_source.py --output p4lang-pi-0.1.3", github.stdout)
        self.assertNotIn("dget", github.stdout)
        self.assertNotIn("rm -rf", github.stdout)
        self.assertEqual(obs.stdout.split("pushd", 1)[1], github.stdout.split("pushd", 1)[1])

    def test_real_make_rejects_unknown_mode_versions_and_cross(self):
        for mode, changes in [("unknown", {}), ("github", {"P4LANG_PI_VERSION": "9"}),
                              ("github", {"P4LANG_PI_VERSION_FULL": "0.1.3-9"}),
                              ("github", {"CROSS_BUILD_ENVIRON": "y"})]:
            with self.subTest(mode=mode, changes=changes):
                result = self.make(mode, changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("dpkg-buildpackage", result.stdout)

    def test_pi_selector_does_not_change_bmv2_or_p4c_commands(self):
        for package in ("bmv2", "p4c"):
            with self.subTest(package=package):
                default = self.make(package=package)
                github = self.make("github", package=package)
                self.assertEqual(default.returncode, 0, default.stderr)
                self.assertEqual(github.returncode, 0, github.stderr)
                self.assertEqual(default.stdout, github.stdout)


if __name__ == "__main__":
    unittest.main()
