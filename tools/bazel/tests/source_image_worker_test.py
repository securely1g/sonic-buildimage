#!/usr/bin/env python3
"""Check fixed image dispatch and public artifact boundaries."""

import importlib.util
import copy
import io
import hashlib
import json
import os
import re
import inspect
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("source_image_worker", ROOT / "tools/bazel/ci/source_image_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class ImageWorkerTest(unittest.TestCase):
    def test_syncd_tap_requires_exact_device_and_private_namespace_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            device = SimpleNamespace(st_mode=stat.S_IFCHR | 0o666, st_rdev=os.makedev(10, 200))
            proof = {"private_network_namespace": True, "tap_created_and_closed": True}
            with patch.object(worker.Path, 'lstat', return_value=device), \
                    patch.object(worker.Path, 'stat', return_value=SimpleNamespace(st_ino=42)), \
                    patch.object(worker, 'output', return_value=json.dumps(proof)) as output:
                worker.prepare_tap_fixture(artifacts)
                command = output.call_args.args[0]
                self.assertEqual(command[:5], ['sudo', '-n', 'unshare', '--net', '--'])
                self.assertEqual(command[5:8], [sys.executable, '-I', '-c'])
                self.assertEqual(command[-1], '42')
                self.assertEqual(output.call_args.kwargs, {'timeout': 30})
                self.assertEqual(json.loads((artifacts / 'worker-tap.json').read_text())['status'], 'passed')
                for mode, number in ((stat.S_IFREG, os.makedev(10, 200)),
                                     (stat.S_IFCHR, os.makedev(10, 201)),
                                     (stat.S_IFLNK, os.makedev(10, 200))):
                    device.st_mode, device.st_rdev = mode, number
                    with self.assertRaisesRegex(ValueError, 'character device'):
                        worker.prepare_tap_fixture(artifacts)
                device.st_mode, device.st_rdev = stat.S_IFCHR, os.makedev(10, 200)
                output.return_value = json.dumps({**proof, 'private_network_namespace': False})
                with self.assertRaisesRegex(ValueError, 'Incomplete private TAP proof'):
                    worker.prepare_tap_fixture(artifacts)
                output.side_effect = subprocess.CalledProcessError(1, command)
                with self.assertRaises(subprocess.CalledProcessError):
                    worker.prepare_tap_fixture(artifacts)

    def test_syncd_tap_child_rejects_shared_namespace_and_closes_descriptor(self):
        import fcntl
        import struct
        from unittest.mock import mock_open
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(worker.Path, 'lstat', return_value=SimpleNamespace(
                    st_mode=stat.S_IFCHR, st_rdev=os.makedev(10, 200))), \
                patch.object(worker.Path, 'stat', return_value=SimpleNamespace(st_ino=42)), \
                patch.object(worker, 'output', return_value='{"private_network_namespace":true,"tap_created_and_closed":true}') as output:
            worker.prepare_tap_fixture(Path(directory))
            script = output.call_args.args[0][-2]
        with patch.object(sys, 'argv', ['tap-probe', '42']), \
                patch.object(os, 'stat', return_value=SimpleNamespace(st_ino=42)), \
                patch('builtins.open') as opened, self.assertRaisesRegex(SystemExit, 'private network namespace'):
            exec(compile(script, '<tap-probe>', 'exec'), {})
            opened.assert_not_called()
        opened = mock_open()
        opened.return_value.fileno.return_value = 7
        with patch.object(sys, 'argv', ['tap-probe', '42']), \
                patch.object(os, 'stat', return_value=SimpleNamespace(st_ino=99)), \
                patch('builtins.open', opened), patch.object(fcntl, 'ioctl', return_value=b'sonic-probe0\0' + b'\0' * 6) as ioctl, \
                patch('sys.stdout', new_callable=io.StringIO) as stdout:
            exec(compile(script, '<tap-probe>', 'exec'), {})
            ioctl.assert_called_once_with(7, 0x400454ca, struct.pack('16sH', b'sonic-probe%d', 0x0002 | 0x1000))
            opened.return_value.__exit__.assert_called_once()
            self.assertTrue(json.loads(stdout.getvalue())['tap_created_and_closed'])

    def test_syncd_tap_gate_precedes_downloads_and_bazel_setup(self):
        source = inspect.getsource(worker.prepare)
        self.assertIn('if args.image == "syncd":\n        prepare_tap_fixture(artifacts)', source)
        self.assertLess(source.index('prepare_tap_fixture(artifacts)'), source.index('prepare_source_downloads('))
        self.assertLess(source.index('prepare_tap_fixture(artifacts)'), source.index('launcher_text('))

    def test_validation_bootstrap_installs_only_missing_execution_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            def available(tool):
                return None if tool == 'bsdtar' and not commands else '/usr/bin/' + tool
            commands = []
            with patch.object(worker.shutil, 'which', side_effect=available), \
                    patch.object(worker, 'run', side_effect=lambda command: commands.append(command)), \
                    patch.object(worker, 'output', return_value='libarchive-tools\t3.7.4'):
                worker.prepare_validation_tools(artifacts)
            self.assertEqual(commands, [
                ['sudo', '-n', 'apt-get', 'update'],
                ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y',
                 '--no-install-recommends', 'libarchive-tools']])
            receipt = json.loads((artifacts / 'worker-execution-tools.json').read_text())
            self.assertEqual(receipt['packages_installed'], ['libarchive-tools'])
            with patch.object(worker.shutil, 'which', return_value='/usr/bin/tool'), \
                    patch.object(worker, 'run') as run, patch.object(worker, 'output', return_value='versions'):
                worker.prepare_validation_tools(artifacts)
                run.assert_not_called()
            with patch.object(worker.shutil, 'which', return_value=None), \
                    patch.object(worker, 'run'), self.assertRaisesRegex(ValueError, 'Missing image validation tool'):
                worker.prepare_validation_tools(artifacts)

    def test_gawk_mirror_changes_only_three_exact_source_urls(self):
        original = b"verified existing mirror fixture\n"
        with patch.object(worker, 'MIRROR_SHA', hashlib.sha256(original).hexdigest()):
            config = worker.mirror_configuration(original).decode()
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                worker.mirror_configuration(original + b'changed')
        rewrites = [line.split() for line in config.splitlines()
                    if line.startswith('rewrite ') and 'gawk' in line]
        self.assertEqual(len(rewrites), 3)
        for host, (_, expression, destination) in zip(
                ('mirrors.kernel.org', 'ftp.gnu.org', 'ftpmirror.gnu.org'), rewrites):
            self.assertTrue(re.fullmatch(expression, host + '/gnu/gawk/gawk-5.3.2.tar.xz'))
            self.assertFalse(re.fullmatch(expression, host + '/gnu/gawk/gawk-5.3.3.tar.xz'))
            self.assertEqual(destination, 'https://mirror.bazel.build/ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz')

    def test_snapshot_fallbacks_keep_original_and_only_exact_observed_urls(self):
        original = b'prior verified mirror rules\n'
        with patch.object(worker, 'MIRROR_SHA', hashlib.sha256(original).hexdigest()):
            config = worker.mirror_configuration(original).decode()
        rules = [line.split()[1:] for line in config.splitlines()
                 if line.startswith('rewrite ') and 'snapshot' in line]
        self.assertEqual(len(rules), 8)
        self.assertEqual(len(worker.SNAPSHOT_FALLBACKS), 4)
        for path, identifier in worker.SNAPSHOT_FALLBACKS.items():
            url = 'snapshot.debian.org/archive/debian/20260727T143429Z/' + path
            alternatives = [destination for expression, destination in rules if re.fullmatch(expression, url)]
            self.assertEqual(alternatives, ['https://snapshot.debian.org/file/' + identifier, 'https://' + url])
            for altered in (url.replace('20260727T143429Z', '20260728T143429Z'),
                            url.replace('_amd64.deb', '_arm64.deb'), url + '?query=1',
                            url.replace('snapshot.debian.org', 'elsewhere.example')):
                self.assertFalse(any(re.fullmatch(expression, altered) for expression, _ in rules))

    def test_selected_profile_overrides_driver_defaults_in_actual_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bazel"
            binary.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            binary.chmod(0o755)
            launcher = root / "launcher"
            for cpus, jobs, memory in ((2, 1, 4000), (4, 2, 8000)):
                with self.subTest(cpus=cpus):
                    profile = worker.select_build_profile(20 * 1024**3, cpus)
                    launcher.write_text(worker.launcher_text(str(binary), root / "output base", profile))
                    argv = json.loads(subprocess.check_output([sys.executable, str(launcher), "--nosystem_rc", "build",
                        "--jobs=6", "--local_resources=cpu=6", "--local_resources=memory=12000", "//example:image"], text=True))
                    self.assertEqual(argv[0], "--output_base=" + str(root / "output base"))
                    self.assertIn("--host_jvm_args=-Xmx3g", argv)
                    self.assertIn("--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts", argv)
                    self.assertEqual([arg for arg in argv if arg.startswith("--jobs=")][-1], "--jobs=" + str(jobs))
                    self.assertEqual([arg for arg in argv if arg.startswith("--local_resources=cpu=")][-1], "--local_resources=cpu=" + str(jobs))
                    self.assertEqual([arg for arg in argv if arg.startswith("--local_resources=memory=")][-1], "--local_resources=memory=" + str(memory))
                    self.assertIn("//example:image", argv)
                    shutdown = json.loads(subprocess.check_output([sys.executable, str(launcher), "shutdown"], text=True))
                    self.assertFalse(any(arg.startswith("--jobs=") for arg in shutdown))

    def test_phase_keeps_the_prepared_profile_and_rejects_changed_limits(self):
        profile = worker.select_build_profile(20 * 1024**3, 4)
        memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
        state = {"build_profile": profile}
        self.assertEqual(worker.prepared_profile(state, memory), profile)
        for changed in ({**memory, "cpu_max": "200000 100000"},
                        {**memory, "limit": str(24 * 1024**3)}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "differ from preparation"):
                worker.prepared_profile(state, changed)
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            args = SimpleNamespace(image="sysmgr", revision="a" * 40, phase="build", workspace=Path("/source"), artifacts=artifacts)
            state.update(image=args.image, revision=args.revision, workspace=str(args.workspace), environment={})
            (artifacts / "worker-private.json").write_text(json.dumps(state))
            with patch.object(worker, "source_check"), patch.object(worker, "memory_snapshot", return_value=memory), \
                    patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                worker.execute_phase(args)
            receipt = json.loads((artifacts / "worker-build.json").read_text())
            self.assertEqual(receipt["build_profile"], profile)
            self.assertEqual(receipt["status"], "passed")

    def test_only_known_images_and_full_commit_ids(self):
        for image in worker.DRIVERS:
            worker.selection(image, "a" * 40)
        for image, revision in (("$(id)", "a" * 40), ("dash-ha", "master"), ("sflow", "a" * 39), ("sysmgr", "A" * 40)):
            with self.subTest(image=image, revision=revision), self.assertRaises(ValueError):
                worker.selection(image, revision)

    def test_fixed_interfaces_do_not_expand_into_owner_suites(self):
        root, artifacts = Path("/source"), Path("/evidence")
        for image in worker.DRIVERS:
            for phase in ("build", "validate", "incremental", "loaded"):
                command = worker.phase_command(image, phase, root, artifacts)
                self.assertEqual(command[1], "-B")
                self.assertTrue(command[2].startswith("/source/tools/bazel/ci/"))
                self.assertFalse(any(value.endswith("/...") or ".deb" in value for value in command))
                self.assertIn(str(artifacts) if image != "orchagent" or phase != "incremental" else str(artifacts / "rebuild"), command)
        with self.assertRaises(ValueError):
            worker.phase_command("sflow", "shell", root, artifacts)
        command = worker.phase_command("orchagent", "incremental", root, artifacts)
        self.assertIn("@scapy//:wheel", command)
        self.assertIn("//dockers/docker-orchagent:docker-orchagent-dbg.gz", command)
        self.assertIn("/source/tools/bazel/ci/review_bazel.sh", worker.phase_command("sysmgr", "build", root, artifacts))

    def test_review_wrapped_images_reuse_fixed_driver_phases(self):
        root, artifacts = Path("/source"), Path("/evidence")
        prefix = [sys.executable, "-B"]
        wrapper = "/source/tools/bazel/ci/review_bazel.sh"
        for image, stem in (("otel", "otel"), ("syncd", "syncd"), ("gearbox", "gbsyncd"), ("dash-engine", "dash_engine"), ("framework", "mgmt_framework")):
            with self.subTest(image=image):
                driver = f"/source/tools/bazel/ci/{stem}_image.py"
                for phase in ("build", "validate", "incremental"):
                    expected = prefix + [driver, phase, "--artifacts", str(artifacts)]
                    if phase != "validate":
                        expected += ["--bazel", wrapper]
                    self.assertEqual(worker.phase_command(image, phase, root, artifacts), expected)
                self.assertEqual(worker.phase_command(image, "loaded", root, artifacts),
                                 prefix + [f"/source/tools/bazel/ci/{stem}_loaded.py", "--artifacts", str(artifacts)])

    def test_orchagent_incremental_uses_current_source_parent_and_all_six_targets(self):
        command = worker.phase_command("orchagent", "incremental", Path("/source"), Path("/evidence"))
        self.assertEqual(command[:5], [
            sys.executable, "-B", "/source/tools/bazel/ci/swss_rebuild.py", "--bazel",
            "/source/tools/bazel/ci/libteam_draft_bazel.sh",
        ])
        self.assertEqual(list(zip(command[5::2], command[6::2])), [
            ("--runtime-target", "//dockers/docker-orchagent:docker-orchagent.gz"),
            ("--additional-runtime-target", "//dockers/docker-orchagent:docker-orchagent-dbg.gz"),
            ("--additional-runtime-target", "//dockers/docker-orchagent:rdeps"),
            ("--symbols-target", "//dockers/docker-orchagent:rdeps_debug_symbols"),
            ("--unchanged-target", "//dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz"),
            ("--unchanged-target", "@scapy//:wheel"),
            ("--output", "/evidence/rebuild"),
        ])
        self.assertEqual(len(command), 19)
        self.assertFalse(any("source_config_engine" in value for value in command))

    def test_gnmi_profiles_have_fixed_flags_for_existing_four_phases(self):
        root, artifacts = Path("/source"), Path("/evidence")
        prefix = [sys.executable, "-B"]
        for image, mode in (("gnmi", "enabled"), ("gnmi-no-dialout", "disabled")):
            for phase in ("build", "validate", "incremental"):
                expected = prefix + ["/source/tools/bazel/ci/gnmi_image.py", phase,
                                      "--artifacts", str(artifacts), "--dialout", mode]
                if phase != "validate":
                    expected += ["--bazel", "/source/tools/bazel/ci/review_bazel.sh"]
                self.assertEqual(worker.phase_command(image, phase, root, artifacts), expected)
            self.assertEqual(worker.phase_command(image, "loaded", root, artifacts),
                             prefix + ["/source/tools/bazel/ci/gnmi_loaded.py", "--artifacts", str(artifacts)])
        with self.assertRaises(ValueError):
            worker.phase_command("gnmi --dialout other", "build", root, artifacts)

    def test_gnmi_publication_preserves_and_checks_selected_profile(self):
        for image, enabled in (("gnmi", True), ("gnmi-no-dialout", False)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "build-receipt.json"
                receipt = {"status": "built", "revision": "a" * 40, "architecture": "amd64",
                           "dialout_enabled": enabled, "artifacts": {}}
                path.write_text(json.dumps(receipt))
                worker.publish(SimpleNamespace(artifacts=root, image=image))
                public = json.loads((root / "public/public-receipt.json").read_text())
                self.assertIs(public["dialout_enabled"], enabled)
                for bad in (not enabled, None, int(enabled)):
                    receipt["dialout_enabled"] = bad
                    path.write_text(json.dumps(receipt))
                    with self.assertRaises(ValueError):
                        worker.publish(SimpleNamespace(artifacts=root, image=image))

    def test_workflow_allowlist_matches_fixed_driver_choices(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        choices = re.search(r"options: \[none, preflight, ([^\]]+)\]", workflow).group(1).split(", ")
        self.assertEqual(set(choices), set(worker.DRIVERS) | {"worker-provenance", "registry-native"})
        gates = re.findall(r"contains\(fromJSON\('(\[[^']+\])'\), inputs.source_image\)", workflow)
        self.assertEqual(len(gates), 2)
        for gate in gates:
            self.assertEqual(set(json.loads(gate)), set(worker.DRIVERS))

    def test_framework_worker_pr_preserves_source_only_scope(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition

        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        branch = "codex/framework-native-worker"
        guards = [step["env"]["SOURCE_ONLY_NO_DEBS"] for job in workflow["jobs"].values()
                  for step in job.get("steps", []) if "SOURCE_ONLY_NO_DEBS" in step.get("env", {})]
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(condition(guard, event="pull_request", branch=branch))
            self.assertFalse(condition(guard, event="pull_request", branch=branch + "-suffix"))
            self.assertFalse(condition(guard, event="pull_request", branch=branch, head_repo="other/fork"))
        self.assertFalse(condition(workflow["jobs"]["vs_container"]["if"], event="pull_request", branch=branch))
        self.assertFalse(condition(workflow["jobs"]["registry_native"]["if"], event="pull_request", branch=branch))

    def test_orchagent_worker_followup_skips_only_its_owned_deb_fixtures_and_vs(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition

        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        branch = "codex/orchagent-source-worker-current"
        guards = [step["env"]["SOURCE_ONLY_NO_DEBS"] for job in workflow["jobs"].values()
                  for step in job.get("steps", []) if "SOURCE_ONLY_NO_DEBS" in step.get("env", {})]
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(condition(guard, event="pull_request", branch=branch))
            for changes in ({"branch": branch + "-suffix"}, {"branch": "unrelated"},
                            {"head_repo": "other/fork"}, {"event": "push"},
                            {"event": "workflow_dispatch"}):
                with self.subTest(changes=changes):
                    context = {"event": "pull_request", "branch": branch, **changes}
                    self.assertFalse(condition(guard, **context))
        for name in ("vs", "vs_container", "source_image", "source_image_inputs", "registry_native",
                     "source_worker_preflight", "worker_provenance"):
            self.assertFalse(condition(workflow["jobs"][name]["if"], event="pull_request", branch=branch))
        self.assertTrue(condition(workflow["jobs"]["vs_container"]["if"], event="pull_request", branch=branch + "-suffix"))
        for name in ("archive", "checks"):
            self.assertTrue(condition(workflow["jobs"][name]["if"], event="pull_request", branch=branch))
        kernel = yaml.safe_load((ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text())
        self.assertFalse(condition(kernel["jobs"]["kernel-bundle"]["if"], event="pull_request", branch=branch))

    def test_selected_workflow_and_wrapper_catalogs_must_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / "tools/bazel/ci").mkdir(parents=True)
            workflow = root / ".github/workflows/bazel-oci.yml"
            workflow.write_text("env:\n  BAZEL: tools/bazel/ci/review_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("sysmgr", root), "a" * 40)
            self.assertEqual(worker.catalog_for("otel", root), "a" * 40)
            self.assertEqual(worker.catalog_for("syncd", root), "a" * 40)
            self.assertEqual(worker.catalog_for("gearbox", root), "a" * 40)
            self.assertEqual(worker.catalog_for("dash-engine", root), "a" * 40)
            self.assertEqual(worker.catalog_for("framework", root), "a" * 40)
            workflow.write_text("env:\n  BAZEL: ${{ github.workspace }}/tools/bazel/ci/libteam_draft_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            wrapper = root / "tools/bazel/ci/libteam_draft_bazel.sh"
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("dash-ha", root), "a" * 40)
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "b" * 40 + "\n")
            with self.assertRaises(ValueError):
                worker.catalog_for("dash-ha", root)

    def test_source_download_handoff_rejects_changed_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, (version, _) in worker.SOURCES.items():
                archive = root / "inputs" / (name + "-" + version) / "src.tar.gz"
                archive.parent.mkdir(parents=True)
                archive.write_bytes(b"changed source archive")
            receipt = [{"module": name, "version": version, "integrity": integrity,
                        "bytes": len(b"changed source archive"),
                        "archive": "artifacts/source-image-inputs/" + name + "-" + version + "/src.tar.gz"}
                       for name, (version, integrity) in worker.SOURCES.items()]
            (root / "inputs/receipt.json").write_text(json.dumps(receipt))
            worker.check_source_inventory(root / "inputs", False)
            with self.assertRaises(subprocess.CalledProcessError):
                worker.prepare_source_downloads(root / "inputs", root / "cache")
            self.assertFalse((root / "cache").exists())

    def test_reused_input_run_requires_successful_same_repository_producer(self):
        selected = {"repository": {"full_name": "securely1g/sonic-buildimage", "id": 99},
                    "path": ".github/workflows/bazel-oci.yml", "name": "Bazel OCI", "head_sha": "a" * 40}
        jobs = {"total_count": 1, "jobs": [{"id": 2, "name": "OCI regression checks (AMD64)", "conclusion": "success",
                "steps": [{"name": name, "conclusion": "success"} for name in
                          ("Retain the verified GitLab source downloads", "Upload the source downloads for the VS job")]}]}
        artifacts = {"total_count": 1, "artifacts": [{"id": 3, "name": "bazel-repository-inputs", "expired": False,
                     "size_in_bytes": 123, "workflow_run": {"id": 1, "head_sha": "a" * 40, "repository_id": 99}}]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "SOURCE_INPUTS_RUN": "1", "GH_TOKEN": "test-only", "GITHUB_REPOSITORY": "securely1g/sonic-buildimage"}):
            args = SimpleNamespace(artifacts=Path(directory))
            for kind in ("valid", "foreign repository", "failed job", "expired artifact"):
                records = copy.deepcopy([selected, jobs, artifacts])
                if kind == "foreign repository": records[0]["repository"]["full_name"] = "elsewhere/repo"
                if kind == "failed job": records[1]["jobs"][0]["conclusion"] = "failure"
                if kind == "expired artifact": records[2]["artifacts"][0]["expired"] = True
                with self.subTest(kind=kind), patch.object(worker.urllib.request, "urlopen",
                        side_effect=[io.StringIO(json.dumps(record)) for record in records]):
                    if kind == "valid":
                        worker.verify_input_run(args)
                        self.assertEqual(json.loads((args.artifacts / "worker-input-run.json").read_text())["artifact"], 3)
                    else:
                        with self.assertRaises(ValueError): worker.verify_input_run(args)

    def test_handoff_rejects_extra_missing_and_linked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = []
            for name, (version, integrity) in worker.SOURCES.items():
                relative = name + "-" + version + "/src.tar.gz"
                archive = root / relative
                archive.parent.mkdir(parents=True)
                archive.write_bytes(b"fixture")
                receipt.append({"module": name, "version": version, "integrity": integrity, "bytes": 7,
                                "archive": "artifacts/source-image-inputs/" + relative})
            (root / "receipt.json").write_text(json.dumps(receipt))
            worker.check_source_inventory(root, False)
            (root / "extra").write_text("unexpected")
            with self.assertRaises(ValueError):
                worker.check_source_inventory(root, False)
            (root / "extra").unlink()
            archive.unlink()
            with self.assertRaises(ValueError):
                worker.check_source_inventory(root, False)
            archive.symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                worker.check_source_inventory(root, False)
            with patch.dict(os.environ, {"SOURCE_INPUTS_RUN": "../41689621493"}), self.assertRaises(ValueError):
                worker.input_run_id()

    def test_publication_preserves_exact_checked_syncd_and_gearbox_policy_bytes(self):
        cases = ({"DEBIAN_FRONTEND": "noninteractive"}, {"DEBIAN_FRONTEND": "interactive"},
                 {"DEBIAN_FRONTEND": "noninteractive", "UNRELATED": "value"})
        for image in ('syncd', 'gearbox'):
            for environment in cases:
                with self.subTest(image=image, environment=environment), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    policy = root / 'policy.json'
                    policy.write_text(json.dumps({'image_validation': {'environment': environment}}))
                    original = policy.read_bytes()
                    (root / 'build-receipt.json').write_text(json.dumps({'status': 'built', 'artifacts': {
                        policy.name: {'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest()}}}))
                    args = SimpleNamespace(artifacts=root, image=image)
                    if environment == {"DEBIAN_FRONTEND": "noninteractive"}:
                        worker.publish(args)
                        self.assertEqual((root / 'public/policy.json').read_bytes(), original)
                        self.assertEqual(policy.read_bytes(), original)
                    else:
                        with self.assertRaisesRegex(ValueError, 'Unexpected image policy environment'):
                            worker.publish(args)
                        self.assertFalse((root / 'public/policy.json').exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / 'policy.json'
            policy.write_text(json.dumps({'image_validation': {'environment': cases[0]},
                                          'diagnostic': {'environment': {'UNRELATED': 'value'}}}))
            (root / 'build-receipt.json').write_text(json.dumps({'artifacts': {policy.name: {
                'bytes': policy.stat().st_size, 'sha256': worker.digest(policy)}}}))
            with self.assertRaises(ValueError):
                worker.publish(SimpleNamespace(artifacts=root, image='syncd'))
            self.assertFalse((root / 'public/policy.json').exists())

    def test_publication_keeps_declared_bytes_and_excludes_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "image.gz"
            payload.write_bytes(b"verified archive fixture")
            receipt = {"status": "built", "revision": "a" * 40,
                       "invocation": {"environment": {"private": "not for publication"}},
                       "artifacts": {payload.name: {"bytes": payload.stat().st_size,
                                                    "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}}}
            (root / "build-receipt.json").write_text(json.dumps(receipt))
            (root / "worker-private.json").write_text('{"environment":{"private":"not for publication"}}')
            (root / "dockerd.log").write_text("private daemon diagnostic")
            (root / "worker-build.json").write_text('{"status":"passed"}')
            worker.publish(SimpleNamespace(artifacts=root))
            worker.publish(SimpleNamespace(artifacts=root))
            public = root / "public"
            self.assertEqual((public / "image.gz").read_bytes(), payload.read_bytes())
            self.assertFalse((public / "worker-private.json").exists())
            self.assertFalse((public / "dockerd.log").exists())
            self.assertNotIn("invocation", json.loads((public / "public-receipt.json").read_text()))
            payload.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                worker.publish(SimpleNamespace(artifacts=root))


if __name__ == "__main__":
    unittest.main()
