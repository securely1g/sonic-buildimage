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

SHARED_CONFIG_FIXTURE = (Path(__file__).parent / "fixtures/source-mirrors-97cc5025.cfg").read_text()

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("source_image_worker", ROOT / "tools/bazel/ci/source_image_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class ImageWorkerTest(unittest.TestCase):
    def scanner_budget_fixture(self, *, image="p4rt", patterns=True, error=None,
                               redacted=False, usage_update=None, receipt_update=None):
        from unittest.mock import MagicMock
        usage = {"maximum_bytes": 32 * 1024 ** 3, "minimum_free_bytes": 16 * 1024 ** 3,
                 "live_bytes": 0, "peak_bytes": 8192, "open_files": 0, "peak_open_files": 2}
        usage.update(usage_update or {})
        budget = MagicMock()
        budget.usage.return_value = usage
        make_budget = MagicMock(return_value=budget)
        redactor = MagicMock()
        redactor.patterns = ("private.example",) if patterns else ()
        redactor.scrub_and_scan.side_effect = error
        make_redactor = MagicMock(return_value=redactor)
        mirrors = SimpleNamespace(MIRROR_ROOTS=(("TEST_MIRROR_ROOT", "unused"),), Redactor=make_redactor)
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            public = artifacts / "public"
            public.mkdir()
            original = {"schema": 1, "image": image, "source_revision": "a" * 40}
            original.update(receipt_update or {})
            path = public / "worker.json"
            path.write_text(json.dumps(original))
            if redacted:
                (public / "mirror-redaction.json").write_text("{}")
            with patch.dict(sys.modules, {"private_mirrors": mirrors,
                                          "mirror_privacy": SimpleNamespace(TemporaryBudget=make_budget)}), \
                    patch.object(sys, "path", list(sys.path)), \
                    patch.dict(os.environ, {"TEST_MIRROR_ROOT": "https://private.example" if patterns else ""}, clear=True), \
                    patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
                expected_error = error is not None or redacted or usage_update or receipt_update
                if expected_error:
                    with self.assertRaises(ValueError):
                        worker.configure_worker_mirrors("scan-mirrors", artifacts, config=artifacts / "mirror.cfg", image=image)
                    self.assertEqual(json.loads(path.read_text()), original)
                else:
                    worker.configure_worker_mirrors("scan-mirrors", artifacts, config=artifacts / "mirror.cfg", image=image)
                self.assertNotIn("TEST_MIRROR_ROOT", os.environ)
            redactor.scrub_and_scan.assert_called_once_with(public)
            self.assertNotIn("private.example", diagnostic.getvalue())
            result = json.loads(path.read_text())
            worker.safe_json(result)
            if image == "p4rt":
                make_budget.assert_called_once_with(artifacts / "mirror-scan-temporaries",
                                                    maximum_bytes=32 * 1024 ** 3, minimum_free_bytes=16 * 1024 ** 3)
                self.assertEqual(stat.S_IMODE((artifacts / "mirror-scan-temporaries").stat().st_mode), 0o700)
                make_redactor.assert_called_once_with(["https://private.example"] if patterns else [], temporary_budget=budget)
            else:
                make_budget.assert_not_called()
                make_redactor.assert_called_once_with(["https://private.example"] if patterns else [])
                self.assertFalse((artifacts / "mirror-scan-temporaries").exists())
            return original, result

    def test_p4rt_scanner_budget_records_only_typed_charged_extents(self):
        original, result = self.scanner_budget_fixture()
        self.assertEqual(result.pop("mirror_scan"), {
            "status": "passed", "traversal_measured": True,
            "maximum_bytes": 34359738368, "minimum_free_bytes": 17179869184,
            "peak_charged_temporary_bytes": 8192, "peak_open_files": 2, "live_bytes": 0, "open_files": 0})
        self.assertEqual(result, original)

    def test_p4rt_scanner_without_patterns_reports_unmeasured(self):
        original, result = self.scanner_budget_fixture(patterns=False)
        self.assertEqual(result.pop("mirror_scan"), {
            "status": "not_run_no_private_patterns", "traversal_measured": False,
            "maximum_bytes": 34359738368, "minimum_free_bytes": 17179869184})
        self.assertEqual(result, original)

    def test_p4rt_scanner_other_profiles_keep_existing_interface(self):
        original, result = self.scanner_budget_fixture(image="syncd")
        self.assertEqual(result, original)

    def test_p4rt_scanner_errors_remain_failures_without_receipt(self):
        self.scanner_budget_fixture(error=ValueError("Scanner temporary-space budget exceeded"))
        self.scanner_budget_fixture(redacted=True)

    def test_p4rt_scanner_rejects_unclean_or_untyped_usage_and_wrong_receipt(self):
        for change in ({"open_files": 1}, {"live_bytes": 4096}, {"peak_bytes": True}):
            with self.subTest(usage=change):
                self.scanner_budget_fixture(usage_update=change)
        for change in ({"image": "syncd"}, {"mirror_scan": {}}):
            with self.subTest(receipt=change):
                self.scanner_budget_fixture(receipt_update=change)

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
                with patch.object(worker, 'prepare_apt_transport') as apt:
                    worker.prepare_execution_network(artifacts)
                apt.assert_called_once_with(artifacts)
            self.assertEqual([call.args[0] for call in output.call_args_list], [
                [sys.executable, '-I', '-c', worker.EXECUTION_NETWORK_PROBE],
                ['sudo', '-n', sys.executable, '-I', '-c', worker.EXECUTION_NETWORK_PROBE]])
            for call in output.call_args_list:
                self.assertEqual(call.kwargs, {'timeout': 20, 'stderr': subprocess.DEVNULL})
            path = artifacts / 'worker-execution-network.json'
            self.assertEqual(json.loads(path.read_text()), {'worker': self.network_result(1000), 'sudo': self.network_result(0)})
            worker.publish(SimpleNamespace(artifacts=artifacts))
            self.assertEqual(path.read_bytes(), (artifacts / 'public' / path.name).read_bytes())

    def apt_probe(self):
        namespace = {'__name__': 'fixed_apt_diagnostic_test'}
        exec(compile(worker.APT_TRANSPORT_PROBE, '<fixed-apt-probe>', 'exec'), namespace)
        return namespace

    def test_apt_configuration_redacts_fixed_values_and_rejects_malformed_output(self):
        import shlex
        probe = self.apt_probe()
        secret = 'https://private-user:private-password@private-host/\nprivate-token'
        raw = 'K0=' + shlex.quote(secret) + "\nK8='true'\nK9='_apt'\nK10='false'\n"
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout=raw)) as query:
            value = probe['configuration']()
        self.assertNotIn('private-', json.dumps(value))
        self.assertEqual(value['proxy_entries_present'], [True] + [False] * 7)
        self.assertFalse(value['srv_default'])
        args = query.call_args.args[0]
        self.assertEqual(args[:2], ['apt-get', 'shell'])
        self.assertEqual(query.call_args.kwargs, dict(executable='/usr/bin/apt-config', check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5))
        self.assertEqual(len(args[2:]), 66)
        for raw in ("K0='x' K0='y'", "UNKNOWN='private-secret'", "K8='private-secret'", "K0='unclosed", 'x' * 65537):
            with self.subTest(raw=raw[:30]), patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout=raw)):
                with self.assertRaises(ValueError):
                    probe['configuration']()
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout="K11='private-secret'")):
            value = probe['configuration']()
        self.assertTrue(value['method_overrides_present'])
        self.assertTrue(value['srv_default'])
        self.assertTrue(value['srv_enabled'])

    def test_apt_helper_order_private_destination_and_inherited_file_limit(self):
        probe = self.apt_probe()
        config = dict(proxy_entries_present=[False] * 8, srv_enabled=True, srv_default=True,
                      sandbox_kind='_apt', no_drop_privs=False, method_overrides_present=False, apt_config_present=False)
        events = []
        def download(directory, disabled):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
            events.append(('download', disabled))
            if not disabled:
                raise RuntimeError('private-secret')
            return {'status': 'exit_failure', 'exit_code': 100}
        with patch.dict(probe, {'configuration': lambda: config, 'download': download}), \
                patch.object(os, 'getuid', return_value=0), \
                patch.object(probe['pwd'], 'getpwnam', return_value=SimpleNamespace(pw_uid=42, pw_gid=42)) as account, \
                patch.object(probe['resource'], 'setrlimit', side_effect=lambda *args: events.append(('limit', args))), \
                patch.object(os, 'umask') as mask, patch.object(os, 'chown') as owner:
            result = probe['main']()
        account.assert_called_once_with('_apt')
        owner.assert_called_once()
        self.assertEqual(owner.call_args.args[1:], (42, 42))
        mask.assert_called_once_with(0o077)
        self.assertEqual(events, [('limit', (probe['resource'].RLIMIT_FSIZE, (1048576, 1048576))),
                                  ('download', False), ('download', True)])
        self.assertEqual(result['attempts'], [{'status': 'error', 'error_class': 'RuntimeError'},
                                             {'status': 'exit_failure', 'exit_code': 100}])
        self.assertNotIn('private-', json.dumps(result))
        for changed in ({'method_overrides_present': True}, {'sandbox_kind': 'other'}):
            with patch.dict(probe, {'configuration': lambda: {**config, **changed}}), \
                    patch.object(os, 'getuid', return_value=0), patch.dict(probe, {'download': unittest.mock.Mock()}):
                result = probe['main']()
                probe['download'].assert_not_called()
            self.assertEqual(result['status'], 'unsupported_configuration')

    def test_apt_helper_commands_size_checks_and_no_raw_output(self):
        from unittest.mock import Mock
        probe = self.apt_probe()
        for disabled in (False, True):
            for payload, expected in ((b'fixed InRelease bytes', 'downloaded'), (b'', 'size_or_type_rejected'),
                                      (b'x' * (1048576 + 1), 'size_or_type_rejected')):
                with tempfile.TemporaryDirectory() as directory:
                    process = Mock(pid=123456)
                    process.wait.return_value = 0
                    path = Path(directory) / ('srv-disabled' if disabled else 'baseline')
                    def start(args, **kwargs):
                        path.write_bytes(payload)
                        return process
                    with patch.object(subprocess, 'Popen', side_effect=start) as popen, patch.object(os, 'killpg'), \
                            patch.object(os, 'waitid', return_value=SimpleNamespace(si_code=os.CLD_EXITED, si_status=0)), \
                            patch.object(probe['time'], 'sleep'):
                        result = probe['download'](directory, disabled)
                    args = ['apt-get', '-o', 'Acquire::Retries=0', '-o', 'Acquire::http::Timeout=10',
                            '-o', 'Acquire::https::Timeout=10']
                    if disabled:
                        args += ['-o', 'Acquire::EnableSrvRecords=false']
                    args += ['download-file', 'https://deb.debian.org/debian/dists/trixie/InRelease', str(path)]
                    popen.assert_called_once_with(args, executable='/usr/lib/apt/apt-helper', start_new_session=True,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.assertEqual(result['status'], expected)
                    if expected == 'downloaded':
                        self.assertEqual(result['bytes'], len(payload))
                        self.assertEqual(result['sha256'], hashlib.sha256(payload).hexdigest())
                    else:
                        self.assertNotIn('sha256', result)

    def test_apt_helper_timeout_terminates_only_its_own_process_group(self):
        from unittest.mock import Mock, call
        probe = self.apt_probe()
        process = Mock(pid=123456)
        process.wait.return_value = -9
        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess, 'Popen', return_value=process), \
                patch.object(os, 'killpg') as kill, patch.object(os, 'waitid', return_value=None) as observe, \
                patch.object(probe['time'], 'monotonic', side_effect=[0, 0, 16]), patch.object(probe['time'], 'sleep'):
            result = probe['download'](directory, False)
        self.assertEqual(result, {'status': 'timeout'})
        self.assertEqual(kill.call_args_list, [call(123456, probe['signal'].SIGTERM), call(123456, probe['signal'].SIGKILL)])
        observe.assert_called_once_with(os.P_PID, 123456, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        process.wait.assert_called_once_with(timeout=2)

    def test_apt_helper_exited_leader_is_not_reaped_until_descendants_are_killed(self):
        from unittest.mock import Mock
        probe = self.apt_probe()
        events = []
        process = Mock(pid=123456)
        process.wait.side_effect = lambda **kwargs: events.append(('reap', kwargs)) or 100
        def observe(*args):
            events.append(('observe_unreaped', args))
            return SimpleNamespace(si_code=os.CLD_EXITED, si_status=100)
        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess, 'Popen', return_value=process), \
                patch.object(os, 'waitid', side_effect=observe), \
                patch.object(os, 'killpg', side_effect=lambda *args: events.append(('signal', args))), \
                patch.object(probe['time'], 'sleep', side_effect=lambda delay: events.append(('grace', delay))):
            result = probe['download'](directory, False)
        self.assertEqual(result, {'status': 'exit_failure', 'exit_code': 100})
        self.assertEqual(events, [('observe_unreaped', (os.P_PID, 123456, os.WEXITED | os.WNOHANG | os.WNOWAIT)),
                                 ('signal', (123456, probe['signal'].SIGTERM)), ('grace', 2),
                                 ('signal', (123456, probe['signal'].SIGKILL)), ('reap', {'timeout': 2})])
        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess, 'Popen', return_value=process), \
                patch.object(os, 'waitid', side_effect=ChildProcessError('private-secret')), patch.object(os, 'killpg') as kill:
            with self.assertRaises(ChildProcessError):
                probe['download'](directory, False)
        kill.assert_not_called()

    def test_apt_transport_receipt_rejects_secrets_and_remains_publishable(self):
        result = {'scope': 'transport_only_no_signature_verification', 'status': 'completed',
                  'config': dict(proxy_entries_present=[False] * 8, srv_enabled=True, srv_default=True,
                                 sandbox_kind='_apt', no_drop_privs=False, method_overrides_present=False,
                                 apt_config_present=False),
                  'attempts': [{'status': 'exit_failure', 'exit_code': 100},
                               {'status': 'downloaded', 'exit_code': 0, 'bytes': 14, 'sha256': 'a' * 64}]}
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            with patch.object(worker, 'output', return_value=json.dumps(result)) as output:
                worker.prepare_apt_transport(artifacts)
            output.assert_called_once_with(['sudo', '-n', sys.executable, '-I', '-c', worker.APT_TRANSPORT_PROBE],
                                           timeout=50, stderr=subprocess.DEVNULL)
            path = artifacts / 'worker-apt-transport.json'
            self.assertEqual(json.loads(path.read_text()), result)
            worker.publish(SimpleNamespace(artifacts=artifacts))
            self.assertEqual(path.read_bytes(), (artifacts / 'public' / path.name).read_bytes())
            invalid = [dict(result, extra='private-password'), dict(result, error_class='private-password://host'),
                       {'scope': result['scope'], 'status': 'completed'},
                       {**result, 'config': {**result['config'], 'sandbox_kind': 'private-account'}},
                       {**result, 'attempts': [{'status': 'error', 'stderr': 'private-token'}] * 2},
                       {**result, 'attempts': [{'status': 'downloaded', 'exit_code': 0, 'bytes': 1048577, 'sha256': 'a' * 64}] * 2}]
            for value in invalid:
                with patch.object(worker, 'output', return_value=json.dumps(value)):
                    worker.prepare_apt_transport(artifacts)
                self.assertEqual(json.loads(path.read_text()), {'status': 'error', 'error_class': 'ValueError'})
                self.assertNotIn('private-', path.read_text())

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

    def test_execution_network_only_after_apt_failure_and_preserves_original(self):
        update = ['sudo', '-n', 'apt-get', '-o', 'Acquire::EnableSrvRecords=false', 'update']
        install = ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get',
                   '-o', 'Acquire::EnableSrvRecords=false', 'install', '-y', '--no-install-recommends', 'libarchive-tools']
        for failure_at in (0, 1):
            for diagnostic_failure in (False, True):
                with self.subTest(failure_at=failure_at, diagnostic_failure=diagnostic_failure), \
                        tempfile.TemporaryDirectory() as directory:
                    artifacts = Path(directory)
                    commands = []
                    original = subprocess.CalledProcessError(100, [update, install][failure_at])
                    def apt(command):
                        commands.append(command)
                        if len(commands) == failure_at + 1:
                            raise original
                    def diagnose(path):
                        self.assertEqual(path, artifacts)
                        commands.append('diagnostic')
                        if diagnostic_failure:
                            raise RuntimeError('private-secret-value')
                    with patch.object(worker.shutil, 'which', side_effect=lambda tool: None if tool == 'bsdtar' else '/usr/bin/' + tool), \
                            patch.object(worker, 'run', side_effect=apt), \
                            patch.object(worker, 'prepare_execution_network', side_effect=diagnose), \
                            patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                            self.assertRaises(subprocess.CalledProcessError) as raised:
                        worker.prepare_validation_tools(artifacts)
                    self.assertIs(raised.exception, original)
                    self.assertEqual(commands, [update, install][:failure_at + 1] + ['diagnostic'])
                    self.assertNotIn('private-', stderr.getvalue())
                    self.assertFalse((artifacts / 'worker-execution-tools.json').exists())

    def test_execution_network_is_skipped_when_apt_succeeds_or_tools_exist(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                commands = []
                def available(tool):
                    return None if missing and tool == 'bsdtar' and not commands else '/usr/bin/' + tool
                with patch.object(worker.shutil, 'which', side_effect=available), \
                        patch.object(worker, 'run', side_effect=lambda command: commands.append(command)), \
                        patch.object(worker, 'output', return_value='versions'), \
                        patch.object(worker, 'prepare_execution_network') as diagnostic, \
                        patch.object(worker.urllib.request, 'urlopen') as opened:
                    worker.prepare_validation_tools(Path(directory))
                diagnostic.assert_not_called()
                opened.assert_not_called()
                self.assertEqual(len(commands), 2 if missing else 0)

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
        self.assertIn('source_image_worker.py" kernel-preflight', step)
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

    def native_job(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())["jobs"]["source_image"]

    def native_paths(self, root):
        workspace, temporary = root / "workspace", root / "temporary"
        workspace.mkdir(); temporary.mkdir()
        environment = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temporary),
                       "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "RUNNER_NAME": "sonic-slave-lgh114-1",
                       "EXPECTED_WORKER": "sonic-slave-lgh114-1", "SELECTED_IMAGE": "dash-engine",
                       "SOURCE_REVISION": "a" * 40}
        for kind in ("CONTROL", "SOURCE", "SCRATCH"):
            environment["SOURCE_WORKER_" + kind] = "source-image-" + kind.lower() + "-123-2-sonic-slave-lgh114-1"
        return workspace, temporary, environment

    def test_native_workflow_preserves_old_work_and_binds_every_driver_invocation(self):
        job = self.native_job()
        steps = {step["name"]: step for step in job["steps"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, temporary, environment = self.native_paths(root)
            old = [workspace / "control/keep", workspace / "source/target/output",
                   workspace / "source/.bazelrc.user", temporary / "source-image-artifacts/receipt.json",
                   temporary / "source-image-inputs/input.tar.gz"]
            for path in old:
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text("retained")
            subprocess.run(["bash", "-e", "-c", steps["Require fresh native worker paths"]["run"]],
                           env=environment, cwd=workspace, check=True, capture_output=True)
            control = workspace / environment["SOURCE_WORKER_CONTROL"]
            helper = control / "tools/bazel/ci/source_image_worker.py"
            helper.parent.mkdir(parents=True)
            helper.write_text("import json,os,sys\nfrom pathlib import Path\n"
                              "with Path(os.environ['TRACE']).open('a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')\n")
            trace = root / "trace.jsonl"; environment["TRACE"] = str(trace)
            for step in job["steps"][1:]:
                if step.get("if") == "inputs.source_image == 'teamd'":
                    continue
                if "run" in step:
                    subprocess.run(["bash", "-e", "-c", step["run"]], env=environment,
                                   cwd=workspace, check=True, capture_output=True)
            commands = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([cmd[0] for cmd in commands],
                             ["selection", "verify-input-run", "prepare-mirrors", "prepare", "build", "validate", "incremental", "loaded", "publish", "scan-mirrors", "stop", "cleanup-mirrors"])
            for cmd in commands:
                self.assertEqual(cmd[1:], ["--image", "dash-engine", "--revision", "a" * 40,
                    "--workspace", str(workspace / environment["SOURCE_WORKER_SOURCE"]),
                    "--artifacts", str(temporary / environment["SOURCE_WORKER_SCRATCH"] / "artifacts")])
            for path in old:
                self.assertEqual(path.read_text(), "retained")
        for name, variable in (("Checkout reviewed dispatcher", "SOURCE_WORKER_CONTROL"),
                               ("Checkout the exact reviewed source", "SOURCE_WORKER_SOURCE")):
            self.assertEqual(steps[name]["with"]["path"], "${{ env." + variable + " }}")
            self.assertIs(steps[name]["with"]["clean"], False)
            self.assertIs(steps[name]["with"]["persist-credentials"], False)
        for name in ("Reuse the existing source-download artifact", "Download the verified source archives"):
            self.assertEqual(steps[name]["with"]["path"], "${{ runner.temp }}/${{ env.SOURCE_WORKER_SCRATCH }}/source-image-inputs")
        self.assertEqual(steps["Upload source images and acceptance receipts"]["with"]["path"],
                         "${{ runner.temp }}/${{ env.SOURCE_WORKER_SCRATCH }}/artifacts/public/")

    def test_native_path_gate_refuses_collisions_and_wrong_identities_without_deleting(self):
        guard = self.native_job()["steps"][0]["run"]
        cases = [(kind, linked) for kind in ("CONTROL", "SOURCE", "SCRATCH") for linked in (False, True)]
        cases += [("worker", False), ("attempt", False), ("traversal", False)]
        for kind, linked in cases:
            with self.subTest(kind=kind, linked=linked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace, temporary, environment = self.native_paths(root)
                keep = root / "keep"; keep.write_text("retained")
                collision = None
                if kind in ("CONTROL", "SOURCE", "SCRATCH"):
                    collision = (temporary if kind == "SCRATCH" else workspace) / environment["SOURCE_WORKER_" + kind]
                    if linked:
                        collision.symlink_to(root / "missing")
                    else:
                        collision.mkdir(); (collision / "keep").write_text("retained")
                elif kind == "worker": environment["RUNNER_NAME"] = "sonic-slave-lgh114-2"
                elif kind == "attempt": environment["GITHUB_RUN_ATTEMPT"] = "../2"
                else: environment["SOURCE_WORKER_SOURCE"] = "../" + environment["SOURCE_WORKER_SOURCE"]
                result = subprocess.run(["bash", "-e", "-c", guard], env=environment, cwd=workspace, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(keep.read_text(), "retained")
                if collision is not None:
                    if linked: self.assertTrue(collision.is_symlink())
                    else: self.assertEqual((collision / "keep").read_text(), "retained")

    def test_worker_temporary_directory_is_bound_to_run_attempt_and_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, temporary, environment = self.native_paths(root)
            with patch.dict(os.environ, environment):
                expected = temporary / environment["SOURCE_WORKER_SCRATCH"]
                self.assertEqual(worker.temporary_directory(), expected)
                self.assertFalse(expected.exists())
                for changed in ("../scratch", "source-image-scratch-123-1-sonic-slave-lgh114-1",
                                "source-image-scratch-123-2-sonic-slave-lgh114-2"):
                    with patch.dict(os.environ, {"SOURCE_WORKER_SCRATCH": changed}), self.assertRaises(ValueError):
                        worker.temporary_directory()
                expected.symlink_to(root / "missing")
                with self.assertRaisesRegex(ValueError, "symlinks"):
                    worker.temporary_directory()
                with patch.dict(os.environ, {"SOURCE_WORKER_SCRATCH": ""}):
                    self.assertEqual(worker.temporary_directory(), temporary)

    def test_optional_catalog_requires_an_immutable_source_pair(self):
        for catalog in ("", "b" * 40):
            with patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": catalog}):
                worker.selection("dash-engine", "a" * 40)
                with self.assertRaises(ValueError): worker.selection("dash-engine", "main")
        for catalog in ("main", "B" * 40, "b" * 39, "b" * 40 + ";id"):
            with self.subTest(catalog=catalog), patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": catalog}), \
                    self.assertRaises(ValueError):
                worker.selection("dash-engine", "a" * 40)

    def test_explicit_catalog_uses_review_entrypoint_without_changing_blank_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / "tools/bazel/ci").mkdir(parents=True)
            path = root / ".github/workflows/bazel-oci.yml"
            path.write_text("env:\n  BAZEL: tools/bazel/ci/review_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("dash-engine", root), "a" * 40)
            self.assertEqual(worker.catalog_for("dash-engine", root, "b" * 40), "b" * 40)
            for catalog in ("main", "B" * 40, "b" * 39):
                with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                    worker.catalog_for("dash-engine", root, catalog)
            path.write_text(path.read_text().replace("review_bazel.sh", "libteam_draft_bazel.sh"))
            (root / "tools/bazel/ci/libteam_draft_bazel.sh").write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("dash-ha", root), "a" * 40)
            self.assertEqual(worker.catalog_for("dash-ha", root, "a" * 40), "a" * 40)
            with self.assertRaisesRegex(ValueError, "cannot override"):
                worker.catalog_for("dash-ha", root, "b" * 40)

    def test_changed_explicit_catalog_is_rejected_before_a_phase_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(image="dash-engine", revision="a" * 40, workspace=Path("/source"),
                                   artifacts=Path(directory), phase="build")
            state = {"image": args.image, "revision": args.revision, "workspace": str(args.workspace),
                     "environment": {"SONIC_BAZEL_REGISTRY_REVISION": "b" * 40}}
            (args.artifacts / "worker-private.json").write_text(json.dumps(state))
            with patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": "b" * 40}):
                self.assertEqual(worker.state_for(args), state)
            with patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": "c" * 40}), \
                    patch.object(worker.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "Prepared catalog"):
                worker.execute_phase(args)
            run.assert_not_called()

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
                ['sudo', '-n', 'apt-get', '-o', 'Acquire::EnableSrvRecords=false', 'update'],
                ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get',
                 '-o', 'Acquire::EnableSrvRecords=false', 'install', '-y',
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
        # Bind the actual immutable base while preserving the existing suffix rules.
        actual = SHARED_CONFIG_FIXTURE.encode()
        self.assertEqual(worker.MIRROR_URL,
                         "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/97cc502556b7ec3435d2d67b105315bcc18bb47e/ci/source_mirrors.cfg")
        self.assertEqual(hashlib.sha256(actual).hexdigest(), worker.MIRROR_SHA)
        self.assertTrue(worker.mirror_configuration(actual).startswith(actual))
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            worker.mirror_configuration(actual + b'changed')
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

    def test_dedicated_checks_precede_build_and_stop_on_failure(self):
        checks = {
            "database": ("database_make_test.py",),
            "eventd": ("eventd_make_test.py",),
            "lldp": ("lldp_make_test.py", "lldp_identity_test.py"),
            "radv": ("radv_make_test.py",),
            "macsec": (),
            "mux": ("mux_make_test.py",),
        }
        memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
        for image, scripts in checks.items():
            for failure in (None, *range(len(scripts))):
                with self.subTest(image=image, failure=failure), tempfile.TemporaryDirectory() as directory:
                    artifacts = Path(directory)
                    args = SimpleNamespace(image=image, revision="a" * 40, phase="build",
                                           workspace=Path("/source"), artifacts=artifacts)
                    state = {"image": image, "revision": args.revision, "workspace": str(args.workspace),
                             "environment": {}, "build_profile": worker.select_build_profile(20 * 1024**3, 4)}
                    (artifacts / "worker-private.json").write_text(json.dumps(state))
                    expected = [[sys.executable, "-B", "/source/tools/bazel/tests/" + script] for script in scripts]
                    expected.append(worker.phase_command(image, "build", args.workspace, artifacts))
                    results = [SimpleNamespace(returncode=0) for _ in expected]
                    if failure is not None:
                        results[failure].returncode = 7
                    with patch.object(worker, "source_check") as source_check, \
                            patch.object(worker, "memory_snapshot", return_value=memory), \
                            patch.object(worker.subprocess, "run", side_effect=results) as run:
                        if failure is None:
                            worker.execute_phase(args)
                        else:
                            with self.assertRaisesRegex(ValueError, "Image phase failed: build"):
                                worker.execute_phase(args)
                        self.assertEqual([call.args[0] for call in run.call_args_list],
                                         expected if failure is None else expected[:failure + 1])
                        self.assertEqual(source_check.call_count, 2)
                    receipt = json.loads((artifacts / "worker-build.json").read_text())
                    self.assertEqual(receipt["status"], "passed" if failure is None else "failed")
                    self.assertEqual(receipt["exit_code"], 0 if failure is None else 7)

    def test_dedicated_build_checks_do_not_repeat_during_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            args = SimpleNamespace(image="lldp", revision="a" * 40, phase="validate",
                                   workspace=Path("/source"), artifacts=artifacts)
            memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
            state = {"image": args.image, "revision": args.revision, "workspace": str(args.workspace),
                     "environment": {}, "build_profile": worker.select_build_profile(20 * 1024**3, 4)}
            (artifacts / "worker-private.json").write_text(json.dumps(state))
            with patch.object(worker, "source_check"), patch.object(worker, "memory_snapshot", return_value=memory), \
                    patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                worker.execute_phase(args)
            self.assertEqual([call.args[0] for call in run.call_args_list],
                             [worker.phase_command("lldp", "validate", args.workspace, artifacts)])

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
        for image, stem in (("otel", "otel"), ("syncd", "syncd"), ("gearbox", "gbsyncd"), ("dash-engine", "dash_engine"), ("framework", "mgmt_framework"),
                            ("database", "database"), ("eventd", "eventd"), ("lldp", "lldp"),
                            ("radv", "radv"), ("macsec", "macsec"), ("mux", "mux"), ("frr", "frr")):
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

    def test_service_dispatches_select_only_the_existing_native_image_job(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition

        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        jobs = workflow["jobs"]
        for image in ("database", "eventd", "lldp", "radv", "macsec", "mux", "frr"):
            with self.subTest(image=image):
                selected = [name for name, job in jobs.items() if condition(
                    str(job.get("if", "true")).replace("inputs.source_inputs_run", "'41689621493'"), mode=image)]
                self.assertEqual(selected, ["source_image"])
                self.assertTrue(condition(jobs["source_image_inputs"]["if"], mode=image))
                self.assertFalse(condition(jobs["source_image"]["if"], mode=image))
        self.assertEqual(jobs["source_image"]["runs-on"],
                         ["sonic-slave-container-dpool", "${{ inputs.source_worker }}"])
        for image in ("snmp", "base", "config-engine", "swss-layer"):
            with self.subTest(unsupported=image), self.assertRaises(ValueError):
                worker.selection(image, "a" * 40)

    def test_worker_followups_skip_only_their_owned_deb_fixtures_and_vs(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition

        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        kernel = yaml.safe_load((ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text())
        guards = [step["env"]["SOURCE_ONLY_NO_DEBS"] for job in workflow["jobs"].values()
                  for step in job.get("steps", []) if "SOURCE_ONLY_NO_DEBS" in step.get("env", {})]
        self.assertEqual(len(guards), 2)
        for branch in ("codex/orchagent-source-worker-current", "codex/source-image-worker-families"):
            for guard in guards:
                self.assertTrue(condition(guard, event="pull_request", branch=branch))
                for changes in ({"branch": branch + "-suffix"}, {"branch": "unrelated"},
                                {"head_repo": "other/fork"}, {"event": "push"},
                                {"event": "workflow_dispatch"}):
                    with self.subTest(branch=branch, changes=changes):
                        context = {"event": "pull_request", "branch": branch, **changes}
                        self.assertFalse(condition(guard, **context))
            for name in ("vs", "vs_container", "source_image", "source_image_inputs", "registry_native",
                         "source_worker_preflight", "worker_provenance"):
                self.assertFalse(condition(workflow["jobs"][name]["if"], event="pull_request", branch=branch))
            self.assertTrue(condition(workflow["jobs"]["vs_container"]["if"], event="pull_request", branch=branch + "-suffix"))
            for name in ("archive", "checks"):
                self.assertTrue(condition(workflow["jobs"][name]["if"], event="pull_request", branch=branch))
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
            self.assertEqual(worker.catalog_for("frr", root), "a" * 40)
            workflow.write_text("env:\n  BAZEL: ${{ github.workspace }}/tools/bazel/ci/libteam_draft_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            wrapper = root / "tools/bazel/ci/libteam_draft_bazel.sh"
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("dash-ha", root), "a" * 40)
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "b" * 40 + "\n")
            with self.assertRaises(ValueError):
                worker.catalog_for("dash-ha", root)

    def test_macsec_catalog_comes_from_its_driver_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / '.github/workflows'; workflows.mkdir(parents=True)
            drivers = root / 'tools/bazel/ci'; drivers.mkdir(parents=True)
            (workflows / 'bazel-oci.yml').write_text('env:\n  BAZEL: tools/bazel/ci/libteam_draft_bazel.sh\n')
            workflow = workflows / 'bazel-macsec.yml'
            prefix = 'env:\n  SONIC_BAZEL_REGISTRY_REVISION: '
            command = '\n  run: |\n    python3 -B tools/bazel/ci/macsec_image.py build --artifacts artifacts/macsec\n'
            driver = drivers / 'macsec_image.py'
            driver.write_text('parser.add_argument("--bazel", default="tools/bazel/ci/review_bazel.sh")\n')
            for expression in ('a' * 40, "${{ inputs.registry_revision || '" + 'a' * 40 + "' }}"):
                workflow.write_text(prefix + expression + command)
                self.assertEqual(worker.catalog_for('macsec', root), 'a' * 40)
                self.assertEqual(worker.catalog_for('macsec', root, 'b' * 40), 'b' * 40)
                for invalid in ('main', 'A' * 40, '--registry=other'):
                    with self.assertRaises(ValueError): worker.catalog_for('macsec', root, invalid)
            for expression in ('main', '${{ inputs.registry_revision }}', '${{ arbitrary.expression }}'):
                workflow.write_text(prefix + expression + command)
                with self.assertRaises(ValueError): worker.catalog_for('macsec', root, 'b' * 40)
            workflow.write_text(prefix + 'a' * 40 + command + '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'b' * 40 + '\n')
            with self.assertRaises(ValueError): worker.catalog_for('macsec', root)

    def test_macsec_catalog_rejects_other_entrypoints_and_commented_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / '.github/workflows'; workflows.mkdir(parents=True)
            drivers = root / 'tools/bazel/ci'; drivers.mkdir(parents=True)
            (workflows / 'bazel-oci.yml').write_text('env:\n  BAZEL: bazel\n')
            workflow = workflows / 'bazel-macsec.yml'
            content = ('env:\n  SONIC_BAZEL_REGISTRY_REVISION: ' + 'a' * 40 + '\n  run: |\n'
                       '    python3 -B tools/bazel/ci/macsec_image.py build --artifacts artifacts/macsec\n')
            workflow.write_text(content)
            driver = drivers / 'macsec_image.py'
            valid = 'parser.add_argument("--bazel", default="tools/bazel/ci/review_bazel.sh")\n'
            for invalid in (valid.replace('review_bazel.sh', 'other.sh'), '# ' + valid,
                            valid.replace('"tools/bazel/ci/review_bazel.sh"', 'dynamic'), valid + valid):
                driver.write_text(invalid)
                with self.assertRaises(ValueError): worker.catalog_for('macsec', root, 'b' * 40)
            driver.write_text(valid)
            workflow.write_text(content.replace(' build --artifacts', ' build --bazel other --artifacts'))
            with self.assertRaises(ValueError): worker.catalog_for('macsec', root, 'b' * 40)

    def test_dedicated_service_catalogs_preserve_combined_source_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / '.github/workflows'; workflows.mkdir(parents=True)
            common = workflows / 'bazel-oci.yml'
            for image, filename in worker.SOURCE_WORKFLOWS.items():
                common.write_text('env:\n  BAZEL: bazel\n')
                dedicated = workflows / filename
                dedicated.write_text('env:\n  BAZEL: ./tools/bazel/ci/review_bazel.sh\n'
                                     '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'a' * 40 + '\n')
                self.assertEqual(worker.catalog_for(image, root), 'a' * 40)
                self.assertEqual(worker.catalog_for(image, root, 'b' * 40), 'b' * 40)
                common.write_text('env:\n  BAZEL: tools/bazel/ci/review_bazel.sh\n'
                                  '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'c' * 40 + '\n')
                self.assertEqual(worker.catalog_for(image, root), 'c' * 40)
                self.assertEqual(worker.catalog_for(image, root, 'b' * 40), 'b' * 40)
                common.write_text('env:\n  BAZEL: bazel\n')
                dedicated.write_text('env:\n  BAZEL: bazel\n'
                                     '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'a' * 40 + '\n')
                if image == 'macsec':
                    (root / 'tools/bazel/ci').mkdir(parents=True)
                    (root / 'tools/bazel/ci/macsec_image.py').write_text('')
                with self.assertRaises(ValueError): worker.catalog_for(image, root, 'b' * 40)
            self.assertNotIn('database', worker.SOURCE_WORKFLOWS)
            with self.assertRaises(ValueError): worker.catalog_for('database', root, 'b' * 40)

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

    def test_publication_retains_checked_loaded_validation_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build-receipt.json").write_text('{"artifacts":{}}')
            receipt = {"status": "passed", "images": {"runtime": {"status": "passed"},
                                                      "debug": {"status": "passed"}}}
            source = root / "loaded-validation.json"
            source.write_text(json.dumps(receipt))
            worker.publish(SimpleNamespace(artifacts=root))
            self.assertEqual(json.loads((root / "public/loaded-validation.json").read_text()), receipt)
            source.write_text(json.dumps({**receipt, "environment": {"TOKEN": "private"}}))
            with self.assertRaises(ValueError):
                worker.publish(SimpleNamespace(artifacts=root))
            self.assertEqual(json.loads((root / "public/loaded-validation.json").read_text()), receipt)


if __name__ == "__main__":
    unittest.main()
