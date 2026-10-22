#!/usr/bin/env python3
"""Regression tests for the native producer's capture and snapshot boundaries."""

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = load_module('snapshot')


class NativeMakeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.makefile = self.root / 'fixture.mk'
        native = Path(__file__).with_name('native.mk').resolve()
        self.makefile.write_text(
            '.DEFAULT_GOAL := ordinary\n'
            'CONFIGURED_PLATFORM := vs\n'
            'CONFIGURED_ARCH := amd64\n'
            'BLDENV := trixie\n'
            'BAZEL_MIN_READINESS := bazel_disabled\n'
            'SECURE_UPGRADE_MODE := no_sign\n'
            'TARGET_PATH := target\n'
            'PYTHON_WHEELS_PATH := target/python-wheels/trixie\n'
            'DOCKER_CONFIG_ENGINE_TRIXIE := docker-config-engine-trixie.gz\n'
            'SCAPY := scapy-2.5.0-py2.py3-none-any.whl\n'
            'sonic-vs.bin_DOCKERS := docker-orchagent.gz docker-other.gz\n'
            'SONIC_PACKAGES_LOCAL := docker-local.gz\n'
            'docker-orchagent.gz_LOAD_DOCKERS := docker-config-engine-trixie.gz\n'
            'docker-other.gz_AFTER := docker-dependency.gz\n'
            'SONIC_IMAGE_VERSION := 20261001.0\n'
            'ordinary:\n'
            '\t@echo ORDINARY prepare=$(BAZEL_NATIVE_PREPARE) excluded=$(BAZEL_NATIVE_EXCLUDED_IMAGES)\n'
            f'include {native}\n'
            'target/sonic-vs.bin:\n'
            '\t@echo IMAGE_PREREQUISITES $^\n'
            'target/docker-config-engine-trixie.gz:\n'
            '\t@echo BUILD_CONFIG_ENGINE\n'
            'target/python-wheels/trixie/scapy-2.5.0-py2.py3-none-any.whl:\n'
            '\t@echo BUILD_SCAPY\n',
            encoding='utf-8',
        )

    def dry_run(self, *overrides, goals=('bazel-vs-native-inputs',), database=False):
        command = [
            'make', '--no-print-directory', '--no-builtin-rules',
            '--no-builtin-variables', '--dry-run', '--file', str(self.makefile),
        ]
        if database:
            command.append('--print-data-base')
        command.extend(goals)
        command.extend(overrides)
        return subprocess.run(command, cwd=self.root, text=True, capture_output=True,
                              env={'PATH': os.defpath, 'LC_ALL': 'C'}, timeout=15)

    def test_ordinary_make_goal_is_unchanged_even_with_unsupported_native_configuration(self):
        result = self.dry_run('CONFIGURED_PLATFORM=broadcom', 'ENABLE_ASAN=y',
                              'BAZEL_MIN_READINESS=experimental', goals=())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('echo ORDINARY prepare= excluded=', result.stdout.strip())

    def test_native_goal_rejects_unsupported_platform_architecture_or_distro(self):
        for override in ('CONFIGURED_PLATFORM=broadcom', 'CONFIGURED_ARCH=arm64', 'BLDENV=bookworm'):
            with self.subTest(override=override):
                result = self.dry_run(override)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('requires AMD64 Trixie VS', result.stderr)

    def test_native_goal_rejects_debug_and_experimental_bazel_selection(self):
        for override, message in (
            ('INSTALL_DEBUG_TOOLS=y', 'Unsupported feature'),
            ('ENABLE_ASAN=y', 'Unsupported feature'),
            ('BAZEL_MIN_READINESS=experimental', 'use BAZEL_MIN_READINESS=bazel_disabled'),
        ):
            with self.subTest(override=override):
                result = self.dry_run(override)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(message, result.stderr)

    def test_native_goal_cannot_be_combined_with_an_ordinary_goal(self):
        result = self.dry_run(goals=('bazel-vs-native-inputs', 'ordinary'))
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Invoke bazel-vs-native-inputs by itself', result.stderr)

    def test_native_goal_requires_bazel_swss_predecessors_before_installer(self):
        result = self.dry_run()
        self.assertEqual(0, result.returncode, result.stderr)
        installer_index = result.stdout.index('echo IMAGE_PREREQUISITES')
        self.assertLess(result.stdout.index('echo BUILD_CONFIG_ENGINE'), installer_index)
        self.assertLess(result.stdout.index('echo BUILD_SCAPY'), installer_index)
        self.assertIn('python3 tools/bazel/image/native/producer.py begin', result.stdout)
        self.assertIn(
            'echo IMAGE_PREREQUISITES target/docker-config-engine-trixie.gz '
            'target/python-wheels/trixie/scapy-2.5.0-py2.py3-none-any.whl', result.stdout,
        )
        self.assertIn('test -s target/bazel-native/provenance.json', result.stdout)
        self.assertFalse((self.root / 'target').exists())

    def test_orchagent_stays_in_service_inventory_with_transitive_native_images(self):
        result = self.dry_run(database=True)
        self.assertEqual(0, result.returncode, result.stderr)
        installed = re.search(
            r'^bazel-native-inventory: BAZEL_INSTALLED_DOCKERS := (.*)$', result.stdout, re.MULTILINE,
        )
        selected = re.search(
            r'^bazel-native-inventory: BAZEL_SELECTED_DOCKERS := (.*)$', result.stdout, re.MULTILINE,
        )
        self.assertIsNotNone(installed, 'Make did not export the installed-image inventory')
        self.assertIsNotNone(selected, 'Make did not export the selected-image closure')
        self.assertEqual({'docker-orchagent.gz', 'docker-other.gz'}, set(installed.group(1).split()))
        self.assertEqual(
            {'docker-orchagent.gz', 'docker-other.gz', 'docker-local.gz',
             'docker-config-engine-trixie.gz', 'docker-dependency.gz'},
            set(selected.group(1).split()),
        )


class EnvironmentCaptureTest(unittest.TestCase):
    def setUp(self):
        self.producer = load_module('producer')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.templates = self.root / 'files/build_templates'
        self.templates.mkdir(parents=True)
        (self.root / 'build_debian.sh').write_text(
            '#!/bin/bash\nprintf "%s\\n" "$installer_images" "${CHANGE_DEFAULT_PASSWORD}"\n',
            encoding='utf-8',
        )
        (self.root / 'slave.mk').write_text(
            'export installer_debs="$(SONIC_INSTALLS)"\n', encoding='utf-8',
        )
        (self.templates / 'sonic_debian_extension.j2').write_text(
            '{% if include_sflow == "y" %}{{ sonic_asic_platform }}{% endif %}\n',
            encoding='utf-8',
        )

    def test_capture_selects_values_from_shell_make_and_jinja_sources(self):
        selected = {
            'installer_images': 'swss|dockers/docker-orchagent||target/docker-orchagent.gz:test',
            'installer_debs': 'target/debs/trixie/example.deb',
            'include_sflow': 'y',
            'sonic_asic_platform': 'vs',
            'CHANGE_DEFAULT_PASSWORD': 'n',
        }
        environment = dict(selected, UNRELATED_CI_VARIABLE='ambient-value')
        before = dict(environment)
        self.assertEqual(dict(selected, SECURE_UPGRADE_MODE='no_sign'),
                         self.producer.capture_environment(self.root, environment))
        self.assertEqual(before, environment)

    def test_capture_keeps_identity_even_when_template_does_not_reference_it(self):
        identity = {
            'CONFIGURED_ARCH': 'amd64',
            'CONFIGURED_PLATFORM': 'vs',
            'TARGET_MACHINE': 'vs',
            'IMAGE_TYPE': 'onie',
            'IMAGE_DISTRO': 'trixie',
            'SONIC_IMAGE_VERSION': '20261001.0',
            'SONIC_BAZEL_SOURCE_COMMIT': '0123456789abcdef0123456789abcdef01234567',
            'SONIC_BAZEL_SOURCE_BRANCH': 'codex/bazel-swss-image',
            'SOURCE_DATE_EPOCH': '1790812800',
        }
        self.assertEqual(dict(identity, SECURE_UPGRADE_MODE='no_sign'),
                         self.producer.capture_environment(self.root, identity))

    def test_new_template_input_is_captured_from_current_checkout(self):
        environment = {'new_service_feature': 'enabled'}
        self.assertNotIn('new_service_feature',
                         self.producer.capture_environment(self.root, environment))
        (self.templates / 'new_service.j2').write_text('{{ new_service_feature }}\n', encoding='utf-8')
        self.assertEqual(dict(environment, SECURE_UPGRADE_MODE='no_sign'),
                         self.producer.capture_environment(self.root, environment))

    def test_referenced_ambient_credentials_proxies_and_signing_material_are_dropped(self):
        forbidden = (
            'HOME', 'PATH', 'PWD', 'SHELL', 'BASH_ENV', 'PYTHONPATH', 'LD_PRELOAD',
            'SSH_AUTH_SOCK', 'PASSWORD', 'DEFAULT_PASSWORD',
            'BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD', 'bmc_root_account_default_password',
            'GITHUB_TOKEN', 'CI_SECRET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
            'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy',
            'no_proxy', 'kube_docker_proxy', 'SIGNING_KEY', 'SIGNING_CERT',
            'SECURE_UPGRADE_DEV_SIGNING_KEY', 'SECURE_UPGRADE_SIGNING_CERT',
            'SECURE_UPGRADE_PROD_SIGNING_TOOL', 'sonic_su_dev_signing_key',
            'sonic_su_signing_cert', 'sonic_su_prod_signing_tool',
        )
        with (self.root / 'build_debian.sh').open('a', encoding='utf-8') as stream:
            stream.write('\n'.join('echo "${' + name + '}"' for name in forbidden))
        environment = {name: 'must-not-be-captured' for name in forbidden}
        environment['CHANGE_DEFAULT_PASSWORD'] = 'y'
        self.assertEqual({'CHANGE_DEFAULT_PASSWORD': 'y', 'SECURE_UPGRADE_MODE': 'no_sign'},
                         self.producer.capture_environment(self.root, environment))

    def test_stage_control_is_not_saved_as_a_resume_input(self):
        names = ('SONIC_BAZEL_BUILD_STAGE', 'SONIC_BAZEL_IMAGE_STAGE',
                 'SONIC_BAZEL_REQUESTED_STAGE')
        with (self.root / 'build_debian.sh').open('a', encoding='utf-8') as stream:
            stream.write('\n'.join('echo "${' + name + '}"' for name in names))
        self.assertEqual({'SECURE_UPGRADE_MODE': 'no_sign'}, self.producer.capture_environment(
            self.root, {name: 'host' for name in names}))

    def test_execution_trust_selectors_never_become_installer_or_service_inputs(self):
        names = ('SONIC_BUILD_SLAVE_CA_BUNDLE', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                 'CURL_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'PIP_CERT',
                 'GIT_SSL_CAINFO', 'GIT_SSL_CAPATH', 'WGETRC')
        (self.templates / 'trust.j2').write_text(
            '\n'.join('{{ ' + name + ' }}' for name in names), encoding='utf-8',
        )
        environment = {name: '/execution-only/ca-bundle.pem' for name in names}
        environment['CHANGE_DEFAULT_PASSWORD'] = 'y'
        self.assertEqual({'SECURE_UPGRADE_MODE': 'no_sign', 'CHANGE_DEFAULT_PASSWORD': 'y'},
                         self.producer.capture_environment(self.root, environment))

    def test_capture_keeps_values_literal(self):
        marker = self.root / 'must-not-exist'
        literal = '$(touch ' + str(marker) + '); `echo executable`; "quoted"\nsecond line'
        result = self.producer.capture_environment(self.root, {'installer_images': literal})
        self.assertEqual(literal, result['installer_images'])
        self.assertFalse(marker.exists())

    def test_selected_values_must_be_strings(self):
        for value in (None, True, 1, ['not', 'a', 'string']):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.producer.capture_environment(self.root, {'installer_images': value})


class SnapshotBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'state.json'
        self.identity = {
            'arch': 'amd64',
            'platform': 'vs',
            'machine': 'vs',
            'image_type': 'onie',
            'distro': 'trixie',
            'image_version': '20261001.0',
            'source_commit': '0123456789abcdef0123456789abcdef01234567',
            'source_branch': 'codex/bazel-swss-image',
            'source_date_epoch': '1790812800',
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            self.record = snapshot.state_record(argparse.Namespace(**self.identity))

    def write_record(self, record):
        self.path.write_text(json.dumps(record), encoding='utf-8')
        return self.path

    def test_snapshot_round_trip_retains_only_version_state(self):
        environment = {
            'kernel_version': '6.12.41+deb13-sonic-amd64',
            'build_version': '20261001.0',
            'commit_id': self.identity['source_commit'],
            'HOME': '/private/home',
            'GITHUB_TOKEN': 'must-not-be-captured',
            'installer_images': 'not-version-state',
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            record = snapshot.state_record(argparse.Namespace(**self.identity))
        state = snapshot.read_record(self.write_record(record), self.identity)
        self.assertEqual('6.12.41+deb13-sonic-amd64', state['kernel_version'])
        self.assertEqual('20261001.0', state['build_version'])
        self.assertEqual(self.identity['source_commit'], state['commit_id'])
        self.assertEqual('', state['build_number'])
        for name in ('HOME', 'GITHUB_TOKEN', 'installer_images'):
            self.assertNotIn(name, state)
        self.assertNotIn('must-not-be-captured', self.path.read_text())

    def test_every_build_identity_field_must_match(self):
        self.write_record(self.record)
        for name in self.identity:
            with self.subTest(field=name):
                expected = dict(self.identity, **{name: 'different'})
                with self.assertRaisesRegex(ValueError, 'identity'):
                    snapshot.read_record(self.path, expected)

    def test_snapshot_cannot_resume_after_container_loading(self):
        for boundary in ('after-container-loading', 'completed-installer', None):
            with self.subTest(boundary=boundary):
                record = dict(self.record, boundary=boundary)
                with self.assertRaisesRegex(ValueError, 'boundary'):
                    snapshot.read_record(self.write_record(record), self.identity)

    def test_snapshot_rejects_unknown_format_and_extra_record_fields(self):
        for record in (
            dict(self.record, format_version=2),
            dict(self.record, environment={'GITHUB_TOKEN': 'not-allowed'}),
            {name: value for name, value in self.record.items() if name != 'state'},
            [],
        ):
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    snapshot.read_record(self.write_record(record), self.identity)

    def test_snapshot_requires_exact_state_fields(self):
        for state in (
            dict(self.record['state'], unexpected='extra'),
            {name: value for name, value in self.record['state'].items() if name != 'kernel_version'},
            [],
        ):
            with self.subTest(state=state):
                record = dict(self.record, state=state)
                with self.assertRaisesRegex(ValueError, 'state field'):
                    snapshot.read_record(self.write_record(record), self.identity)

    def test_snapshot_rejects_nontext_and_nul_state(self):
        for value in (None, 123, ['value'], 'kernel\0injected'):
            with self.subTest(value=value):
                record = copy.deepcopy(self.record)
                record['state']['kernel_version'] = value
                with self.assertRaisesRegex(ValueError, 'state value'):
                    snapshot.read_record(self.write_record(record), self.identity)

    def test_snapshot_record_cannot_be_a_symlink(self):
        self.write_record(self.record)
        link = self.root / 'linked-state.json'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'record file'):
            snapshot.read_record(link, self.identity)

    def test_snapshot_record_has_a_size_limit(self):
        record = copy.deepcopy(self.record)
        record['state']['kernel_version'] = 'x' * 65536
        with self.assertRaisesRegex(ValueError, 'record file'):
            snapshot.read_record(self.write_record(record), self.identity)

    def test_snapshot_rejects_fifo_before_attempting_a_blocking_read(self):
        os.mkfifo(self.path)
        with mock.patch.object(Path, 'open', side_effect=AssertionError('must not read a FIFO')) as opened:
            with self.assertRaisesRegex(ValueError, 'record file'):
                snapshot.read_record(self.path, self.identity)
        opened.assert_not_called()

    def test_only_checkout_fsroot_is_eligible_for_cleanup(self):
        root = self.root / 'fsroot-vs'
        root.mkdir()
        with mock.patch.object(Path, 'cwd', return_value=self.root):
            self.assertEqual(root, snapshot.scratch_root(root, must_exist=True))
            for path in (self.root, self.root / 'fsroot-vs-other',
                         root / 'child', self.root / 'other' / 'fsroot-vs',
                         self.root / '..' / 'fsroot-vs'):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, 'working directory'):
                        snapshot.scratch_root(path, must_exist=False)

    def test_cleanup_rejects_symlink_even_to_checkout_root(self):
        actual = self.root / 'actual-root'
        actual.mkdir()
        root = self.root / 'fsroot-vs'
        root.symlink_to(actual, target_is_directory=True)
        with mock.patch.object(Path, 'cwd', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                snapshot.scratch_root(root, must_exist=True)

    def test_missing_root_is_only_allowed_for_inspection_or_removal(self):
        root = self.root / 'fsroot-vs'
        with mock.patch.object(Path, 'cwd', return_value=self.root):
            self.assertEqual(root, snapshot.scratch_root(root, must_exist=False))
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                snapshot.scratch_root(root, must_exist=True)
            root.write_text('not a directory')
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                snapshot.scratch_root(root, must_exist=True)

    def test_mount_selection_excludes_siblings_and_unmounts_children_first(self):
        root = self.root / 'with space' / 'fsroot-vs'
        paths = [str(root), str(root / 'run'), str(root / 'run/containerd'),
                 str(root) + '-other/run', str(root.parent), '/']
        lines = []
        for index, path in enumerate(paths):
            escaped = path.replace(' ', '\\040')
            lines.append(f'{index + 1} 0 0:1 / {escaped} rw - tmpfs tmpfs rw\n')
        with mock.patch('builtins.open', mock.mock_open(read_data=''.join(lines))):
            self.assertEqual([str(root / 'run/containerd'), str(root / 'run'), str(root)],
                             snapshot.mounts_below(root))

    def test_process_selection_requires_exact_root_or_descendant(self):
        root = self.root / 'fsroot-vs'
        for path, expected in ((str(root), True), (str(root / 'nested'), True),
                               (str(root) + '-other', False), ('/', False)):
            with self.subTest(process_root=path):
                with mock.patch.object(snapshot.os, 'readlink', return_value=path):
                    self.assertEqual(expected, snapshot.process_is_below(root, 123))
        with mock.patch.object(snapshot.os, 'readlink', side_effect=ProcessLookupError):
            self.assertFalse(snapshot.process_is_below(root, 123))

    def test_signal_rechecks_process_scope_after_opening_pidfd(self):
        root = self.root / 'fsroot-vs'
        with mock.patch.object(snapshot.os, 'pidfd_open', return_value=99, create=True), \
                mock.patch.object(snapshot, 'process_is_below', return_value=False), \
                mock.patch.object(snapshot.signal, 'pidfd_send_signal', create=True) as send, \
                mock.patch.object(snapshot.os, 'close') as close:
            snapshot.signal_processes(root, [123], signal.SIGTERM)
        send.assert_not_called()
        close.assert_called_once_with(99)

    def test_missing_pidfd_support_aborts_before_signaling_or_unmounting(self):
        root = self.root / 'fsroot-vs'
        for missing in ('pidfd_open', 'pidfd_send_signal'):
            with self.subTest(missing=missing):
                runtime_os = SimpleNamespace(close=mock.Mock(), kill=mock.Mock())
                runtime_signal = SimpleNamespace(SIGTERM=signal.SIGTERM, SIGKILL=signal.SIGKILL)
                if missing != 'pidfd_open':
                    runtime_os.pidfd_open = mock.Mock(return_value=99)
                if missing != 'pidfd_send_signal':
                    runtime_signal.pidfd_send_signal = mock.Mock()
                with mock.patch.object(snapshot, 'os', runtime_os), \
                        mock.patch.object(snapshot, 'signal', runtime_signal), \
                        mock.patch.object(snapshot, 'processes_below', return_value=[123]), \
                        mock.patch.object(snapshot.subprocess, 'run') as run:
                    with self.assertRaisesRegex(ValueError, 'requires Linux pidfd support'):
                        snapshot.quiesce(root)
                runtime_os.kill.assert_not_called()
                runtime_os.close.assert_not_called()
                run.assert_not_called()

    def test_process_appearing_during_wait_still_requires_pidfd_support(self):
        root = self.root / 'fsroot-vs'
        with mock.patch.object(snapshot, 'os', SimpleNamespace()), \
                mock.patch.object(snapshot, 'processes_below', return_value=[]), \
                mock.patch.object(snapshot, 'wait_for_processes', return_value=[123]), \
                mock.patch.object(snapshot.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'requires Linux pidfd support'):
                snapshot.quiesce(root)
        run.assert_not_called()

    def test_root_without_processes_can_unmount_without_pidfd_support(self):
        root = self.root / 'fsroot-vs'
        with mock.patch.object(snapshot, 'os', SimpleNamespace()), \
                mock.patch.object(snapshot, 'processes_below', return_value=[]), \
                mock.patch.object(snapshot, 'wait_for_processes', return_value=[]), \
                mock.patch.object(snapshot, 'mounts_below', side_effect=[[str(root / 'proc')], []]), \
                mock.patch.object(snapshot.subprocess, 'run') as run:
            snapshot.quiesce(root)
        run.assert_called_once_with(['umount', str(root / 'proc')], check=True)


if __name__ == '__main__':
    unittest.main()
