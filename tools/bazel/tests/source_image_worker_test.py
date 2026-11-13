#!/usr/bin/env python3
"""Check fixed image dispatch and public artifact boundaries."""

import importlib.util
from contextlib import ExitStack, contextmanager
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

    def test_execution_network_uses_actual_worker_and_sudo_contexts_and_stays_private(self):
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
            path = artifacts / 'private-worker-execution-network.json'
            self.assertEqual(json.loads(path.read_text()), {'worker': self.network_result(1000), 'sudo': self.network_result(0)})
            worker.publish(SimpleNamespace(artifacts=artifacts))
            self.assertFalse((artifacts / 'public' / path.name).exists())

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

    def test_apt_transport_receipt_rejects_secrets_and_stays_private(self):
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
            path = artifacts / 'private-worker-apt-transport.json'
            self.assertEqual(json.loads(path.read_text()), result)
            worker.publish(SimpleNamespace(artifacts=artifacts))
            self.assertFalse((artifacts / 'public' / path.name).exists())
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
            path = artifacts / 'private-worker-execution-network.json'
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

    @patch.object(worker, 'select_tools_cache_transport', return_value=False)
    def test_execution_network_only_after_apt_failure_and_preserves_original(self, _transport):
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

    @patch.object(worker, 'select_tools_cache_transport', return_value=False)
    def test_execution_network_is_skipped_when_apt_succeeds_or_tools_exist(self, _transport):
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

    def test_pool_selection_preserves_legacy_and_limits_k3s_images(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(worker.selected_worker_pool(), "sonic-slave-container-dpool")
            worker.selection("sysmgr", "a" * 40)
        with patch.dict(os.environ, {"WORKER_POOL": "sonic-slave-container-k3s"}, clear=True):
            for image in ("syncd", "gearbox", "p4rt"):
                worker.selection(image, "a" * 40)
            with self.assertRaisesRegex(ValueError, "not admitted"):
                worker.selection("sysmgr", "a" * 40)
        with patch.dict(os.environ, {"WORKER_POOL": "unknown"}, clear=True), self.assertRaises(ValueError):
            worker.selection("syncd", "a" * 40)

    def test_pool_cache_root_is_fixed_and_preserves_dpool_layout(self):
        self.assertEqual(worker.worker_cache_root(), Path("/bazel_cache"))
        self.assertEqual(worker.worker_cache_root("sonic-slave-container-k3s"),
                         Path("/actions/_work/.source-image-cache"))
        with self.assertRaises(ValueError):
            worker.worker_cache_root("/private/cache")

    def test_k3s_workflow_binds_actual_assignment_and_request_concurrency(self):
        job = self.native_job()
        self.assertNotIn("SOURCE_WORKER_CONTROL", job["env"])
        self.assertEqual(job["env"]["WORKER_POOL"], "${{ inputs.source_worker_pool }}")
        self.assertIn("inputs.source_image, inputs.source_ci_request", job["concurrency"]["group"])
        self.assertNotIn("sonic-slave-container-k3s-vs", job["concurrency"]["group"])
        self.assertIs(job["concurrency"]["cancel-in-progress"], False)
        steps = {step["name"]: step for step in job["steps"]}
        first = steps["Require fresh native worker paths"]
        self.assertEqual(first["env"]["ASSIGNED_WORKER"], "${{ runner.name }}")
        self.assertIn("SOURCE_CI_REQUEST", first["run"])
        for name in ("Configure Debian downloads for the native worker",
                     "Scan staged source-image artifacts for private mirror locations"):
            for value in steps[name]["env"].values():
                self.assertIn("inputs.source_worker_pool != 'sonic-slave-container-k3s'", value)
        names = [step["name"] for step in job["steps"]]
        self.assertLess(names.index("Bind the exact normal CI merge before native work"),
                        names.index("Prepare private Docker and persistent worker caches"))
        self.assertLess(names.index("Stop only this job's private Docker daemon"),
                        names.index("Prepare declared artifacts and public evidence"))

    def native_job(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())["jobs"]["source_image"]

    def native_paths(self, root):
        workspace, temporary = root / "workspace", root / "temporary"
        workspace.mkdir(); temporary.mkdir()
        environment = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temporary),
                       "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "RUNNER_NAME": "sonic-slave-lgh114-1",
                       "EXPECTED_WORKER": "sonic-slave-lgh114-1", "SELECTED_IMAGE": "dash-engine",
                       "SOURCE_REVISION": "a" * 40,
                       "WORKER_POOL": "sonic-slave-container-dpool",
                       "ASSIGNED_WORKER": "sonic-slave-lgh114-1",
                       "GITHUB_OUTPUT": str(root / "github-output"), "GITHUB_ENV": str(root / "github-env")}
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
                if step.get("if") in ("inputs.source_image == 'teamd'", "inputs.source_image == 'p4rt'") or "inputs.source_ci_request" in step.get("if", ""):
                    continue
                if "run" in step:
                    subprocess.run(["bash", "-e", "-c", step["run"]], env=environment,
                                   cwd=workspace, check=True, capture_output=True)
            commands = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([cmd[0] for cmd in commands],
                             ["selection", "verify-input-run", "prepare-mirrors", "prepare", "build", "validate", "incremental", "loaded", "stop", "publish", "scan-mirrors", "cleanup-mirrors"])
            for cmd in commands:
                self.assertEqual(cmd[1:], ["--image", "dash-engine", "--revision", "a" * 40,
                    "--workspace", str(workspace / environment["SOURCE_WORKER_SOURCE"]),
                    "--artifacts", str(temporary / environment["SOURCE_WORKER_SCRATCH"] / "artifacts")])
            for path in old:
                self.assertEqual(path.read_text(), "retained")
        for name, variable in (("Checkout reviewed dispatcher", "SOURCE_WORKER_CONTROL"),
                               ("Checkout the exact reviewed source", "SOURCE_WORKER_SOURCE")):
            self.assertEqual(steps[name]["with"]["path"], "${{ steps.worker_paths.outputs." + variable.removeprefix("SOURCE_WORKER_").lower() + " }}")
            self.assertIs(steps[name]["with"]["clean"], False)
            self.assertIs(steps[name]["with"]["persist-credentials"], False)
        for name in ("Reuse the existing source-download artifact", "Download the verified source archives"):
            self.assertEqual(steps[name]["with"]["path"], "${{ runner.temp }}/${{ steps.worker_paths.outputs.scratch }}/source-image-inputs")
        self.assertEqual(steps["Upload source images and acceptance receipts"]["with"]["path"],
                         "${{ runner.temp }}/${{ steps.worker_paths.outputs.scratch }}/artifacts/public/")

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
        for pool, runner in (("sonic-slave-container-dpool", "sonic-slave-lgh114-1"),
                             ("sonic-slave-container-k3s", "ks-fixture-123")):
            with self.subTest(pool=pool), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, temporary, environment = self.native_paths(root)
                environment.update(WORKER_POOL=pool, RUNNER_NAME=runner,
                    SOURCE_WORKER_SCRATCH=f"source-image-scratch-123-2-{runner}")
                with patch.dict(os.environ, environment):
                    expected = temporary / environment["SOURCE_WORKER_SCRATCH"]
                    self.assertEqual(worker.temporary_directory(), expected)
                    self.assertFalse(expected.exists())
                    for changed in ("../scratch", f"source-image-scratch-123-1-{runner}",
                                    "source-image-scratch-123-2-other-worker"):
                        with patch.dict(os.environ, {"SOURCE_WORKER_SCRATCH": changed}), self.assertRaises(ValueError):
                            worker.temporary_directory()
                    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
                        for invalid in ("", "0", "01", "../2", "2\n"):
                            with self.subTest(key=key, invalid=invalid), patch.dict(os.environ, {key: invalid}), \
                                    self.assertRaises(ValueError):
                                worker.temporary_directory()
                    expected.symlink_to(root / "missing")
                    with self.assertRaisesRegex(ValueError, "symlinks"):
                        worker.temporary_directory()
                    with patch.dict(os.environ, {"SOURCE_WORKER_SCRATCH": ""}):
                        self.assertEqual(worker.temporary_directory(), temporary)

    def test_worker_scratch_rejects_mismatched_and_unknown_pools(self):
        for pool, runner in (("sonic-slave-container-dpool", "ks-fixture-123"),
                             ("sonic-slave-container-k3s", "sonic-slave-lgh114-1"),
                             ("unknown-pool", "ks-fixture-123"),
                             ("sonic-slave-container-k3s", "ks-fixture/escape")):
            with self.subTest(pool=pool, runner=runner), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, _, environment = self.native_paths(root)
                environment.update(WORKER_POOL=pool, RUNNER_NAME=runner,
                    SOURCE_WORKER_SCRATCH=f"source-image-scratch-123-2-{runner}")
                with patch.dict(os.environ, environment), self.assertRaises(ValueError):
                    worker.temporary_directory()

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

    @patch.object(worker, 'select_tools_cache_transport', return_value=False)
    def test_validation_bootstrap_installs_only_missing_execution_tools(self, _transport):
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
            for cpus, jobs, memory in ((2, 1, 4000), (4, 2, 8000), (6, 2, 8000)):
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
        for image in worker.DRIVERS.keys() - {"p4rt"}:
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

    def p4rt_owner(self, workspace, temporary):
        launch, rc = temporary / "p4rt-owner-bazel", temporary / "p4rt-owner.bazelrc"
        launch.write_text("reviewed launcher"); rc.write_text("reviewed rc")
        return {"source": str(workspace / worker.P4RT_OWNER_PATH), "revision": worker.P4RT_OWNER,
                "bazel": str(launch), "bazel_sha256": worker.digest(launch), "rc": str(rc),
                "rc_sha256": worker.digest(rc), "output_base": "/bazel_cache/source-images/sonic-slave-lgh114-1/p4rt-owner/output-base",
                "jobs": 1}

    def test_p4rt_uses_exact_seven_existing_driver_interfaces(self):
        workspace, artifacts = Path("/source"), Path("/evidence")
        owner = {"source": "/source/" + worker.P4RT_OWNER_PATH, "revision": worker.P4RT_OWNER,
                 "bazel": "/private/owner-bazel", "output_base": "/cache/p4rt-owner/output-base", "jobs": 1}
        common = ["--directory", str(artifacts), "--owner-source", owner["source"]]
        prefix = [sys.executable, "-B"]
        wrapper = "/source/tools/bazel/ci/review_bazel.sh"
        phases = ("prepare-owner", "prepare-owner-runtime", "owner-tests", "build", "validate", "incremental", "loaded")
        self.assertEqual(worker.P4RT_PHASES, phases)
        for phase in phases:
            if phase == "loaded":
                expected = prefix + ["/source/tools/bazel/ci/p4rt_loaded.py", *common]
            elif phase in ("validate", "incremental"):
                expected = prefix + ["/source/tools/bazel/ci/p4rt_validate.py", phase, *common, "--bazel", wrapper]
            else:
                expected = prefix + ["/source/tools/bazel/ci/p4rt_image.py", phase, *common, "--bazel", wrapper,
                    "--owner-revision", worker.P4RT_OWNER, "--owner-bazel", owner["bazel"],
                    "--owner-output-base", owner["output_base"], "--jobs", "1"]
            self.assertEqual(worker.phase_command("p4rt", phase, workspace, artifacts, owner), expected)
        for change in ({"revision": "a" * 40}, {"source": "/other"}, {"jobs": 2}):
            with self.assertRaises(ValueError):
                worker.phase_command("p4rt", "build", workspace, artifacts, {**owner, **change})
        with self.assertRaises(ValueError):
            worker.phase_command("p4rt", "build", workspace, artifacts)
        with self.assertRaises(ValueError):
            worker.phase_command("frr", "owner-tests", workspace, artifacts, owner)

    def test_p4rt_workflow_preparation_precedes_four_phases_and_stops_on_failure(self):
        import yaml
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        steps = workflow["jobs"]["source_image"]["steps"]
        selected = []
        for step in steps:
            match = re.search(r'source_image_worker.py" ([a-z-]+) ', step.get("run", ""))
            if match and match.group(1) in worker.P4RT_PHASES:
                selected.append((match.group(1), step))
        self.assertEqual(tuple(phase for phase, _ in selected), worker.P4RT_PHASES)
        for index, (phase, step) in enumerate(selected):
            self.assertNotIn("continue-on-error", step)
            if index < 3:
                self.assertEqual(step["if"], "inputs.source_image == 'p4rt'")
            else:
                self.assertNotIn("if", step)  # GitHub's implicit success() gate.
        self.assertEqual(workflow["jobs"]["source_image"]["timeout-minutes"], 360)
        self.assertEqual(workflow["jobs"]["source_image"]["runs-on"],
                         "${{ inputs.source_worker_pool == 'sonic-slave-container-k3s' && "
                         "fromJSON('[\"sonic-slave-container-k3s\"]') || "
                         "fromJSON(format('[\"sonic-slave-container-dpool\",\"{0}\"]', inputs.source_worker)) }}")

    def test_p4rt_owner_setup_verifies_gitlink_and_separate_cache_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);workspace = root / "source";workspace.mkdir()
            (workspace / ".bazelversion").write_text("8.5.1\n")
            owner = workspace / worker.P4RT_OWNER_PATH;owner.mkdir(parents=True)
            (owner / ".bazelversion").write_text("6.4.0\n")
            (owner / "WORKSPACE.bazel").write_text("workspace(name='owner')\n")
            (owner / "ci").mkdir();(owner / "ci/production_runtime.py").write_text("# reviewed driver\n")
            temporary = root / "scratch";temporary.mkdir();cache = root / "cache"
            def output(argv):
                if "ls-tree" in argv: return "160000 commit " + worker.P4RT_OWNER + "\t" + worker.P4RT_OWNER_PATH
                if "config" in argv: return "https://github.com/securely1g/sonic-pins.git"
                if "rev-parse" in argv: return worker.P4RT_OWNER
                if "status" in argv: return ""
                raise AssertionError(argv)
            with patch.object(worker, "output", side_effect=output), patch.object(worker, "run") as run, \
                    patch.object(worker.shutil, "which", return_value="/tools/bazelisk"):
                context = worker.prepare_p4rt(workspace, cache, temporary, [cache / "distdir"], temporary / "mirrors.cfg")
            self.assertEqual(run.call_args_list[0].args[0], ["git", "-C", str(workspace), "submodule", "update", "--init", "--checkout", "--", worker.P4RT_OWNER_PATH])
            self.assertEqual(context["output_base"], str(cache / "p4rt-owner/output-base"))
            self.assertNotEqual(context["output_base"], str(cache / "p4rt/output-base"))
            rc = Path(context["rc"]).read_text()
            for option in ("common --repository_cache=", "build --disk_cache=", "build --local_ram_resources=4096", "common --distdir=", "common --experimental_downloader_config="):
                self.assertIn(option, rc)
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", [context["bazel"], "build", "//p4rt_app:p4rt_runtime"]), \
                    patch.object(os, "execv") as execute:
                exec(compile(Path(context["bazel"]).read_text(), context["bazel"], "exec"), {})
                self.assertEqual(os.environ["USE_BAZEL_VERSION"], "6.4.0")
                self.assertEqual(os.environ["BAZELISK_HOME"], str(cache / "p4rt-owner/bazelisk"))
            argv = execute.call_args.args[1]
            self.assertIn("--output_base=" + context["output_base"], argv)
            self.assertIn("--host_jvm_args=-Xmx2g", argv)
            self.assertIn("--bazelrc=" + context["rc"], argv)
            self.assertIn("--nohome_rc", argv)
            self.assertNotIn("--noworkspace_rc", argv)  # Preserve the owner's p4rt-runtime config.
            self.assertFalse(any("remote_download_regex" in x or "local_resources=" in x for x in argv))
            self.assertEqual(argv[-2:], ["build", "//p4rt_app:p4rt_runtime"])
            (workspace / ".bazelversion").write_text("6.4.0\n")
            with patch.object(worker, "run") as run, self.assertRaises(ValueError):
                worker.prepare_p4rt(workspace, cache, temporary, [], temporary / "mirror")
            run.assert_not_called()
            (workspace / ".bazelversion").write_text("8.5.1\n")
            with patch.object(worker, "output", return_value="160000 commit " + "a" * 40), \
                    patch.object(worker, "run") as run, self.assertRaises(ValueError):
                worker.prepare_p4rt(workspace, cache, temporary, [], temporary / "mirror")
            run.assert_not_called()
            (owner / ".bazelversion").write_text("8.5.1\n")
            with patch.object(worker, "output", side_effect=output), patch.object(worker, "run"), self.assertRaises(ValueError):
                worker.p4rt_owner_identity(workspace)

    def test_private_transport_diagnostics_stay_out_of_all_public_publishers(self):
        diagnostics = (
            "private-worker-execution-network.json",
            "private-worker-apt-transport.json",
            "private-worker-apt-transport-selection.json",
        )
        for image in ("syncd", "gearbox", "p4rt"):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as directory:
                artifacts = Path(directory)
                private = json.dumps({"environment": {"FIXTURE": "private-diagnostic"}}) + "\n"
                for name in diagnostics:
                    (artifacts / name).write_text(private)
                approved = {"status": "passed"}
                (artifacts / "worker-execution-tools.json").write_text(json.dumps(approved) + "\n")
                worker.publish(SimpleNamespace(image=image, artifacts=artifacts))
                public = artifacts / "public"
                self.assertEqual({path.name for path in public.iterdir()},
                                 {"worker-execution-tools.json", "build-summary.json"})
                self.assertEqual(json.loads((public / "worker-execution-tools.json").read_text()), approved)
                for name in diagnostics:
                    self.assertEqual((artifacts / name).read_text(), private)
                    self.assertFalse((public / name).exists())

    def test_public_publishers_still_reject_unredacted_selected_worker_evidence(self):
        for image in ("syncd", "gearbox", "p4rt"):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as directory:
                artifacts = Path(directory)
                (artifacts / "worker-execution-tools.json").write_text(
                    json.dumps({"environment": {"FIXTURE": "private-diagnostic"}}) + "\n")
                with self.assertRaises(ValueError):
                    worker.publish(SimpleNamespace(image=image, artifacts=artifacts))

    def test_p4rt_publication_delegates_before_generic_directory_creation(self):
        from tools.bazel.ci import p4rt_public
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def prepare(path):
                self.assertEqual(path, root)
                self.assertFalse((root / "public").exists())
                (root / "public").mkdir()
                return root / "public"
            with patch.object(p4rt_public, "prepare", side_effect=prepare) as call:
                self.assertEqual(worker.publish(SimpleNamespace(image="p4rt", artifacts=root)), root / "public")
            call.assert_called_once_with(root)

    def test_p4rt_phase_receipts_enforce_order_and_preserve_failure(self):
        memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory);artifacts = root / "artifacts";artifacts.mkdir()
                workspace = root / "source";workspace.mkdir();temporary = root / "scratch";temporary.mkdir()
                args = SimpleNamespace(image="p4rt", revision="a" * 40, workspace=workspace, artifacts=artifacts, phase="prepare-owner")
                owner = self.p4rt_owner(workspace, temporary)
                state = {"image": "p4rt", "revision": args.revision, "workspace": str(workspace), "environment": {"BAZEL_REAL": "/root-bazel"},
                         "p4rt_owner": owner, "build_profile": worker.select_build_profile(20 * 1024**3, 4)}
                (artifacts / "worker-private.json").write_text(json.dumps(state))
                def result(argv, **kwargs):
                    return SimpleNamespace(returncode=1 if fail and "owner-tests" in argv else 0)
                with patch.dict(os.environ, {"RUNNER_NAME": "sonic-slave-lgh114-1", "SOURCE_REGISTRY_REVISION": ""}), \
                        patch.object(worker, "temporary_directory", return_value=temporary), \
                        patch.object(worker, "p4rt_owner_identity", return_value=Path(owner["source"])), \
                        patch.object(worker, "source_check"), patch.object(worker, "memory_snapshot", return_value=memory), \
                        patch.object(worker.subprocess, "run", side_effect=result) as run:
                    args.phase = "build"
                    with self.assertRaises(FileNotFoundError): worker.execute_phase(args)
                    run.assert_not_called()
                    for phase in worker.P4RT_PHASES:
                        args.phase = phase
                        if fail and phase == "owner-tests":
                            with self.assertRaisesRegex(ValueError, "Image phase failed"): worker.execute_phase(args)
                            self.assertEqual(json.loads((artifacts / "worker-owner-tests.json").read_text())["status"], "failed")
                            calls = run.call_count;args.phase = "build"
                            with self.assertRaisesRegex(ValueError, "predecessor"): worker.execute_phase(args)
                            self.assertEqual(run.call_count, calls)
                            break
                        worker.execute_phase(args)
                        receipt = json.loads((artifacts / ("worker-" + phase + ".json")).read_text())
                        self.assertEqual(receipt["owner_profile"], worker.P4RT_OWNER_PROFILE)
                    if not fail:
                        self.assertEqual(run.call_count, 8)  # Seven phases and the existing root JVM shutdown.
                        self.assertEqual(run.call_args_list[-2].args[0], ["/root-bazel", "shutdown"])
                    Path(owner["rc"]).write_text("tampered")
                    args.phase = "prepare-owner"
                    with self.assertRaisesRegex(ValueError, "configuration changed"): worker.execute_phase(args)

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
        import yaml
        jobs = yaml.safe_load(workflow)["jobs"]
        gates = [re.search(r"contains\(fromJSON\('(\[[^']+\])'\), inputs.source_image\)", jobs[name]["if"]).group(1)
                 for name in ("source_image_inputs", "source_image")]
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
        for image in ("database", "eventd", "lldp", "radv", "macsec", "mux", "frr", "p4rt"):
            with self.subTest(image=image):
                selected = [name for name, job in jobs.items() if condition(
                    str(job.get("if", "true")).replace("inputs.source_inputs_run", "'41689621493'"), mode=image)]
                self.assertEqual(selected, ["source_image"])
                self.assertTrue(condition(jobs["source_image_inputs"]["if"], mode=image))
                self.assertFalse(condition(jobs["source_image"]["if"], mode=image))
        self.assertIn("inputs.source_worker_pool == 'sonic-slave-container-k3s'", jobs["source_image"]["runs-on"])
        self.assertIn('sonic-slave-container-dpool', jobs["source_image"]["runs-on"])
        self.assertIn('inputs.source_worker', jobs["source_image"]["runs-on"])
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
        for branch in ("codex/orchagent-source-worker-current", "codex/source-image-worker-families", "codex/source-image-worker-p4rt-feature"):
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


class PrivateDockerMountTeardownTest(unittest.TestCase):
    def cleanup_fixture(self, root, snapshots, clock):
        """Exercise the exact elevated stdlib program with no process or mount action."""
        reader = patch.object(Path, "read_text", side_effect=snapshots)
        return reader, patch.object(worker.time, "monotonic", side_effect=clock)

    def run_cleanup(self, root, snapshots, clock):
        reader, timer = self.cleanup_fixture(root, snapshots, clock)
        with reader as reads, timer, patch.object(worker.time, "sleep") as sleep, \
                patch.object(worker.shutil, "rmtree") as remove, \
                patch.object(sys, "argv", ["private-docker-cleanup", str(root)]), \
                patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
            error = None
            try:
                exec(compile(worker.docker_cleanup_script(), "private-docker-cleanup", "exec"), {})
            except (AssertionError, ValueError, IndexError) as caught:
                error = caught
            return error, reads, sleep, remove, diagnostic.getvalue()

    def mount_row(self, root, relative="overlay2/layer/merged", number=100, filesystem="overlay"):
        target = str(root / relative).replace("\\", "\\134").replace(" ", "\\040")
        return f"{number} 1 0:42 / {target} rw - {filesystem} private-source rw,private-option\n"

    def test_delayed_teardown_waits_before_removing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-docker"
            root.mkdir()
            error, reads, sleep, remove, diagnostic = self.run_cleanup(
                root, [self.mount_row(root), ""], [0, 0])
            self.assertIsNone(error)
            self.assertEqual(reads.call_count, 2)
            sleep.assert_called_once_with(0.5)
            remove.assert_called_once_with(root)
            self.assertEqual(diagnostic, "")

    def test_persistent_mount_refuses_deletion_with_bounded_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-docker"
            root.mkdir()
            relative = "volumes/private token/merged"
            row = self.mount_row(root, relative)
            error, reads, sleep, remove, diagnostic = self.run_cleanup(root, [row, row], [0, 0, 30])
            self.assertIsInstance(error, AssertionError)
            self.assertIn("still has a mounted child", str(error))
            self.assertEqual(reads.call_count, 2)
            sleep.assert_called_once_with(0.5)
            remove.assert_not_called()
            value = json.loads(diagnostic)
            self.assertEqual((value["phase"], value["wait_limit_seconds"], value["mount_count"]),
                             ("private_docker_mount_teardown", 30, 1))
            self.assertEqual(value["reported_mounts"][0], {
                "mount_id": 100, "parent_id": 1, "filesystem": "overlay", "location": "volumes",
                "relative_depth": 3, "relative_path_sha256": hashlib.sha256(relative.encode()).hexdigest()})
            self.assertFalse(value["truncated"])
            for private in (str(root), "private token", "private-source", "private-option"):
                self.assertNotIn(private, diagnostic)
            self.assertTrue(root.is_dir())

    def test_timeout_diagnostics_limit_rows_and_unknown_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-docker"
            root.mkdir()
            rows = "".join(self.mount_row(root, "private-name/" + str(i), i + 100, "private-fstype")
                           for i in range(20))
            error, reads, sleep, remove, diagnostic = self.run_cleanup(root, [rows], [0, 30])
            self.assertIsInstance(error, AssertionError)
            sleep.assert_not_called()
            remove.assert_not_called()
            value = json.loads(diagnostic)
            self.assertEqual(value["mount_count"], 20)
            self.assertEqual(len(value["reported_mounts"]), 16)
            self.assertTrue(value["truncated"])
            self.assertTrue(all(x["filesystem"] == x["location"] == "other" for x in value["reported_mounts"]))
            self.assertNotIn("private-name", diagnostic)
            self.assertNotIn("private-fstype", diagnostic)

    def test_unrelated_prefix_mount_does_not_block_private_data_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-docker"
            root.mkdir()
            row = self.mount_row(root.with_name(root.name + "-other"))
            error, reads, sleep, remove, diagnostic = self.run_cleanup(root, [row], [0])
            self.assertIsNone(error)
            sleep.assert_not_called()
            remove.assert_called_once_with(root)
            self.assertEqual(diagnostic, "")

    def test_symlink_or_unparseable_mount_state_never_deletes_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-docker"
            root.mkdir()
            link = Path(directory) / "linked-docker"
            link.symlink_to(root, target_is_directory=True)
            for selected, snapshots in ((link, []), (root, ["invalid\n"])):
                with self.subTest(path=selected):
                    error, reads, sleep, remove, diagnostic = self.run_cleanup(selected, snapshots, [0])
                    self.assertIsNotNone(error)
                    remove.assert_not_called()
                    sleep.assert_not_called()
                    self.assertTrue(root.is_dir())


class ProvedDockerRootMountTest(unittest.TestCase):
    def program(self):
        namespace = {"__name__": "owned_mount_fixture"}
        exec(compile(worker.docker_owned_mount_script(), "owned-mount-fixture", "exec"), namespace)
        return namespace

    def fixture(self, directory):
        root = Path(directory) / "source-image-docker-123-1"
        root.mkdir()
        pidfile = Path(directory) / "daemon.pid"
        pidfile.write_text("123\n")
        functions = self.program()
        parent = {"id": 10, "parent": 1, "device": "8:1", "root": "/actions",
                  "point": directory, "options": "rw", "optional": [],
                  "filesystem": "ext4", "source": "/dev/fixture", "super": ["rw"]}
        mounted = dict(parent, id=11, parent=10, root="/actions/" + root.name,
                       point=str(root), optional=["shared:99"])
        before = {"covering": parent, "mount_ids": [10], "parent": functions["directory"](root.parent)}
        ready = {"scope": {}, "root_mount": mounted, "directory": functions["directory"](root), "daemon": {"pid": 123}}
        inventory = {"schema": 1, "scope": {}, "ready_sha256": "ready", "daemon": ready["daemon"],
                     "monotonic": 100, "inventory": {"root": str(root), "containers": 0,
                     "running": 0, "paused": 0, "stopped": 0}}
        return functions, root, pidfile, parent, mounted, before, ready, inventory

    def cleanup_case(self, fixture, rows, *, times=(100, 100), returncode=0, alive_children=()):
        functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
        original_exists = Path.exists
        def exists(path):
            return (False if str(path) == "/proc/123" else True if str(path) in {"/proc/" + str(pid) for pid in alive_children} else original_exists(path))
        with patch.dict(functions, {"load_proof": lambda *args: (before, ready, "ready"),
                                    "read_record": lambda path: ({"schema": 1, "scope": {}, "ready_sha256": "ready",
                                        "daemon": ready["daemon"], "descendants": ready.get("descendants", []),
                                        "monotonic": 100, "status": "OBSERVED_OWNED_PROCESSES_CLOSED"}, "closed")
                                        if path.name.endswith(".owned-process-closure.json") else (inventory, "inventory"),
                                    "namespace": lambda: [1, 2]}), \
             patch.dict(functions, {"mounts": unittest.mock.Mock(side_effect=rows)}), \
             patch.object(functions["time"], "monotonic", side_effect=times), \
             patch.object(functions["time"], "sleep") as sleep, \
             patch.object(Path, "exists", exists), \
             patch.object(functions["subprocess"], "run", return_value=SimpleNamespace(returncode=returncode)) as command, \
             patch.object(functions["shutil"], "rmtree") as remove, \
             patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
            error = None
            try:
                functions["cleanup"](root, pidfile, {})
            except (ValueError, OSError) as caught:
                error = caught
            return error, command, remove, sleep, diagnostic.getvalue()

    def test_proves_only_new_same_backing_root_self_bind(self):
        with tempfile.TemporaryDirectory() as directory:
            functions, root, pidfile, parent, mounted, before, ready, inventory = self.fixture(directory)
            functions["self_bind"](before, mounted, root)
            for changes in ({"id": 10}, {"parent": 99}, {"device": "8:2"},
                            {"root": "/different"}, {"source": "/dev/other"},
                            {"point": str(root / "child")}, {"optional": ["master:99"]}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    functions["self_bind"](before, dict(mounted, **changes), root)
            changed = dict(before, covering=dict(parent, optional=["shared:5"]))
            with self.assertRaises(ValueError):
                functions["self_bind"](changed, mounted, root)

    def test_mount_parser_decodes_paths_and_rejects_duplicate_identity(self):
        functions = self.program()
        value = "11 10 8:1 /actions/a\\040b /tmp/a\\040b rw shared:99 - ext4 /dev/fixture rw\n"
        rows = functions["parse_mounts"](value)
        self.assertEqual((rows[0]["root"], rows[0]["point"]), ("/actions/a b", "/tmp/a b"))
        for data in (value + value, "invalid\n", "1 0 8:1 / /tmp/../other rw - ext4 fixture rw\n"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                functions["parse_mounts"](data)

    def test_existing_root_cannot_receive_fresh_before_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            functions, root, pidfile, parent, mounted, before, ready, inventory = self.fixture(directory)
            with patch.dict(functions, {"mounts": lambda: [parent], "write_record": unittest.mock.Mock(), "tools_ready": lambda: None}):
                with self.assertRaises(ValueError):
                    functions["capture"]("before", root, pidfile, {})
                functions["write_record"].assert_not_called()

    def test_nonempty_or_wrong_endpoint_root_inventory_is_rejected(self):
        functions = self.program()
        root = Path("/actions/_work/root")
        valid = {"root": str(root), "containers": 0, "running": 0, "paused": 0, "stopped": 0}
        functions["empty_inventory"](valid, root)
        for changes in ({"root": "/other"}, {"containers": 1}, {"running": 1},
                        {"paused": 1}, {"stopped": 1}, {"containers": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                functions["empty_inventory"](dict(valid, **changes), root)

    def test_delayed_descendant_then_one_normal_fd_anchored_unmount(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
            child = dict(mounted, id=12, parent=11, point=str(root / "overlay2/layer/merged"))
            error, command, remove, sleep, diagnostic = self.cleanup_case(
                fixture, [[parent, mounted, child], [parent, mounted], [parent, mounted], [parent]],
                times=(100, 100, 100))
            self.assertIsNone(error)
            sleep.assert_called_once_with(0.5)
            command.assert_called_once()
            argv = command.call_args.args[0]
            self.assertEqual(argv[:-1], ["/usr/bin/umount", "--no-canonicalize", "--internal-only", "--no-mtab", "--"])
            self.assertRegex(argv[-1], r"^/proc/self/fd/[0-9]+/source-image-docker-123-1$")
            self.assertEqual(command.call_args.kwargs["timeout"], 10)
            self.assertEqual(len(command.call_args.kwargs["pass_fds"]), 1)
            remove.assert_called_once_with(root)
            self.assertEqual(diagnostic, "")

    def test_busy_normal_unmount_preserves_data_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
            error, command, remove, sleep, diagnostic = self.cleanup_case(
                fixture, [[parent, mounted], [parent, mounted], [parent, mounted]], returncode=32)
            self.assertIsInstance(error, ValueError)
            command.assert_called_once()
            remove.assert_not_called()
            self.assertTrue(root.is_dir())
            self.assertEqual(json.loads(diagnostic)["reported_mounts"][0]["location"], "root")

    def test_prior_normal_unmount_attempt_refuses_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
            record = root.with_name(root.name + ".normal-unmount-attempt.json")
            functions["write_record"](record, {"status": "previous-attempt"})
            original = record.read_bytes()
            error, command, remove, sleep, diagnostic = self.cleanup_case(
                fixture, [[parent, mounted], [parent, mounted], [parent, mounted]])
            self.assertIsInstance(error, FileExistsError)
            command.assert_not_called()
            remove.assert_not_called()
            self.assertEqual(record.read_bytes(), original)
            self.assertTrue(root.is_dir())

    def test_persistent_descendants_or_changed_root_never_unmount(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
            child = dict(mounted, id=12, parent=11, point=str(root / "containers/private/merged"))
            for rows, times in (([parent, mounted, child], (100, 100, 130)),
                                ([parent, dict(mounted, id=99)], (100, 100))):
                with self.subTest(rows=rows):
                    error, command, remove, sleep, diagnostic = self.cleanup_case(fixture, [rows], times=times)
                    self.assertIsInstance(error, ValueError)
                    command.assert_not_called()
                    remove.assert_not_called()
                    self.assertNotIn(str(root), diagnostic)
                    self.assertNotIn("private", diagnostic.replace("private_docker_mount_teardown", ""))

    def test_daemon_already_removed_its_root_mount_needs_no_unmount(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
            error, command, remove, sleep, diagnostic = self.cleanup_case(fixture, [[parent], [parent]])
            self.assertIsNone(error)
            command.assert_not_called()
            remove.assert_called_once_with(root)

    def test_stale_empty_inventory_never_unmounts_or_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fixture[-1]["monotonic"] = 0
            error, command, remove, sleep, diagnostic = self.cleanup_case(fixture, [], times=(100,))
            self.assertIsInstance(error, ValueError)
            command.assert_not_called()
            remove.assert_not_called()

    def test_proof_symlink_and_nonroot_record_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            functions = self.program()
            original = Path(directory) / "proof.json"
            original.write_text("{}\n")
            original.chmod(0o444)
            linked = Path(directory) / "link.json"
            linked.symlink_to(original)
            with self.assertRaises(OSError):
                functions["read_record"](linked)
            if os.getuid() != 0:
                with self.assertRaises(ValueError):
                    functions["read_record"](original)

    def test_legacy_state_dispatch_remains_unchanged_and_unknown_protocol_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(artifacts=Path(directory))
            (args.artifacts / "worker-private.json").write_text("{}\n")
            with patch.object(worker, "state_for", return_value={}), patch.object(worker, "stop") as legacy, \
                 patch.object(worker, "stop_with_mount_proof") as current:
                worker.stop_selected(args)
                legacy.assert_called_once_with(args)
                current.assert_not_called()
            with patch.object(worker, "state_for", return_value={"docker_mount_protocol": True}), \
                 patch.object(worker, "stop") as legacy, patch.object(worker, "stop_with_mount_proof") as current:
                with self.assertRaises(ValueError):
                    worker.stop_selected(args)
                legacy.assert_not_called()
                current.assert_not_called()

    def test_inventory_failure_still_stops_reproved_daemon_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            pidfile = Path(directory) / "daemon.pid"
            pidfile.write_text("123\n")
            state = {"docker_root": str(root), "pidfile": str(pidfile)}
            calls = []
            def action(args, state, mode):
                calls.append(mode)
                if mode == "inventory":
                    raise ValueError("Inventory cannot be established")
            with patch.object(worker, "docker_mount_scope", return_value={}), \
                 patch.object(worker, "run_docker_mount_action", side_effect=action):
                with self.assertRaisesRegex(ValueError, "Inventory cannot be established"):
                    worker.stop_with_mount_proof(SimpleNamespace(), state)
            self.assertEqual(calls, ["inventory", "stop"])
            self.assertTrue(root.is_dir() and pidfile.exists())

    def stop_case(self, *, mismatch=False, capture_error=False, persistent_child=False, disappearing_tasks=False):
        functions = self.program()
        current = {"pid": 123, "start_ticks": 40, "namespace": [1, 2]}
        child = {"pid": 456, "start_ticks": 50, "namespace": [1, 2]}
        ready = {"daemon": current, "descendants": [child]}
        alive = {123: True, 456: not capture_error}
        def exists(path):
            return alive.get(int(path.name), False) if str(path).startswith("/proc/") else False
        def signal_owned(handle, number, info, flags):
            self.assertEqual((handle, number, info, flags), (99, functions["signal"].SIGTERM, None, 0))
            alive[123] = bool(disappearing_tasks)
        def sleep(seconds):
            self.assertEqual(seconds, 0.5)
            alive[123] = False
            if not persistent_child:
                alive[456] = False
        functions.update({"load_proof": lambda *args: ({}, ready, "ready"),
                          "daemon": unittest.mock.Mock(side_effect=ValueError("Daemon generation or mount namespace changed"))
                            if mismatch else unittest.mock.Mock(return_value=current),
                          "descendants": unittest.mock.Mock(side_effect=ValueError("Child capture failed"))
                            if capture_error else (unittest.mock.Mock(side_effect=[[child], FileNotFoundError("task exited")])
                            if disappearing_tasks else unittest.mock.Mock(return_value=[child])),
                          "process_start": lambda pid: {123: 40, 456: 50}[pid],
                          "write_record": unittest.mock.Mock()})
        with patch.object(Path, "exists", exists), \
             patch.object(functions["os"].path, "lexists", return_value=False), \
             patch.object(functions["os"], "pidfd_open", return_value=99) as opened, \
             patch.object(functions["os"], "close") as closed, \
             patch.object(functions["signal"], "pidfd_send_signal", side_effect=signal_owned) as sent, \
             patch.object(functions["time"], "monotonic", side_effect=[0, 60] if persistent_child else None,
                          return_value=0), \
             patch.object(functions["time"], "sleep", side_effect=sleep):
            error = None
            try:
                functions["stop_daemon"](Path("/actions/_work/private"), Path("/actions/_work/pid"), {})
            except ValueError as failure:
                error = failure
            if disappearing_tasks:
                self.assertEqual(functions["descendants"].call_count, 1)
            return error, opened, sent, closed, functions["write_record"]

    def test_generation_or_namespace_failure_prevents_any_signal(self):
        error, opened, sent, closed, record = self.stop_case(mismatch=True)
        self.assertRegex(str(error), "generation or mount namespace")
        opened.assert_not_called()
        sent.assert_not_called()
        closed.assert_not_called()
        record.assert_not_called()

    def test_proved_pidfd_stop_waits_for_observed_descendant(self):
        error, opened, sent, closed, record = self.stop_case()
        self.assertIsNone(error)
        opened.assert_called_once_with(123, 0)
        sent.assert_called_once()
        closed.assert_called_once_with(99)
        value = record.call_args.args[1]
        self.assertEqual(value["status"], "OBSERVED_OWNED_PROCESSES_CLOSED")
        self.assertEqual(value["descendants"][0]["pid"], 456)

    def test_descendant_timeout_retains_data_and_never_records_closure(self):
        error, opened, sent, closed, record = self.stop_case(persistent_child=True)
        self.assertRegex(str(error), "descendants have not stopped")
        sent.assert_called_once()
        closed.assert_called_once_with(99)
        record.assert_not_called()

    def test_child_capture_failure_still_stops_proved_daemon_without_closure(self):
        error, opened, sent, closed, record = self.stop_case(capture_error=True)
        self.assertRegex(str(error), "Child capture failed")
        sent.assert_called_once()
        closed.assert_called_once_with(99)
        record.assert_not_called()

    def test_cleanup_refuses_live_captured_descendant_even_without_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fixture[-2]["descendants"] = [{"pid": 456}]
            error, command, remove, sleep, diagnostic = self.cleanup_case(
                fixture, [], times=(100,), alive_children=(456,))
            self.assertRegex(str(error), "Observed owned process still exists")
            command.assert_not_called()
            remove.assert_not_called()

    def test_fixed_tool_absence_prevents_startup_capture(self):
        functions = self.program()
        for missing in ("/usr/bin/docker", "/usr/bin/umount"):
            def lstat(path):
                if str(path) == missing:
                    raise FileNotFoundError(missing)
                return SimpleNamespace(st_mode=functions["stat"].S_IFREG | 0o755, st_uid=0)
            with self.subTest(missing=missing), patch.object(Path, "lstat", lstat), \
                 patch.object(functions["os"], "access", return_value=True), \
                 patch.dict(functions, {"mounts": lambda: [], "write_record": unittest.mock.Mock()}):
                with self.assertRaises(FileNotFoundError):
                    functions["capture"]("before", Path("/actions/_work/root"), Path("/actions/_work/pid"), {})
                functions["write_record"].assert_not_called()

    def test_descendant_walk_stays_on_proved_tree_and_rejects_excess_depth(self):
        functions = self.program()
        identities = {pid: {"pid": pid, "start_ticks": pid, "namespace": [1, 2]}
                      for pid in range(123, 134)}
        seen = []
        def children(value):
            seen.append(value["pid"])
            return [value["pid"] + 1] if value["pid"] < 133 else []
        with patch.dict(functions, {"child_ids": children, "process_identity": lambda pid: identities[pid]}), \
             patch.object(functions["time"], "monotonic", return_value=0):
            with self.assertRaisesRegex(ValueError, "traversal bound"):
                functions["descendants"](identities[123])
        self.assertEqual(seen, list(range(123, 132)))

    def test_same_pid_comm_and_root_cannot_mask_changed_generation_or_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            pidfile = Path(directory) / "pid"
            pidfile.write_text("123\n")
            expected = {"pid": 123, "start_ticks": 40, "namespace": [1, 2]}
            original_text, original_bytes = Path.read_text, Path.read_bytes
            def text(path, *args, **kwargs):
                return "dockerd\n" if str(path) == "/proc/123/comm" else original_text(path, *args, **kwargs)
            def data(path):
                return ("dockerd\0--data-root=" + str(root) + "\0").encode() if str(path) == "/proc/123/cmdline" else original_bytes(path)
            for actual in (dict(expected, start_ticks=41), dict(expected, namespace=[3, 4])):
                functions = self.program()
                functions.update({"load_proof": lambda *args: ({}, {"daemon": expected}, "ready"),
                                  "process_identity": lambda pid: actual,
                                  "namespace": lambda: actual["namespace"]})
                with self.subTest(actual=actual), patch.object(Path, "read_text", text), \
                     patch.object(Path, "read_bytes", data), \
                     patch.object(functions["os"], "pidfd_open") as opened, \
                     patch.object(functions["signal"], "pidfd_send_signal") as sent:
                    with self.assertRaisesRegex(ValueError, "generation or mount namespace"):
                        functions["stop_daemon"](root, pidfile, {})
                    opened.assert_not_called()
                    sent.assert_not_called()

    def test_shutdown_waits_on_preterm_snapshot_without_reenumerating_exited_tasks(self):
        error, opened, sent, closed, record = self.stop_case(disappearing_tasks=True)
        self.assertIsNone(error)
        sent.assert_called_once()
        closed.assert_called_once_with(99)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1]["status"], "OBSERVED_OWNED_PROCESSES_CLOSED")

    def test_exit_proof_allows_removed_pidfile_but_not_dangling_or_changed_file(self):
        for condition in ("removed", "dangling", "changed"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(directory)
                functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
                if condition == "changed":
                    pidfile.write_text("456\n")
                else:
                    pidfile.unlink()
                    if condition == "dangling":
                        pidfile.symlink_to(Path(directory) / "missing")
                rows = [[parent, mounted], [parent, mounted], [parent]] if condition == "removed" else []
                error, command, remove, sleep, diagnostic = self.cleanup_case(
                    fixture, rows, times=(100, 100) if condition == "removed" else (100,))
                if condition == "removed":
                    self.assertIsNone(error)
                    command.assert_called_once()
                    remove.assert_called_once_with(root)
                    self.assertFalse(os.path.lexists(pidfile))
                else:
                    self.assertIsNotNone(error)
                    command.assert_not_called()
                    remove.assert_not_called()
                    self.assertTrue(os.path.lexists(pidfile))

    def test_cgroup_parent_root_metadata_is_preserved_without_path_normalization(self):
        functions = self.program()
        for filesystem in ("cgroup", "cgroup2"):
            for root in ("/..", "/../../ancestor"):
                with self.subTest(filesystem=filesystem, root=root):
                    text = f"20 10 0:32 {root} /sys/fs/cgroup rw - {filesystem} cgroup rw\n"
                    row, = functions["parse_mounts"](text)
                    self.assertEqual(row["root"], root)
                    self.assertEqual(row["point"], "/sys/fs/cgroup")
                    self.assertEqual(row["filesystem"], filesystem)

    def test_namespace_mountpoints_remain_canonical_even_for_cgroup_metadata(self):
        functions = self.program()
        for point in ("relative-private", "/tmp/../private", "/sys/./fs/cgroup", "//sys/fs/cgroup"):
            with self.subTest(point=point):
                with self.assertRaisesRegex(ValueError, "^Invalid namespace mountpoint$") as caught:
                    functions["parse_mounts"](f"20 10 0:32 /.. {point} rw - cgroup2 cgroup rw\n")
                self.assertNotIn(point, str(caught.exception))

    def test_only_absolute_cgroup_metadata_gets_parent_component_exception(self):
        functions = self.program()
        for filesystem in ("ext4", "xfs", "overlay", "tmpfs", "unknown"):
            with self.subTest(filesystem=filesystem), self.assertRaisesRegex(
                    ValueError, "^Parent components in non-cgroup mount root$"):
                functions["parse_mounts"](f"20 10 0:32 /../private /tmp/point rw - {filesystem} fixture rw\n")
        for filesystem in ("cgroup", "cgroup2", "ext4"):
            with self.subTest(filesystem=filesystem), self.assertRaisesRegex(
                    ValueError, "^Invalid filesystem mount root$"):
                functions["parse_mounts"](f"20 10 0:32 ../private /tmp/point rw - {filesystem} fixture rw\n")

    def test_cgroup_root_metadata_cannot_be_selected_as_backing_mount(self):
        functions = self.program()
        root = Path("/actions/_work/source-image-docker-123-1")
        rows = functions["parse_mounts"](
            "10 1 8:1 /actions /actions/_work rw - ext4 fixture rw\n"
            "20 1 0:32 /.. /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
        self.assertEqual(functions["covering"](rows, root), rows[0])
        self.assertEqual(functions["below"](rows, root), [])
        for value in (dict(rows[1], point="/actions/_work"),
                      dict(rows[0], root="/actions/./nested"), dict(rows[0], root="//actions")):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "^Selected mount root is not canonical$"):
                functions["covering"]([value], root)

    def test_noncanonical_backing_or_owned_root_cannot_enter_self_bind_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            functions, root, pidfile, parent, mounted, before, ready, inventory = self.fixture(directory)
            for old, current in ((dict(before, covering=dict(parent, root="/..")), mounted),
                                 (before, dict(mounted, root="/actions/../private")),
                                 (before, dict(mounted, root="//actions/private"))):
                with self.subTest(old=old, current=current), self.assertRaisesRegex(
                        ValueError, "^Selected mount root is not canonical$"):
                    functions["self_bind"](old, current, root)

    def test_loaded_proof_and_unmount_refuse_noncanonical_selected_root(self):
        functions = self.program()
        root = Path("/actions/_work/source-image-docker-123-1")
        before = {"covering": {"root": "/.."}}
        ready = {"root_mount": {"root": "/actions/private"}}
        with patch.dict(functions, {"read_record": unittest.mock.Mock(side_effect=[(before, "b"), (ready, "r")])}):
            with self.assertRaisesRegex(ValueError, "^Selected mount root is not canonical$"):
                functions["load_proof"](root, {})
        for old, current in ((before, ready),
                             ({"covering": {"root": "/actions"}}, {"root_mount": {"root": "/.."}})):
            with self.subTest(old=old, current=current), patch.object(functions["os"], "open") as opened:
                with self.assertRaisesRegex(ValueError, "^Selected mount root is not canonical$"):
                    functions["normal_unmount"](root, old, current)
                opened.assert_not_called()

    def test_parent_relative_cgroup_child_still_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            functions, root, pidfile, parent, mounted, before, ready, inventory = fixture
            child, = functions["parse_mounts"](
                f"12 11 0:32 /.. {root}/cgroup rw - cgroup2 cgroup rw\n")
            error, command, remove, sleep, diagnostic = self.cleanup_case(
                fixture, [[parent, mounted, child]], times=(100, 100, 130))
            self.assertRegex(str(error), "Private Docker still has a mounted child")
            command.assert_not_called()
            remove.assert_not_called()
            record = json.loads(diagnostic)
            self.assertEqual(record["mount_count"], 2)
            self.assertEqual(record["reported_mounts"][1]["filesystem"], "cgroup2")
            self.assertNotIn(str(root), diagnostic)
            self.assertNotIn("/..", diagnostic)











class RemoteOnlyWorkerTest(unittest.TestCase):
    pool = "sonic-slave-container-k3s"
    runner = "ks-e6fb1976-715102e6996d-0-" + "a" * 32
    canary = "Basic Zml4dHVyZTpub3QtYS1jcmVkZW50aWFs"

    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            workspace = root / "source"; workspace.mkdir()
            helper = root / "installed/cache_credentials.py"; helper.parent.mkdir()
            helper.write_bytes(b"from _managed_owner_fixture import HOST_CREDENTIALS, settings, probe_access, imported\nimported()\n".ljust(7646, b" "))
            helper.chmod(0o755)
            scratch = root / "scratch"; scratch.mkdir()
            stack.enter_context(patch.dict(worker.K3S_MANAGED_REMOTE_CACHE_POLICY,
                {"credential_helper_path": str(helper), "credential_helper_sha256": worker.digest(helper)}))
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
            def owned(info):
                return SimpleNamespace(**{**{name: getattr(info, name) for name in fields}, "st_uid": 0})
            original_lstat = Path.lstat
            original_fstat = os.fstat
            def root_owned(path, *args, **kwargs):
                info = original_lstat(path, *args, **kwargs)
                return owned(info) if path == helper.parent or path.is_relative_to(helper.parent) else info
            stack.enter_context(patch.object(Path, "lstat", root_owned))
            stack.enter_context(patch.object(os, "fstat", side_effect=lambda fd: owned(original_fstat(fd))))
            owner = SimpleNamespace(HOST_CREDENTIALS=SimpleNamespace(is_file=lambda: True),
                settings=lambda: {"endpoint": "https://managed-cache.invalid/", "host": "managed-cache.invalid"},
                probe_access=unittest.mock.Mock(), imported=unittest.mock.Mock())
            stack.enter_context(patch.dict(sys.modules, {"_managed_owner_fixture": owner}))
            stack.enter_context(patch.object(os, "getuid", return_value=1000))
            stack.enter_context(patch.object(os, "geteuid", return_value=1000))
            stack.enter_context(patch.dict(os.environ, {"BAZEL_REMOTE_AUTH": self.canary}, clear=True))
            yield root, workspace, helper, scratch

    @staticmethod
    def project_owner(helper):
        version = helper.parent / "..2026_11_13_00_00_00.1"
        version.mkdir()
        target = version / helper.name
        helper.replace(target)
        (helper.parent / "..data").symlink_to(version.name)
        helper.symlink_to("..data/" + helper.name)
        return target

    def test_projected_owner_uses_canonical_verified_target_and_stable_diagnostic_identity(self):
        with self.fixture() as (root, workspace, helper, scratch):
            target = self.project_owner(helper)
            configuration = worker.remote_cache_configuration(workspace)
            self.assertEqual(configuration["helper"], str(target))
            self.assertEqual(worker.load_managed_cache_owner(target).__file__, str(helper))
            self.assertIn("--credential_helper=" + configuration["host"] + "=" + str(target),
                          worker.remote_cache_options(configuration))
            owner = self.fake_owner(configuration)
            code, receipt, imported = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual(code, 0)
            worker.validate_managed_cache_probe(receipt, configuration)
            imported.assert_called_once_with()
            owner.probe_access.assert_called_once_with()

    def test_projected_owner_cannot_escape_fixed_runtime_root(self):
        with self.fixture() as (root, workspace, helper, scratch):
            outside = root / "outside/cache_credentials.py"
            outside.parent.mkdir()
            helper.replace(outside)
            helper.symlink_to(outside)
            with patch.object(worker, "managed_cache_source") as read:
                with self.assertRaisesRegex(ValueError, "helper differs"):
                    worker.managed_cache_helper()
                read.assert_not_called()

    def test_projected_owner_rotation_during_read_stops_before_import(self):
        with self.fixture() as (root, workspace, helper, scratch):
            target = self.project_owner(helper)
            next_version = helper.parent / "..2026_11_13_00_00_00.2"
            next_version.mkdir()
            replacement = next_version / helper.name
            replacement.write_bytes(target.read_bytes())
            replacement.chmod(0o755)
            read = worker.managed_cache_source
            def rotate(path, expected):
                source = read(path, expected)
                data = helper.parent / "..data"
                data.unlink()
                data.symlink_to(next_version.name)
                return source
            with patch.object(worker, "managed_cache_source", side_effect=rotate):
                with self.assertRaisesRegex(ValueError, "selection changed"):
                    worker.remote_cache_configuration(workspace)
            sys.modules["_managed_owner_fixture"].imported.assert_not_called()

    def test_projected_owner_directories_require_root_and_no_writes(self):
        with self.fixture() as (root, workspace, helper, scratch):
            target = self.project_owner(helper)
            lstat = Path.lstat
            for directory in (helper.parent, target.parent):
                for field, value in (("st_uid", 1000), ("st_mode", stat.S_IFDIR | 0o775)):
                    def changed(path, *args, **kwargs):
                        info = lstat(path, *args, **kwargs)
                        return SimpleNamespace(**{**vars(info), field: value}) if path == directory else info
                    with self.subTest(directory=directory.name, field=field):
                        with patch.object(Path, "lstat", changed), patch.object(worker, "managed_cache_source") as read:
                            with self.assertRaisesRegex(ValueError, "helper differs"):
                                worker.managed_cache_helper()
                            read.assert_not_called()

    def test_readonly_projection_protects_directory_write_bits_but_not_untrusted_files(self):
        with self.fixture() as (root, workspace, helper, scratch):
            target = self.project_owner(helper)
            lstat = Path.lstat
            def writable_directories(path, *args, **kwargs):
                info = lstat(path, *args, **kwargs)
                if path in (helper.parent, target.parent):
                    return SimpleNamespace(**{**vars(info), "st_mode": stat.S_IFDIR | 0o2777})
                return info
            with patch.object(Path, "lstat", writable_directories):
                with patch.object(os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    self.assertEqual(worker.managed_cache_helper(), target)
                    target.chmod(0o775)
                    with self.assertRaisesRegex(ValueError, "helper differs"):
                        worker.managed_cache_helper()
                    target.chmod(0o755)
                with patch.object(os, "statvfs", return_value=SimpleNamespace(f_flag=0)):
                    with self.assertRaisesRegex(ValueError, "helper differs"):
                        worker.managed_cache_helper()

    def test_held_owner_read_rejects_untrusted_or_changing_metadata(self):
        with self.fixture() as (root, workspace, helper, scratch):
            fstat = os.fstat
            for field, value, only_after in (("st_uid", 1000, False),
                    ("st_mode", stat.S_IFREG | 0o775, False), ("st_size", 7647, False),
                    ("st_ctime_ns", 0, True)):
                calls = []
                def changed(fd):
                    info = fstat(fd)
                    calls.append(fd)
                    if not only_after or len(calls) > 1:
                        return SimpleNamespace(**{**vars(info), field: value})
                    return info
                with self.subTest(field=field), patch.object(os, "fstat", side_effect=changed):
                    with self.assertRaisesRegex(ValueError, "helper differs"):
                        worker.managed_cache_source(helper, worker.K3S_MANAGED_REMOTE_CACHE_POLICY["credential_helper_sha256"])

    def test_probe_rejects_same_size_changed_source_before_owner_import(self):
        with self.fixture() as (root, workspace, helper, scratch):
            configuration = worker.remote_cache_configuration(workspace)
            owner = self.fake_owner(configuration)
            helper.write_bytes(b"raise AssertionError('unverified owner executed')\n".ljust(7646, b" "))
            with patch.object(worker, "remote_cache_configuration", return_value=configuration):
                code, receipt, imported = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual(code, 1)
            self.assertEqual(receipt["stage"], "owner_identity")
            self.assertEqual(receipt["error_type"], "ValueError")
            imported.assert_not_called()
            owner.probe_access.assert_not_called()

    def test_projected_owner_rotation_invalidates_prepared_helper_configuration(self):
        with self.fixture() as (root, workspace, helper, scratch):
            target = self.project_owner(helper)
            configuration = worker.remote_cache_configuration(workspace)
            next_version = helper.parent / "..2026_11_13_00_00_00.2"
            next_version.mkdir()
            replacement = next_version / helper.name
            replacement.write_bytes(target.read_bytes())
            replacement.chmod(0o755)
            data = helper.parent / "..data"
            data.unlink()
            data.symlink_to(next_version.name)
            with self.assertRaisesRegex(ValueError, "Unexpected managed remote-cache configuration"):
                worker.remote_cache_options(configuration)
            self.assertEqual(configuration["helper"], str(target))

    def test_run_attempt_work_is_fresh_and_never_adopts_previous_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "source-images" / self.runner; parent.mkdir(parents=True)
            old = parent / "disk/keep"; old.parent.mkdir(); old.write_text("previous registration output")
            with patch.object(worker, "worker_cache_root", return_value=root), patch.dict(os.environ,
                    {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}, clear=True):
                first = worker.prepare_work_directory(self.pool, self.runner, "gearbox")
                self.assertEqual(first, parent / "run-123-1")
                self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
                self.assertEqual({p.name for p in first.iterdir()}, {"repository", "bazelisk", "gearbox"})
                with self.assertRaises(FileExistsError):
                    worker.prepare_work_directory(self.pool, self.runner, "gearbox")
                with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
                    second = worker.prepare_work_directory(self.pool, self.runner, "gearbox")
                self.assertNotEqual(first, second)
                for run in ("", "0", "../123"):
                    with patch.dict(os.environ, {"GITHUB_RUN_ID": run}), self.assertRaises(ValueError):
                        worker.worker_work_directory(self.pool, self.runner)
            self.assertEqual(old.read_text(), "previous registration output")

    def test_dpool_keeps_its_cache_layout_without_remote_auth(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            with patch.object(worker, "worker_cache_root", return_value=root):
                cache = worker.prepare_work_directory("sonic-slave-container-dpool", "sonic-slave-lgh114-1", "p4rt")
                self.assertEqual(cache, root / "source-images/sonic-slave-lgh114-1")
                self.assertEqual({p.name for p in cache.iterdir()}, {"repository", "disk", "bazelisk", "p4rt"})
                self.assertEqual(worker.prepare_work_directory("sonic-slave-container-dpool", "sonic-slave-lgh114-1", "p4rt"), cache)
            self.assertEqual(worker.owner_build_profile("sonic-slave-container-dpool"), worker.P4RT_OWNER_PROFILE)

    def test_missing_managed_owner_stops_before_probe_or_phase_execution(self):
        with patch.dict(os.environ, {"WORKER_POOL": self.pool}, clear=True), ExitStack() as stack:
            stack.enter_context(patch.dict(worker.K3S_MANAGED_REMOTE_CACHE_POLICY,
                {"credential_helper_path": "/absent/native/cache_credentials.py"}))
            invoke = stack.enter_context(patch.object(worker, "run"))
            with self.assertRaisesRegex(ValueError, "legacy fallback is disabled"):
                worker.prepare_remote_cache(Path("/absent/source"), Path("/absent/state"))
            invoke.assert_not_called()
            measure = stack.enter_context(patch.object(worker, "measure"))
            with self.assertRaisesRegex(ValueError, "legacy fallback is disabled"):
                worker.prepare(SimpleNamespace(workspace=Path("/absent/source"),
                    artifacts=Path("/absent/state"), image="gearbox"))
            measure.assert_not_called()
            stack.enter_context(patch.object(worker, "state_for", return_value={"remote_cache": {}}))
            stack.enter_context(patch.object(worker, "source_check"))
            memory = stack.enter_context(patch.object(worker, "memory_snapshot"))
            phase = stack.enter_context(patch.object(worker.subprocess, "run"))
            args = SimpleNamespace(workspace=Path("/absent/source"), revision="a" * 40, image="gearbox")
            with self.assertRaisesRegex(ValueError, "legacy fallback is disabled"):
                worker.execute_phase(args)
            memory.assert_not_called(); phase.assert_not_called()

    def test_probe_uses_only_installed_owner_and_does_not_serialize_auth(self):
        with self.fixture() as (_, workspace, helper, scratch):
            def complete(argv, **kwargs):
                self.assertEqual(argv[:5], [sys.executable, "-I", "-S", "-B", "-c"])
                self.assertEqual(argv[5], worker.managed_cache_owner_program())
                configuration = json.loads(argv[6])
                self.assertEqual(configuration["helper"], str(helper))
                self.assertEqual(kwargs, {"timeout": 45, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})
                self.assertNotIn(self.canary, repr(argv))
                Path(argv[-1]).write_text(json.dumps(self.access_receipt(configuration)))
            with patch.object(worker, "run", side_effect=complete) as invoke:
                configuration = worker.prepare_remote_cache(workspace, scratch)
            invoke.assert_called_once()
            worker.safe_json({"schema": 3, "cache_mode": "remote_only_run_attempt", "remote_cache": {
                **worker.K3S_MANAGED_REMOTE_CACHE_POLICY, "endpoint": configuration["endpoint"],
                "host": configuration["host"], "credential_helper_path": "/opt/sonic-native-runtime/cache_credentials.py"}})
            profile = worker.select_build_profile(20 * 1024**3, 6)
            rendered = [json.dumps(configuration), (scratch / "remote-cache-access.json").read_text(),
                worker.launcher_text("/tools/bazel", scratch / "root-output", profile, configuration),
                worker.owner_launcher_text("/tools/bazel", scratch / "owner-output", scratch / "owner.rc", scratch, configuration),
                json.dumps(worker.K3S_MANAGED_REMOTE_CACHE_POLICY)]
            for value in rendered:
                self.assertNotIn(self.canary, value)
                self.assertNotIn("BAZEL_REMOTE_AUTH", value)
                self.assertNotIn("--action_env", value)
                self.assertNotIn("authenticated", value)
            with patch.object(worker, "run") as invoke, self.assertRaisesRegex(ValueError, "fresh"):
                worker.prepare_remote_cache(workspace, scratch)
            invoke.assert_not_called()

    def test_probe_failure_and_unexpected_receipt_never_fall_back(self):
        with self.fixture() as (_, workspace, _, scratch):
            with patch.object(worker, "run", side_effect=subprocess.CalledProcessError(1, ["probe"])):
                with self.assertRaisesRegex(ValueError, "no local-cache fallback") as error:
                    worker.prepare_remote_cache(workspace, scratch)
                self.assertNotIn(self.canary, str(error.exception))
            valid = self.access_receipt(worker.remote_cache_configuration(workspace))
            for changes in ({"access_probe": "failed"}, {"access_probe": True}, {"stage": "access"},
                            {"endpoint": "https://different.invalid"}, {"authenticated": True}):
                receipt = scratch / "remote-cache-access.json"; receipt.unlink(missing_ok=True)
                def complete(argv, **kwargs):
                    Path(argv[-1]).write_text(json.dumps({**valid, **changes}))
                with self.subTest(changes=changes), patch.object(worker, "run", side_effect=complete):
                    with self.assertRaisesRegex(ValueError, "did not pass"):
                        worker.prepare_remote_cache(workspace, scratch)

    def test_changed_or_linked_credential_helper_is_rejected_before_execution(self):
        with self.fixture() as (root, workspace, helper, scratch):
            original = helper.read_bytes(); helper.write_bytes(original + b"# changed\n")
            with patch.object(worker, "run") as invoke, self.assertRaisesRegex(ValueError, "helper differs"):
                worker.prepare_remote_cache(workspace, scratch)
            invoke.assert_not_called()
            helper.unlink(); target = root / "other-helper"; target.write_bytes(original); target.chmod(0o755)
            helper.symlink_to(target)
            with patch.object(worker, "run") as invoke, self.assertRaisesRegex(ValueError, "helper differs"):
                worker.prepare_remote_cache(workspace, scratch)
            invoke.assert_not_called()

    def test_both_launchers_override_local_or_unscoped_caller_cache_flags(self):
        with self.fixture() as (root, workspace, _, scratch):
            configuration = worker.remote_cache_configuration(workspace)
            binary = root / "bazel"
            binary.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            binary.chmod(0o755)
            root_text = worker.launcher_text(str(binary), scratch / "root-output",
                worker.select_build_profile(20 * 1024**3, 6), configuration)
            owner_text = worker.owner_launcher_text(str(binary), scratch / "owner-output",
                scratch / "owner.rc", scratch, configuration)
            expected = worker.remote_cache_options(configuration)
            for name, text in (("root", root_text), ("owner", owner_text)):
                launch = root / (name + "-launcher"); launch.write_text(text)
                for command in ("build", "test", "aquery", "cquery"):
                    with self.subTest(launcher=name, command=command):
                        argv = json.loads(subprocess.check_output([sys.executable, str(launch), command,
                            "--remote_cache=", "--disk_cache=/old/local", "--repository_cache=/old/repository",
                            "--remote_executor=grpc://different.invalid", "//:literal"], text=True))
                        for prefix in ("--remote_cache=", "--credential_helper=", "--disk_cache=",
                                       "--repository_cache=", "--remote_executor="):
                            self.assertEqual([arg for arg in argv if arg.startswith(prefix)][-1],
                                             next(arg for arg in expected if arg.startswith(prefix)))
                        self.assertNotIn(self.canary, json.dumps(argv))
                shutdown = json.loads(subprocess.check_output([sys.executable, str(launch), "shutdown"], text=True))
                self.assertFalse(any(arg.startswith("--remote_cache=") for arg in shutdown))

    def test_p4rt_context_remains_bound_to_this_run_attempt_and_owner_config(self):
        with self.fixture() as (root, workspace, _, scratch), ExitStack() as stack:
            (workspace / ".bazelversion").write_text("8.5.1\n")
            owner = workspace / worker.P4RT_OWNER_PATH; owner.mkdir(parents=True)
            cache_root = root / "cache"; parent = cache_root / "source-images" / self.runner; parent.mkdir(parents=True)
            stack.enter_context(patch.dict(os.environ, {"WORKER_POOL": self.pool, "RUNNER_NAME": self.runner,
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}))
            stack.enter_context(patch.object(worker, "worker_cache_root", return_value=cache_root))
            stack.enter_context(patch.object(worker, "temporary_directory", return_value=scratch))
            stack.enter_context(patch.object(worker, "p4rt_owner_identity", return_value=owner))
            def output(argv):
                if "ls-tree" in argv: return "160000 commit " + worker.P4RT_OWNER + "\t" + worker.P4RT_OWNER_PATH
                if "config" in argv: return "https://github.com/securely1g/sonic-pins.git"
                raise AssertionError(argv)
            stack.enter_context(patch.object(worker, "output", side_effect=output))
            stack.enter_context(patch.object(worker, "run"))
            stack.enter_context(patch.object(worker.shutil, "which", return_value="/tools/bazel"))
            cache = worker.prepare_work_directory(self.pool, self.runner, "p4rt")
            configuration = worker.remote_cache_configuration(workspace)
            context = worker.prepare_p4rt(workspace, cache, scratch, [cache / "distdir"], scratch / "mirrors", configuration)
            self.assertFalse((cache / "p4rt-owner/disk").exists())
            self.assertIn("build --disk_cache=\n", Path(context["rc"]).read_text())
            self.assertIn("common --repository_cache=\n", Path(context["rc"]).read_text())
            self.assertIn("common --distdir=" + str(cache / "distdir"), Path(context["rc"]).read_text())
            args = SimpleNamespace(workspace=workspace, artifacts=root, revision="a" * 40, phase="prepare-owner")
            self.assertEqual(worker.p4rt_context(args, {"p4rt_owner": context}), context)
            with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), self.assertRaisesRegex(ValueError, "context changed"):
                worker.p4rt_context(args, {"p4rt_owner": context})
            self.assertEqual(worker.owner_build_profile(self.pool)["cache_mode"], "separate_remote_only_run_attempt")
            self.assertEqual(worker.P4RT_OWNER_PROFILE["cache_mode"], "separate_persistent_worker_local")

    def test_changed_prepared_remote_policy_stops_before_phase_execution(self):
        with self.fixture() as (_, workspace, _, _), ExitStack() as stack:
            configuration = worker.remote_cache_configuration(workspace)
            configuration["endpoint"] = "https://different.invalid"
            stack.enter_context(patch.dict(os.environ, {"WORKER_POOL": self.pool}))
            stack.enter_context(patch.object(worker, "state_for", return_value={"remote_cache": configuration}))
            stack.enter_context(patch.object(worker, "source_check"))
            phase = stack.enter_context(patch.object(worker.subprocess, "run"))
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                worker.execute_phase(SimpleNamespace(workspace=workspace, revision="a" * 40, image="gearbox"))
            phase.assert_not_called()

    def test_remote_auth_workflow_scope_keeps_both_actor_and_pool_guards(self):
        expression = ImageWorkerTest().native_job()["env"]["BAZEL_REMOTE_AUTH"]
        for clause in ("github.event_name == 'workflow_dispatch'", "github.repository == 'securely1g/sonic-buildimage'",
                       "github.actor == 'securely1g'", "github.triggering_actor == 'securely1g'",
                       "inputs.source_worker_pool == 'sonic-slave-container-k3s'", "secrets.BAZEL_REMOTE_AUTH || ''"):
            self.assertIn(clause, expression)

    @staticmethod
    def access_receipt(configuration):
        return {"schema": 1, "provider": configuration["provider"], "endpoint": configuration["endpoint"],
                "host": configuration["host"], "helper_sha256": configuration["helper_sha256"],
                "access_probe": "passed", "stage": "complete"}

    def owner_probe(self, root, workspace, scratch, owner):
        configuration = worker.remote_cache_configuration(workspace)
        receipt = scratch / "owned-probe.json"
        previous_umask = os.umask(0o077)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"BAZEL_REMOTE_AUTH": self.canary}, clear=True))
                stack.enter_context(patch.object(sys, "argv", ["probe", json.dumps(configuration), str(receipt)]))
                stack.enter_context(patch.object(os, "getuid", return_value=1000))
                stack.enter_context(patch.object(os, "geteuid", return_value=1000))
                stack.enter_context(patch.dict(sys.modules, {"_managed_owner_fixture": owner}))
                with self.assertRaises(SystemExit) as stopped:
                    exec(worker.managed_cache_owner_program(), {"__name__": "__main__"})
            value = json.loads(receipt.read_text())
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            self.assertNotIn(self.canary, receipt.read_text())
            self.assertNotIn("authenticated", value)
            return stopped.exception.code, value, owner.imported
        finally:
            os.umask(previous_umask)

    def fake_owner(self, configuration):
        return SimpleNamespace(HOST_CREDENTIALS=SimpleNamespace(is_file=unittest.mock.Mock(return_value=True)),
            settings=unittest.mock.Mock(return_value={"endpoint": configuration["endpoint"], "host": configuration["host"]}),
            probe_access=unittest.mock.Mock(), imported=unittest.mock.Mock())

    def test_managed_settings_and_access_do_not_require_legacy_auth(self):
        with self.fixture() as (root, workspace, _, scratch):
            with patch.dict(os.environ, {}, clear=True):
                configuration = worker.remote_cache_configuration(workspace)
            owner = self.fake_owner(configuration)
            code, value, imported = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual(code, 0)
            worker.validate_managed_cache_probe(value, configuration)
            imported.assert_called_once_with()
            self.assertEqual(owner.settings.call_count, 2)
            owner.probe_access.assert_called_once_with()

    def test_owner_adapter_refuses_legacy_fallback_before_settings_or_http(self):
        with self.fixture() as (root, workspace, _, scratch):
            owner = self.fake_owner(worker.remote_cache_configuration(workspace))
            owner.HOST_CREDENTIALS.is_file.return_value = False
            code, value, _ = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual((code, value["stage"], value["error_type"]), (1, "host_configuration", "ValueError"))
            owner.settings.assert_not_called(); owner.probe_access.assert_not_called()

    def test_owner_adapter_refuses_different_settings_before_http_without_disclosure(self):
        with self.fixture() as (root, workspace, _, scratch):
            owner = self.fake_owner(worker.remote_cache_configuration(workspace))
            owner.settings.return_value = {"endpoint": "https://different.invalid/", "host": "different.invalid"}
            code, value, _ = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual((code, value["stage"]), (1, "settings"))
            self.assertNotIn("different.invalid", json.dumps(value))
            owner.probe_access.assert_not_called()

    def test_owner_adapter_retains_safe_stage_only_on_access_failure(self):
        with self.fixture() as (root, workspace, _, scratch):
            owner = self.fake_owner(worker.remote_cache_configuration(workspace))
            owner.probe_access.side_effect = ValueError(self.canary)
            code, value, _ = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual((code, value["stage"], value["error_type"]), (1, "access", "ValueError"))
            self.assertEqual(value["access_probe"], "failed")

    def test_owner_adapter_refuses_settings_changed_after_probe(self):
        with self.fixture() as (root, workspace, _, scratch):
            owner = self.fake_owner(worker.remote_cache_configuration(workspace))
            owner.settings.side_effect = [owner.settings.return_value, {"endpoint": "https://changed.invalid/"}]
            code, value, _ = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual((code, value["stage"]), (1, "stable_settings"))
            owner.probe_access.assert_called_once_with()
            self.assertNotIn("changed.invalid", json.dumps(value))

    def test_owner_adapter_rechecks_source_identity_before_import(self):
        with self.fixture() as (root, workspace, helper, scratch):
            owner = self.fake_owner(worker.remote_cache_configuration(workspace))
            configuration = worker.remote_cache_configuration(workspace)
            helper.chmod(0o777)
            with patch.object(worker, "remote_cache_configuration", return_value=configuration):
                code, value, imported = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual((code, value["stage"]), (1, "owner_identity"))
            imported.assert_not_called(); owner.settings.assert_not_called(); owner.probe_access.assert_not_called()


    def test_operator_public_settings_change_without_source_or_http_change(self):
        with self.fixture() as (_root, workspace, _helper, _scratch):
            owner = sys.modules["_managed_owner_fixture"]
            first = worker.remote_cache_configuration(workspace)
            owner.settings = unittest.mock.Mock(return_value={
                "endpoint": "https://next-managed-cache.invalid/", "host": "next-managed-cache.invalid"})
            second = worker.remote_cache_configuration(workspace)
            self.assertEqual(second["endpoint"], "https://next-managed-cache.invalid/")
            self.assertNotEqual(first["endpoint"], second["endpoint"])
            self.assertEqual(first["helper_sha256"], second["helper_sha256"])
            self.assertNotIn("endpoint", worker.K3S_MANAGED_REMOTE_CACHE_POLICY)
            self.assertNotIn("host", worker.K3S_MANAGED_REMOTE_CACHE_POLICY)
            owner.probe_access.assert_not_called()

    def test_host_mode_disappearance_cannot_pass_even_with_same_public_origin(self):
        with self.fixture() as (root, workspace, _helper, scratch):
            configuration = worker.remote_cache_configuration(workspace)
            owner = self.fake_owner(configuration)
            owner.HOST_CREDENTIALS.is_file.side_effect = [True, False]
            code, receipt, _ = self.owner_probe(root, workspace, scratch, owner)
            self.assertEqual(code, 1)
            self.assertEqual(receipt["access_probe"], "failed")
            self.assertEqual(receipt["stage"], "stable_settings")
            self.assertEqual(receipt["error_type"], "ValueError")
            owner.probe_access.assert_called_once_with()
            owner.settings.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
