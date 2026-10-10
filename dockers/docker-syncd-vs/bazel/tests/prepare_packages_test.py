#!/usr/bin/env python3
"""Exercise the native Make package handoff with sample Debian packages.

Run this directly with Python. It intentionally creates sample DEBs with
native dpkg-deb and is not registered as a Bazel test.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest

OWNER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OWNER / "bazel"))
import prepare_packages as subject


@unittest.skipUnless(shutil.which("dpkg-deb"), "native dpkg-deb is required")
class PreparePackagesTest(unittest.TestCase):
    def test_direct_cli_finds_its_helper_with_safe_python_imports(self):
        """Make can invoke the preparation script when Python omits the script directory from sys.path."""
        result = subprocess.run([sys.executable, "-P", str(OWNER / "bazel/prepare_packages.py"), "--help"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--variant", result.stdout)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-package-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "debs").mkdir()
        self.counter = 0
        self.fips = self.deb("openssh-client", version="1:10.0p1-7+fips")

    def deb(self, name, value=b"installed payload\n", *, architecture="amd64", version="1.0"):
        self.counter += 1
        tree = self.root / ("source-" + str(self.counter))
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "DEBIAN").chmod(0o755)
        (tree / "DEBIAN/control").write_text(
            f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n"
            "Maintainer: SONiC test <test@example.invalid>\nDescription: sample package\n"
            "Depends: libc6 (>= 2.38)\nMulti-Arch: foreign\n")
        data = tree / "usr/share" / name
        data.mkdir(parents=True)
        (data / "data").write_bytes(value)
        (data / "link").symlink_to("data")
        (tree / "usr/bin").mkdir()
        program = tree / "usr/bin" / name
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
        output = self.root / "debs" / f"{name}_{version}_{architecture}.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(tree), str(output)],
                       capture_output=True, check=True)
        return output

    def arguments(self, packages, *, variant="runtime", required=None, runtime_manifest=None, runtime_fips=True):
        if variant == "runtime" and runtime_fips:
            packages = [*packages, self.fips]
        return argparse.Namespace(
            output=str(self.root / "handoff" / variant), variant=variant,
            architecture="amd64", distribution="trixie", include_vs_dash_sai="y",
            include_fips="y", enable_asan="n", enable_syncd_rpc="n",
            package=[str(path) for path in packages], required_package=required or [],
            debug_apt_package=sorted(subject.DEBUG_APT_PACKAGES) if variant == "debug" else [],
            runtime_manifest=str(runtime_manifest) if runtime_manifest else None,
            dpkg_deb="dpkg-deb", tar="tar")

    def manifest(self, variant="runtime"):
        return self.root / "handoff" / variant / "manifest.json"

    def test_exact_payload_and_metadata_are_published(self):
        deb = self.deb("syncd-vs")
        result = subject.prepare(self.arguments([deb], required=["syncd-vs"]))
        manifest = json.loads(self.manifest().read_bytes())
        record = manifest["packages"][0]
        expected = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(deb)])
        self.assertTrue(self.manifest().parent.is_symlink())
        self.assertEqual(record["source_sha256"], hashlib.sha256(deb.read_bytes()).hexdigest())
        self.assertEqual(record["payload_sha256"], hashlib.sha256(expected).hexdigest())
        aggregate = (self.manifest().parent / "payload.tar").read_bytes()
        self.assertEqual(manifest["payload"]["sha256"], hashlib.sha256(aggregate).hexdigest())
        self.assertEqual((record["package"], record["version"], record["architecture"]),
                         ("syncd-vs", "1.0", "amd64"))
        self.assertIn("control", record["control_files"])
        self.assertEqual(record["control_fields"]["Depends"], "libc6 (>= 2.38)")
        self.assertEqual(record["control_fields"]["Multi-Arch"], "foreign")
        self.assertEqual(record["control_fields"]["Package"], "syncd-vs")
        self.assertEqual(record["control_fields"]["Version"], "1.0")
        self.assertEqual(record["control_fields"]["Architecture"], "amd64")
        fips = manifest["packages"][1]
        self.assertEqual((fips["package"], fips["version"]), ("openssh-client", "1:10.0p1-7+fips"))
        self.assertEqual(fips["source_sha256"], hashlib.sha256(self.fips.read_bytes()).hexdigest())
        fips_payload = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(self.fips)])
        fips_control = subprocess.check_output(["dpkg-deb", "--ctrl-tarfile", str(self.fips)])
        self.assertEqual(fips["payload_sha256"], hashlib.sha256(fips_payload).hexdigest())
        self.assertEqual(fips["control_sha256"], hashlib.sha256(fips_control).hexdigest())
        self.assertEqual(fips["control_fields"]["Version"], fips["version"])
        def contents(data):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                return [(member.name, member.type, member.mode, member.linkname,
                         archive.extractfile(member).read() if member.isfile() else None)
                        for member in archive]
        self.assertEqual(contents(aggregate), contents(expected) + contents(fips_payload))
        self.assertEqual(result["package_count"], 2)

    def test_repeat_preserves_publication_and_changed_bytes_make_a_new_generation(self):
        deb = self.deb("syncd-vs")
        args = self.arguments([deb], required=["syncd-vs"])
        first = subject.prepare(args)
        output = self.manifest().parent
        first_link = (output.lstat().st_ino, output.lstat().st_mtime_ns, output.readlink())
        old_generation = output.resolve()
        old_manifest = self.manifest().read_bytes()
        self.assertEqual(subject.prepare(args), first)
        self.assertEqual((output.lstat().st_ino, output.lstat().st_mtime_ns, output.readlink()), first_link)
        self.deb("syncd-vs", b"changed payload\n")
        second = subject.prepare(args)
        self.assertNotEqual(second["generation"], first["generation"])
        self.assertEqual((old_generation / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual(len(list((output.parent / ".runtime.generations").glob("[0-9a-f]*"))), 2)

    def test_dependency_order_and_duplicate_arguments_match_make(self):
        first = self.deb("first-package")
        second = self.deb("second-package")
        subject.prepare(self.arguments([first, second, first], required=["first-package", "second-package"]))
        records = json.loads(self.manifest().read_bytes())["packages"]
        self.assertEqual([record["package"] for record in records], ["first-package", "second-package", "openssh-client"])
        expected = []
        for package in (first, second, self.fips):
            data = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(package)])
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                expected.extend(member.name for member in archive)
        with tarfile.open(self.manifest().parent / "payload.tar", "r:") as archive:
            self.assertEqual([member.name for member in archive], expected)

    def test_runtime_requires_fips_openssh_before_publishing(self):
        """A supported FIPS runtime must not fall back to Debian's transitive SSH dependency."""
        runtime = self.deb("syncd-vs")
        subject.prepare(self.arguments([runtime]))
        old_target = self.manifest().parent.readlink()
        ordinary = self.deb("openssh-client", version="1:10.0p1-7+deb13u4")
        for packages in ([runtime], [runtime, ordinary]):
            with self.subTest(packages=packages), self.assertRaisesRegex(ValueError, "requires the Make FIPS"):
                subject.prepare(self.arguments(packages, runtime_fips=False))
            self.assertEqual(self.manifest().parent.readlink(), old_target)

    def test_make_cannot_publish_source_libraries_or_their_old_symbols(self):
        """Reject moved packages by their Debian identity in either variant, preserving a valid generation."""
        runtime = self.deb("syncd-vs")
        symbols = self.deb("syncd-vs-dbgsym")
        subject.prepare(self.arguments([runtime]))
        subject.prepare(self.arguments([symbols], variant="debug", runtime_manifest=self.manifest()))
        previous = {variant: self.manifest(variant).parent.readlink() for variant in ("runtime", "debug")}
        for name in ("libswsscommon", "libsairedis", "libsaimetadata"):
            for package in (name, name + "-dbgsym"):
                moved = self.deb(package)
                # Filenames are not the ownership contract; control metadata is.
                renamed = moved.with_name("unrelated-name.deb")
                shutil.copyfile(moved, renamed)
                for variant, retained in (("runtime", runtime), ("debug", symbols)):
                    args = self.arguments([retained, renamed], variant=variant,
                                          runtime_manifest=self.manifest() if variant == "debug" else None)
                    with self.subTest(package=package, variant=variant), self.assertRaisesRegex(
                            ValueError, "contains packages now built from source: " + package + "|inherited base-symbol original package changed"):
                        subject.prepare(args)
                    self.assertEqual(self.manifest(variant).parent.readlink(), previous[variant])

    def test_debug_rejects_a_stale_runtime_with_make_owned_source_libraries(self):
        """A fresh debug handoff cannot be paired with an older runtime that imported shared libraries."""
        runtime = self.deb("syncd-vs")
        symbols = self.deb("syncd-vs-dbgsym")
        subject.prepare(self.arguments([runtime]))
        subject.prepare(self.arguments([symbols], variant="debug", runtime_manifest=self.manifest()))
        previous = self.manifest("debug").parent.readlink()
        original = json.loads(self.manifest().read_bytes())
        stale = self.root / "stale-runtime.json"
        for name in ("libswsscommon", "libsairedis", "libsaimetadata"):
            document = json.loads(json.dumps(original))
            document["packages"].append({"package": name})
            stale.write_text(json.dumps(document))
            args = self.arguments([symbols], variant="debug", runtime_manifest=stale)
            with self.subTest(package=name), self.assertRaisesRegex(ValueError, "contains packages now built from source: " + name):
                subject.prepare(args)
            self.assertEqual(self.manifest("debug").parent.readlink(), previous)

    def test_debug_inherits_fips_and_rejects_stale_runtime_manifests(self):
        """Debug adds symbols/tools without introducing or replacing runtime OpenSSH."""
        runtime = self.deb("syncd-vs")
        symbols = self.deb("syncd-vs-dbgsym")
        subject.prepare(self.arguments([runtime]))
        args = self.arguments([symbols], variant="debug", runtime_manifest=self.manifest())
        subject.prepare(args)
        old_target = self.manifest("debug").parent.readlink()
        self.assertNotIn("openssh-client", [r["package"] for r in json.loads(self.manifest("debug").read_bytes())["packages"]])
        for version in ("1:10.0p1-7+fips", "1:10.0p1-7+deb13u4"):
            ssh = self.deb("openssh-client", version=version)
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "must inherit runtime FIPS"):
                subject.prepare(self.arguments([symbols, ssh], variant="debug", runtime_manifest=self.manifest()))
        original = json.loads(self.manifest().read_bytes())
        stale = self.root / "stale-runtime.json"
        for change in ("missing", "ordinary", "control"):
            manifest = json.loads(json.dumps(original))
            ssh = next(r for r in manifest["packages"] if r["package"] == "openssh-client")
            if change == "missing":
                manifest["packages"].remove(ssh)
            elif change == "ordinary":
                ssh["version"] = "1:10.0p1-7+deb13u4"
                ssh["control_fields"]["Version"] = ssh["version"]
            else:
                ssh["control_fields"]["Version"] = "1:10.0p1-7+deb13u4"
            stale.write_text(json.dumps(manifest))
            args.runtime_manifest = str(stale)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "FIPS openssh-client"):
                subject.prepare(args)
            self.assertEqual(self.manifest("debug").parent.readlink(), old_target)

    def test_debug_rejects_a_different_runtime_package_and_preserves_previous_output(self):
        runtime = self.deb("syncd-vs")
        symbols = self.deb("syncd-vs-dbgsym")
        runtime_args = self.arguments([runtime], required=["syncd-vs"])
        subject.prepare(runtime_args)
        debug_args = self.arguments([runtime, symbols], variant="debug", required=["syncd-vs-dbgsym"],
                                    runtime_manifest=self.manifest())
        first = subject.prepare(debug_args)
        old_target = self.manifest("debug").parent.readlink()
        self.deb("syncd-vs", b"new runtime package\n")
        with self.assertRaisesRegex(ValueError, "would replace a runtime package"):
            subject.prepare(debug_args)
        self.assertEqual(self.manifest("debug").parent.readlink(), old_target)
        subject.prepare(runtime_args)
        second = subject.prepare(debug_args)
        self.assertNotEqual(second["generation"], first["generation"])
        debug = json.loads(self.manifest("debug").read_bytes())
        self.assertEqual(debug["runtime_manifest_sha256"], hashlib.sha256(self.manifest().read_bytes()).hexdigest())

    def test_invalid_debs_and_features_preserve_previous_output(self):
        valid = self.deb("syncd-vs")
        args = self.arguments([valid], required=["syncd-vs"])
        subject.prepare(args)
        old_target = self.manifest().parent.readlink()
        valid.write_bytes(b"not a Debian package")
        with self.assertRaisesRegex(ValueError, "package inspection failed"):
            subject.prepare(args)
        self.assertEqual(self.manifest().parent.readlink(), old_target)
        wrong = self.deb("wrong-architecture", architecture="arm64")
        with self.assertRaisesRegex(ValueError, "wrong Debian architecture"):
            subject.prepare(self.arguments([wrong]))
        args.include_fips = "n"
        with self.assertRaisesRegex(ValueError, "unsupported syncd-vs OCI feature"):
            subject.prepare(args)
        self.assertEqual(self.manifest().parent.readlink(), old_target)

    def test_missing_required_package_and_custom_debug_tools_are_rejected(self):
        deb = self.deb("syncd-vs")
        with self.assertRaisesRegex(ValueError, "missing required syncd-vs packages"):
            subject.prepare(self.arguments([deb], required=["libsai"]))
        runtime_args = self.arguments([deb], required=["syncd-vs"])
        subject.prepare(runtime_args)
        args = self.arguments([deb], variant="debug", runtime_manifest=self.manifest())
        args.debug_apt_package = ["gdb"]
        with self.assertRaisesRegex(ValueError, "debug tools differ"):
            subject.prepare(args)

    def test_unrelated_output_directory_is_preserved(self):
        deb = self.deb("syncd-vs")
        output = self.manifest().parent
        output.mkdir(parents=True)
        (output / "keep").write_text("unrelated\n")
        with self.assertRaisesRegex(ValueError, "refusing to replace a non-symlink"):
            subject.prepare(self.arguments([deb]))
        self.assertEqual((output / "keep").read_text(), "unrelated\n")


if __name__ == "__main__":
    unittest.main()
