"""Pure/mocked planner checks and a toy Make grouped-target check; no DEBs."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).parents[1] / "ci" / "vs_reuse_plan.py"
spec = importlib.util.spec_from_file_location("vs_reuse_plan_under_test", SOURCE)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


SOURCE_HEAD = "a" * 40
SOURCE_TREE = "c" * 40
SOURCE_MERGE = "b" * 40
ASSEMBLY = {"commit": SOURCE_MERGE, "tree": SOURCE_TREE}


def fixture_args(**kwargs):
    return argparse.Namespace(**{"source_head": SOURCE_HEAD, "source_tree": SOURCE_TREE, "merge": SOURCE_MERGE, **kwargs})


def source_cli():
    return ["--source-head", SOURCE_HEAD, "--source-tree", SOURCE_TREE]


class ReusePlanTests(unittest.TestCase):
    def test_preparation_policies_reach_host_hook_before_any_assembly(self):
        # Only this toy hook runs remotely: no project/package recipe or DEB.
        identity = {"BUILD_NUMBER": "1", "BUILD_TIMESTAMP": "20261110.0",
                    "SOURCE_DATE_EPOCH": "1794268800", "BUILD_HOSTNAME": "fixture ' host",
                    "SONIC_IMAGE_VERSION": "bazel-ci"}
        original = dict(identity)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = io.StringIO()
            argv = ["vs_reuse_plan.py", "environment", "--workspace", str(root),
                    "--manifest", str(root / "unused-fixture.json"),
                    "--manifest-sha256", "a" * 64, "--merge", "b" * 40]
            with mock.patch.object(plan.sys, "argv", argv + source_cli()), \
                 mock.patch.object(plan, "inputs", return_value=(root, {"build_environment": identity})), \
                 contextlib.redirect_stdout(stdout):
                plan.main()
            exports = stdout.getvalue()
            self.assertEqual(identity, original)
            (root / "Makefile").write_text(
                "ifneq ($(SONIC_DEB_BUILD_POLICY),reuse-only)\n$(error preparation policy missing)\nendif\n"
                "ifneq ($(SONIC_BAZEL_KERNEL_PREPARE),verify)\n$(error kernel policy missing)\nendif\n"
                ".PHONY: all\nall: hook-output\n"
                "hook-output: hook-source\n"
                "\t@printf checked > hook-policy-checked; "
                "case \"$(SONIC_DEB_BUILD_POLICY)\" in reuse-only) exit 23;; esac; "
                "printf forbidden > generation-sentinel\n")
            (root / "hook-source").write_text("original prerequisite")
            for state in ("missing", "stale"):
                with self.subTest(state=state):
                    if state == "stale":
                        (root / "hook-output").write_text("existing original fixture")
                        os.utime(root / "hook-output", (1, 1))
                        os.utime(root / "hook-source", (2, 2))
                    result = subprocess.run(
                        ["bash", "-eu", "-c", exports +
                         "exec make --no-builtin-rules --no-builtin-variables -f Makefile all\n"],
                        cwd=root, text=True, capture_output=True, timeout=10,
                        env={**os.environ, "SONIC_DEB_BUILD_POLICY": "build", "SONIC_BAZEL_KERNEL_PREPARE": "ensure",
                             "MAKEFLAGS": "", "MFLAGS": "", "GNUMAKEFLAGS": "", "MAKEFILES": "", "MAKEOVERRIDES": ""})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual((root / "hook-policy-checked").read_text(), "checked")
                    (root / "hook-policy-checked").unlink()
                    self.assertFalse((root / "generation-sentinel").exists())
                    if state == "stale":
                        self.assertEqual((root / "hook-output").read_text(), "existing original fixture")

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
        self.assertEqual(plan.WORKER, "1e423956afe2cb706c239c8be93c1000bbc7a479")
        for value in ("SONIC_IMAGE_VERSION=bazel-ci", "CROSS_BUILD_ENVIRON=n",
                      "MULTIARCH_QEMU_ENVIRON=n", "SONIC_BAZEL_KERNEL_PREPARE=verify",
                      "SONIC_DEB_BUILD_POLICY=reuse-only", "SONIC_DPKG_CACHE_METHOD=none",
                      "SONIC_DPKG_CACHE_METHOD_OVERRIDE=none", "SONIC_BAZEL_CACHE_POLICY=remote-only"):
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
                  "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT_SHA256=" + "f" * 64,
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
                   "consumer": {"commit": ASSEMBLY["commit"],
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
        args = fixture_args(producer_workspace=root, registry_workspace=root)
        overrides = {p: files[p]["sha256"] for p in ("kernel-packages.json", "kernel-provenance.json")}
        return value, args, overrides

    def test_original_kernel_hashes_and_five_packages_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); value, args, originals = self.kernel_fixture(root)
            with mock.patch.object(plan, "KERNEL_ORIGINALS", originals):
                forwarded = plan.kernel_arguments(root, value, args)
                self.assertEqual(len(forwarded), 5)
                value["files"]["package0.deb"] = {"bytes": 2, "sha256": "0" * 64}
                with self.assertRaises(ValueError):
                    plan.kernel_arguments(root, value, args)

    def test_changed_original_metadata_hash_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); value, args, originals = self.kernel_fixture(root)
            with mock.patch.object(plan, "KERNEL_ORIGINALS", originals):
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


class CacheFreePackageReuseTests(unittest.TestCase):
    def fixture(self):
        # Metadata only. These are not DEB payloads or producer receipts.
        names = ["target/debs/trixie/host_1.0-1_all.deb", "target/debs/trixie/kernel_6.1_amd64.deb"]
        return {"admitted_debs": names[:],
                "groups": {"packages": names[:1] + ["target/python-wheels/trixie/tool.whl"],
                           "kernel": names[1:] + ["kernel-packages.json"]},
                "files": {name: {"bytes": 11, "sha256": "a" * 64} for name in names}}

    def test_cache_free_policy_is_exported_before_host_preparation(self):
        original = {"BUILD_NUMBER": "1", "BUILD_TIMESTAMP": "fixture", "SOURCE_DATE_EPOCH": "1",
                    "BUILD_HOSTNAME": "fixture", "SONIC_IMAGE_VERSION": "bazel-ci"}
        environment = dict(original)
        stdout = io.StringIO()
        argv = ["vs_reuse_plan.py", "environment", "--workspace", "/fixture", "--manifest", "/fixture/unused.json",
                "--manifest-sha256", "a" * 64, "--merge", "b" * 40]
        with mock.patch.object(plan.sys, "argv", argv + source_cli()), \
             mock.patch.object(plan, "inputs", return_value=(Path("/fixture"), {"build_environment": environment})), \
             contextlib.redirect_stdout(stdout):
            plan.main()
        self.assertEqual(environment, original)
        for item in ("SONIC_DPKG_CACHE_METHOD=none", "SONIC_DPKG_CACHE_METHOD_OVERRIDE=none",
                     "SONIC_BAZEL_CACHE_POLICY=remote-only", "SONIC_DEB_BUILD_POLICY=reuse-only",
                     "SONIC_BAZEL_KERNEL_PREPARE=verify"):
            self.assertIn("export " + item + "\n", stdout.getvalue())

    def test_explicit_admission_preserves_the_inventory(self):
        value = self.fixture()
        original = json.dumps(value, sort_keys=True)
        self.assertEqual(plan.package_paths(value), sorted(value["admitted_debs"]))
        self.assertEqual(json.dumps(value, sort_keys=True), original)

    def test_missing_duplicate_or_partial_admission_rejected(self):
        for names in (None, [], ["target/debs/trixie/host_1.0-1_all.deb"],
                      ["target/debs/trixie/host_1.0-1_all.deb"] * 2):
            value = self.fixture()
            value["admitted_debs"] = names
            with self.subTest(names=names), self.assertRaises(ValueError):
                plan.package_paths(value)

    def test_unreviewed_package_is_not_added_by_filename(self):
        value = self.fixture()
        name = "target/debs/trixie/unreviewed.deb"
        value["admitted_debs"].append(name)
        value["files"][name] = {"bytes": 11, "sha256": "b" * 64}
        with self.assertRaises(ValueError):
            plan.package_paths(value)

    def test_wheel_phony_installer_and_unsafe_deb_paths_rejected(self):
        for name in ("target/python-wheels/trixie/tool.whl", "bazel-kernel-ensure",
                     "target/sonic-vs.bin", "installer.platform", "host-install",
                     "target/debs/trixie/../../bad.deb", "target/debs/bookworm/other.deb",
                     "target/debs/trixie/x.deb --old-file=installer.platform",
                     "target/debs/trixie/$(shell touch bad).deb"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                plan.make_command(["target/docker-x.gz"], [], debs=[name])

    def test_empty_or_unbound_package_rejected(self):
        for mutation in ("empty", "unbound"):
            value = self.fixture()
            name = value["admitted_debs"][0]
            if mutation == "empty":
                value["files"][name]["bytes"] = 0
            else:
                del value["files"][name]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                plan.package_paths(value)

    def test_command_disables_cache_and_keeps_consumer_checks_outside_old_files(self):
        value = self.fixture()
        debs = plan.package_paths(value)
        command = plan.make_command(["target/docker-x.gz"], ["SONIC_BAZEL_KERNEL_PREPARE=verify"], debs=debs)
        self.assertEqual([arg for arg in command if arg.startswith("--old-file=")],
                         ["--old-file=" + name for name in sorted(["target/docker-x.gz", *debs])])
        self.assertIn("SONIC_DPKG_CACHE_METHOD=none", command)
        self.assertIn("SONIC_DPKG_CACHE_METHOD_OVERRIDE=none", command)
        for name in debs:
            self.assertIn(Path(name).name + "_CACHE_OVERRIDE=none", command)
        for name in ("installer.platform", "bazel-kernel-ensure", "host-install",
                     "target/python-wheels/trixie/tool.whl", "target/sonic-vs.bin"):
            self.assertNotIn("--old-file=" + name, command)
        self.assertIn("SONIC_DEB_BUILD_POLICY=reuse-only", command)
        self.assertEqual(command[-1], "SONIC_BAZEL_KERNEL_PREPARE=verify")





class EarlyNativeAdmissionTests(unittest.TestCase):
    def cli(self, root):
        return ["vs_reuse_plan.py", "admission", "--workspace", str(root),
                "--manifest", str(root / "manifest.json"), "--manifest-sha256", "a" * 64,
                "--merge", "b" * 40]

    def test_early_mode_reuses_admission_without_configuration_or_commands(self):
        root = Path("/fixture-frozen-assembly")
        value = {"fixture": True, "assembly": dict(ASSEMBLY)}
        output = io.StringIO()
        with mock.patch.object(plan.sys, "argv", self.cli(root) + source_cli()), \
             mock.patch.object(plan, "inputs", return_value=(root, value)), \
             mock.patch.object(plan, "verify_native_inputs", return_value=({"head": SOURCE_HEAD}, {"gearbox": {"fixture": True}})) as admit, \
             mock.patch.object(plan, "kernel_arguments", side_effect=AssertionError("No kernel configuration")), \
             mock.patch.object(plan, "make_command", side_effect=AssertionError("No assembly command")), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("No child execution")), \
             contextlib.redirect_stdout(output):
            plan.main()
        self.assertEqual(admit.call_count, 1)
        self.assertIs(admit.call_args.args[1], value)
        self.assertEqual(json.loads(output.getvalue())["status"], "NATIVE_INPUTS_VERIFIED_BEFORE_PREPARATION_ONLY")

    def test_early_mode_preserves_admission_failure_without_output(self):
        root = Path("/fixture-frozen-assembly")
        output = io.StringIO()
        with mock.patch.object(plan.sys, "argv", self.cli(root) + source_cli()), \
             mock.patch.object(plan, "inputs", return_value=(root, {})), \
             mock.patch.object(plan, "verify_native_inputs", side_effect=ValueError("original producer failed")), \
             contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, "original producer failed"):
                plan.main()
        self.assertEqual(output.getvalue(), "")

    def fixture(self, root):
        request = {"image": "gearbox", "pr": 206, "head": SOURCE_HEAD, "merge": "b" * 40,
                   "tree": SOURCE_TREE, "controller": plan.WORKER, "catalog": plan.CATALOG,
                   "repository": "securely1g/sonic-buildimage", "workflow": "bazel-oci.yml", "base": "c" * 40}
        names = ["docker-gbsyncd-vs.gz", "docker-gbsyncd-vs-dbg.gz"]
        artifacts = {name: {"bytes": 3, "sha256": "a" * 64} for name in names}
        records = {"request.json": request, "provenance.json": {"fixture": True},
                   "tools/bazel/ci/gearbox_worker_contract.json": {"fixture": True},
                   "accepted/public-receipt.json": {"artifacts": artifacts}}
        for name, value in records.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, sort_keys=True) + "\n")
        value = {"native": {"gearbox": {"request": "request.json", "provenance": "provenance.json", "acceptance": "accepted"}},
                 "groups": {"source": ["tools/bazel/ci/gearbox_worker_contract.json"]},
                 "files": {"target/" + name: dict(row) for name, row in artifacts.items()}}
        return value, fixture_args(merge="b" * 40), {name: (root / name).read_bytes() for name in records}

    def test_extracted_loop_keeps_original_worker_failure_fatal(self):
        from tools.bazel.ci import image_worker_receipt as worker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value, args, original = self.fixture(root)
            with mock.patch.object(plan.sys, "path", list(plan.sys.path)), \
                 mock.patch.object(worker, "validate_request", side_effect=lambda value: value), \
                 mock.patch.object(worker, "verify_evidence", side_effect=ValueError("original worker rejected")):
                with self.assertRaisesRegex(ValueError, "original worker rejected"):
                    plan.verify_native_inputs(root, value, args)
            self.assertEqual({name: (root / name).read_bytes() for name in original}, original)

    def test_extracted_loop_binds_both_archive_records_without_rewriting(self):
        from tools.bazel.ci import image_worker_receipt as worker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value, args, original = self.fixture(root)
            with mock.patch.object(plan.sys, "path", list(plan.sys.path)), \
                 mock.patch.object(worker, "validate_request", side_effect=lambda value: value), \
                 mock.patch.object(worker, "verify_evidence", return_value={"fixture": True}) as verified:
                common, workers = plan.verify_native_inputs(root, value, args)
                self.assertEqual(common["head"], SOURCE_HEAD)
                self.assertEqual(workers, {"gearbox": {"fixture": True}})
                self.assertEqual(verified.call_count, 1)
                value["files"]["target/docker-gbsyncd-vs-dbg.gz"]["sha256"] = "f" * 64
                with self.assertRaisesRegex(ValueError, "Full image bytes differ"):
                    plan.verify_native_inputs(root, value, args)
            self.assertEqual({name: (root / name).read_bytes() for name in original}, original)



class CurrentNativeContextTests(EarlyNativeAdmissionTests):
    # Inherit helpers, not the four already covered methods.
    test_early_mode_reuses_admission_without_configuration_or_commands = None
    test_early_mode_preserves_admission_failure_without_output = None
    test_extracted_loop_keeps_original_worker_failure_fatal = None
    test_extracted_loop_binds_both_archive_records_without_rewriting = None

    def test_changed_head_tree_or_merge_rejects_before_canonical_acceptance(self):
        from tools.bazel.ci import image_worker_receipt as worker
        for key in ("head", "tree", "merge", "controller"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                value, args, original = self.fixture(root)
                request = json.loads((root / "request.json").read_bytes())
                request[key] = "f" * 40
                (root / "request.json").write_text(json.dumps(request))
                with mock.patch.object(plan.sys, "path", list(plan.sys.path)), \
                     mock.patch.object(worker, "validate_request", side_effect=lambda value: value), \
                     mock.patch.object(worker, "verify_evidence", side_effect=AssertionError("No acceptance")):
                    with self.assertRaisesRegex(ValueError, "another source domain"):
                        plan.verify_native_inputs(root, value, args)


    def test_advanced_base_preserves_requested_head_and_uses_new_tested_merge(self):
        from tools.bazel.ci import image_worker_receipt as worker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value, args, _ = self.fixture(root)
            request = json.loads((root / "request.json").read_bytes())
            request.update(base="e" * 40, merge="f" * 40, tree="9" * 40)
            original = json.dumps(request, sort_keys=True).encode()
            (root / "request.json").write_bytes(original)
            args.merge, args.source_tree = request["merge"], request["tree"]
            with mock.patch.object(plan.sys, "path", list(plan.sys.path)), \
                 mock.patch.object(worker, "validate_request", side_effect=lambda value: value), \
                 mock.patch.object(worker, "verify_evidence", return_value={"fixture": True}):
                common, _ = plan.verify_native_inputs(root, value, args)
            self.assertEqual(common["head"], SOURCE_HEAD)
            self.assertEqual(common["base"], "e" * 40)
            self.assertEqual(common["merge"], "f" * 40)
            self.assertEqual(common["tree"], "9" * 40)
            domain = {"commit": common["merge"], "tree": common["tree"]}
            self.assertEqual(plan.assembly_domain({"assembly": domain},
                             tested_merge=args.merge, tested_tree=args.source_tree), domain)
            self.assertEqual((root / "request.json").read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "assembly source domain"):
                plan.assembly_domain({"assembly": {"commit": SOURCE_HEAD, "tree": common["tree"]}},
                                     tested_merge=args.merge, tested_tree=args.source_tree)

    def test_source_context_cannot_be_inferred_from_payloads(self):
        for key in ("source_head", "source_tree", "merge"):
            args = fixture_args(merge="b" * 40)
            setattr(args, key, "")
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "immutable workflow"):
                plan.source_domain(args)


class ExplicitWheelReuseTests(unittest.TestCase):
    def fixture(self):
        names = ["target/python-wheels/trixie/helper-1.0-py3-none-any.whl",
                 "target/python-wheels/trixie/tool-1.0-py3-none-any.whl"]
        return {"admitted_wheels": names[:], "groups": {"packages": names[:]},
                "files": {name: {"bytes": 11, "sha256": "a" * 64} for name in names}}

    def test_explicit_wheel_admission_preserves_inventory(self):
        value = self.fixture()
        original = json.dumps(value, sort_keys=True)
        self.assertEqual(plan.wheel_paths(value), sorted(value["admitted_wheels"]))
        self.assertEqual(json.dumps(value, sort_keys=True), original)

    def test_missing_duplicate_or_partial_wheel_review_is_refused(self):
        for names in (None, [], [self.fixture()["admitted_wheels"][0]],
                      [self.fixture()["admitted_wheels"][0]] * 2):
            value = self.fixture()
            value["admitted_wheels"] = names
            with self.subTest(names=names), self.assertRaises(ValueError):
                plan.wheel_paths(value)

    def test_filename_does_not_admit_an_extra_wheel(self):
        value = self.fixture()
        extra = "target/python-wheels/trixie/unreviewed-1-py3-none-any.whl"
        value["admitted_wheels"].append(extra)
        value["files"][extra] = {"bytes": 11, "sha256": "b" * 64}
        with self.assertRaises(ValueError):
            plan.wheel_paths(value)

    def test_empty_and_unbound_wheel_are_refused(self):
        for mutation in ("empty", "unbound"):
            value = self.fixture()
            name = value["admitted_wheels"][0]
            if mutation == "empty":
                value["files"][name]["bytes"] = 0
            else:
                del value["files"][name]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                plan.wheel_paths(value)

    def test_install_targets_and_unsafe_wheel_paths_cannot_be_ignored(self):
        for name in ("target/python-wheels/trixie/tool.whl-install", ".platform",
                     "bazel-kernel-ensure", "target/sonic-vs.bin",
                     "target/debs/trixie/host.deb", "target/python-wheels/bookworm/tool.whl",
                     "target/python-wheels/trixie/../../tool.whl",
                     "target/python-wheels/trixie/$(shell touch bad).whl",
                     "target/python-wheels/trixie/tool.whl --old-file=.platform"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                plan.make_command(["target/docker-x.gz"], [], wheels=[name])

    def test_command_keeps_install_checks_and_disables_wheel_cache_override(self):
        value = self.fixture()
        wheels = plan.wheel_paths(value)
        command = plan.make_command(["target/docker-x.gz"], [], wheels=wheels)
        self.assertEqual([x for x in command if x.startswith("--old-file=")],
                         ["--old-file=" + name for name in sorted(["target/docker-x.gz", *wheels])])
        for name in wheels:
            self.assertIn(Path(name).name + "_CACHE_OVERRIDE=none", command)
            self.assertNotIn("--old-file=" + name + "-install", command)
        self.assertNotIn("--old-file=bazel-kernel-ensure", command)
        self.assertIn("SONIC_DEB_BUILD_POLICY=reuse-only", command)

    def test_plan_uses_explicit_wheels_and_retains_assembly_refusal(self):
        value = self.fixture()
        deb = "target/debs/trixie/host_1_all.deb"
        value.update({"source": {"fixture": "preserved original domain"}, "assembly": dict(ASSEMBLY), "admitted_debs": [deb]})
        value["groups"]["packages"].append(deb)
        value["groups"]["kernel"] = []
        value["files"][deb] = {"bytes": 9, "sha256": "b" * 64}
        args = fixture_args(manifest=Path("/fixture/inventory.json"), manifest_sha256="c" * 64,
                                  merge=SOURCE_MERGE, receipt=Path("/fixture/capture.json"),
                                  make_inputs=Path("/fixture/target/sonic-vs.bin.make-inputs.tsv"),
                                  make_inputs_sha256="f" * 64)
        final23 = mock.Mock()
        with mock.patch.object(plan, "inputs", return_value=(Path("/fixture"), value)), \
             mock.patch.object(plan, "verify_native_inputs", return_value=({}, {})), \
             mock.patch("tools.bazel.ci.vs_final23", final23, create=True), \
             mock.patch.object(plan, "archive_paths", return_value=["target/docker-x.gz"]), \
             mock.patch.object(plan, "bazel_marker_archives", return_value=["target/docker-fixture.gz"]), \
             mock.patch.object(plan, "kernel_arguments", return_value=[]), \
             mock.patch.object(plan, "save_capture", return_value="e" * 64), \
             mock.patch.object(plan, "safe", return_value=args.make_inputs), \
             mock.patch.object(plan, "make_input_capture", return_value={}), \
             mock.patch.object(plan, "required_make_packages", return_value={}), \
             mock.patch.object(plan, "configured_source_closure", return_value={}):
            result = plan.plan(args)
        self.assertEqual(result["admitted_wheel_inputs"],
                         {name: value["files"][name] for name in value["admitted_wheels"]})
        for name in value["admitted_wheels"]:
            self.assertIn("--old-file=" + name, result["build_argv"])
        self.assertEqual(result["status"], "CONFIGURED_INPUTS_VERIFIED_COMMANDS_ONLY")
        self.assertIn("canonical kernel prepare.py verify with all16 actual Make fields", result["required_before_assembly"])

    def test_make_reuses_wheels_but_keeps_recursive_install_and_kernel_checks(self):
        # Literal fixture files only; no wheel/DEB/archive tool or project Makefile.
        for case in ("producer_not_ignored", "reused", "missing_host", "failed_kernel"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                wheel_dir = "target/python-wheels/trixie"
                deb_dir = "target/debs/trixie"
                wheels = [wheel_dir + "/tool.whl", wheel_dir + "/helper.whl"]
                deb = deb_dir + "/host.deb"
                names = ["target/docker-x.gz", deb, *wheels]
                for name in names:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("literal fixture input\n")
                before = {name: ((root / name).read_bytes(), (root / name).stat().st_mtime_ns)
                          for name in names}
                (root / "Makefile").write_text("\n".join([
                    ".SECONDEXPANSION:",
                    ".PHONY: all .platform bazel-kernel-ensure producer-only",
                    "W := " + wheel_dir,
                    "D := " + deb_dir,
                    "WHEELS := tool.whl helper.whl",
                    "tool.whl_DEPENDS := helper.whl",
                    "tool.whl_DEBS_DEPENDS := host.deb",
                    "helper.whl_DEBS_DEPENDS := host.deb",
                    "all: target/docker-x.gz $(W)/tool.whl-install | bazel-kernel-ensure",
                    "$(addprefix $(W)/,$(WHEELS)): $(W)/%: .platform producer-only",
                    "\t$(error WHEEL_PRODUCER_MUST_NOT_RUN)",
                    "$(addsuffix -install,$(addprefix $(W)/,$(WHEELS))): $(W)/%-install: .platform $$(addsuffix -install,$$(addprefix $(W)/,$$($$*_DEPENDS))) $$(addsuffix -install,$$(addprefix $(D)/,$$($$*_DEBS_DEPENDS))) $(W)/$$*",
                    "\t@test -f $(W)/$*",
                    "\t@printf 'wheel:%s\\n' '$*' >> events",
                    "$(D)/host.deb-install: .platform $(D)/host.deb",
                    "\t@test -f $(D)/host.deb",
                    "\t@printf 'host\\n' >> events",
                    "$(D)/host.deb: .platform",
                    "\t$(error DEB_PRODUCER_MUST_NOT_RUN)",
                    "target/docker-x.gz: .platform",
                    "\t$(error ARCHIVE_PRODUCER_MUST_NOT_RUN)",
                    "producer-only:",
                    "\t$(error WHEEL_PRODUCER_MUST_NOT_RUN)",
                    "bazel-kernel-ensure:",
                    "\t@test '$(FAIL_KERNEL)' != y",
                    "\t@printf 'kernel\\n' >> events",
                    "",
                ]))
                command = plan.make_command(["target/docker-x.gz"], [], debs=[deb],
                                            wheels=[] if case == "producer_not_ignored" else wheels)
                if case == "missing_host":
                    (root / deb).unlink()
                argv = ["make", "--no-builtin-rules", "--no-builtin-variables", "-f", "Makefile",
                        *[x for x in command if x.startswith("--old-file=")], "all",
                        "FAIL_KERNEL=" + ("y" if case == "failed_kernel" else "n")]
                result = subprocess.run(argv, cwd=root, text=True, capture_output=True, timeout=10)
                if case == "reused":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((root / "events").read_text().splitlines(),
                                     ["host", "wheel:helper.whl", "wheel:tool.whl", "kernel"])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    if case == "producer_not_ignored":
                        self.assertIn("WHEEL_PRODUCER_MUST_NOT_RUN", result.stderr)
                    if case == "failed_kernel":
                        self.assertEqual((root / "events").read_text().splitlines(),
                                         ["host", "wheel:helper.whl", "wheel:tool.whl"])
                for name, identity in before.items():
                    if case == "missing_host" and name == deb:
                        continue
                    self.assertEqual(((root / name).read_bytes(), (root / name).stat().st_mtime_ns), identity)



class ActualAssemblyDomainTests(unittest.TestCase):
    def domain(self, value):
        return plan.assembly_domain(value, tested_merge=SOURCE_MERGE, tested_tree=SOURCE_TREE)

    def test_assembly_uses_tested_merge_separately_from_requested_head(self):
        value = {"source": {"head": SOURCE_HEAD}, "assembly": dict(ASSEMBLY)}
        original = json.dumps(value, sort_keys=True)
        self.assertEqual(self.domain(value), ASSEMBLY)
        self.assertNotEqual(ASSEMBLY["commit"], value["source"]["head"])
        self.assertEqual(json.dumps(value, sort_keys=True), original)
        self.assertFalse(hasattr(plan, "HEAD"))
        self.assertFalse(hasattr(plan, "ASSEMBLY"))
        self.assertEqual(plan.WORKER, "1e423956afe2cb706c239c8be93c1000bbc7a479")

    def test_old_or_extra_assembly_identity_never_relabels_the_source(self):
        for domain in (None, {}, {**ASSEMBLY, "commit": "f" * 40},
                       {**ASSEMBLY, "tree": "f" * 40}, {**ASSEMBLY, "parent": SOURCE_HEAD},
                       {**ASSEMBLY, "accepted": True}):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                self.domain({"assembly": domain})

    def test_inputs_refuse_missing_domain_before_any_payload_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {"schema": 1, "kind": "sonic-final23-reuse-input-inventory",
                     "source": {"head": SOURCE_HEAD, "tree": SOURCE_TREE,
                                "controller": plan.WORKER, "catalog": plan.CATALOG,
                                "pr": 206, "merge": "a" * 40}}
            manifest = root / "inventory.json"
            manifest.write_text(json.dumps(value))
            args = fixture_args(workspace=root, manifest=manifest,
                manifest_sha256=plan.digest(manifest.read_bytes()), merge="a" * 40,
                require_environment=False)
            with mock.patch.object(plan, "file_digest", side_effect=AssertionError("No payload read")), \
                 self.assertRaisesRegex(ValueError, "assembly source domain"):
                plan.inputs(args)

    def test_old_kernel_consumer_context_cannot_masquerade_as_actual_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value, args, originals = ReusePlanTests().kernel_fixture(root)
            path = root / "context.json"
            context = json.loads(path.read_text())
            context["consumer"]["commit"] = "f" * 40
            path.write_text(json.dumps(context))
            value["files"]["context.json"] = {"bytes": path.stat().st_size,
                                               "sha256": plan.digest(path.read_bytes())}
            with mock.patch.object(plan, "KERNEL_ORIGINALS", originals), \
                 self.assertRaisesRegex(ValueError, "producer or consumer identity"):
                plan.kernel_arguments(root, value, args)

    def test_postcheck_rejects_another_assembly_before_archive_verification(self):
        value = {"source": {"head": SOURCE_HEAD}, "assembly": dict(ASSEMBLY)}
        args = fixture_args(receipt=Path("/tmp/preassembly.json"),
                           receipt_sha256="a" * 64, manifest_sha256="b" * 64)
        for domain in (None, {**ASSEMBLY, "commit": "f" * 40}):
            capture = {"schema": 1, "kind": "sonic-final23-preassembly-capture",
                       "source": value["source"], "assembly": domain,
                       "manifest_sha256": args.manifest_sha256}
            with self.subTest(domain=domain), \
                 mock.patch.object(plan, "inputs", return_value=(Path("/tmp"), value)), \
                 mock.patch.object(plan, "data", return_value=capture), \
                 mock.patch.object(plan, "archive_paths", side_effect=AssertionError("No archive verification")), \
                 self.assertRaisesRegex(ValueError, "capture identity"):
                plan.verify_after(args)

    def test_rootfs_network_policy_is_requested_without_ignoring_guards(self):
        command = plan.make_command(["target/docker-x.gz"], ["KERNEL_FIXTURE=verify"])
        self.assertIn("SONIC_ROOTFS_DOCKER_NETWORK=none", command)
        self.assertEqual(command[-1], "KERNEL_FIXTURE=verify")
        self.assertEqual([x for x in command if x.startswith("--old-file=")],
                         ["--old-file=target/docker-x.gz"])
        self.assertIn("SONIC_DEB_BUILD_POLICY=reuse-only", command)



class MakeInputCaptureTests(unittest.TestCase):
    def fixture(self, rows=None):
        temporary = tempfile.TemporaryDirectory(prefix="vs-capture-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "capture.tsv"
        if rows is None:
            rows = ["variable\tSONIC_IMAGE_VERSION\tvalue " + chr(96) + "literal" + chr(96),
                    "extra-exports-present\tn", "installer\tDEPENDS\thost.deb leaf.deb",
                    "deb\thost.deb\tleaf.deb\ta.whl\truntime.deb\tcheck-host\tnone\t\tvs\t",
                    "wheel\ta.whl\tb.whl\thost.deb\tinput.txt\tnone\t",
                    "rfs\timage.rfs\tbase.rfs\tsonic-vs.bin\tvs",
                    "file\tinput.txt\tsource/input\tother.txt\tfile-check",
                    "body-glob\tlibyang3_*.deb\ttarget/libyang3_1.deb target/libyang3_2.deb",
                    "optional-input\trules/config.organization\tabsent"]
        raw = ("sonic-vs-make-inputs-v1\n" + "\n".join(rows) +
               "\nend\tsonic-vs-make-inputs-v1\n").encode()
        path.write_bytes(raw)
        return path, raw

    def test_all_typed_maps_preserve_original_bytes_and_literal_values(self):
        path, raw = self.fixture()
        before = path.stat().st_mtime_ns
        result = plan.make_input_capture(path, hashlib.sha256(raw).hexdigest())
        self.assertEqual((result["raw_bytes"], result["bytes"], result["sha256"]),
                         (raw, len(raw), hashlib.sha256(raw).hexdigest()))
        self.assertEqual(result["variables"]["SONIC_IMAGE_VERSION"], "value " + chr(96) + "literal" + chr(96))
        self.assertEqual(result["installer"]["DEPENDS"], "host.deb leaf.deb")
        self.assertEqual(result["debs"]["host.deb"], {"depends": ["leaf.deb"], "wheel_depends": ["a.whl"],
                         "rdepends": ["runtime.deb"], "phonies": ["check-host"], "cache_override": "none",
                         "cache_mode": "", "platform": "vs", "machine": ""})
        self.assertEqual(result["wheels"]["a.whl"]["debs_depends"], ["host.deb"])
        self.assertEqual(result["rfs"]["image.rfs"]["dependent_rfs"], ["base.rfs"])
        self.assertEqual(result["files"]["input.txt"]["path"], "source/input")
        self.assertEqual(result["body_globs"]["libyang3_*.deb"], ["target/libyang3_1.deb", "target/libyang3_2.deb"])
        self.assertEqual(result["optional_inputs"], {"rules/config.organization": False})
        self.assertFalse(result["extra_exports_present"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (raw, before))

    def test_boolean_presence_is_typed_without_export_values(self):
        path, raw = self.fixture(["extra-exports-present\ty", "optional-input\toptional.file\tpresent"])
        result = plan.make_input_capture(path)
        self.assertIs(result["extra_exports_present"], True)
        self.assertIs(result["optional_inputs"]["optional.file"], True)
        self.assertEqual(result["variables"], {})

    def test_header_footer_and_final_newline_are_required(self):
        path, raw = self.fixture()
        for body in (raw.replace(b"sonic-vs-make-inputs-v1", b"wrong", 1), raw[:-1],
                     raw.replace(b"end\tsonic-vs-make-inputs-v1\n", b""), raw + b"unexpected\n"):
            with self.subTest(body=body[:20]), self.assertRaises(ValueError):
                path.write_bytes(body)
                plan.make_input_capture(path)

    def test_every_row_kind_has_exact_field_count(self):
        path, raw = self.fixture()
        rows = raw.decode().splitlines()[1:-1]
        for original in rows:
            for row in (original + "\textra", original.rsplit("\t", 1)[0]):
                selected = [row if item == original else item for item in rows]
                changed, unused = self.fixture(selected)
                with self.subTest(kind=original.split("\t")[0], row=row), self.assertRaises(ValueError):
                    plan.make_input_capture(changed)

    def test_duplicate_keys_and_extra_exports_are_rejected(self):
        path, raw = self.fixture()
        rows = raw.decode().splitlines()[1:-1]
        for row in rows:
            changed, unused = self.fixture(rows + [row])
            with self.subTest(kind=row.split("\t")[0]), self.assertRaises(ValueError):
                plan.make_input_capture(changed)

    def test_unknown_rows_names_and_presence_values_are_rejected(self):
        for row in ("secret\tNAME\tvalue", "variable\tlower-case\tvalue", "variable\t\tvalue",
                    "file\tspace name\t\t\t", "installer\tUNKNOWN\tvalue",
                    "optional-input\tname\tyes", "extra-exports-present\ttrue"):
            rows = [row] if row.startswith("extra-exports") else ["extra-exports-present\tn", row]
            path, unused = self.fixture(rows)
            with self.subTest(row=row), self.assertRaises(ValueError):
                plan.make_input_capture(path)
        path, unused = self.fixture(["variable\tNAME\tvalue"])
        with self.assertRaisesRegex(ValueError, "missing"):
            plan.make_input_capture(path)

    def test_control_characters_and_invalid_utf8_are_rejected(self):
        for value in ("a\0b", "a\rb", "a\x7fb", "a\x85b", "a\u2028b", "a\tb", "a\nb"):
            path, unused = self.fixture(["extra-exports-present\tn", "variable\tNAME\t" + value])
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                plan.make_input_capture(path)
        path, raw = self.fixture()
        path.write_bytes(raw.replace(b"value", b"\xff"))
        with self.assertRaises(UnicodeDecodeError):
            plan.make_input_capture(path)

    def test_line_and_total_read_bounds_are_independent(self):
        path, raw = self.fixture(["extra-exports-present\tn", "variable\tNAME\t" + "x" * 80])
        with mock.patch.object(plan, "MAX_CAPTURE_LINE", 64), self.assertRaisesRegex(ValueError, "line exceeds"):
            plan.make_input_capture(path)
        with mock.patch.object(plan, "MAX_JSON", len(raw) - 1), self.assertRaisesRegex(ValueError, "Oversized"):
            plan.make_input_capture(path)

    def test_independent_hash_and_nofollow_are_preserved(self):
        path, raw = self.fixture()
        with self.assertRaisesRegex(ValueError, "independent hash"):
            plan.make_input_capture(path, "0" * 64)
        link = path.with_name("link.tsv")
        link.symlink_to(path)
        with self.assertRaises(OSError):
            plan.make_input_capture(link)
        self.assertEqual(path.read_bytes(), raw)

    def test_held_file_change_is_rejected_before_parsing(self):
        path, raw = self.fixture()
        before = path.stat()
        after = mock.Mock(st_dev=before.st_dev, st_ino=before.st_ino, st_size=before.st_size,
                          st_mtime_ns=before.st_mtime_ns + 1, st_ctime_ns=before.st_ctime_ns)
        with mock.patch.object(plan.os, "fstat", side_effect=[before, after]), \
             self.assertRaisesRegex(ValueError, "changed while reading"):
            plan.make_input_capture(path)

    def test_factored_json_reader_retains_duplicate_and_nonfinite_refusals(self):
        path, unused = self.fixture()
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                plan.data(path)
        path.write_bytes(b'{"a":1}')
        self.assertEqual(plan.data(path, hashlib.sha256(path.read_bytes()).hexdigest()), {"a": 1})



class ConfiguredPackageClosureTests(unittest.TestCase):
    def fixture(self):
        variables = {name: "" for name in ['TARGET_PATH', 'DEBS_PATH', 'IMAGE_DISTRO_DEBS_PATH', 'PYTHON_WHEELS_PATH', 'FILES_PATH', 'RULES_PATH', 'LINUX_KERNEL_VERSION', 'KVERSION', 'SONIC_PACKAGES', 'PHONY_PATH', 'CONFIGURED_ARCH', 'CONFIGURED_PLATFORM', 'BLDENV', 'SONIC_IMAGE_VERSION', 'INSTALL_DEBUG_TOOLS', 'BUILD_WITH_BAZEL_WHEN_AVAILABLE', 'SONIC_BAZEL_VS_ARCHIVE_PROFILE', 'SONIC_DEB_BUILD_POLICY', 'SONIC_BAZEL_KERNEL_PREPARE', 'SONIC_BAZEL_KERNEL_INPUTS', 'SONIC_ROOTFS_DOCKER_NETWORK', 'SONIC_DPKG_CACHE_METHOD', 'SONIC_DPKG_CACHE_METHOD_OVERRIDE', 'INCLUDE_FIPS', 'INCLUDE_VS_DASH_SAI', 'ENABLE_ASAN', 'ENABLE_SYNCD_RPC', 'CROSS_BUILD_ENVIRON', 'MULTIARCH_QEMU_ENVIRON', 'INCLUDE_SYSTEM_EVENTD', 'BUILD_REDUCE_IMAGE_SIZE', 'ENABLE_ZTP', 'PDDF_SUPPORT', 'ENABLE_ORGANIZATION_EXTENSIONS', 'SECURE_UPGRADE_MODE', 'INITRAMFS_TOOLS', 'INITRAMFS_TOOLS_CORE', 'LINUX_KERNEL', 'LINUX_KERNEL_DBG', 'LINUX_KBUILD', 'LINUX_HEADERS', 'SONIC_DEVICE_DATA', 'IFUPDOWN2', 'MAKEDUMPFILE', 'KDUMP_TOOLS', 'LIBPAM_RADIUS', 'LIBNSS_RADIUS', 'LIBPAM_TACPLUS', 'LIBNSS_TACPLUS', 'MONIT', 'OPENSSH_SERVER', 'OPENSSH_VERSION_FULL', 'PYTHON3_SWSSCOMMON', 'SONIC_DB_CLI', 'SONIC_NETTOOLS', 'SONIC_RSYSLOG_PLUGIN', 'SONIC_UTILITIES_DATA', 'SONIC_CTRMGRD_RS', 'SONIC_HOST_SERVICES_RS', 'SONIC_HOST_SERVICES_DATA', 'SOCAT', 'BASH', 'BASH_TACPLUS', 'AUDISP_TACPLUS', 'SYSLOG_COUNTER', 'SEDUTIL', 'SONIC_ZTP', 'SONIC_INSTALLER_EXTRA_DEBS', 'SYMCRYPT_OPENSSL', 'FIPS_BASEIMAGE_INSTALLERS', 'FIPS_BASEIMAGE_PYTHON_INSTALLERS', 'RASDAEMON', 'LIBSWSSCOMMON', 'PYTHON_SWSSCOMMON', 'SONIC_UTILITIES_PY3', 'SONIC_PY_COMMON_PY2', 'SONIC_PY_COMMON_PY3', 'SONIC_CONFIG_ENGINE_PY2', 'SONIC_CONFIG_ENGINE_PY3', 'SONIC_PLATFORM_COMMON_PY3', 'REDIS_DUMP_LOAD_PY2', 'REDIS_DUMP_LOAD_PY3', 'SONIC_PLATFORM_API_PY2', 'SONIC_PLATFORM_API_PY3', 'PDDF_PLATFORM_API_BASE_PY2', 'PDDF_PLATFORM_API_BASE_PY3', 'SONIC_YANG_MODELS_PY3', 'SONIC_CTRMGRD', 'SONIC_YANG_MGMT_PY3', 'SYSTEM_HEALTH', 'SONIC_HOST_SERVICES_PY3', 'SONIC_DEBIAN_EXTENSION_DEPENDS', 'SONIC_BAZEL_VS_CONTRACT_DEPENDS', 'SONIC_PACKAGES_LOCAL', 'SONIC_INSTALLERS', 'SONIC_RFS_TARGETS', 'SONIC_ONLINE_DEBS', 'SONIC_COPY_DEBS', 'SONIC_MAKE_DEBS', 'SONIC_DPKG_DEBS', 'SONIC_PYTHON_STDEB_DEBS', 'SONIC_DERIVED_DEBS', 'SONIC_EXTRA_DEBS', 'SONIC_BAZEL_KERNEL_DEBS', 'SONIC_PYTHON_WHEELS', 'SONIC_COPY_FILES', 'SONIC_ONLINE_FILES', 'SONIC_MAKE_FILES']}
        variables.update(
            TARGET_PATH="target", CONFIGURED_ARCH="amd64", CONFIGURED_PLATFORM="vs", BLDENV="trixie",
            IMAGE_DISTRO_DEBS_PATH="target/debs/trixie", DEBS_PATH="target/debs/trixie",
            PYTHON_WHEELS_PATH="target/python-wheels/trixie", LINUX_KERNEL_VERSION="6.1",
            SONIC_DEB_BUILD_POLICY="reuse-only", SONIC_BAZEL_KERNEL_PREPARE="verify",
            SONIC_BAZEL_VS_ARCHIVE_PROFILE="final23", BUILD_WITH_BAZEL_WHEN_AVAILABLE="y",
            INCLUDE_VS_DASH_SAI="y", INCLUDE_FIPS="y", ENABLE_ASAN="n", ENABLE_SYNCD_RPC="n",
            CROSS_BUILD_ENVIRON="n", MULTIARCH_QEMU_ENVIRON="n", INSTALL_DEBUG_TOOLS="n",
            SONIC_IMAGE_VERSION="bazel-ci", SONIC_DPKG_CACHE_METHOD="none",
            SONIC_DPKG_CACHE_METHOD_OVERRIDE="none", SONIC_ROOTFS_DOCKER_NETWORK="none",
            ENABLE_ZTP="n", PDDF_SUPPORT="n", INCLUDE_SYSTEM_EVENTD="y", BUILD_REDUCE_IMAGE_SIZE="n",
            OPENSSH_VERSION_FULL="10.0p1-7",
        )
        self.capture = {
            "variables": variables, "debs": {}, "wheels": {}, "files": {}, "rfs": {},
            "installer": {key: "" for key in (
                "DEPENDS", "INSTALLS", "LAZY_INSTALLS", "LAZY_BUILD_INSTALLS",
                "PYTHON_WHEELS", "FILES", "RFS_DEPENDS", "MACHINE", "IMAGE_TYPE", "DOCKERS")},
            "body_globs": {}, "optional_inputs": {}, "extra_exports_present": False,
        }
        self.capture["installer"].update(MACHINE="vs", IMAGE_TYPE="onie")
        self.debs, self.wheels = set(), set()
        for pattern in ['audisp-tacplus_*.deb', 'bash-tacplus_*.deb', 'bash_*.deb', 'ifupdown2_*.deb', 'kdump-tools_*.deb', 'libnss-radius_*.deb', 'libnss-tacplus_*.deb', 'libpam-radius-auth_*.deb', 'libpam-tacplus_*.deb', 'libtac2_*.deb', 'libyang3_*.deb', 'makedumpfile_*.deb', 'monit_*.deb', 'python3-libyang_*.deb', 'rasdaemon_*.deb', 'sedutil_*.deb', 'sonic-ctrmgrd-rs_*.deb', 'sonic-db-cli_*.deb', 'sonic-device-data_*.deb', 'sonic-host-services-data_*.deb', 'sonic-host-services-rs_*.deb', 'sonic-nettools_*.deb', 'sonic-rsyslog-plugin_*.deb', 'sonic-utilities-data_*.deb', 'sonic-ztp_*.deb', 'syslog-counter_*.deb', 'openssh-server_10.0p1-7_*.deb', 'openssh-client_10.0p1-7_*.deb', 'openssh-sftp-server_10.0p1-7_*.deb', 'initramfs-tools-core_*.deb', 'initramfs-tools_*.deb', 'linux-image-6.1-*_amd64.deb']:
            if pattern == "sonic-ztp_*.deb":
                self.capture["body_globs"][pattern] = []
                continue
            replacement = "amd64" if pattern.startswith("openssh-") else "1_amd64"
            if pattern.startswith("linux-image-"):
                replacement = "sonic_1"
            if pattern.startswith("initramfs-"):
                replacement = "1_all"
            name = pattern.replace("*", replacement)
            self.add_deb(name)
            self.capture["body_globs"][pattern] = ["target/debs/trixie/" + name]
        self.add_deb("kbuild_1_amd64.deb")
        variables["LINUX_KBUILD"] = "kbuild_1_amd64.deb"

    def add_deb(self, name):
        self.capture["debs"][name] = {
            "depends": [], "wheel_depends": [], "rdepends": [], "phonies": [],
            "cache_override": "none", "cache_mode": "none", "platform": "", "machine": "",
        }
        self.capture["variables"]["SONIC_DPKG_DEBS"] = " ".join(sorted(self.capture["debs"]))
        self.debs.add("target/debs/trixie/" + name)

    def add_wheel(self, name):
        self.capture["wheels"][name] = {
            "depends": [], "debs_depends": [], "files": [], "cache_override": "none", "cache_mode": "none",
        }
        self.capture["variables"]["SONIC_PYTHON_WHEELS"] = " ".join(sorted(self.capture["wheels"]))
        self.wheels.add("target/python-wheels/trixie/" + name)

    def compare(self):
        return plan.required_make_packages(self.capture, sorted(self.debs), sorted(self.wheels))

    def test_host_install_walks_mixed_transitive_edges_and_cycles_once(self):
        self.fixture()
        self.add_deb("host_1_amd64.deb")
        self.add_wheel("host.whl")
        self.add_wheel("leaf.whl")
        self.capture["debs"]["kbuild_1_amd64.deb"]["depends"] = ["host_1_amd64.deb"]
        self.capture["debs"]["host_1_amd64.deb"]["wheel_depends"] = ["host.whl"]
        self.capture["wheels"]["host.whl"]["depends"] = ["leaf.whl"]
        self.capture["wheels"]["leaf.whl"]["debs_depends"] = ["kbuild_1_amd64.deb"]
        result = self.compare()
        self.assertEqual(result["host_deb_installs"], ["host_1_amd64.deb", "kbuild_1_amd64.deb"])
        self.assertEqual(result["host_wheel_installs"], ["host.whl", "leaf.whl"])

    def test_swss_runtime_sibling_is_required_even_when_producer_is_reused(self):
        self.fixture()
        for name in ("swss_1_amd64.deb", "swss-runtime_1_amd64.deb"):
            self.add_deb(name)
        self.capture["variables"]["LIBSWSSCOMMON"] = "swss_1_amd64.deb"
        self.capture["debs"]["swss_1_amd64.deb"]["rdepends"] = ["swss-runtime_1_amd64.deb"]
        self.debs.remove("target/debs/trixie/swss-runtime_1_amd64.deb")
        with self.assertRaisesRegex(ValueError, "not admitted"):
            self.compare()

    def test_recipe_body_requires_redis_python3_wheel(self):
        self.fixture()
        self.add_wheel("redis-py3.whl")
        self.capture["variables"]["REDIS_DUMP_LOAD_PY3"] = "redis-py3.whl"
        self.wheels.clear()
        with self.assertRaisesRegex(ValueError, "wheels are not admitted"):
            self.compare()

    def test_fips_body_packages_require_explicit_admission(self):
        self.fixture()
        self.add_deb("fips-python_1_all.deb")
        self.capture["variables"]["FIPS_BASEIMAGE_PYTHON_INSTALLERS"] = "fips-python_1_all.deb"
        self.debs.remove("target/debs/trixie/fips-python_1_all.deb")
        with self.assertRaisesRegex(ValueError, "not admitted"):
            self.compare()

    def test_actual_glob_cannot_hide_unreviewed_version(self):
        self.fixture()
        self.capture["body_globs"]["libyang3_*.deb"].append("target/debs/trixie/libyang3_2_amd64.deb")
        with self.assertRaisesRegex(ValueError, "glob differs"):
            self.compare()

    def test_openssh_glob_cannot_hide_another_architecture(self):
        self.fixture()
        name = "openssh-client_10.0p1-7_arm64.deb"
        self.add_deb(name)
        self.capture["body_globs"]["openssh-client_10.0p1-7_*.deb"].append("target/debs/trixie/" + name)
        with self.assertRaisesRegex(ValueError, "outside the AMD64"):
            self.compare()

    def test_actual_glob_must_contain_every_admitted_matching_input(self):
        self.fixture()
        self.add_deb("libyang3_2_amd64.deb")
        with self.assertRaisesRegex(ValueError, "glob differs"):
            self.compare()

    def test_missing_capture_policy_cannot_drop_the_policy_check(self):
        self.fixture()
        del self.capture["variables"]["SONIC_DPKG_CACHE_METHOD"]
        with self.assertRaisesRegex(ValueError, "profile differs"):
            self.compare()

    def test_selected_package_cache_override_is_refused(self):
        self.fixture()
        self.capture["debs"]["kbuild_1_amd64.deb"]["cache_override"] = "rwcache"
        with self.assertRaisesRegex(ValueError, "cache override"):
            self.compare()

    def test_unselected_vendor_package_does_not_expand_scope(self):
        self.fixture()
        self.add_deb("unused-vendor_1_arm64.deb")
        self.capture["debs"]["unused-vendor_1_arm64.deb"]["cache_override"] = "rwcache"
        self.debs.remove("target/debs/trixie/unused-vendor_1_arm64.deb")
        result = self.compare()
        self.assertFalse(any("unused-vendor" in path for path in result["required_debs"]))

    def test_reused_wheel_does_not_walk_suppressed_producer_dependencies(self):
        self.fixture()
        self.add_wheel("ready.whl")
        self.capture["variables"]["SONIC_UTILITIES_PY3"] = "ready.whl"
        self.capture["wheels"]["ready.whl"]["debs_depends"] = ["unbuilt_1_amd64.deb"]
        result = self.compare()
        self.assertEqual(result["required_wheels"], ["target/python-wheels/trixie/ready.whl"])
        self.assertEqual(result["host_wheel_installs"], [])

    def test_inactive_template_glob_does_not_require_a_package(self):
        self.fixture()
        self.capture["variables"]["INCLUDE_SYSTEM_EVENTD"] = "n"
        self.capture["body_globs"]["sonic-rsyslog-plugin_*.deb"] = ["target/debs/trixie/unreviewed-unused.deb"]
        result = self.compare()
        self.assertFalse(any("sonic-rsyslog-plugin" in p for p in result["required_debs"]))

    def test_direct_make_prerequisite_remains_required_when_template_branch_is_off(self):
        self.fixture()
        name = "sonic-rsyslog-plugin_1_amd64.deb"
        self.capture["variables"].update(INCLUDE_SYSTEM_EVENTD="n", SONIC_RSYSLOG_PLUGIN=name)
        self.debs.remove("target/debs/trixie/" + name)
        with self.assertRaisesRegex(ValueError, "not admitted"):
            self.compare()

    def test_file_host_dependencies_and_rfs_roots_are_not_lost(self):
        self.fixture()
        self.add_deb("file-host_1_amd64.deb")
        self.capture["variables"]["SONIC_MAKE_FILES"] = "selected.txt"
        self.capture["files"]["selected.txt"] = {"path": "source", "depends": ["file-host_1_amd64.deb"], "phonies": []}
        self.capture["installer"]["FILES"] = "selected.txt"
        rfs = "sonic-vs.bin__vs__rfs.squashfs"
        self.capture["variables"]["SONIC_RFS_TARGETS"] = rfs
        self.capture["installer"]["RFS_DEPENDS"] = rfs
        self.capture["rfs"][rfs] = {"dependent_rfs": [], "installer": "sonic-vs.bin", "machine": "vs"}
        result = self.compare()
        self.assertIn("file-host_1_amd64.deb", result["host_deb_installs"])
        self.assertEqual(result["selected_rfs"], [rfs])
        self.assertEqual(result["selected_files"], ["selected.txt"])

    def test_missing_registered_row_is_not_treated_as_an_empty_dependency_set(self):
        self.fixture()
        del self.capture["debs"]["kbuild_1_amd64.deb"]
        with self.assertRaisesRegex(ValueError, "registration capture is incomplete"):
            self.compare()

    def test_missing_glob_row_is_not_treated_as_no_match(self):
        self.fixture()
        del self.capture["body_globs"]["libtac2_*.deb"]
        with self.assertRaisesRegex(ValueError, "glob capture is incomplete"):
            self.compare()

    def test_comparison_preserves_original_capture_and_admission(self):
        self.fixture()
        before = json.dumps(self.capture, sort_keys=True), set(self.debs), set(self.wheels)
        self.compare()
        self.assertEqual(before, (json.dumps(self.capture, sort_keys=True), self.debs, self.wheels))

    def test_selected_phony_requires_further_review_not_implicit_admission(self):
        self.fixture()
        self.capture["debs"]["kbuild_1_amd64.deb"]["phonies"] = ["unknown-builder"]
        result = self.compare()
        self.assertEqual(result["host_phonies_requiring_review"], ["unknown-builder"])
        self.assertIn("remain required", result["scope"])



class MetadataCachePolicyTests(unittest.TestCase):
    def metadata(self, debs, wheels):
        root = Path("/fixture-assembly")
        output = io.StringIO()
        argv = ["planner", "metadata", "--workspace", str(root), "--manifest", str(root / "input.json"),
                "--manifest-sha256", "a" * 64, "--merge", "b" * 40,
                "--producer-workspace", "/fixture-producer", "--registry-workspace", "/fixture-registry"]
        with mock.patch.object(plan.sys, "argv", argv + source_cli()), \
             mock.patch.object(plan, "inputs", return_value=(root, {})), \
             mock.patch.object(plan, "package_paths", return_value=debs), \
             mock.patch.object(plan, "wheel_paths", return_value=wheels), \
             mock.patch.object(plan, "kernel_arguments", return_value=["KERNEL_FIXTURE=unchanged"]), \
             mock.patch.object(plan, "bazel_marker_archives", return_value=["target/docker-example.gz"]), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("No subprocess")), \
             contextlib.redirect_stdout(output):
            plan.main()
        return json.loads(output.getvalue())["argv"]

    def test_metadata_and_final_make_have_identical_explicit_package_cache_policy(self):
        debs = ["target/debs/trixie/host_1_amd64.deb"]
        wheels = ["target/python-wheels/trixie/tool-1-py3-none-any.whl"]
        metadata = self.metadata(debs, wheels)
        final = plan.make_command(["target/docker-x.gz"], ["KERNEL_FIXTURE=unchanged"],
                                  debs=debs, wheels=wheels)
        expected = {"host_1_amd64.deb_CACHE_OVERRIDE=none", "tool-1-py3-none-any.whl_CACHE_OVERRIDE=none"}
        self.assertEqual({x for x in metadata if x.endswith("_CACHE_OVERRIDE=none")}, expected)
        self.assertEqual({x for x in final if x.endswith("_CACHE_OVERRIDE=none")}, expected)
        for key in ("SONIC_DPKG_CACHE_METHOD=none", "SONIC_DPKG_CACHE_METHOD_OVERRIDE=none"):
            self.assertIn(key, metadata)
            self.assertIn(key, final)
        self.assertEqual(metadata[-1], "KERNEL_FIXTURE=unchanged")

    def test_metadata_never_turns_admitted_inputs_into_build_targets(self):
        metadata = self.metadata(["target/debs/trixie/host_1_amd64.deb"], [])
        self.assertIn("vs-reuse-selection", metadata)
        self.assertNotIn("target/sonic-vs.bin", metadata)
        self.assertFalse(any(x.startswith("--old-file=") for x in metadata))

    def test_metadata_rejects_nonpackage_or_duplicate_cache_overrides(self):
        for debs, wheels in (([".platform"], []),
                            (["target/debs/trixie/a.deb"] * 2, []),
                            ([], ["target/python-wheels/trixie/tool.whl"] * 2)):
            with self.subTest(debs=debs, wheels=wheels), self.assertRaises(ValueError):
                self.metadata(debs, wheels)


class ConfiguredSourceClosureTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        hook = "files/build_templates/organization_extensions.sh"
        recipes = {"slave.mk": b"reviewed recipe fixture\n", hook: b"reviewed inert hook fixture\n"}
        copies = {name: (name + " fixture\n").encode() for name in plan.CTR_COPY_SOURCES}
        for name, body in recipes.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        for name, body in copies.items():
            target = root / "src/sonic-ctrmgrd/ctrmgr" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        patches = [
            mock.patch.object(plan, "REVIEWED_MAKE_SOURCES",
                              {name: hashlib.sha256(body).hexdigest() for name, body in recipes.items()}),
            mock.patch.object(plan, "CTR_COPY_SOURCES",
                              {name: hashlib.sha256(body).hexdigest() for name, body in copies.items()}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        kbuild = "linux-kbuild-fixture_1_amd64.deb"
        common = "linux-headers-fixture-common-sonic_1_all.deb"
        image = "linux-image-fixture_1_amd64.deb"
        rfs = "sonic-vs.bin__vs__rfs.squashfs"
        capture = {
            "variables": {"SECURE_UPGRADE_MODE": '"no_sign"', "ENABLE_ORGANIZATION_EXTENSIONS": "y",
                          "SONIC_PACKAGES": "", "FILES_PATH": "target/files/trixie",
                          "SONIC_COPY_FILES": " ".join(copies), "SONIC_ONLINE_FILES": "",
                          "SONIC_MAKE_FILES": "", "LINUX_KBUILD": kbuild},
            "extra_exports_present": False,
            "optional_inputs": {hook: True, "rules/config.organization": False,
                "files/build_templates/build_debian.organization.sh": False,
                "files/build_templates/sonic_debian_extension_organization_packages.j2": False,
                "files/build_templates/sonic_debian_extension_organization_docker_images.j2": False},
            "files": {name: {"path": "src/sonic-ctrmgrd/ctrmgr", "depends": [], "phonies": []}
                      for name in copies},
            "rfs": {rfs: {"dependent_rfs": [], "installer": "sonic-vs.bin", "machine": "vs"}},
            "debs": {kbuild: {"depends": [common]}},
        }
        packages = {"selected_files": sorted(copies), "selected_rfs": [rfs],
                    "host_deb_installs": sorted([kbuild, common]), "host_wheel_installs": [],
                    "host_phonies_requiring_review": []}
        kernels = ["target/debs/trixie/" + name for name in (kbuild, common, image)]
        return root, capture, packages, kernels

    def test_exact_copies_rfs_and_inert_organization_source_are_accepted(self):
        result = plan.configured_source_closure(*self.fixture())
        self.assertEqual(len(result["selected_files"]), 5)
        self.assertEqual(result["selected_rfs"], ["sonic-vs.bin__vs__rfs.squashfs"])
        self.assertEqual(result["organization_hook"], "exact reviewed inert source")

    def test_changed_copied_source_is_rejected(self):
        root, capture, packages, kernels = self.fixture()
        source = root / "src/sonic-ctrmgrd/ctrmgr/container"
        source.write_bytes(b"changed source\n")
        with self.assertRaisesRegex(ValueError, "ctrmgr source changed"):
            plan.configured_source_closure(root, capture, packages, kernels)

    def test_absent_hook_cannot_silently_appear(self):
        root, capture, packages, kernels = self.fixture()
        path = root / "rules/config.organization"
        path.parent.mkdir()
        path.write_text("unexpected hook\n")
        with self.assertRaisesRegex(ValueError, "Optional hook presence"):
            plan.configured_source_closure(root, capture, packages, kernels)

    def test_make_file_rfs_and_phony_expansion_require_review(self):
        for change in ("make", "rfs", "phony"):
            with self.subTest(change=change):
                root, capture, packages, kernels = self.fixture()
                if change == "make":
                    capture["variables"]["SONIC_MAKE_FILES"] = "container"
                elif change == "rfs":
                    packages["selected_rfs"].append("another-rfs")
                else:
                    packages["host_phonies_requiring_review"].append("build-package")
                with self.assertRaises(ValueError):
                    plan.configured_source_closure(root, capture, packages, kernels)

    def test_admitted_kernel_image_is_not_a_permitted_host_install(self):
        root, capture, packages, kernels = self.fixture()
        packages["host_deb_installs"].append("linux-image-fixture_1_amd64.deb")
        with self.assertRaisesRegex(ValueError, "kbuild/common headers"):
            plan.configured_source_closure(root, capture, packages, kernels)

    def test_existing_copy_target_must_match_the_reviewed_source(self):
        root, capture, packages, kernels = self.fixture()
        target = root / "target/files/trixie/container"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stale previously copied bytes\n")
        with self.assertRaisesRegex(ValueError, "Existing copied payload"):
            plan.configured_source_closure(root, capture, packages, kernels)

    def test_plan_does_not_admit_a_fresh_checkout_with_empty_body_globs(self):
        from types import SimpleNamespace
        case = ConfiguredPackageClosureTests("test_host_install_walks_mixed_transitive_edges_and_cycles_once")
        case.fixture()
        case.capture["body_globs"]["monit_*.deb"] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "target/sonic-vs.bin.make-inputs.tsv"
            source.parent.mkdir()
            source.write_bytes(b"mocked actual capture bytes\n")
            args = fixture_args(make_inputs=source, make_inputs_sha256="a" * 64,
                                      manifest_sha256="b" * 64, receipt=root / "capture.json")
            fake = SimpleNamespace(capture=mock.Mock(return_value={}), verify=mock.Mock())
            with mock.patch("tools.bazel.ci.vs_final23", fake, create=True), \
                 mock.patch.object(plan, "inputs", return_value=(root, {"groups": {"kernel": []}})), \
                 mock.patch.object(plan, "verify_native_inputs", return_value=({}, {})), \
                 mock.patch.object(plan, "archive_paths", return_value=["target/docker-fixture.gz"]), \
                 mock.patch.object(plan, "bazel_marker_archives", return_value=["target/docker-fixture.gz"]), \
                 mock.patch.object(plan, "package_paths", return_value=sorted(case.debs)), \
                 mock.patch.object(plan, "wheel_paths", return_value=[]), \
                 mock.patch.object(plan, "kernel_arguments", return_value=[]), \
                 mock.patch.object(plan, "make_input_capture", return_value=case.capture), \
                 mock.patch.object(plan, "save_capture") as save:
                with self.assertRaisesRegex(ValueError, "Recipe-body glob"):
                    plan.plan(args)
                save.assert_not_called()



class FrozenInventoryBoundaryTests(unittest.TestCase):
    def fixture(self, root):
        value = {"schema": 1, "kind": "sonic-final23-reuse-input-inventory",
                 "bazel_archive_builds": {},
                 "source": {"head": SOURCE_HEAD, "tree": SOURCE_TREE, "merge": "b" * 40,
                            "controller": plan.WORKER, "catalog": plan.CATALOG, "pr": 206},
                 "assembly": dict(ASSEMBLY),
                 "build_environment": {"BUILD_NUMBER": "1", "BUILD_TIMESTAMP": "20261113.0",
                     "SOURCE_DATE_EPOCH": "1794549600", "BUILD_HOSTNAME": "fixture",
                     "SONIC_IMAGE_VERSION": "bazel-ci"},
                 "files": {"target/plain-input": {"bytes": 1, "sha256": "e" * 64}}}
        path = root / "inventory.json"
        path.write_text(json.dumps(value))
        args = fixture_args(workspace=root, manifest=path,
                            manifest_sha256=plan.digest(path.read_bytes()), merge="b" * 40,
                            require_environment=False)
        return value, args

    def test_metadata_admission_does_not_claim_missing_payloads_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value, args = self.fixture(root)
            with mock.patch.object(plan, "file_digest", side_effect=AssertionError("No metadata payload read")):
                self.assertEqual(plan.inventory(args), (root, value))
            with self.assertRaisesRegex(ValueError, "Missing original input"):
                plan.inputs(args)
            self.assertFalse((root / "target/plain-input").exists())

    def test_restamped_inventory_cannot_change_the_workflow_source(self):
        for key in ("head", "tree", "merge", "controller"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                value, args = self.fixture(root)
                value["source"][key] = "f" * 40
                args.manifest.write_text(json.dumps(value))
                args.manifest_sha256 = plan.digest(args.manifest.read_bytes())
                with self.assertRaisesRegex(ValueError, "workflow/native source domain"):
                    plan.inventory(args)


if __name__ == "__main__":
    unittest.main()
