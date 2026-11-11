#!/usr/bin/env python3
"""Check ordinary and reviewed LLDP graph publication boundaries."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import public_artifacts as public

SOURCE, CONTROL, CATALOG = "a" * 40, "c" * 40, "b" * 40
PREFIX = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"


class LldpGraphDiagnosticsTest(unittest.TestCase):
    def fixture(self, root, catalog):
        workspace, directory, controller = root / "source", root / "artifacts", root / "control"
        workspace.mkdir(); directory.mkdir(); controller.mkdir()
        if catalog == "main":
            entrypoint = workspace / "tools/bazel/ci/libteam_draft_bazel.sh"
            entrypoint.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "tools/bazel/ci/libteam_draft_bazel.sh", entrypoint)
        else:
            entrypoint = root / "private-lldp-bazel"
            entrypoint.write_text("private fixture\n")
        graph = {"key": "<root>", "name": "sonic-buildimage", "version": "", "root": True,
                 "dependencies": [{"key": "fixture@1", "name": "fixture", "version": "1", "dependencies": []}]}
        lock = {"lockFileVersion": 24, "registryFileHashes": {
            PREFIX + "d" * 40 + "/modules/fixture/1/MODULE.bazel": "e" * 64}, "moduleExtensions": {}}
        (directory / "module-graph.json").write_text(json.dumps(graph) + "\n")
        (workspace / "MODULE.bazel.lock").write_text(json.dumps(lock) + "\n")
        (directory / "module-graph.stderr").write_text(
            "ERROR: GET returned 503 from https://snapshot.debian.org/private\nERROR: github_pat_" + "x" * 30 + "\n")
        argv = [str(entrypoint), "mod", "graph", "--output=json", "--extension_info=hidden",
                "--lockfile_mode=off", "--color=no", "--curses=no"]
        (directory / "module-graph-inspection.json").write_text(json.dumps({"argv": argv, "returncode": 7}))
        (directory / "build-receipt.json").write_text(json.dumps({"status": "failed", "revision": SOURCE}))
        return workspace, directory, controller, entrypoint

    def prepare(self, values, catalog, *, actual_source=SOURCE, actual_control=CONTROL):
        workspace, directory, controller, entrypoint = values
        def revision(argv, **_kwargs):
            return (actual_source if argv[2] == str(workspace) else actual_control) + "\n"
        with patch.object(public.subprocess, "check_output", side_effect=revision), patch.object(public.subprocess, "run"):
            public.prepare_lldp_graph_failure(workspace, directory, directory / "graph-failure", controller=controller,
                source_revision=SOURCE, controller_revision=CONTROL, registry_endpoint=PREFIX + catalog,
                bazel_entrypoint=entrypoint)

    def test_both_paths_publish_validated_bytes_and_fixed_diagnostics(self):
        for catalog in ("main", CATALOG):
            with self.subTest(catalog=catalog), tempfile.TemporaryDirectory() as temporary:
                values = self.fixture(Path(temporary), catalog)
                workspace, directory, _, _ = values
                self.prepare(values, catalog)
                output = directory / "graph-failure"
                self.assertEqual({path.name for path in output.iterdir()},
                                 {"module-graph.json", "MODULE.bazel.lock", "inspection-summary.json"})
                self.assertEqual((output / "module-graph.json").read_bytes(), (directory / "module-graph.json").read_bytes())
                self.assertEqual((output / "MODULE.bazel.lock").read_bytes(), (workspace / "MODULE.bazel.lock").read_bytes())
                summary = json.loads((output / "inspection-summary.json").read_text())
                self.assertEqual(summary["selection"]["registry_endpoint"], PREFIX + catalog)
                self.assertEqual(summary["selection"].get("registry_revision"), None if catalog == "main" else catalog)
                self.assertEqual(summary["lock_capture"], "post_failure")
                self.assertEqual(summary["diagnostic"]["http_statuses"], [503])
                self.assertNotIn("github_pat_", json.dumps(summary))
                self.assertNotIn("private-lldp-bazel", json.dumps(summary))
                stderr = (directory / "module-graph.stderr").read_bytes()
                self.assertEqual(summary["captures"]["stderr"]["sha256"], hashlib.sha256(stderr).hexdigest())
                public.safe_json(summary)

    def test_mismatched_identity_or_capture_is_not_published(self):
        cases = ("source", "controller", "receipt", "entrypoint", "graph", "lock", "symlink", "oversized")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                values = self.fixture(Path(temporary), CATALOG)
                workspace, directory, _, _ = values
                changes = {}
                if case == "source": changes["actual_source"] = "f" * 40
                if case == "controller": changes["actual_control"] = "f" * 40
                if case == "receipt": (directory / "build-receipt.json").write_text('{"status":"built"}')
                if case == "entrypoint":
                    path = directory / "module-graph-inspection.json"; value = json.loads(path.read_text())
                    value["argv"][0] = "/other/bazel"; path.write_text(json.dumps(value))
                if case == "graph": (directory / "module-graph.json").write_text('{"key":"<root>"}')
                if case == "lock": (workspace / "MODULE.bazel.lock").write_text('{"lockFileVersion":24}')
                if case == "symlink":
                    path = directory / "module-graph.stderr"; path.unlink(); path.symlink_to(workspace / "MODULE.bazel.lock")
                if case == "oversized":
                    with (directory / "module-graph.stderr").open("wb") as stream: stream.truncate(64 * 1024 * 1024 + 1)
                with self.assertRaises((ValueError, OSError)):
                    self.prepare(values, CATALOG, **changes)
                self.assertFalse((directory / "graph-failure").exists())

    def test_ordinary_upload_requires_failed_build_and_valid_bundle(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        step = workflow.split("- name: Retain public LLDP failed graph evidence", 1)[1].split("- name:", 1)[0]
        self.assertIn("steps.lldp_build.outcome == 'failure'", step)
        self.assertIn("steps.lldp_graph_failure.outcome == 'success'", step)
        self.assertIn("path: artifacts/lldp/graph-failure/", step)
        self.assertNotIn("module-graph.stderr", step)
        self.assertNotIn("continue-on-error", step)

    def test_partial_write_and_existing_destination_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = self.fixture(Path(temporary), CATALOG)
            directory = values[1]
            original = Path.write_bytes
            def fail_lock(path, value):
                if path.name == "MODULE.bazel.lock": raise OSError("fixture write failure")
                return original(path, value)
            with patch.object(Path, "write_bytes", fail_lock), self.assertRaises(OSError):
                self.prepare(values, CATALOG)
            self.assertFalse((directory / "graph-failure").exists())
            self.assertFalse(list(directory.glob("lldp-graph-*")))
            output = directory / "graph-failure"
            output.mkdir(); (output / "stale").write_text("unverified")
            with self.assertRaises(ValueError): self.prepare(values, CATALOG)
            self.assertEqual({path.name for path in output.iterdir()}, {"stale"})

    def test_symlinked_diagnostic_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = list(self.fixture(root, CATALOG))
            link = root / "linked-artifacts"; link.symlink_to(values[1], target_is_directory=True)
            values[1] = link
            with self.assertRaises(ValueError): self.prepare(values, CATALOG)
            self.assertFalse((link / "graph-failure").exists())

    def test_failure_after_successful_inspection_retains_only_fixed_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = self.fixture(Path(temporary), CATALOG)
            directory = values[1]
            path = directory / "module-graph-inspection.json"
            inspection = json.loads(path.read_text())
            inspection.update(returncode=0, module_count=2, module_graph_complete=True,
                              extension_inspection_complete=True, environment={"PRIVATE": "not public"})
            path.write_text(json.dumps(inspection))
            (directory / "module-graph.stderr").write_bytes(b"")
            self.prepare(values, CATALOG)
            summary = json.loads((directory / "graph-failure/inspection-summary.json").read_text())
            self.assertEqual(summary["diagnostic"]["returncode"], 0)
            self.assertEqual(summary["diagnostic"]["signals"], [])
            self.assertNotIn("environment", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
