#!/usr/bin/env python3
"""Check kernel bundle integrity and the real Make copy recipe without Bazel."""

import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import kernel


class KernelBundleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / kernel.KERNEL_PATH
        self.source.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        (self.source / "Makefile").write_text(".DEFAULT_GOAL := fail\n.PHONY: fail\nfail:\n\t@touch COMPILED\n\t@false\n")
        (self.source / "manage-config").write_text("fixture configuration\n")
        for name in ("config.local", "patches-debian", "patches-sonic"):
            (self.source / name).mkdir()
        self.files = [{"name": name, "mode": 0o644, "sha256": kernel.sha256(self.source / name)}
                      for name in ("Makefile", "manage-config")]
        identity = hashlib.sha256(json.dumps(self.files, sort_keys=True).encode()).hexdigest()
        source_tools = self.source / "tools/bazel"
        source_tools.mkdir(parents=True)
        (source_tools / "kernel_action.py").write_text("print('" + identity + "')\n")
        (source_tools / "build.py").write_text(
            'WORKER_IMAGE = "debian:trixie@sha256:' + "a" * 64 + '"\n'
            'BAZEL_URL = "https://releases.bazel.build/8.5.1/release/bazel-8.5.1-linux-x86_64"\n'
            'BAZEL_SHA256 = "' + "b" * 64 + '"\n'
            "raise SystemExit('fixture does not run Bazel')\n")
        (self.source / ".bazelversion").write_text("8.5.1\n")
        (source_tools / "sources.bzl").write_text('''KERNEL_SOURCES = {
    "kernel_dsc": struct(name = "linux_6.12.41-1.dsc", sha256 = "''' + "d" * 64 + '''"),
    "kernel_orig": struct(name = "linux_6.12.41.orig.tar.xz", sha256 = "''' + "e" * 64 + '''"),
    "kernel_debian": struct(name = "linux_6.12.41-1.debian.tar.xz", sha256 = "''' + "f" * 64 + '''"),
}
''')
        module = (ROOT / "tools/bazel/kernel/MODULE.bazel").read_text()
        infra_line = next(line for line in module.splitlines() if 'name = "sonic-build-infra"' in line)
        (self.source / "MODULE.bazel").write_text('module(name = "sonic-linux-kernel", version = "0.0.1")\n' + infra_line + "\n")
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        self.commit(self.source, "Fixture kernel source")
        self.gitlink = kernel.git(self.source, "rev-parse", "HEAD")
        (self.root / "rules").mkdir()
        for name in ("linux-kernel.mk", "linux-kernel.dep"):
            shutil.copyfile(ROOT / "rules" / name, self.root / "rules" / name)
        shutil.copyfile(ROOT / "Makefile.work", self.root / "Makefile.work")
        shutil.copyfile(ROOT / ".gitmodules", self.root / ".gitmodules")
        shutil.copytree(ROOT / "tools/bazel/kernel", self.root / "tools/bazel/kernel")
        ci = self.root / "tools/bazel/ci"
        ci.mkdir(parents=True)
        for name in ("kernel.py", "resolution.py"):
            shutil.copyfile(ROOT / "tools/bazel/ci" / name, ci / name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", ".gitmodules", "Makefile.work", "rules", "tools"], check=True)
        subprocess.run(["git", "-C", str(self.root), "update-index", "--add", "--cacheinfo",
                        "160000," + self.gitlink + "," + str(kernel.KERNEL_PATH)], check=True)
        self.commit(self.root, "Fixture image source")
        self.state = kernel.source_state(self.root)
        self.bundle = self.root / kernel.INPUTS
        self.bundle.mkdir(parents=True)
        self.config, self.packages = kernel.contract(self.root)
        tool_inputs = ["a" * 64, "b" * 64]
        tool_identity = hashlib.sha256(json.dumps({"architecture": "amd64", "inputs": tool_inputs},
                                                  sort_keys=True).encode()).hexdigest()
        self.manifest = dict(self.config, source_date_epoch=1754969284, source_files=self.files,
                             source_tree_sha256=identity, source_archives=self.state["source_archives"],
                             build_tools={"schema_version": 1, "kind": "debian-build-tools",
                                          "architecture": "amd64", "identity_sha256": tool_identity,
                                          "input_sha256": tool_inputs, "packages": {"make": "4.4.1-2"}}, packages=[])
        self.package_root = self.root / "package-root"
        control = self.package_root / "DEBIAN/control"
        control.parent.mkdir(parents=True)
        for name, metadata in self.packages.items():
            control.write_text("".join(key.title() + ": " + value + "\n" for key, value in metadata.items())
                               + "Maintainer: Fixture <fixture@example.test>\nDescription: Test kernel package\n")
            path = self.bundle / name
            subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(self.package_root), str(path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.manifest["packages"].append(dict(metadata, name=name, sha256=kernel.sha256(path), size=path.stat().st_size))
        self.selected = {"kernel_source_kind": "local_override", "kernel_source_commit": self.gitlink,
                         "modules": self.state["modules"], "registry": kernel.DRAFT_REGISTRY,
                         "infra_source_commit": self.state["modules"]["sonic-build-infra"].rsplit("-", 1)[1],
                         "graph_sha256": "a" * 64, "lock_sha256": "b" * 64,
                         "infra_source_json_sha256": "c" * 64,
                         "graph_inspection": {"module_graph_complete": True}}
        self.provenance = {"schema": 2, "source_commit": self.state["source_commit"], "kernel_gitlink": self.gitlink,
                           "modules": self.state["modules"], "launcher": self.state["launcher"],
                           "target": kernel.TARGET, "resolution": self.selected}
        self.write_manifest()

    def commit(self, workspace, message):
        subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm", message], check=True)

    def write_manifest(self):
        (self.bundle / kernel.MANIFEST).write_text(json.dumps(self.manifest))
        self.provenance["manifest_sha256"] = kernel.sha256(self.bundle / kernel.MANIFEST)
        (self.bundle / kernel.PROVENANCE).write_text(json.dumps(self.provenance))

    def resolution_files(self, workdir, consumer):
        workdir.mkdir(parents=True, exist_ok=True)
        version = self.state["modules"]["sonic-build-infra"]
        graph = {"key": "<root>", "root": True, "dependencies": [
            {"key": "sonic-linux-kernel@_"}, {"key": "sonic-build-infra@" + version}]}
        (workdir / "module-graph.json").write_text(json.dumps(graph))
        (workdir / "module-graph.stderr").write_text("")
        (workdir / "module-graph.exit-code").write_text("0\n")
        commit = version.rsplit("-", 1)[1]
        metadata = json.dumps({"url": "https://github.com/securely1g/sonic-build-infra/archive/" + commit + ".tar.gz",
                               "strip_prefix": "sonic-build-infra-" + commit,
                               "integrity": "sha256-" + base64.b64encode(b"x" * 32).decode()}).encode()
        url = kernel.DRAFT_REGISTRY + "/modules/sonic-build-infra/" + version + "/source.json"
        (consumer / "MODULE.bazel.lock").write_text(json.dumps({"registryFileHashes": {
            url: hashlib.sha256(metadata).hexdigest()}}))
        return metadata

    def test_verified_bundle_copies_without_sharing_inodes(self):
        result = kernel.verify(self.bundle, self.root)
        self.assertEqual(result["manifest"], self.manifest)
        copied = self.root / "staged"
        self.assertEqual(kernel.copy_bundle(self.bundle, copied, self.root), result)
        for path in self.bundle.iterdir():
            self.assertEqual(path.read_bytes(), (copied / path.name).read_bytes())
            self.assertNotEqual(path.stat().st_ino, (copied / path.name).stat().st_ino)
        with self.assertRaises(FileExistsError):
            kernel.copy_bundle(self.bundle, copied, self.root)

    def test_corrupt_missing_extra_and_symlink_outputs_are_rejected(self):
        path = self.bundle / next(iter(self.packages))
        original = path.read_bytes()
        path.write_bytes(original + b"corrupt")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            kernel.verify(self.bundle, self.root)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unexpected"):
            kernel.verify(self.bundle, self.root)
        replacement = self.root / "other.deb"
        replacement.write_bytes(original)
        path.symlink_to(replacement)
        with self.assertRaisesRegex(ValueError, "regular kernel output"):
            kernel.verify(self.bundle, self.root)
        path.unlink()
        path.write_bytes(original)
        (self.bundle / "stale.deb").write_bytes(b"stale")
        with self.assertRaisesRegex(ValueError, "missing or unexpected"):
            kernel.verify(self.bundle, self.root)

    def test_changed_contract_source_archives_tools_and_metadata_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        mutations = [
            lambda value: value.update(kernel_abi="other-abi"),
            lambda value: value.update(signing="signed"),
            lambda value: value.update(source_tree_sha256="0" * 64),
            lambda value: value["source_files"][0].update(sha256="0" * 64),
            lambda value: value["source_archives"][0].update(sha256="0" * 64),
            lambda value: value["build_tools"]["input_sha256"].__setitem__(0, "0" * 64),
            lambda value: value["packages"].pop(),
            lambda value: value["packages"][0].update(architecture="arm64"),
        ]
        for mutation in mutations:
            self.manifest = copy.deepcopy(original)
            mutation(self.manifest)
            self.write_manifest()
            with self.subTest(manifest=self.manifest), self.assertRaises(ValueError):
                kernel.verify(self.bundle, self.root)

    def test_deb_control_metadata_is_checked_even_when_hash_matches(self):
        item = self.manifest["packages"][0]
        control = self.package_root / "DEBIAN/control"
        control.write_text("Package: unrelated\nVersion: 1\nArchitecture: all\n"
                           "Maintainer: Fixture <fixture@example.test>\nDescription: Unexpected package\n")
        path = self.bundle / item["name"]
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(self.package_root), str(path)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        item.update(size=path.stat().st_size, sha256=kernel.sha256(path))
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "DEB control metadata"):
            kernel.verify(self.bundle, self.root)

    def test_stale_source_and_provenance_are_rejected(self):
        self.provenance["kernel_gitlink"] = "0" * 40
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "provenance differs"):
            kernel.verify(self.bundle, self.root)
        self.provenance["kernel_gitlink"] = self.gitlink
        self.write_manifest()
        (self.source / "Makefile").write_text("changed source\n")
        with self.assertRaisesRegex(ValueError, "inputs must match|checkout must have"):
            kernel.verify(self.bundle, self.root)

    def test_resolution_binds_local_kernel_and_exact_tools_source(self):
        workdir, consumer = self.root / "resolution", self.root / "consumer"
        consumer.mkdir()
        metadata = self.resolution_files(workdir, consumer)
        selected = kernel.inspect_resolution(workdir, consumer, self.state, kernel.DRAFT_REGISTRY,
                                             fetch=lambda _url: metadata)
        self.assertEqual(selected["kernel_source_kind"], "local_override")
        self.assertEqual(selected["kernel_source_commit"], self.gitlink)
        graph = json.loads((workdir / "module-graph.json").read_text())
        graph["dependencies"][1]["key"] = "sonic-build-infra@0.0.1-other"
        (workdir / "module-graph.json").write_text(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "different kernel tools module"):
            kernel.inspect_resolution(workdir, consumer, self.state, kernel.DRAFT_REGISTRY,
                                      fetch=lambda _url: metadata)

    def test_producer_uses_separate_source_override_and_retains_evidence(self):
        prebuilt = self.bundle
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            kernel.plan(self.root, self.bundle / "state", None, False, True)
        calls = []
        metadata = None

        def execute(command, workspace):
            nonlocal metadata
            calls.append(command)
            self.assertEqual(workspace, self.root)
            consumer = Path(command[command.index("--workspace") + 1])
            workdir = Path(command[command.index("--work-dir") + 1])
            self.assertIn(kernel.DRAFT_REGISTRY, (consumer / ".bazelrc").read_text())
            self.assertNotIn(kernel.DRAFT_REGISTRY, (self.root / "tools/bazel/kernel/.bazelrc").read_text())
            outputs = workdir / "outputs"
            outputs.mkdir(parents=True)
            for name in [kernel.MANIFEST, *self.packages]:
                shutil.copyfile(prebuilt / name, outputs / name)
            (workdir / "output-paths.txt").write_text("".join(
                "/work/outputs/" + name + "\n" for name in [kernel.MANIFEST, *self.packages]))
            metadata = self.resolution_files(workdir, consumer)
            (workdir / "invocation.json").write_text(json.dumps({
                "schema": 1, "target": kernel.TARGET, "worker": self.state["launcher"]["worker"],
                "remote_cache": "http://127.0.0.1:8080", "remote_cache_read_only": True, "disk_cache": None}))

        receipt = kernel.build(self.root, self.root / "state", "http://127.0.0.1:8080", False, True,
                               execute=execute, fetch=lambda _url: metadata)
        command = calls[0]
        self.assertIn("--remote-cache-read-only", command)
        self.assertNotIn("--disk-cache", command)
        self.assertEqual(command[command.index("--target") + 1], kernel.TARGET)
        self.assertEqual(command[command.index("--module-override") + 1], "sonic-linux-kernel=" + str(self.source))
        self.assertEqual(receipt["result"]["manifest"], self.manifest)
        self.assertEqual(receipt["plan"]["bundle"], str(self.root / "state/bundle"))
        self.assertEqual(kernel.verify(self.bundle, self.root)["manifest"], self.manifest)
        self.assertTrue((self.root / "state/bundle/kernel-provenance.json").is_file())
        self.assertTrue((self.root / "state/plan.json").is_file())
        self.assertTrue((self.root / "state/kernel-receipt.json").is_file())
        self.assertFalse((self.root / "tools/bazel/kernel/MODULE.bazel.lock").exists())

    def make_fixture(self):
        output = self.root / "target/debs/trixie"
        output.mkdir(parents=True)
        (self.root / ".platform").touch()
        slave = (ROOT / "slave.mk").read_text()
        copy_recipe = slave[slave.index("# Copy debian packages from local directory"):slave.index("# Copy regular files from local directory")]
        derived_recipe = slave[slave.index("# Rules for derived debian packages"):slave.index("# Rules for extra debian packages")]
        functions = (ROOT / "rules/functions").read_text()
        start = functions.index("define add_derived_package\n")
        derived_macro = functions[start:functions.index("endef", start) + len("endef")]
        makefile = self.root / "test.mk"
        makefile.write_text(
            ".DEFAULT_GOAL := all\n.SHELLFLAGS := -ec\n.ONESHELL:\n.SECONDEXPANSION:\n"
            "SRC_PATH := src\nDEBS_PATH := target/debs/trixie\n"
            "CONFIGURED_ARCH := amd64\nCONFIGURED_PLATFORM := vs\nBLDENV := trixie\n"
            "SECURE_UPGRADE_MODE := no_sign\nSONIC_BAZEL_KERNEL_PACKAGES := target/bazel-kernel-inputs\n"
            + derived_macro + "\ninclude rules/linux-kernel.mk\ninclude rules/linux-kernel.dep\n"
            "all: $(DEBS_PATH)/$(LINUX_HEADERS)\n"
            "\t@test -z '$(SONIC_MAKE_DEBS)'\n"
            "\t@test '$($(LINUX_HEADERS_COMMON)_CACHE_MODE)' = none\n"
            + copy_recipe + derived_recipe)
        return makefile

    def test_real_make_copy_recipe_imports_all_packages_and_rechecks_stale_source(self):
        makefile = self.make_fixture()
        unrelated = self.root / "target/debs/trixie/unrelated.deb"
        unrelated.write_bytes(b"unrelated native output")
        for name in self.packages:
            (self.root / "target/debs/trixie" / name).write_bytes(b"stale package")
        command = ["make", "--no-print-directory", "--no-builtin-rules", "-f", str(makefile)]
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("cp target/bazel-kernel-inputs/"), 4)
        self.assertFalse((self.source / "COMPILED").exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated native output")
        for name in self.packages:
            self.assertEqual((self.bundle / name).read_bytes(), (self.root / "target/debs/trixie" / name).read_bytes())
        (self.source / "Makefile").write_text("changed source\n")
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("cp target/bazel-kernel-inputs/", result.stdout)
        self.assertFalse((self.source / "COMPILED").exists())

    def test_make_rejects_unsupported_modes_before_copying(self):
        makefile = self.make_fixture()
        cases = [
            ("CONFIGURED_PLATFORM=mellanox", "AMD64 Trixie VS"),
            ("CONFIGURED_ARCH=arm64", "AMD64 Trixie VS"),
            ("BLDENV=bookworm", "AMD64 Trixie VS"),
            ("CROSS_BUILD_ENVIRON=y", "native AMD64 execution"),
            ("MULTIARCH_QEMU_ENVIRON=y", "native AMD64 execution"),
            ("KERNEL_VERSION=6.12.42", "6.12.41-1 +deb13 sonic contract"),
            ("KVERSION=other-abi", "KVERSION=6.12.41+deb13-sonic-amd64"),
            ("ADDITIONAL_BUILD_PROFILES=other", "default kernel build profiles"),
            ("SECURE_UPGRADE_MODE=sign", "SECURE_UPGRADE_MODE=no_sign"),
            ("INCLUDE_EXTERNAL_PATCHES=y", "external platform patches"),
            ("SONIC_BAZEL_KERNEL_PACKAGES=/other", "target/bazel-kernel-inputs"),
        ]
        for setting, message in cases:
            result = subprocess.run(["make", "--no-print-directory", "-f", str(makefile), setting],
                                    cwd=self.root, text=True, capture_output=True)
            with self.subTest(setting=setting):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertNotIn("cp target/bazel-kernel-inputs/", result.stdout)

    def test_make_forwards_kernel_selector_to_the_slave_command(self):
        contents = (ROOT / "Makefile.work").read_text()
        definition = "SONIC_BUILD_INSTRUCTION :=" + contents.split("SONIC_BUILD_INSTRUCTION :=", 1)[1].split("\n\n", 1)[0]
        makefile = self.root / "forward.mk"
        makefile.write_text("SONIC_BAZEL_KERNEL_PACKAGES := target/bazel-kernel-inputs\n" + definition
                            + "\n.PHONY: check\ncheck:\n\t$(file >forwarded.txt,$(SONIC_BUILD_INSTRUCTION))\n\t@true\n")
        for distro, value in (("trixie", "target/bazel-kernel-inputs"), ("bookworm", "")):
            subprocess.run(["make", "--no-print-directory", "-f", str(makefile), "check", "BLDENV=" + distro],
                           cwd=self.root, check=True)
            self.assertIn("SONIC_BAZEL_KERNEL_PACKAGES=" + value,
                          (self.root / "forwarded.txt").read_text().split())


class KernelConfigurationTest(unittest.TestCase):
    def test_registry_defaults_to_main_and_draft_selection_replaces_it(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            shutil.copyfile(ROOT / "tools/bazel/kernel/.bazelrc", workspace / ".bazelrc")
            original = (workspace / ".bazelrc").read_text()
            self.assertEqual(kernel.configure_registry(workspace, False), kernel.DEFAULT_REGISTRY)
            self.assertEqual((workspace / ".bazelrc").read_text(), original)
            self.assertEqual(kernel.configure_registry(workspace, True), kernel.DRAFT_REGISTRY)
            selected = (workspace / ".bazelrc").read_text()
            self.assertEqual(selected.count(kernel.REGISTRY_PREFIX), 1)
            self.assertIn("common --registry=https://bcr.bazel.build", selected)

    def test_native_version_change_requires_a_kernel_contract_update(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "rules").mkdir()
            contents = (ROOT / "rules/linux-kernel.mk").read_text().replace("KERNEL_VERSION = 6.12.41", "KERNEL_VERSION = 6.12.42")
            (workspace / "rules/linux-kernel.mk").write_text(contents)
            with self.assertRaisesRegex(ValueError, "supports only the 6.12.41-1"):
                kernel.contract(workspace)

    def test_cache_endpoint_rejects_credentials_and_control_characters(self):
        for value in ("http://127.0.0.1:8080", "https://cache.example.test", "grpcs://cache.example.test:9092"):
            self.assertEqual(kernel.endpoint(value), value)
        for value in ("https://token@cache.example.test", "https://cache.example.test?token=private",
                      "https://cache.example.test/#secret", "file:///cache", "https://cache.example.test\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                kernel.endpoint(value)


if __name__ == "__main__":
    unittest.main()
