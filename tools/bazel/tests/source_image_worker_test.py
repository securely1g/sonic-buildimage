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
    def test_normal_ci_has_no_branch_specific_exceptions(self):
        workflow = (ROOT / '.github/workflows/bazel-oci.yml').read_text()
        self.assertNotIn('SOURCE_ONLY_NO_DEBS', workflow)
        for name in ('Check shared Make rules with unrelated container targets',
                     'Check Make handoff and archive validation'):
            step = workflow.split('- name: ' + name, 1)[1].split('\n      - name:', 1)[0]
            self.assertIn("python3 -B -m unittest discover -s tools/bazel/tests -p '*_test.py'", step)
        vs = workflow.split('\n  vs_container:\n', 1)[1].split('    if: >-\n', 1)[1].split('    runs-on:', 1)[0]
        for event, branch, same_repo, image, skip, full_vs in (
            ('pull_request', 'codex/teamd-source-worker', True, 'none', True, True),
            ('pull_request', 'codex/teamd-source-worker', False, 'none', True, False),
            ('pull_request', 'codex/source-worker-apt-diagnostic', True, 'none', True, True),
            ('pull_request', 'codex/source-worker-apt-diagnostic', False, 'none', True, False),
            ('pull_request', 'codex/source-image-worker-dispatch', True, 'none', True, True),
            ('pull_request', 'codex/image-environment', True, 'none', True, True),
            ('pull_request', 'codex/sysmgr-source-image', True, 'none', True, True),
            ('pull_request', 'codex/sysmgr-source-image', False, 'none', True, False),
            ('pull_request', 'ordinary', True, 'none', True, True),
            ('pull_request', 'ordinary', False, 'none', True, False),
            ('push', '', True, 'none', True, True),
            ('workflow_dispatch', '', True, 'none', False, True),
            ('workflow_dispatch', '', True, 'none', True, False),
            ('workflow_dispatch', '', True, 'teamd', False, False),
            ('workflow_dispatch', '', True, 'dash-engine', False, False),
            ('workflow_dispatch', '', True, 'preflight', False, False),
        ):
            github = SimpleNamespace(event_name=event, head_ref=branch, repository='securely1g/sonic-buildimage',
                ref='refs/heads/master', event=SimpleNamespace(pull_request=SimpleNamespace(
                    head=SimpleNamespace(repo=SimpleNamespace(full_name='securely1g/sonic-buildimage' if same_repo else 'other/sonic-buildimage')))))
            inputs = SimpleNamespace(source_image=image, skip_vs=skip)
            def evaluate(expression):
                expression = ' '.join(expression.split()).replace('&&', ' and ').replace('||', ' or ')
                return bool(eval(re.sub(r'!(?!=)', 'not ', expression), {'__builtins__': {}},
                                 {'github': github, 'inputs': inputs}))
            with self.subTest(event=event, branch=branch, same_repo=same_repo, image=image):
                self.assertEqual(evaluate(vs), full_vs)

    def test_teamd_kernel_reads_preloaded_directories_and_retains_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modules, artifacts = root / 'modules', root / 'artifacts'
            modules.mkdir(); artifacts.mkdir()
            with patch.object(worker, 'run') as run, patch.object(worker, 'output') as output:
                for name in ('team', 'team_mode_loadbalance'):
                    with self.assertRaisesRegex(ValueError, 'preloaded kernel modules'):
                        worker.prepare_teamd_kernel(artifacts, modules)
                    receipt = json.loads((artifacts / 'worker-teamd-kernel.json').read_text())
                    self.assertEqual(receipt['status'], 'failed')
                    self.assertIn(name, receipt['missing_modules'])
                    (modules / name).write_text('regular file is not a loaded module')
                    with self.assertRaises(ValueError):
                        worker.prepare_teamd_kernel(artifacts, modules)
                    (modules / name).unlink(); (modules / name).mkdir()
                worker.prepare_teamd_kernel(artifacts, modules)
                receipt = json.loads((artifacts / 'worker-teamd-kernel.json').read_text())
                self.assertEqual(receipt, {'status': 'passed', 'read_only': True,
                    'required_modules': ['team', 'team_mode_loadbalance'], 'missing_modules': [],
                    'preloaded_modules': ['team', 'team_mode_loadbalance']})
                run.assert_not_called(); output.assert_not_called()

    def test_teamd_missing_modules_precede_all_direct_preparation_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            gate = worker.prepare_teamd_kernel
            with patch.object(worker, 'prepare_teamd_kernel',
                              side_effect=lambda out: gate(out, artifacts / 'missing')) as checked, \
                    patch.object(worker, 'measure') as measure, \
                    patch.object(worker, 'prepare_validation_tools') as tools, \
                    patch.object(worker, 'prepare_source_downloads') as downloads, \
                    patch.object(worker, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'preloaded kernel modules'):
                    worker.prepare(SimpleNamespace(image='teamd', workspace=Path('/source'), artifacts=artifacts))
                checked.assert_called_once_with(artifacts)
                for command in (measure, tools, downloads, run): command.assert_not_called()
            worker.publish(SimpleNamespace(artifacts=artifacts, image='teamd'))
            retained = json.loads((artifacts / 'public/worker-teamd-kernel.json').read_text())
            self.assertEqual(retained['status'], 'failed')
            self.assertEqual(retained['missing_modules'], ['team', 'team_mode_loadbalance'])

    def test_teamd_workflow_checks_modules_before_checkout_download_or_setup(self):
        workflow = (ROOT / '.github/workflows/bazel-oci.yml').read_text()
        gate = workflow.index('- name: Verify Teamd kernel prerequisites without host changes')
        end = workflow.index('- name: Checkout the exact reviewed source', gate)
        step = workflow[gate:end]
        self.assertIn("if: inputs.source_image == 'teamd'", step)
        self.assertIn('source_image_worker.py kernel-preflight', step)
        for name in ('Checkout the exact reviewed source', 'Reuse the existing source-download artifact',
                     'Download the verified source archives', 'Prepare private Docker and persistent worker caches'):
            self.assertLess(gate, workflow.index('- name: ' + name))
        self.assertIn("inputs.source_image == 'teamd' && steps.teamd_kernel.outcome != 'skipped'", workflow)

    def test_publication_retains_the_actual_aggregate_loaded_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            receipt = {'status': 'passed', 'revision': 'a' * 40, 'architecture': 'amd64',
                       'images': {'docker-teamd': {'status': 'passed'},
                                  'docker-teamd-dbg': {'status': 'passed'}}}
            source = artifacts / 'loaded-validation.json'
            source.write_text(json.dumps(receipt, indent=2) + '\n')
            worker.publish(SimpleNamespace(artifacts=artifacts, image='teamd'))
            self.assertEqual((artifacts / 'public/loaded-validation.json').read_bytes(), source.read_bytes())

    def test_teamd_fixed_driver_reuses_compatibility_wrapper_and_four_phases(self):
        workspace, artifacts = Path('/source'), Path('/evidence')
        self.assertEqual(worker.WRAPPERS['teamd'], 'libteam_draft_bazel.sh')
        for phase in ('build', 'validate', 'incremental'):
            expected = [sys.executable, '-B', '/source/tools/bazel/ci/teamd_image.py', phase,
                        '--artifacts', str(artifacts)]
            if phase != 'validate': expected += ['--bazel', '/source/tools/bazel/ci/libteam_draft_bazel.sh']
            self.assertEqual(worker.phase_command('teamd', phase, workspace, artifacts), expected)
        self.assertEqual(worker.phase_command('teamd', 'loaded', workspace, artifacts),
                         [sys.executable, '-B', '/source/tools/bazel/ci/teamd_loaded.py', '--artifacts', str(artifacts)])

    def network_result(self, uid):
        return {"uid": uid, "proxy_names_present": {name: False for name in
                ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")},
                "urllib_proxy_schemes": [], "http_status": 200}

    def test_execution_network_child_redacts_values_and_preserves_normal_tls(self):
        from unittest.mock import MagicMock
        import urllib.error
        secret = "https://private-user:private-password@private-proxy.example/?token=private-token"
        response = MagicMock()
        response.__enter__.return_value.status = 200
        for failure, expected in ((None, {"http_status": 200}),
                                  (urllib.error.URLError(secret), {"error_class": "URLError"}),
                                  (urllib.error.HTTPError(secret, 503, secret, {}, None),
                                   {"error_class": "HTTPError", "http_status": 503})):
            with self.subTest(failure=expected), patch.dict(os.environ, {"https_proxy": secret}, clear=True), \
                    patch.object(os, 'getuid', return_value=1000), \
                    patch.object(worker.urllib.request, 'getproxies', return_value={'https': secret, 'private-scheme': secret}), \
                    patch.object(worker.urllib.request, 'urlopen', return_value=response, side_effect=failure) as opened, \
                    patch('sys.stdout', new_callable=io.StringIO) as stream:
                exec(compile(worker.EXECUTION_NETWORK_PROBE, '<fixed-network-probe>', 'exec'), {})
            value = json.loads(stream.getvalue())
            self.assertEqual(value['uid'], 1000)
            self.assertTrue(value['proxy_names_present']['https_proxy'])
            self.assertFalse(value['proxy_names_present']['HTTPS_PROXY'])
            self.assertEqual(value['urllib_proxy_schemes'], ['https'])
            self.assertEqual({k: value[k] for k in ('http_status', 'error_class') if k in value}, expected)
            self.assertNotIn('private-', stream.getvalue())
            opened.assert_called_once_with('https://deb.debian.org/debian/dists/trixie/InRelease', timeout=10)
            worker.safe_json(value)

    def test_execution_network_uses_actual_worker_and_sudo_contexts_and_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            with patch.object(worker, 'output', side_effect=[json.dumps(self.network_result(1000)),
                                                            json.dumps(self.network_result(0))]) as output:
                worker.prepare_execution_network(artifacts)
            self.assertEqual([call.args[0] for call in output.call_args_list], [
                [sys.executable, '-I', '-c', worker.EXECUTION_NETWORK_PROBE],
                ['sudo', '-n', sys.executable, '-I', '-c', worker.EXECUTION_NETWORK_PROBE]])
            for call in output.call_args_list:
                self.assertEqual(call.kwargs, {'timeout': 20, 'stderr': subprocess.DEVNULL})
            path = artifacts / 'worker-execution-network.json'
            self.assertEqual(json.loads(path.read_text()), {'worker': self.network_result(1000), 'sudo': self.network_result(0)})
            worker.publish(SimpleNamespace(artifacts=artifacts))
            self.assertEqual(path.read_bytes(), (artifacts / 'public' / path.name).read_bytes())

    def test_execution_network_failure_and_unexpected_identity_are_nonfatal_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            failures = [subprocess.CalledProcessError(1, ['private-command'], output='private-auth', stderr='private-password'),
                        subprocess.TimeoutExpired(['private-command'], 20, output='private-token')]
            with patch.object(worker, 'output', side_effect=failures):
                worker.prepare_execution_network(artifacts)
            path = artifacts / 'worker-execution-network.json'
            self.assertEqual(json.loads(path.read_text()), {'worker': {'error_class': 'CalledProcessError'},
                                                          'sudo': {'error_class': 'TimeoutExpired'}})
            self.assertNotIn('private-', path.read_text())
            for value in (self.network_result(0), {**self.network_result(1000), 'proxy_names_present': {'https_proxy': 'private-password'}},
                          {**self.network_result(1000), 'error_class': 'private-auth://url'},
                          {**self.network_result(1000), 'extra': 'private-token'}):
                with patch.object(worker, 'output', side_effect=[json.dumps(value), json.dumps(self.network_result(0))]):
                    worker.prepare_execution_network(artifacts)
                self.assertEqual(json.loads(path.read_text())['worker'], {'error_class': 'ValueError'})
                self.assertNotIn('private-', path.read_text())

    def test_execution_network_precedes_apt_and_does_not_replace_apt_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            calls = []
            def apt(command):
                calls.append(command)
                raise subprocess.CalledProcessError(100, command)
            with patch.object(worker.shutil, 'which', return_value=None), \
                    patch.object(worker, 'prepare_execution_network', side_effect=lambda path: calls.append('diagnostic')), \
                    patch.object(worker, 'run', side_effect=apt), self.assertRaises(subprocess.CalledProcessError) as raised:
                worker.prepare_validation_tools(artifacts)
            self.assertEqual(raised.exception.returncode, 100)
            self.assertEqual(calls, ['diagnostic', ['sudo', '-n', 'apt-get', 'update']])
            with patch.object(worker.shutil, 'which', return_value='/usr/bin/tool'), \
                    patch.object(worker, 'output', return_value='versions'), \
                    patch.object(worker, 'prepare_execution_network') as diagnostic:
                worker.prepare_validation_tools(artifacts)
                diagnostic.assert_not_called()

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
                    patch.object(worker, 'prepare_execution_network'), \
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
                    patch.object(worker, 'prepare_execution_network'), \
                    patch.object(worker, 'run'), self.assertRaisesRegex(ValueError, 'Missing image validation tool'):
                worker.prepare_validation_tools(artifacts)

    def test_gawk_mirror_changes_only_three_exact_source_urls(self):
        original = b"verified existing mirror fixture\n"
        with patch.object(worker.source_mirrors, 'SHA256', hashlib.sha256(original).hexdigest()):
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
        with patch.object(worker.source_mirrors, 'SHA256', hashlib.sha256(original).hexdigest()):
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
        for image, stem in (("otel", "otel"), ("syncd", "syncd"), ("gearbox", "gbsyncd"), ("dash-engine", "dash_engine")):
            with self.subTest(image=image):
                driver = f"/source/tools/bazel/ci/{stem}_image.py"
                for phase in ("build", "validate", "incremental"):
                    expected = prefix + [driver, phase, "--artifacts", str(artifacts)]
                    if phase != "validate":
                        expected += ["--bazel", wrapper]
                    self.assertEqual(worker.phase_command(image, phase, root, artifacts), expected)
                self.assertEqual(worker.phase_command(image, "loaded", root, artifacts),
                                 prefix + [f"/source/tools/bazel/ci/{stem}_loaded.py", "--artifacts", str(artifacts)])

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
        self.assertEqual(set(choices), set(worker.DRIVERS))
        gates = re.findall(r"contains\(fromJSON\('(\[[^']+\])'\), inputs.source_image\)", workflow)
        self.assertEqual(len(gates), 2)
        for gate in gates:
            self.assertEqual(set(json.loads(gate)), set(worker.DRIVERS))

    def test_teamd_uses_one_landed_catalog_and_the_bounded_launcher(self):
        catalog = "a" * 40
        with patch.object(worker, "output", return_value=catalog + "\trefs/heads/main") as lookup:
            self.assertEqual(worker.catalog_for("teamd", ROOT), catalog)
            self.assertEqual(lookup.call_args.args[0][-1], "refs/heads/main")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "actual-bazel"
            binary.write_text("#!" + sys.executable + "\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            binary.chmod(0o755)
            bounded = root / "bounded-bazel"
            bounded.write_text(worker.launcher_text(str(binary), root / "base", worker.select_build_profile(20 * 1024**3, 2)))
            bounded.chmod(0o755)
            wrapper = ROOT / "tools/bazel/ci" / worker.WRAPPERS["teamd"]
            env = {**os.environ, "BAZEL_REAL": str(bounded), "SONIC_BASE_REVIEW_REGISTRY": catalog}
            self.assertEqual(json.loads(subprocess.check_output([wrapper, "--version"], env=env)), ["--version"])
            args = json.loads(subprocess.check_output([wrapper, "build", "--jobs=8", "//dockers/docker-teamd:docker-teamd.gz"], env=env))
            self.assertEqual([arg for arg in args if arg.startswith("--registry=")], [
                "--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" + catalog,
                "--registry=https://bcr.bazel.build"])
            self.assertIn("--output_base=" + str(root / "base"), args)
            self.assertEqual([arg for arg in args if arg.startswith("--jobs=")][-1], "--jobs=1")

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
