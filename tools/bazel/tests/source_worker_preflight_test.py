#!/usr/bin/env python3
"""Reject unsupported worker limits and untrusted image markers."""

import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("source_worker_preflight", ROOT / "tools/bazel/ci/source_worker_preflight.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class WorkerPreflightTest(unittest.TestCase):
    def preflight_job(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())["jobs"]["source_worker_preflight"]

    def test_job_environment_uses_only_contexts_available_before_runner_assignment(self):
        # GitHub contexts reference: jobs.<job_id>.env excludes runner and env.
        allowed = {"github", "needs", "strategy", "matrix", "vars", "secrets", "inputs"}
        def contexts(value):
            return {name for expression in re.findall(r"\$\{\{(.*?)\}\}", value)
                    for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.", expression)}
        for name, value in self.preflight_job()["env"].items():
            with self.subTest(name=name):
                self.assertLessEqual(contexts(value), allowed)
        self.assertFalse(contexts("${{ runner.temp }}/receipt.json") <= allowed)

    def test_preflight_uses_a_fresh_checkout_and_receipt_without_touching_previous_work(self):
        job = self.preflight_job()
        steps = {step["name"]: step for step in job["steps"]}
        checkout = steps["Checkout dispatcher"]["with"]
        self.assertEqual(checkout["path"], "${{ env.PREFLIGHT_CONTROL }}")
        self.assertIs(checkout["clean"], False)
        self.assertIs(checkout["persist-credentials"], False)
        guard = steps["Require fresh preflight paths"]["run"]
        measure = steps["Measure this worker's resources and namespace capabilities"]["run"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, temporary = root / "workspace", root / "temporary"
            workspace.mkdir(); temporary.mkdir()
            old_checkout = workspace / "source"; old_checkout.mkdir()
            old_output = old_checkout / "retained-output"; old_output.write_text("previous build")
            old_receipt = workspace / "artifacts/source-worker/preflight.json"
            old_receipt.parent.mkdir(parents=True); old_receipt.write_text("previous receipt")
            name = "source-worker-preflight-123-2-sonic-slave-lgh114-1"
            receipt = temporary / (name + ".json")
            environment = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temporary),
                           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "EXPECTED_WORKER": worker.WORKERS[0],
                           "PREFLIGHT_CONTROL": name, "PREFLIGHT_RECEIPT": receipt.name}
            subprocess.run(["bash", "-e", "-c", guard], env=environment, check=True, capture_output=True)
            self.assertFalse((workspace / name).exists())
            self.assertFalse(receipt.exists())
            # Execute the actual workflow command against a harmless checkout fixture.
            helper = workspace / name / "tools/bazel/ci/source_worker_preflight.py"
            helper.parent.mkdir(parents=True)
            helper.write_text("import json,sys\nfrom pathlib import Path\n"
                              "assert sys.argv[1:3] == ['--worker','sonic-slave-lgh114-1']\n"
                              "assert sys.argv[3] == '--output'\n"
                              "Path(sys.argv[4]).write_text(json.dumps({'status':'fixture'}))\n")
            subprocess.run(["bash", "-e", "-c", measure], env=environment, check=True, capture_output=True)
            self.assertEqual(json.loads(receipt.read_text()), {"status": "fixture"})
            self.assertEqual(old_output.read_text(), "previous build")
            self.assertEqual(old_receipt.read_text(), "previous receipt")
            self.assertEqual(steps["Retain measured resource evidence"]["with"]["path"], "${{ runner.temp }}/${{ env.PREFLIGHT_RECEIPT }}")

    def test_preflight_refuses_existing_or_misdirected_paths_without_cleaning_them(self):
        script = self.preflight_job()["steps"][0]["run"]
        for case in ("existing checkout", "existing receipt", "linked checkout", "wrong path", "wrong worker", "wrong attempt"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); workspace = root / "workspace"; workspace.mkdir()
                temporary = root / "temporary"; temporary.mkdir()
                name = "source-worker-preflight-123-2-sonic-slave-lgh114-1"
                control, receipt = workspace / name, temporary / (name + ".json")
                keep = workspace / "existing-output"; keep.write_text("keep")
                environment = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temporary),
                               "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "EXPECTED_WORKER": worker.WORKERS[0],
                               "PREFLIGHT_CONTROL": name, "PREFLIGHT_RECEIPT": receipt.name}
                if case == "existing checkout": control.mkdir(); (control / "keep").write_text("keep")
                if case == "existing receipt": receipt.write_text("keep")
                if case == "linked checkout": control.symlink_to(workspace / "missing")
                if case == "wrong path": environment["PREFLIGHT_CONTROL"] = "../" + name
                if case == "wrong worker": environment["EXPECTED_WORKER"] = "sonic-slave-lgh114-1-extra"
                if case == "wrong attempt": environment["GITHUB_RUN_ATTEMPT"] = "../2"
                result = subprocess.run(["bash", "-e", "-c", script], env=environment, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(keep.read_text(), "keep")
                if case == "existing checkout": self.assertEqual((control / "keep").read_text(), "keep")
                if case == "existing receipt": self.assertEqual(receipt.read_text(), "keep")
                if case == "linked checkout": self.assertTrue(control.is_symlink())

    def test_preflight_routes_only_existing_four_workers_and_uploads_only_new_measurement(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition
        flow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        self.assertEqual([name for name, job in flow["jobs"].items()
                          if condition(str(job.get("if", "true")), mode="preflight")], ["source_worker_preflight"])
        job = flow["jobs"]["source_worker_preflight"]
        self.assertEqual(job["strategy"]["matrix"]["worker"], list(worker.WORKERS))
        self.assertEqual(job["runs-on"], ["sonic-slave-container-dpool", "${{ matrix.worker }}"])
        upload = job["steps"][-1]
        self.assertIn("github.run_attempt", upload["with"]["name"])
        expression = upload["if"].replace("&&", " and ").replace("||", " or ")
        for outcome, expected in (("success", True), ("failure", True), ("skipped", False), ("", False)):
            actual = eval(expression, {"__builtins__": {}}, {"always": lambda: True,
                "steps": SimpleNamespace(measure=SimpleNamespace(outcome=outcome))})
            self.assertEqual(actual, expected)

    def test_real_cgroup_cpu_format(self):
        self.assertEqual(worker.cpu_limit("600000 100000\n"), 6)
        self.assertEqual(worker.cpu_limit("250000 100000\n"), 2.5)
        for value in ("max 100000", "0 100000", "600000 0", "invalid", "-1 100000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.cpu_limit(value)

    def test_marker_must_be_immutable_and_identify_the_selected_slave(self):
        value = {"uid": 1000, "gid": 1000, "runner_root": "/opt/actions-runner", "work_root": "/actions/_work",
                 "base_image": "sha256:" + "a" * 64}
        path = mock.Mock()
        path.read_text.return_value = json.dumps(value)
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=0)
        self.assertEqual(worker.marker_identity(path), {"base_image": value["base_image"]})
        for mode, uid in ((stat.S_IFREG | 0o644, 0), (stat.S_IFLNK | 0o444, 0), (stat.S_IFREG | 0o444, 1000)):
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
            with self.subTest(mode=mode, uid=uid), self.assertRaises(ValueError):
                worker.marker_identity(path)
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=0)
        value["base_image"] = "latest"
        path.read_text.return_value = json.dumps(value)
        with self.assertRaises(ValueError):
            worker.marker_identity(path)

    def test_measured_image_memory_is_required_by_real_preflight(self):
        # The old 10 GiB workers passed isolation, but the cold Sysmgr image
        # consumed 14.69 GiB. Exercise the actual cgroup gate at its boundary.
        for gib, expected in ((10, False), (16, False), (20, True), (24, True)):
            values = {
                "/sys/fs/cgroup/memory.max": str(gib * 1024**3),
                "/sys/fs/cgroup/memory.current": "1024",
                "/sys/fs/cgroup/cpu.max": "400000 100000",
                "/proc/self/cgroup": "0::/",
                "/sys/fs/cgroup/cgroup.type": "domain",
                "/sys/fs/cgroup/cgroup.controllers": "cpu memory pids",
            }
            with self.subTest(gib=gib), \
                    mock.patch.object(worker.Path, "read_text", autospec=True,
                                      side_effect=lambda path: values[str(path)]), \
                    mock.patch.object(worker, "marker_identity", return_value={"base_image": "sha256:" + "a" * 64}), \
                    mock.patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                    mock.patch.object(worker.subprocess, "check_output", return_value="ext4"), \
                    mock.patch.object(worker.shutil, "disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)):
                receipt = worker.measure(worker.WORKERS[0])
            self.assertEqual(receipt["checks"]["bounded_build_resources"], expected)
            self.assertEqual(receipt["facts"]["memory_limit_bytes"], gib * 1024**3)
            if expected:
                self.assertEqual(receipt["build_profile"]["name"], "two_actions")
                self.assertEqual(receipt["build_profile"]["measured_memory_limit_bytes"], gib * 1024**3)
                self.assertEqual(receipt["build_profile"]["measured_cpu_limit"], 4)
                self.assertEqual(receipt["build_profile"]["minimum_memory_bytes"], 20 * 1024**3)
            else:
                self.assertIsNone(receipt["build_profile"])

    def test_cpu_boundary_selects_two_actions_only_on_four_cpu_workers(self):
        for cpus, jobs, memory in ((2, 1, 4000), (3.999, 1, 4000), (4, 2, 8000), (6, 2, 8000)):
            with self.subTest(cpus=cpus):
                profile = worker.select_build_profile(20 * 1024**3, cpus)
                self.assertEqual((profile["jobs"], profile["cpu"], profile["scheduler_memory_mb"]), (jobs, jobs, memory))
                self.assertEqual(profile["measured_cpu_limit"], cpus)
                self.assertEqual(profile["jvm_heap_gib"], 3)
        for memory, cpus in ((20 * 1024**3 - 1, 4), (20 * 1024**3, 1.999),
                             (20 * 1024**3, 6.001), (20 * 1024**3, float("nan"))):
            with self.subTest(memory=memory, cpus=cpus), self.assertRaises(ValueError):
                worker.select_build_profile(memory, cpus)

    def test_failed_preflight_retains_public_receipt(self):
        receipt = {"schema": 1, "status": "failed", "checks": {"worker_affinity": False}, "facts": {"worker": "unexpected"}}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            with mock.patch.object(worker, "measure", return_value=receipt), \
                    mock.patch.object(worker.subprocess, "check_output", return_value="a" * 40), \
                    mock.patch("sys.argv", ["preflight", "--worker", worker.WORKERS[0], "--output", str(target)]), \
                    mock.patch("builtins.print"):
                self.assertEqual(worker.main(), 1)
            self.assertEqual(json.loads(target.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
