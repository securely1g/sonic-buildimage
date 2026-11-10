"""Pure/mocked planner checks and a toy Make grouped-target check; no DEBs."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).parents[1] / "ci" / "vs_reuse_plan.py"
spec = importlib.util.spec_from_file_location("vs_reuse_plan_under_test", SOURCE)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class ReusePlanTests(unittest.TestCase):
    def test_original_provenance_without_hash_is_preserved(self):
        original_request = b'{"image":"gearbox","request_attempt":1}\n'
        provenance = {"artifact": {"id": 123}, "origin": {"id": 456}}
        original_bytes = json.dumps(provenance, sort_keys=True).encode()
        bound = plan.request_provenance(provenance, original_request)
        self.assertEqual(bound["request_sha256"], hashlib.sha256(original_request).hexdigest())
        self.assertEqual({key: value for key, value in bound.items() if key != "request_sha256"}, provenance)
        self.assertEqual(json.dumps(provenance, sort_keys=True).encode(), original_bytes)
        self.assertNotIn("request_sha256", provenance)

    def test_supplied_matching_request_hash_is_preserved(self):
        original_request = b'{"image":"syncd","request_attempt":2}\n'
        provenance = {"origin": {"id": 456},
                      "request_sha256": hashlib.sha256(original_request).hexdigest()}
        original_bytes = json.dumps(provenance, sort_keys=True).encode()
        bound = plan.request_provenance(provenance, original_request)
        self.assertEqual(bound, provenance)
        self.assertIsNot(bound, provenance)
        self.assertEqual(json.dumps(provenance, sort_keys=True).encode(), original_bytes)

    def test_supplied_request_hash_mismatch_rejected_without_rewrite(self):
        original_request = b'{"image":"p4rt","request_attempt":1}\n'
        changed_request = b'{"image":"p4rt","request_attempt":2}\n'
        for supplied in (hashlib.sha256(changed_request).hexdigest(), None, ""):
            provenance = {"origin": {"id": 456}, "request_sha256": supplied}
            original_bytes = json.dumps(provenance, sort_keys=True).encode()
            with self.subTest(supplied=supplied), self.assertRaisesRegex(ValueError, "Supplied provenance request hash"):
                plan.request_provenance(provenance, original_request)
            self.assertEqual(json.dumps(provenance, sort_keys=True).encode(), original_bytes)

    def test_current_domain_and_native_flags(self):
        self.assertEqual(plan.HEAD, "cf07bf776521e28063cb316713355391b55e6670")
        self.assertEqual(plan.WORKER, "87b459c50a4ee8c90679a1b3756a5bb724c6d765")
        for value in ("SONIC_IMAGE_VERSION=bazel-ci", "CROSS_BUILD_ENVIRON=n",
                      "MULTIARCH_QEMU_ENVIRON=n", "SONIC_BAZEL_KERNEL_PREPARE=verify",
                      "SONIC_DEB_BUILD_POLICY=reuse-only", "SONIC_DPKG_CACHE_METHOD=rcache"):
            self.assertIn(value, plan.FLAGS)

    def test_duplicate_and_nonfinite_json_rejected(self):
        for raw in ('{"schema":1,"schema":1}', '{"value":NaN}', '{"value":Infinity}'):
            with self.assertRaises(ValueError):
                plan.strict_json(raw)

    def test_make_expression_path_rejected(self):
        for path in ("/tmp/$(shell-id)", "/tmp/one two", "/tmp/../other", "relative"):
            with self.assertRaises(ValueError):
                plan.make_path(path)

    def test_symlink_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "real").mkdir(); (root / "link").symlink_to(root / "real")
            with self.assertRaises(ValueError):
                plan.make_path(root / "link", directory=True)

    def test_only_archive_targets_may_be_old_file(self):
        for name in ("target/debs/trixie/kernel.deb", "bazel-kernel-ensure", "target/sonic-vs.bin",
                     "src/sonic-build-hooks/all", "target/docker-x.gz --old-file=other"):
            with self.assertRaises(ValueError):
                plan.make_command([name], [])

    def test_command_preserves_kernel_forwarding(self):
        kernel = ["SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT=/inputs/original-context.json",
                  "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT_SHA256=" + plan.KERNEL_CONTEXT,
                  "SONIC_BAZEL_KERNEL_PRODUCER_WORKSPACE=/inputs/original-source",
                  "SONIC_BAZEL_KERNEL_REGISTRY_WORKSPACE=/inputs/catalog",
                  "SONIC_REVIEW_REGISTRY=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" + plan.CATALOG]
        command = plan.make_command(["target/docker-x.gz", "target/docker-x-dbg.gz"], kernel)
        self.assertEqual(command[-len(kernel):], kernel)
        self.assertEqual([x for x in command if x.startswith("--old-file=")],
                         ["--old-file=target/docker-x-dbg.gz", "--old-file=target/docker-x.gz"])
        self.assertNotIn("verify_vs_oci.sh", " ".join(command))
        self.assertNotIn("bazel", command)
        self.assertIn("target/sonic-vs.bin", command)

    def archive_fixture(self):
        rows = {"target/docker-test-" + str(i) + ".gz": {"bytes": 1, "sha256": "a" * 64}
                for i in range(42)}
        rows["target/docker-nat.gz"] = {"bytes": 1, "sha256": "b" * 64}
        receipt = {"archives": {Path(p).name[:-3]: {} for p in rows if p != "target/docker-nat.gz"},
                   "legacy": {"target/docker-nat.gz": rows["target/docker-nat.gz"]},
                   "files": rows, "selection": {"installer_images": [{"path": p} for p in rows], "local_packages": []}}
        value = {"files": dict(rows), "groups": {"archives": list(rows)}}
        return receipt, value

    def test_selected_legacy_archives_are_included(self):
        receipt, value = self.archive_fixture()
        self.assertIn("target/docker-nat.gz", plan.archive_paths(receipt, value))

    def test_missing_or_extra_archive_closure_rejected(self):
        receipt, value = self.archive_fixture()
        value["groups"]["archives"].pop()
        with self.assertRaises(ValueError):
            plan.archive_paths(receipt, value)
        receipt, value = self.archive_fixture()
        value["groups"]["archives"].append("target/docker-unknown.gz")
        with self.assertRaises(ValueError):
            plan.archive_paths(receipt, value)

    def test_changed_archive_digest_rejected(self):
        receipt, value = self.archive_fixture()
        value["files"]["target/docker-nat.gz"] = {"bytes": 1, "sha256": "c" * 64}
        with self.assertRaises(ValueError):
            plan.archive_paths(receipt, value)

    def kernel_fixture(self, root):
        context = {"producer": {"commit": plan.KERNEL_PRODUCER,
                    "registry": "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main"},
                   "consumer": {"commit": plan.HEAD,
                    "registry": "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" + plan.CATALOG},
                   "catalog": {"commit": plan.CATALOG}}
        original = {"packages": [{"name": "package" + str(i) + ".deb", "size": 1, "sha256": str(i) * 64}
                                 for i in range(5)]}
        (root / "kernel-packages.json").write_text(json.dumps(original))
        (root / "kernel-provenance.json").write_text("{}")
        (root / "context.json").write_text(json.dumps(context))
        files = {name: {"bytes": (root / name).stat().st_size, "sha256": plan.digest((root / name).read_bytes())}
                 for name in ("context.json", "kernel-packages.json", "kernel-provenance.json")}
        files.update({item["name"]: {"bytes": item["size"], "sha256": item["sha256"]} for item in original["packages"]})
        value = {"files": files, "kernel_context": "context.json",
                 "groups": {"profile": ["context.json"], "kernel": [p for p in files if p != "context.json"]}}
        args = argparse.Namespace(producer_workspace=root, registry_workspace=root)
        overrides = {p: files[p]["sha256"] for p in ("kernel-packages.json", "kernel-provenance.json")}
        return value, args, overrides

    def test_original_kernel_hashes_and_five_packages_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); value, args, originals = self.kernel_fixture(root)
            with mock.patch.object(plan, "KERNEL_CONTEXT", value["files"]["context.json"]["sha256"]), \
                 mock.patch.object(plan, "KERNEL_ORIGINALS", originals):
                forwarded = plan.kernel_arguments(root, value, args)
                self.assertEqual(len(forwarded), 5)
                value["files"]["package0.deb"] = {"bytes": 2, "sha256": "0" * 64}
                with self.assertRaises(ValueError):
                    plan.kernel_arguments(root, value, args)

    def test_changed_original_metadata_hash_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); value, args, originals = self.kernel_fixture(root)
            with mock.patch.object(plan, "KERNEL_CONTEXT", value["files"]["context.json"]["sha256"]), \
                 mock.patch.object(plan, "KERNEL_ORIGINALS", originals):
                value["files"]["kernel-provenance.json"]["sha256"] = "f" * 64
                with self.assertRaises(ValueError):
                    plan.kernel_arguments(root, value, args)

    def test_capture_is_exclusive_and_original_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.json"
            digest = plan.save_capture(path, {"original": True})
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            with self.assertRaises(FileExistsError):
                plan.save_capture(path, {"changed": True})
            self.assertEqual(path.read_bytes(), raw)

    def test_capture_hash_validation_uses_the_parsed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.json"
            digest = plan.save_capture(path, {"original": True})
            self.assertEqual(plan.data(path, digest), {"original": True})
            path.write_text('{"original":false}')
            with self.assertRaises(ValueError):
                plan.data(path, digest)

    def test_grouped_archives_skip_but_kernel_verifier_remains(self):
        # Toy graph only: no package tool, archive builder or project import.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "target").mkdir()
            for name in ("docker-x.gz", "docker-x-dbg.gz"):
                (root / "target" / name).write_text("existing")
            (root / "Makefile").write_text(
                ".PHONY: FORCE kernel-verify all\n"
                "all: target/docker-x.gz target/docker-x-dbg.gz kernel-verify\n"
                "target/docker-x.gz target/docker-x-dbg.gz &: FORCE\n\t$(error archive builder invoked)\n"
                "kernel-verify:\n\t@printf verified > kernel-verified\n")
            result = subprocess.run(["make", "--no-builtin-rules", "--no-builtin-variables", "-f", "Makefile",
                                     "--old-file=target/docker-x.gz", "--old-file=target/docker-x-dbg.gz", "all"],
                                    cwd=root, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "kernel-verified").read_text(), "verified")
            self.assertEqual((root / "target/docker-x.gz").read_text(), "existing")


if __name__ == "__main__":
    unittest.main()
