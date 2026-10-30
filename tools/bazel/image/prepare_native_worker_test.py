#!/usr/bin/env python3
"""Check worker identity and fail-closed native input preparation."""

import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import prepare_native_worker as prepare


class NativeWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.archives = self.workspace / "artifacts/repository-inputs"
        self.archives.mkdir(parents=True)
        self.evidence = Path("artifacts/vs")
        self.tag = "sonic-slave-trixie:123456789ab"
        self.tag_output = "native build diagnostics\n" + self.tag + "\n"
        self.base = {"Id": "sha256:" + "a" * 64, "RootFS": {"Layers": ["base-a", "base-b"]}}
        self.worker_id = "sha256:" + "b" * 64
        self.worker = {"Id": self.worker_id, "RootFS": {"Layers": ["base-a", "base-b", "worker"]}}
        self.commands = []
        self.failure = None

    def execute(self, argv, **kwargs):
        self.assertEqual(kwargs["cwd"], self.workspace)
        self.assertTrue(kwargs["check"])
        self.commands.append(argv)
        if self.failure is not None and self.failure in argv:
            raise subprocess.CalledProcessError(17, argv)
        if argv[0] == "make":
            kwargs["stdout"].write(self.tag_output)
        elif argv[:2] == ["docker", "build"]:
            (self.workspace / "target/bazel-image-worker.id").write_text(self.worker_id + "\n")
        elif "tools/bazel/gzip/source_archive.py" in argv:
            kwargs["stdout"].write('[{"integrity":"verified-source"}]\n')

    def inspect(self, argv, **kwargs):
        self.assertEqual(kwargs, {"cwd": self.workspace, "text": True})
        self.assertEqual(argv[:3], ["docker", "image", "inspect"])
        self.assertIn(argv[3], (self.tag, self.worker_id))
        return json.dumps([self.base if argv[3] == self.tag else self.worker])

    def run_prepare(self):
        with patch.object(prepare.subprocess, "run", side_effect=self.execute), \
                patch.object(prepare.subprocess, "check_output", side_effect=self.inspect):
            prepare.prepare(self.workspace, Path("artifacts/repository-inputs"), self.evidence)

    def test_freezes_native_worker_identity_before_verifying_and_carrying_sources(self):
        self.run_prepare()
        spec = json.loads((self.workspace / "target/bazel-image-worker.json").read_text())
        self.assertEqual(spec, {
            "schema": 1, "platform": "linux/amd64", "distribution": "trixie",
            "worker_image": self.worker_id, "sonic_slave_image": self.base["Id"],
            "docker_version": "28.5.2", "containerd_version": "1.7.28",
            "storage_driver": "overlay2",
        })
        self.assertEqual(self.commands[1],
                         ["make", "-s", "-f", "Makefile.work", "BLDENV=trixie", "showtag"])
        self.assertIn("BASE_IMAGE=" + self.tag, self.commands[2])
        frozen, sources = self.commands[-2:]
        self.assertIn("tools/bazel/image/prepare_inputs.py", frozen)
        self.assertIn("target/bazel-native", frozen)
        self.assertIn("target/bazel-image-worker.json", frozen)
        self.assertIn("tools/bazel/gzip/source_archive.py", sources)
        self.assertEqual(sources[sources.index("--archives-dir") + 1], str(self.archives))
        self.assertEqual(sources[sources.index("--output") + 1],
                         "target/bazel-image-inputs/source-downloads")
        receipt = self.workspace / self.evidence / "assembly-source-download-receipt.json"
        self.assertEqual(json.loads(receipt.read_text()), [{"integrity": "verified-source"}])

    def test_missing_or_ambiguous_slave_tag_stops_before_docker_build(self):
        for output in ("", "sonic-slave-bookworm:123456789ab\n", self.tag + "\n" + self.tag + "\n"):
            with self.subTest(output=output):
                self.commands.clear()
                self.tag_output = output
                with self.assertRaisesRegex(ValueError, "exactly one native Trixie"):
                    self.run_prepare()
                self.assertFalse(any(argv[0] == "docker" for argv in self.commands))
                self.assertFalse((self.workspace / "target/bazel-image-worker.json").exists())

    def test_different_or_empty_native_layer_ancestry_cannot_freeze_inputs(self):
        cases = [(["other-base", "base-b", "worker"], ["base-a", "base-b"]),
                 (["base-a"], ["base-a", "base-b"]), (["worker"], [])]
        for worker_layers, native_layers in cases:
            with self.subTest(worker_layers=worker_layers, native_layers=native_layers):
                self.commands.clear()
                self.worker["RootFS"]["Layers"] = worker_layers
                self.base["RootFS"]["Layers"] = native_layers
                with self.assertRaisesRegex(ValueError, "does not retain"):
                    self.run_prepare()
                self.assertFalse(any("tools/bazel/image/prepare_inputs.py" in argv
                                     for argv in self.commands))
                self.assertFalse((self.workspace / "target/bazel-image-worker.json").exists())

    def test_inspected_worker_must_match_the_immutable_build_output(self):
        worker = copy.deepcopy(self.worker)
        worker["Id"] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(ValueError, "immutable"):
            prepare.worker_spec(self.base, worker, self.worker_id)
        with self.assertRaisesRegex(ValueError, "immutable"):
            prepare.worker_spec(self.base, self.worker, "mutable-worker:latest")

    def test_failed_input_provenance_check_cannot_publish_source_receipt(self):
        self.failure = "tools/bazel/image/prepare_inputs.py"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_prepare()
        self.assertFalse(any("tools/bazel/gzip/source_archive.py" in argv for argv in self.commands))
        self.assertFalse((self.workspace / self.evidence / "assembly-source-download-receipt.json").exists())

    def test_source_archive_verification_failure_is_not_ignored(self):
        self.failure = "tools/bazel/gzip/source_archive.py"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_prepare()

    def test_cli_requires_archives_and_defaults_to_native_evidence_directory(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failed:
            prepare.main([])
        self.assertEqual(failed.exception.code, 2)
        with patch.object(prepare, "prepare") as invoked:
            prepare.main(["--archives-dir", "artifacts/repository-inputs"])
        invoked.assert_called_once_with(Path.cwd(), Path("artifacts/repository-inputs"),
                                        Path("artifacts/unprivileged"))


if __name__ == "__main__":
    unittest.main()
