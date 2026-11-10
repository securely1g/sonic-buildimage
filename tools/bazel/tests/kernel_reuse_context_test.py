"""Metadata and Make forwarding tests; no installable DEBs or builds are made."""

import copy
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.kernel import reuse_context as context


class KernelReuseContextTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.consumer = self.root / "consumer"
        self.producer = self.root / "producer"
        self.catalog = self.root / "registry"
        self.bundle = self.root / "existing"
        for path in (self.consumer, self.producer, self.catalog, self.bundle):
            path.mkdir()
        self.settings = {key: "fixture" for key in context.SETTINGS}
        self.state = {key: {} for key in context.PRODUCTION_FIELDS}
        self.state.update(kernel_config=self.settings.copy(), consumer_inputs_sha256="b" * 64)
        self.provenance = {"source_commit": "1" * 40, "consumer_inputs_sha256": "b" * 64,
                           "resolution": {"registry": "original-registry"},
                           "kernel_config": self.settings.copy()}
        self.manifest_path = self.bundle / "kernel-packages.json"
        self.provenance_path = self.bundle / "kernel-provenance.json"
        self.manifest_path.write_text('{"test_only": true}\n')
        self.provenance_path.write_text(json.dumps(self.provenance))
        self.document = {
            "schema": 1,
            "producer": {"commit": "1" * 40, "prepare_sha256": "a" * 64,
                         "inputs_sha256": "b" * 64, "registry": "original-registry",
                         "manifest_sha256": context.digest(self.manifest_path.read_bytes()),
                         "provenance_sha256": context.digest(self.provenance_path.read_bytes())},
            "consumer": {"commit": "2" * 40, "inputs_sha256": "b" * 64,
                         "registry": "immutable-consumer-registry"},
            "catalog": {"commit": "3" * 40}, "differences": [],
        }
        self.document_path = self.root / "reviewed-context.json"
        self.api = SimpleNamespace(
            __file__=str(self.consumer / "tools/bazel/kernel/prepare.py"),
            MANIFEST="kernel-packages.json", PROVENANCE="kernel-provenance.json", WATCHED=["fixture"],
            git=mock.Mock(side_effect=lambda root, *args: "1" * 40 if root == self.producer else "2" * 40),
            source_state=mock.Mock(return_value=copy.deepcopy(self.state)),
            contract=mock.Mock(return_value=({"package": "fixture"}, {})),
        )
        self.original = SimpleNamespace(
            WATCHED=["fixture", "tools/bazel/debian_mirrors.cfg"],
            source_state=mock.Mock(return_value=copy.deepcopy(self.state)),
            contract=mock.Mock(return_value=({"package": "fixture"}, {})),
            verify=mock.Mock(return_value={"original": "result"}),
        )

    def invoke(self):
        self.document_path.write_text(json.dumps(self.document))
        expected = context.digest(self.document_path.read_bytes())
        with mock.patch.object(context, "inventory", return_value=({}, "b" * 64)), \
                mock.patch.object(context, "__file__", str(self.consumer / "tools/bazel/kernel/reuse_context.py")), \
                mock.patch.object(context, "load_original", return_value=self.original), \
                mock.patch.object(context, "verify_catalog"), \
                mock.patch("subprocess.check_output", side_effect=AssertionError("no command in metadata fixture")):
            return context.verify(
                self.api, self.bundle, self.consumer, context_path=self.document_path,
                context_sha256=expected, producer_workspace=self.producer,
                registry_workspace=self.catalog, architecture="amd64", kernel_config=self.settings)

    def test_original_source_api_and_metadata_are_preserved(self):
        before = [p.read_bytes() for p in (self.manifest_path, self.provenance_path)]
        def verify(*args, **kwargs):
            self.assertEqual(os.environ["SONIC_REVIEW_REGISTRY"], "original-registry")
            self.assertIs(kwargs["source"], self.original.source_state.return_value)
            self.assertEqual(args, (self.bundle, self.producer))
            return {"original": "result"}
        self.original.verify.side_effect = verify
        with mock.patch.dict(os.environ, {"SONIC_REVIEW_REGISTRY": "current-registry"}):
            self.assertEqual(self.invoke(), {"original": "result"})
            self.assertEqual(os.environ["SONIC_REVIEW_REGISTRY"], "current-registry")
        self.assertEqual(before, [p.read_bytes() for p in (self.manifest_path, self.provenance_path)])
        self.original.verify.assert_called_once()

    def test_original_package_rejection_is_preserved_and_registry_restored(self):
        self.original.verify.side_effect = ValueError("original package validation failed")
        with mock.patch.dict(os.environ, {"SONIC_REVIEW_REGISTRY": "current-registry"}):
            with self.assertRaisesRegex(ValueError, "original package validation failed"):
                self.invoke()
            self.assertEqual(os.environ["SONIC_REVIEW_REGISTRY"], "current-registry")

    def test_a_different_loaded_consumer_verifier_is_rejected(self):
        self.api.__file__ = str(self.producer / "tools/bazel/kernel/prepare.py")
        with self.assertRaisesRegex(ValueError, "explicitly selected consumer checkout"):
            self.invoke()
        self.original.verify.assert_not_called()

    def test_every_effective_setting_is_checked_against_original(self):
        for key in context.SETTINGS:
            with self.subTest(setting=key):
                self.api.source_state.return_value = copy.deepcopy(self.state)
                self.original.source_state.return_value = copy.deepcopy(self.state)
                self.api.source_state.return_value["kernel_config"][key] = "changed"
                self.original.source_state.return_value["kernel_config"][key] = "changed"
                with self.assertRaisesRegex(ValueError, "configuration differs"):
                    self.invoke()
        self.original.verify.assert_not_called()

    def test_production_changes_do_not_reach_package_acceptance(self):
        for key in context.PRODUCTION_FIELDS:
            with self.subTest(field=key):
                self.api.source_state.return_value = copy.deepcopy(self.state)
                self.api.source_state.return_value[key] = {"changed": True}
                with self.assertRaises(ValueError):
                    self.invoke()
        self.original.verify.assert_not_called()

    def test_missing_setting_and_changed_producer_fail(self):
        self.settings.pop("KVERSION")
        with self.assertRaisesRegex(ValueError, "every effective"):
            self.invoke()
        self.settings["KVERSION"] = "fixture"
        self.document["producer"]["commit"] = "9" * 40
        with self.assertRaisesRegex(ValueError, "producer commit"):
            self.invoke()
        self.original.verify.assert_not_called()

    def test_changed_original_metadata_is_not_relabelled(self):
        self.provenance_path.write_text('{"replacement": true}\n')
        with self.assertRaisesRegex(ValueError, "original metadata hash"):
            self.invoke()
        self.original.verify.assert_not_called()

    def test_context_hash_duplicate_keys_and_unknown_fields(self):
        self.document_path.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(ValueError, "SHA256 differs"):
            context.load_context(self.document_path, "0" * 64)
        self.document_path.write_text('{"schema": 1, "schema": 1}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            context.load_context(self.document_path, context.digest(self.document_path.read_bytes()))
        self.document["ignore_fingerprint"] = True
        self.document_path.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(ValueError, "unknown context fields"):
            context.load_context(self.document_path, context.digest(self.document_path.read_bytes()))

    def test_unreviewed_tuple_or_production_delta_is_rejected(self):
        old = {"tools/bazel/kernel/README.md": {"sha": "a"}}
        new = {"tools/bazel/kernel/README.md": {"sha": "b"}}
        review = [{"path": "tools/bazel/kernel/README.md", "producer": old[next(iter(old))],
                   "consumer": new[next(iter(new))], "reason": "documentation"}]
        context.compare_inventories(old, new, review)
        new[next(iter(new))] = {"sha": "different"}
        with self.assertRaisesRegex(ValueError, "pinned review"):
            context.compare_inventories(old, new, review)
        with self.assertRaisesRegex(ValueError, "production input"):
            context.compare_inventories({}, {"tools/bazel/kernel/MODULE.bazel": {"sha": "changed"}}, [])

    def test_original_code_is_not_loaded_before_hash_validation(self):
        path = self.producer / "tools/bazel/kernel/prepare.py"
        path.parent.mkdir(parents=True)
        path.write_text('raise RuntimeError("must not execute")\n')
        with mock.patch.object(context.importlib.util, "spec_from_file_location") as loader:
            with self.assertRaisesRegex(ValueError, "verifier source differs"):
                context.load_original(self.api, self.producer, "0" * 64)
            loader.assert_not_called()

    def test_materialized_watched_bytes_and_modes_match_the_pinned_tuple(self):
        path = self.consumer / "watched.txt"
        original = b"original watched input\n"
        path.write_bytes(original)
        path.chmod(0o644)
        oid = hashlib.sha1(b"blob " + str(len(original)).encode() + b"\0" + original).hexdigest()
        row = "100644 blob " + oid + "\twatched.txt"
        api = SimpleNamespace(git=mock.Mock(return_value=row))
        entries, fingerprint = context.inventory(api, self.consumer, ["watched.txt"])
        self.assertEqual(entries["watched.txt"]["sha"], oid)
        self.assertEqual(fingerprint, context.digest(row.encode()))
        path.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(ValueError, "immutable source"):
            context.inventory(api, self.consumer, ["watched.txt"])
        path.write_bytes(original)
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "mode differs"):
            context.inventory(api, self.consumer, ["watched.txt"])
        path.unlink()
        outside = self.root / "outside.txt"
        outside.write_bytes(original)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes checkout"):
            context.inventory(api, self.consumer, ["watched.txt"])

    def test_catalog_requires_original_descriptors_and_patch_bytes(self):
        prefix = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
        selected = {"sonic-linux-kernel": "0.0.1-fixture", "sonic-build-infra": "0.0.18-fixture"}
        provenance = {"resolution": {}}
        for module, key in (("sonic-linux-kernel", "kernel_source_json_sha256"),
                            ("sonic-build-infra", "infra_source_json_sha256")):
            entry = self.catalog / "modules" / module / selected[module]
            (entry / "patches").mkdir(parents=True)
            (entry / "patches/version.patch").write_bytes(b"metadata-only patch fixture\n")
            sri = "sha256-" + base64.b64encode(hashlib.sha256(b"metadata-only patch fixture\n").digest()).decode()
            descriptor = json.dumps({"patches": {"version.patch": sri}}).encode()
            (entry / "source.json").write_bytes(descriptor)
            provenance["resolution"][key] = context.digest(descriptor)

        def git(root, *args):
            if args == ("rev-parse", "HEAD"):
                return "3" * 40
            self.assertEqual(args[:4], ("ls-tree", "-r", "HEAD", "--"))
            rows = []
            for path in sorted(args[4:]):
                data = (root / path).read_bytes()
                oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                rows.append("100644 blob " + oid + "\t" + path)
            return "\n".join(rows)

        api = SimpleNamespace(git=git, REGISTRY_PREFIX=prefix, selected_registry=lambda: prefix + "3" * 40)
        self.document["consumer"]["registry"] = prefix + "3" * 40
        context.verify_catalog(api, self.catalog, self.document, {"modules": selected}, provenance)
        patch = self.catalog / "modules/sonic-linux-kernel/0.0.1-fixture/patches/version.patch"
        patch.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ValueError, "SRI"):
            context.verify_catalog(api, self.catalog, self.document, {"modules": selected}, provenance)
        api.selected_registry = lambda: prefix + "main"
        with self.assertRaisesRegex(ValueError, "explicit immutable catalog"):
            context.verify_catalog(api, self.catalog, self.document, {"modules": selected}, provenance)

    def test_build_modes_reject_context_before_any_preparation(self):
        for mode in ("plan", "build", "ensure"):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/bazel/kernel/prepare.py"),
                                     mode, "--producer-context", "missing"],
                                    cwd=self.root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertIn("producer-context options require verify", result.stderr)

    def test_direct_cli_loads_sibling_before_rejecting_a_wrong_context_hash(self):
        self.document_path.write_text("{}\n")
        argv = [sys.executable, "-I", "-B", str(ROOT / "tools/bazel/kernel/prepare.py"),
                "verify", "--workspace", str(self.consumer), "--debs-directory", str(self.bundle),
                "--architecture", "amd64", "--producer-context", str(self.document_path),
                "--producer-context-sha256", "0" * 64,
                "--producer-workspace", str(self.producer), "--registry-workspace", str(self.catalog)]
        for key in sorted(context.SETTINGS):
            argv += ["--kernel-config", key + "=fixture"]
        result = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("producer context SHA256 differs", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


class KernelMakeContextForwardingTest(unittest.TestCase):
    def test_actual_recipe_runs_with_archive_old_file_options_and_propagates_failure(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "rules").mkdir()
            shutil.copyfile(ROOT / "rules/linux-kernel.mk", root / "rules/linux-kernel.mk")
            (root / "tools/bazel/kernel").mkdir(parents=True)
            # This recorder writes JSON only. It cannot create/install a DEB.
            (root / "tools/bazel/kernel/prepare.py").write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                "assert sys.argv[1]=='verify'\n"
                "Path('verify-argv.json').write_text(json.dumps(sys.argv[1:]))\n"
                "raise SystemExit(int(os.environ.get('VERIFY_FAIL','0')))\n")
            (root / "Makefile").write_text(
                "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y\nSONIC_BAZEL_KERNEL_PREPARE=verify\n"
                "CONFIGURED_ARCH=amd64\nCONFIGURED_PLATFORM=vs\nBLDENV=trixie\nDEBS_PATH=debs\n"
                "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT=reviewed context.json\n"
                "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT_SHA256=" + "a" * 64 + "\n"
                "SONIC_BAZEL_KERNEL_PRODUCER_WORKSPACE=original checkout\n"
                "SONIC_BAZEL_KERNEL_REGISTRY_WORKSPACE=registry checkout\n"
                "include rules/linux-kernel.mk\n"
                ".PHONY: all\nall: $(SONIC_BAZEL_KERNEL_INPUTS) image.tar.gz debug.tar.gz\n"
                "image.tar.gz debug.tar.gz &:\n\t@echo Unexpected archive generation; exit 99\n")
            # Non-installable text stand-ins exercise only the canonical Make
            # dependency and argument forwarding; no package tool is invoked.
            (root / "debs").mkdir()
            for stem, arch in (("linux-headers-6.12.41+deb13-common-sonic", "all"),
                               ("linux-kbuild-6.12.41+deb13", "amd64"),
                               ("linux-image-6.12.41+deb13-sonic-amd64-unsigned", "amd64"),
                               ("linux-headers-6.12.41+deb13-sonic-amd64", "amd64"),
                               ("linux-image-6.12.41+deb13-sonic-amd64-dbg", "amd64")):
                (root / "debs" / (stem + "_6.12.41-1_" + arch + ".deb")).write_text("not a Debian package\n")
            for file in ("debs/kernel-packages.json", "debs/kernel-provenance.json", "image.tar.gz", "debug.tar.gz"):
                (root / file).write_text("metadata-only test stand-in\n")
            argv = ["make", "--no-print-directory", "--old-file=image.tar.gz", "--old-file=debug.tar.gz", "all"]
            result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads((root / "verify-argv.json").read_text())
            for flag, value in (("--producer-context", "reviewed context.json"),
                                ("--producer-context-sha256", "a" * 64),
                                ("--producer-workspace", "original checkout"),
                                ("--registry-workspace", "registry checkout")):
                self.assertEqual(actual[actual.index(flag) + 1], value)
            settings = [actual[i + 1].split("=", 1)[0] for i, item in enumerate(actual) if item == "--kernel-config"]
            self.assertEqual(set(settings), context.SETTINGS)
            self.assertEqual(len(settings), 16)
            variables = ("SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT", "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT_SHA256",
                         "SONIC_BAZEL_KERNEL_PRODUCER_WORKSPACE", "SONIC_BAZEL_KERNEL_REGISTRY_WORKSPACE")
            result = subprocess.run(argv + [key + "=" for key in variables], cwd=root,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("--producer-context", json.loads((root / "verify-argv.json").read_text()))
            before = (root / "verify-argv.json").read_bytes()
            result = subprocess.run(argv + [variables[-1] + "="], cwd=root,
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing " + variables[-1], result.stderr)
            self.assertEqual((root / "verify-argv.json").read_bytes(), before)
            result = subprocess.run(argv, cwd=root, env={**os.environ, "VERIFY_FAIL": "17"},
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Unexpected archive generation", result.stdout)


if __name__ == "__main__":
    unittest.main()
