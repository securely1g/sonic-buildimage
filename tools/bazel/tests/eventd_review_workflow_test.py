"""Exercise reviewed Eventd selection without Bazel or private services."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def document():
    return yaml.safe_load(WORKFLOW.read_text())


def step(job, name):
    return next(item for item in job["steps"] if item.get("name") == name)


class EventdReviewWorkflowTest(unittest.TestCase):
    def test_launcher_preserves_source_defaults_and_controller_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            controller = fixture / "controller"
            source = controller / "review-source"
            for name in ("review_bazel.sh", "review_bazel_rc.py"):
                target = controller / "tools/bazel/ci" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / "tools/bazel/ci" / name, target)
            common = source / "tools/bazel/common.bazelrc"
            common.parent.mkdir(parents=True)
            common.write_text((ROOT / "tools/bazel/common.bazelrc").read_text())
            (source / ".bazelrc.user").write_text("common --remote_cache=grpc://untrusted\ncommon --registry=https://untrusted\n")
            setup = fixture / "setup"
            setup.mkdir()
            (setup / ".bazelrc").write_text("build --disk_cache=/retained/disk\ncommon --repository_cache=/retained/repository\n")
            spec = importlib.util.spec_from_file_location("cache_handoff", ROOT / "tools/bazel/ci/preserve_setup_bazel_cache.py")
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            helper.preserve(controller, setup, private_remote_cache=True)
            with (controller / ".bazelrc.user").open("a") as stream:
                stream.write("common --downloader_config=/verified/mirrors.cfg\nbuild --remote_upload_local_results=false\n")
            recorder = fixture / "recorder"
            recorder.write_text("#!/usr/bin/python3\nimport json, os, sys\nfrom pathlib import Path\nargs = sys.argv[1:]\nrc = Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('--bazelrc=')))\nPath(os.environ['RECORD']).write_text(json.dumps({'argv': args, 'rc': rc.read_text()}))\n")
            recorder.chmod(0o755)
            record = fixture / "record.json"
            env = dict(os.environ, BAZEL_REAL=str(recorder), RECORD=str(record), SONIC_BAZEL_REGISTRY_REVISION="a" * 40, TMPDIR=str(fixture), PYTHONDONTWRITEBYTECODE="1")
            launcher = controller / "tools/bazel/ci/review_bazel.sh"
            subprocess.run([str(launcher), "--output_base=/isolated/proof", "build", "//dockers/docker-eventd:runtime"], cwd=source, env=env, check=True)
            observed = json.loads(record.read_text())
            self.assertIn("--output_base=/isolated/proof", observed["argv"])
            self.assertIn("--nohome_rc", observed["argv"])
            self.assertIn("--noworkspace_rc", observed["argv"])
            self.assertEqual([arg for arg in observed["argv"] if arg.startswith("--registry=")], ["--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" + "a" * 40, "--registry=https://bcr.bazel.build"])
            for line in common.read_text().splitlines():
                if line not in ("try-import %workspace%/.bazelrc.user",
                                "common --downloader_config=tools/bazel/debian_mirrors.cfg"):
                    self.assertIn(line, observed["rc"].splitlines())
            for line in (controller / ".bazelrc.user").read_text().splitlines():
                self.assertIn(line, observed["rc"].splitlines())
            self.assertNotIn("untrusted", observed["rc"])
            self.assertEqual([line for line in observed["rc"].splitlines() if "--downloader_config=" in line], ["common --downloader_config=/verified/mirrors.cfg"])
            self.assertEqual([line for line in observed["rc"].splitlines() if "--remote_upload_local_results=" in line][-1], "build --remote_upload_local_results=false")
            subprocess.run([str(launcher), "shutdown"], cwd=source, env=env, check=True)
            self.assertFalse(any(arg.startswith("--registry=") for arg in json.loads(record.read_text())["argv"]))
            rejected = subprocess.run([str(launcher), "build", "--registry=https://untrusted", "//:x"], cwd=source, env=env, capture_output=True)
            self.assertEqual(rejected.returncode, 2)
            with (controller / ".bazelrc.user").open("a") as stream:
                stream.write("common --registry=https://untrusted\n")
            rejected = subprocess.run([str(launcher), "build", "//:x"], cwd=source, env=env, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            policy = controller / ".bazelrc.user"
            policy.write_text(policy.read_text().replace("common --registry=https://untrusted\n", ""))
            with common.open("a") as stream:
                stream.write("common --remote_cache=grpc://untrusted\n")
            rejected = subprocess.run([str(launcher), "build", "//:x"], cwd=source, env=env, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)

    def test_workflow_confines_reviewed_selection_to_eventd(self):
        value = document()
        jobs = value["jobs"]
        native = jobs["eventd_review"]
        runtime = jobs["eventd_review_runtime"]
        archive = jobs["archive"]
        self.assertEqual(native["if"], "github.event_name == 'workflow_dispatch' && inputs.source_image == 'eventd'")
        self.assertEqual(native["runs-on"], ["self-hosted", "linux", "bazel-x64"])
        self.assertEqual(native["container"], archive["container"])
        self.assertEqual(native["env"]["SOURCE_DIRECTORY"], "review-source")
        self.assertEqual(native["env"]["BAZEL"], "${{ github.workspace }}/tools/bazel/ci/review_bazel.sh")
        self.assertNotIn("review-source", str(archive))
        self.assertNotIn("registry_revision", str(archive))
        self.assertEqual(archive["env"]["BAZEL"], "${{ github.workspace }}/tools/bazel/ci/libteam_draft_bazel.sh")
        self.assertNotIn("inputs.source_revision", str(jobs["eventd_runtime"]))
        self.assertNotIn("inputs.registry_revision", str(jobs["eventd_runtime"]))
        self.assertNotIn('"eventd"', jobs["source_image"]["if"])
        self.assertNotIn('"eventd"', jobs["source_image_inputs"]["if"])
        setup = step(native, "Set up Bazel")["with"]
        self.assertEqual(setup["disk-cache"], "swss-layer-amd64")
        for name in ("Configure remote cache uploads", "Preserve setup-bazel caches for source checks", "Restore default Bazel startup ownership", "Configure shared source download mirrors", "Bind the tested checkout identity"):
            self.assertNotIn('cd "$SOURCE_DIRECTORY"', step(native, name)["run"])
        for name in ("Build Eventd runtime and debug images", "Validate Eventd image contents, matching symbols and incremental reuse", "Bind the completed Eventd artifacts to the reviewed source"):
            self.assertTrue(step(native, name)["run"].startswith('cd "$SOURCE_DIRECTORY"\n'))
        identity = step(native, "Bind the tested checkout identity")["run"]
        self.assertIn("tools/bazel/ci/checkout_identity.py", identity)
        self.assertIn('--head-sha "$EVENT_HEAD_SHA" --event-base-sha "$EVENT_BASE_SHA"', identity)
        self.assertTrue(step(native, "Check selected native architecture and generated dependency state")["run"].startswith('cd "$SOURCE_DIRECTORY"\n'))
        publish = step(native, "Prepare Eventd archives and reviewed public evidence")["run"]
        self.assertNotIn('cd "$SOURCE_DIRECTORY"', publish)
        self.assertIn("tools/bazel/ci/eventd_image.py publish", publish)
        self.assertIn("from tools.bazel.ci.public_artifacts import safe_json", publish)
        upload = next(item for item in native["steps"] if "upload-artifact@" in item.get("uses", ""))
        self.assertTrue(all("/public/" in line for line in upload["with"]["path"].splitlines()))
        download = next(item for item in runtime["steps"] if "download-artifact@" in item.get("uses", ""))
        self.assertEqual(upload["with"]["name"], download["with"]["name"])
        self.assertNotEqual(upload["with"]["name"], "eventd-image-amd64")
        self.assertNotEqual(runtime["name"], jobs["eventd_runtime"]["name"])
        self.assertEqual(runtime["needs"], "eventd_review")
        checkout = next(item for item in runtime["steps"] if "checkout@" in item.get("uses", ""))
        self.assertEqual(checkout["with"]["ref"], "${{ inputs.source_revision }}")
        names = [item.get("name") for item in runtime["steps"]]
        self.assertLess(names.index("Verify the selected source against the downloaded Eventd evidence"), names.index("Verify loaded archives and installed event publishing and database delivery"))
        self.assertFalse((ROOT / ".github/workflows/bazel-eventd-image.yml").exists())

    def test_selection_requires_paired_immutable_revisions(self):
        script = step(document()["jobs"]["eventd_review"], "Validate the optional reviewed Eventd source selection")["run"]
        cases = [("a" * 40, "b" * 40, "", 0), ("", "", "", 2), ("a" * 40, "", "", 2), ("", "b" * 40, "", 2), ("main", "b" * 40, "", 2), ("A" * 40, "b" * 40, "", 2), ("a" * 39, "b" * 40, "", 2), ("a" * 40, "b" * 40, "123", 2)]
        for source, registry, inputs, expected in cases:
            result = subprocess.run(["bash", "-eu", "-c", script], env=dict(os.environ, SOURCE_REVISION=source, REGISTRY_REVISION=registry, SOURCE_INPUTS_RUN=inputs), capture_output=True)
            self.assertEqual(result.returncode, expected)

    def test_receipt_and_publication_bind_source_catalog_controller_and_driver(self):
        jobs = document()["jobs"]
        bind = step(jobs["eventd_review"], "Bind the completed Eventd artifacts to the reviewed source")["run"]
        publish = step(jobs["eventd_review"], "Prepare Eventd archives and reviewed public evidence")["run"]
        verify = step(jobs["eventd_review_runtime"], "Verify the selected source against the downloaded Eventd evidence")["run"]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            source = fixture / "review-source"
            artifacts = source / "artifacts/eventd"
            artifacts.mkdir(parents=True)
            driver = source / "tools/bazel/ci/eventd_loaded.py"
            driver.parent.mkdir(parents=True)
            driver.write_text("# selected loaded driver\n")
            revision = "a5a1f8dfa22b0b99226968f8d11dfe9d654e9a97"
            registry = "6a69c2d89070d4d2ffc80b1e13626da387f7ea65"
            for name, status in (("build-receipt.json", "built"), ("validation.json", "passed")):
                (artifacts / name).write_text(json.dumps({"revision": revision, "status": status, "artifacts": {}}))
            diagnostic = {"url": "https://snapshot.debian.org/file/648dd0b86870e854e02868553a2246b3470b96dd", "sha256": "b5cb023bfaad356053583af384f7b75caec3ac127a197f889164ef4d96397cef", "bytes": 852668, "filename": "libpcre2-dev_10.46-1~deb13u1_amd64.deb", "reused_verified_file": False}
            (artifacts / "libpcre2-dev-download.json").write_text(json.dumps(diagnostic))
            binary = fixture / "bin"
            binary.mkdir()
            git = binary / "git"
            git.write_text("#!/bin/sh\nif [ \"$1\" = -C ]; then printf '%s\\n' \"$OBSERVED_CONTROLLER\"; elif [ \"$1\" = rev-parse ]; then printf '%s\\n' \"$OBSERVED_SOURCE\"; elif [ \"$1\" = diff ]; then test \"$OBSERVED_DIRTY\" = 0; fi\n")
            git.chmod(0o755)
            env = dict(os.environ, PATH=str(binary) + ":/usr/bin:/bin", SOURCE_DIRECTORY="review-source", SOURCE_REVISION=revision, REGISTRY_REVISION=registry, GITHUB_SHA="c" * 40, CONTROL_REVISION="c" * 40, CONTROL_HEAD_SHA="", CONTROL_BASE_SHA="", OBSERVED_CONTROLLER="c" * 40, OBSERVED_SOURCE=revision, OBSERVED_DIRTY="0", GITHUB_WORKSPACE=str(fixture), PYTHONDONTWRITEBYTECODE="1")
            subprocess.run(["bash", "-eu", "-c", bind], cwd=fixture, env=env, check=True)
            subprocess.run(["bash", "-eu", "-c", publish], cwd=ROOT, env=env, check=True)
            public = artifacts / "public"
            self.assertEqual(json.loads((public / "libpcre2-dev-download.json").read_text()), diagnostic)
            runtime = fixture / "runtime"
            runtime_artifacts = runtime / "artifacts/eventd"
            shutil.copytree(public, runtime_artifacts)
            runtime_driver = runtime / "tools/bazel/ci/eventd_loaded.py"
            runtime_driver.parent.mkdir(parents=True)
            shutil.copy2(driver, runtime_driver)
            subprocess.run(["bash", "-eu", "-c", verify], cwd=runtime, env=env, check=True)
            receipt = runtime_artifacts / "reviewed-selection.json"
            original = receipt.read_text()
            for field in ("source_revision", "registry_revision", "control_revision", "control_head_sha", "control_base_sha", "loaded_driver_sha256"):
                value = json.loads(original)
                value[field] = "d" * 40
                receipt.write_text(json.dumps(value))
                self.assertNotEqual(subprocess.run(["bash", "-eu", "-c", verify], cwd=runtime, env=env, capture_output=True).returncode, 0, field)
            receipt.write_text(original)
            self.assertNotEqual(subprocess.run(["bash", "-eu", "-c", bind], cwd=fixture, env=dict(env, OBSERVED_SOURCE="d" * 40), capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run(["bash", "-eu", "-c", bind], cwd=fixture, env=dict(env, OBSERVED_CONTROLLER="d" * 40), capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run(["bash", "-eu", "-c", bind], cwd=fixture, env=dict(env, OBSERVED_DIRTY="1"), capture_output=True).returncode, 0)
            runtime_driver.write_text("# changed driver\n")
            self.assertNotEqual(subprocess.run(["bash", "-eu", "-c", verify], cwd=runtime, env=env, capture_output=True).returncode, 0)
            diagnostic["bytes"] = 1
            (artifacts / "libpcre2-dev-download.json").write_text(json.dumps(diagnostic))
            self.assertNotEqual(subprocess.run(["bash", "-eu", "-c", publish], cwd=ROOT, env=env, capture_output=True).returncode, 0)


class ReviewedDownloadTest(unittest.TestCase):
    STEP = "Prepare the reviewed libpcre2-dev download"
    SOURCE = "a5a1f8dfa22b0b99226968f8d11dfe9d654e9a97"
    REGISTRY = "6a69c2d89070d4d2ffc80b1e13626da387f7ea65"

    def implementation(self):
        script = step(document()["jobs"]["eventd_review"], self.STEP)["run"]
        code = script.split("python3 - <<'PYDEB'\n", 1)[1].rsplit("\nPYDEB", 1)[0]
        namespace = {"__name__": "download_test"}
        with mock.patch.object(sys, "path", [str(ROOT), *sys.path]):
            exec(compile(code, str(WORKFLOW), "exec"), namespace)
        return namespace

    def test_exact_manual_graph_only_and_before_analysis(self):
        workflow = WORKFLOW.read_text()
        selected = step(document()["jobs"]["eventd_review"], self.STEP)
        gate = selected["if"]
        def enabled(event, source, registry):
            expression = gate.replace("github.event_name", repr(event)).replace("inputs.source_revision", repr(source)).replace("inputs.registry_revision", repr(registry)).replace("&&", " and ").replace("||", " or ")
            return eval(expression, {"__builtins__": {}})
        valid = {"event": "workflow_dispatch", "source": self.SOURCE, "registry": self.REGISTRY}
        self.assertTrue(enabled(**valid))
        for changed in ({"event": "pull_request"}, {"event": "push"}, {"source": ""}, {"registry": ""}, {"source": "b" * 40}, {"registry": "c" * 40}):
            self.assertFalse(enabled(**{**valid, **changed}), changed)
        self.assertLess(workflow.index("Configure shared source download mirrors"), workflow.index(self.STEP))
        self.assertLess(workflow.index(self.STEP), workflow.index("Build Eventd runtime and debug images"))
        self.assertLess(workflow.index(self.STEP), workflow.index("Build Eventd runtime and debug images"))
        self.assertIn('>> "$GITHUB_WORKSPACE/.bazelrc.user"', selected["run"])
        self.assertIn("artifacts/eventd/libpcre2-dev-download.json", workflow)
        self.assertIn("'libpcre2-dev-download.json'", workflow)

    def test_fixed_official_address_and_original_locked_identity(self):
        ns = self.implementation()
        self.assertEqual(ns["URL"], "https://snapshot.debian.org/file/648dd0b86870e854e02868553a2246b3470b96dd")
        self.assertEqual(ns["SHA256"], "b5cb023bfaad356053583af384f7b75caec3ac127a197f889164ef4d96397cef")
        self.assertEqual(ns["SIZE"], 852668)
        self.assertEqual(ns["FILENAME"], "libpcre2-dev_10.46-1~deb13u1_amd64.deb")

    @contextlib.contextmanager
    def fixture(self):
        # Plain bytes test download integrity; no Debian package is produced.
        payload = b"ordinary download test bytes"
        ns = self.implementation()
        ns["SIZE"] = len(payload)
        ns["SHA256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            destination = Path("artifacts/source-downloads") / ns["FILENAME"]
            receipt = Path("artifacts/eventd/libpcre2-dev-download.json")
            with contextlib.redirect_stdout(io.StringIO()):
                yield ns, payload, destination, receipt

    def response(self, payload, *, status=200):
        response = io.BytesIO(payload)
        response.status = status
        return response

    def test_verified_download_atomic_publication_and_reuse(self):
        with self.fixture() as (ns, payload, destination, receipt):
            fetch = mock.Mock(return_value=self.response(payload))
            ns["urlopen"] = fetch
            ns["prepare"]()
            fetch.assert_called_once_with(ns["URL"], timeout=60)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertFalse(json.loads(receipt.read_text())["reused_verified_file"])
            self.assertEqual(sorted(x.name for x in destination.parent.iterdir()), [ns["FILENAME"]])
            ns["urlopen"] = mock.Mock(side_effect=AssertionError("Verified cache must avoid network"))
            ns["prepare"]()
            self.assertTrue(json.loads(receipt.read_text())["reused_verified_file"])
            self.assertEqual(set(json.loads(receipt.read_text())), {"url", "sha256", "bytes", "filename", "reused_verified_file"})

    def test_corrupt_short_oversized_and_http_payloads_are_rejected(self):
        for mode in ("corrupt", "short", "oversized", "http"):
            with self.subTest(mode=mode), self.fixture() as (ns, payload, destination, receipt):
                bad = {"corrupt": b"X" * len(payload), "short": payload[:-1],
                       "oversized": payload + b"X", "http": payload}[mode]
                ns["urlopen"] = mock.Mock(return_value=self.response(bad, status=503 if mode == "http" else 200))
                with self.assertRaises(ValueError):
                    ns["prepare"]()
                self.assertFalse(destination.exists())
                self.assertFalse(receipt.exists())

    def test_interrupted_download_does_not_replace_existing_file(self):
        with self.fixture() as (ns, payload, destination, receipt):
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old unverified download")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.side_effect = OSError("interrupted transfer")
            ns["urlopen"] = mock.Mock(return_value=response)
            with self.assertRaises(OSError):
                ns["prepare"]()
            response.read.assert_called_once_with(len(payload) + 1)
            self.assertEqual(destination.read_bytes(), b"old unverified download")
            self.assertFalse(receipt.exists())

    def test_interrupted_atomic_replace_removes_only_temporary_file(self):
        with self.fixture() as (ns, payload, destination, receipt):
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old unverified download")
            ns["urlopen"] = mock.Mock(return_value=self.response(payload))
            with mock.patch.object(Path, "replace", side_effect=OSError("interrupted replace")):
                with self.assertRaises(OSError):
                    ns["prepare"]()
            self.assertEqual(destination.read_bytes(), b"old unverified download")
            self.assertEqual(list(destination.parent.iterdir()), [destination])
            self.assertFalse(receipt.exists())

    def test_symlink_destination_is_not_read_or_replaced(self):
        with self.fixture() as (ns, payload, destination, receipt):
            destination.parent.mkdir(parents=True)
            target = Path("unrelated")
            target.write_bytes(payload)
            destination.symlink_to(target.resolve())
            ns["urlopen"] = mock.Mock(side_effect=AssertionError("No download for unsafe path"))
            with self.assertRaises(ValueError):
                ns["prepare"]()
            self.assertTrue(destination.is_symlink())
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
