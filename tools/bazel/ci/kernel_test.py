#!/usr/bin/env python3
"""Check cached kernel package integrity and the real Make import recipe."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import kernel


ROOT = Path(__file__).resolve().parents[3]
REGISTRY_RC = ("common --registry=" + kernel.REGISTRY_PREFIX + "a" * 40 + "\n"
               "common --registry=https://bcr.bazel.build\ncommon --lockfile_mode=update\n")


class KernelRegistryTest(unittest.TestCase):
    def test_local_snapshot_and_ci_branch_use_one_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            rc = workspace / ".bazelrc"
            original = REGISTRY_RC
            rc.write_text(original)
            selected = kernel.configure_registry(workspace, False)
            self.assertRegex(selected, r"/[0-9a-f]{40}$")
            self.assertEqual(rc.read_text(), original)
            self.assertEqual(kernel.configure_registry(workspace, True), kernel.CI_REGISTRY)
            self.assertEqual(rc.read_text(), original.replace(selected, kernel.CI_REGISTRY))
            self.assertEqual(rc.read_text().count(kernel.REGISTRY_PREFIX), 1)
            self.assertIn("common --registry=https://bcr.bazel.build\n", rc.read_text())
            rc.write_text(original + "common --registry=" + kernel.CI_REGISTRY + "\n")
            with self.assertRaisesRegex(ValueError, "one immutable"):
                kernel.configure_registry(workspace, True)


class KernelImportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "update-index", "--add", "--cacheinfo",
                        "160000," + "f" * 40 + ",src/sonic-linux-kernel"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm", "Fixture kernel gitlink"], check=True)
        identity = self.root / "src/sonic-linux-kernel/tools/bazel/kernel_action.py"
        identity.parent.mkdir(parents=True)
        identity.write_text("print('" + "a" * 64 + "')\n")
        (self.root / "rules").mkdir()
        for name in ("linux-kernel.mk", "linux-kernel.dep"):
            shutil.copyfile(ROOT / "rules" / name, self.root / "rules" / name)
        self.bundle = self.root / kernel.INPUTS
        self.bundle.mkdir(parents=True)
        self.config, self.packages = kernel.contract(self.root)
        self.manifest = dict(self.config, source_tree_sha256="a" * 64,
                             build_tools={"schema_version": 1, "kind": "debian-build-tools",
                                          "architecture": "amd64", "identity_sha256": "b" * 64,
                                          "packages": {"make": "4.4.1-2"}},
                             source_archives=[{"name": "linux.tar.xz", "sha256": "d" * 64}],
                             packages=[])
        package_root = self.root / "package-root"
        control = package_root / "DEBIAN/control"
        control.parent.mkdir(parents=True)
        for name, metadata in self.packages.items():
            control.write_text("".join(key.title() + ": " + value + "\n" for key, value in metadata.items())
                               + "Maintainer: Fixture <fixture@example.test>\nDescription: Test kernel package\n")
            path = self.bundle / name
            subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(package_root), str(path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.manifest["packages"].append(dict(metadata, name=name, sha256=kernel.sha256(path), size=path.stat().st_size))
        self.write_manifest()
        self.provenance = {"schema": 1, "source_commit": "e" * 40, "kernel_gitlink": "f" * 40,
                           "manifest_sha256": kernel.sha256(self.bundle / kernel.MANIFEST), "target": kernel.TARGET}
        (self.bundle / kernel.PROVENANCE).write_text(json.dumps(self.provenance))

    def write_manifest(self):
        (self.bundle / kernel.MANIFEST).write_text(json.dumps(self.manifest))

    def test_verified_packages_are_copied_without_sharing_inodes(self):
        self.assertEqual(kernel.verify_provenance(self.bundle, self.root, "e" * 40)["manifest"], self.manifest)
        copied = self.root / "staged"
        kernel.copy_bundle(self.bundle, copied, self.root)
        for name in [kernel.MANIFEST, kernel.PROVENANCE, *self.packages]:
            self.assertEqual((self.bundle / name).read_bytes(), (copied / name).read_bytes())
            self.assertNotEqual((self.bundle / name).stat().st_ino, (copied / name).stat().st_ino)
        with self.assertRaises(FileExistsError):
            kernel.copy_bundle(self.bundle, copied, self.root)

    def test_corrupt_missing_and_symlink_packages_fail_closed(self):
        name = next(iter(self.packages))
        path = self.bundle / name
        original = path.read_bytes()
        path.write_bytes(original + b"corrupt")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            kernel.verify(self.bundle, self.root)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "regular kernel output"):
            kernel.verify(self.bundle, self.root)
        replacement = self.root / "other.deb"
        replacement.write_bytes(original)
        path.symlink_to(replacement)
        with self.assertRaisesRegex(ValueError, "regular kernel output"):
            kernel.verify(self.bundle, self.root)

    def test_different_abi_package_set_and_metadata_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        mutations = [
            lambda value: value.update(kernel_abi="other-abi"),
            lambda value: value.update(signing="signed"),
            lambda value: value.update(source_tree_sha256="0" * 64),
            lambda value: value["packages"].pop(),
            lambda value: value["packages"][0].update(name="../outside.deb"),
            lambda value: value["packages"][0].update(architecture="arm64"),
            lambda value: value["build_tools"].update(identity_sha256="mutable"),
        ]
        for mutation in mutations:
            self.manifest = copy.deepcopy(original)
            mutation(self.manifest)
            self.write_manifest()
            with self.subTest(manifest=self.manifest), self.assertRaises(ValueError):
                kernel.verify(self.bundle, self.root)

    def test_deb_control_metadata_is_verified_even_when_file_hash_matches(self):
        item = self.manifest["packages"][0]
        control = self.root / "package-root/DEBIAN/control"
        control.write_text("Package: unrelated\nVersion: 1\nArchitecture: all\n"
                           "Maintainer: Fixture <fixture@example.test>\nDescription: Unexpected package\n")
        path = self.bundle / item["name"]
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(control.parent.parent), str(path)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        item.update(size=path.stat().st_size, sha256=kernel.sha256(path))
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "DEB control metadata"):
            kernel.verify(self.bundle, self.root)

    def test_provenance_must_belong_to_the_source_invocation(self):
        with self.assertRaisesRegex(ValueError, "source invocation"):
            kernel.verify_provenance(self.bundle, self.root, "0" * 40)
        self.provenance["kernel_gitlink"] = "0" * 40
        (self.bundle / kernel.PROVENANCE).write_text(json.dumps(self.provenance))
        with self.assertRaisesRegex(ValueError, "gitlink differs"):
            kernel.verify_provenance(self.bundle, self.root, "e" * 40)
        self.provenance["manifest_sha256"] = "0" * 64
        (self.bundle / kernel.PROVENANCE).write_text(json.dumps(self.provenance))
        with self.assertRaisesRegex(ValueError, "source invocation"):
            kernel.verify_provenance(self.bundle, self.root, "e" * 40)

    def test_cache_endpoint_does_not_accept_embedded_credentials(self):
        for url in ("https://cache.example.test", "http://127.0.0.1:8080", "grpcs://cache.example.test:9092"):
            self.assertEqual(kernel.endpoint(url), url)
        for url in ("https://token@cache.example.test", "https://cache.example.test?token=private",
                    "https://cache.example.test/#secret", "file:///cache", "https://cache.example.test\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                kernel.endpoint(url)

    def test_staging_accepts_only_declared_outputs_inside_kernel_work_directory(self):
        workdir = self.root / "work"
        (workdir / "outputs").mkdir(parents=True)
        for name in [kernel.MANIFEST, *self.packages]:
            shutil.copyfile(self.bundle / name, workdir / "outputs" / name)
        outputs = workdir / "output-paths.txt"
        outputs.write_text("".join("/work/outputs/" + name + "\n" for name in [kernel.MANIFEST, *self.packages]))
        source = {"source_commit": "e" * 40, "components": {"src/sonic-linux-kernel": {"gitlink": "f" * 40}}}
        evidence = kernel.stage_outputs(workdir, self.root / "result", self.root, source)
        self.assertEqual(evidence["manifest"], self.manifest)
        outputs.write_text("/other/old/kernel-packages.json\n")
        with self.assertRaisesRegex(ValueError, "output root"):
            kernel.stage_outputs(workdir, self.root / "bad", self.root, source)
        (workdir / "escape").symlink_to(self.bundle, target_is_directory=True)
        outputs.write_text("/work/escape/kernel-packages.json\n")
        with self.assertRaisesRegex(ValueError, "escapes"):
            kernel.stage_outputs(workdir, self.root / "bad", self.root, source)

    def test_launcher_uses_independent_consumer_workspace_and_retains_cache_evidence(self):
        consumer = self.root / "tools/bazel/kernel"
        consumer.mkdir(parents=True)
        (consumer / "MODULE.bazel").write_text("module(name = 'kernel_consumer')\n")
        original_rc = REGISTRY_RC
        (consumer / ".bazelrc").write_text(original_rc)
        state, artifacts = self.root / "state", self.root / "artifacts"
        state.mkdir()
        artifacts.mkdir()
        source = {"source_commit": "e" * 40, "components": {"src/sonic-linux-kernel": {"gitlink": "f" * 40}}}
        calls = []

        def execute(command, *_arguments):
            calls.append(command)
            copied = Path(command[command.index("--workspace") + 1])
            self.assertNotEqual(copied, consumer)
            self.assertEqual((copied / "MODULE.bazel").read_bytes(), (consumer / "MODULE.bazel").read_bytes())
            self.assertIn("common --registry=" + kernel.CI_REGISTRY + "\n", (copied / ".bazelrc").read_text())
            self.assertEqual((consumer / ".bazelrc").read_text(), original_rc)
            (copied / "MODULE.bazel.lock").write_text('{"lockFileVersion":24}\n')
            work = Path(command[command.index("--work-dir") + 1])
            (work / "outputs").mkdir(parents=True)
            for name in [kernel.MANIFEST, *self.packages]:
                shutil.copyfile(self.bundle / name, work / "outputs" / name)
            (work / "output-paths.txt").write_text("".join(
                "/work/outputs/" + name + "\n" for name in [kernel.MANIFEST, *self.packages]))
            (work / "execution.json").write_text('{"cacheHit":true}\n')

        receipt = {}
        ca_bundle = self.root / "public-ca.pem"
        ca_bundle.write_text("public certificate fixture\n")
        java_trust_store = self.root / "public-java-cacerts"
        java_trust_store.write_bytes(b"public trust store fixture")
        bundle = kernel.build(self.root, state, artifacts, source, "fixture", "http://127.0.0.1:8080",
                              False, execute, receipt, ca_bundle=ca_bundle, java_trust_store=java_trust_store,
                              ci_registry=True)
        self.assertIn("--remote-cache-read-only", calls[0])
        self.assertNotIn("--disk-cache", calls[0])
        self.assertEqual(kernel.verify(bundle, self.root), self.manifest)
        self.assertFalse((consumer / "MODULE.bazel.lock").exists())
        self.assertTrue((artifacts / "kernel/MODULE.bazel.lock").is_file())
        self.assertTrue((artifacts / "kernel/execution.json").is_file())
        self.assertFalse(receipt["kernel"]["upload_local_results"])
        self.assertEqual(receipt["kernel"]["registry"], kernel.CI_REGISTRY)
        self.assertEqual(calls[0][calls[0].index("--ca-bundle") + 1], str(ca_bundle))
        self.assertEqual(calls[0][calls[0].index("--java-trust-store") + 1], str(java_trust_store))
        self.assertEqual(receipt["kernel"]["execution_trust"], {
            "ca_bundle_sha256": kernel.sha256(ca_bundle), "java_trust_store_sha256": kernel.sha256(java_trust_store)})
        self.assertFalse((artifacts / "kernel" / ca_bundle.name).exists())
        self.assertFalse((artifacts / "kernel" / java_trust_store.name).exists())

    def make_fixture(self):
        helper = self.root / "tools/bazel/ci/kernel.py"
        helper.parent.mkdir(parents=True)
        shutil.copyfile(Path(kernel.__file__), helper)
        (self.root / "src/sonic-linux-kernel").mkdir(parents=True, exist_ok=True)
        (self.root / "src/sonic-linux-kernel/Makefile").write_text(
            ".DEFAULT_GOAL := fail\n.PHONY: fail\nfail:\n\t@touch COMPILED\n\t@false\n")
        output = self.root / "target/debs/trixie"
        output.mkdir(parents=True)
        (self.root / ".platform").touch()
        slave = (ROOT / "slave.mk").read_text()
        copy_recipe = slave[slave.index("# Copy debian packages from local directory"):slave.index("# Copy regular files from local directory")]
        derived_recipe = slave[slave.index("# Rules for derived debian packages"):slave.index("# Rules for extra debian packages")]
        functions = (ROOT / "rules/functions").read_text()
        macro_start = functions.index("define add_derived_package\n")
        derived_macro = functions[macro_start:functions.index("endef", macro_start) + len("endef")]
        makefile = self.root / "test.mk"
        makefile.write_text(
            ".DEFAULT_GOAL := all\n.SHELLFLAGS := -ec\n.ONESHELL:\n.SECONDEXPANSION:\n"
            "SRC_PATH := src\nDEBS_PATH := target/debs/trixie\n"
            "CONFIGURED_ARCH := amd64\nCONFIGURED_PLATFORM := vs\nBLDENV := trixie\n"
            "SECURE_UPGRADE_MODE := no_sign\nSONIC_BAZEL_KERNEL_PACKAGES := target/bazel-kernel-inputs\n"
            + derived_macro + "\n"
            "include rules/linux-kernel.mk\ninclude rules/linux-kernel.dep\n"
            "all: $(DEBS_PATH)/$(LINUX_HEADERS)\n"
            "\t@test -z '$(SONIC_MAKE_DEBS)'\n"
            "\t@test '$($(LINUX_HEADERS_COMMON)_CACHE_MODE)' = none\n"
            + copy_recipe + derived_recipe)
        return makefile

    def test_real_native_copy_recipe_imports_all_packages_without_kernel_submake(self):
        makefile = self.make_fixture()
        unrelated = self.root / "target/debs/trixie/unrelated.deb"
        unrelated.write_bytes(b"keep unrelated native output")
        for name in self.packages:
            (self.root / "target/debs/trixie" / name).write_bytes(b"stale package")
        result = subprocess.run(["make", "--no-print-directory", "--no-builtin-rules", "-f", str(makefile)],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("cp target/bazel-kernel-inputs/"), 4)
        self.assertFalse((self.root / "src/sonic-linux-kernel/COMPILED").exists())
        self.assertEqual(unrelated.read_bytes(), b"keep unrelated native output")
        for name in self.packages:
            self.assertEqual((self.bundle / name).read_bytes(), (self.root / "target/debs/trixie" / name).read_bytes())
        # Even with up-to-date copied DEBs, a changed source identity must fail
        # before the copy recipe can execute again.
        identity = self.root / "src/sonic-linux-kernel/tools/bazel/kernel_action.py"
        identity.write_text("print('" + "0" * 64 + "')\n")
        result = subprocess.run(["make", "--no-print-directory", "-f", str(makefile)],
                                cwd=self.root, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source inputs differ", result.stderr)
        self.assertNotIn("cp target/bazel-kernel-inputs/", result.stdout)

    def test_make_import_fails_on_corruption_without_compilation_fallback(self):
        makefile = self.make_fixture()
        (self.bundle / next(iter(self.packages))).write_bytes(b"broken")
        result = subprocess.run(["make", "--no-print-directory", "-f", str(makefile)],
                                cwd=self.root, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256", result.stderr)
        self.assertFalse((self.root / "src/sonic-linux-kernel/COMPILED").exists())
        self.assertEqual(list((self.root / "target/debs/trixie").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
