#!/usr/bin/env python3
"""Exercise execution-only CA preparation without Docker or changing host trust."""

import ast
import hashlib
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[4]
TRUST_PATH = '/usr/local/share/sonic-build-trust/ca-bundle.pem'
BEGIN = '# SONIC native execution trust BEGIN'
END = '# SONIC native execution trust END'


def certificate_fixture(name):
    tree = ast.parse((SOURCE / 'tools/bazel/ci/trust_test.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('missing synthetic public CA fixture: ' + name)


class NativeTrustHookTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shared = self.root / 'src/sonic-build-hooks/buildinfo'
        self.shared.mkdir(parents=True)
        (self.shared / 'sonic-build-hooks_1.0_all.deb').write_bytes(b'fixture hooks')
        utils = self.shared.parent / 'scripts/utils.sh'
        utils.parent.mkdir()
        utils.write_text('#!/bin/bash\n', encoding='utf-8')
        for name in ('build_mirror_config.sh', 'docker_version_control.sh', 'versions_manager.py'):
            path = self.root / 'scripts' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            path.chmod(0o755)
        apt_config = self.root / 'files/apt/apt.conf.d/fixture'
        apt_config.parent.mkdir(parents=True)
        apt_config.write_text('Acquire::Retries "1";\n', encoding='utf-8')
        trust = self.root / 'tools/bazel/ci/trust.py'
        trust.parent.mkdir(parents=True)
        shutil.copyfile(SOURCE / 'tools/bazel/ci/trust.py', trust)
        self.bundle = self.root / 'explicit-public-ca.pem'
        self.bundle.write_bytes(certificate_fixture('TEST_CA_PEM'))

    def prepare(self, image='sonic-slave-trixie', slave='y', enabled=True):
        context = self.root / image
        context.mkdir(exist_ok=True)
        dockerfile = context / 'Dockerfile'
        if not dockerfile.exists():
            dockerfile.write_text('FROM debian:trixie\nRUN echo fixture-build\n', encoding='utf-8')
        environment = {'PATH': os.defpath, 'LC_ALL': 'C', 'BUILD_SLAVE': slave,
                       'ENABLE_VERSION_CONTROL_DOCKER': 'n'}
        if enabled:
            environment['SONIC_BUILD_SLAVE_CA_BUNDLE'] = str(self.bundle)
        result = subprocess.run(
            ['bash', str(SOURCE / 'scripts/prepare_docker_buildinfo.sh'),
             image, str(dockerfile), 'amd64', str(dockerfile), 'trixie'],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=15,
        )
        return result, dockerfile

    def assert_success(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def assert_shared_source_unchanged(self):
        self.assertEqual(['sonic-build-hooks_1.0_all.deb'],
                         sorted(path.name for path in self.shared.iterdir()))
        self.assertEqual(b'fixture hooks',
                         (self.shared / 'sonic-build-hooks_1.0_all.deb').read_bytes())

    def test_make_shell_preparation_emits_no_text_into_makefile_syntax(self):
        context = self.root / 'sonic-slave-trixie'
        context.mkdir()
        dockerfile = context / 'Dockerfile'
        dockerfile.write_text('FROM debian:trixie\nRUN echo fixture-build\n', encoding='utf-8')
        makefile = self.root / 'Makefile'
        makefile.write_text(
            '$(shell BUILD_SLAVE=y ENABLE_VERSION_CONTROL_DOCKER=n '
            'SONIC_BUILD_SLAVE_CA_BUNDLE="' + str(self.bundle) + '" bash "' +
            str(SOURCE / 'scripts/prepare_docker_buildinfo.sh') + '" '
            'sonic-slave-trixie sonic-slave-trixie/Dockerfile amd64 "" trixie)\n'
            '.PHONY: all\nall:\n\t@echo native-preparation-complete\n', encoding='utf-8',
        )
        result = subprocess.run(
            ['make', '--no-print-directory', '--file', str(makefile), 'all'],
            cwd=self.root, env={'PATH': os.defpath, 'LC_ALL': 'C'},
            capture_output=True, text=True, timeout=15,
        )
        self.assert_success(result)
        self.assertEqual('native-preparation-complete\n', result.stdout)
        self.assertIn(BEGIN, dockerfile.read_text(encoding='utf-8'))
        self.assertEqual(self.bundle.read_bytes(),
                         (context / 'buildinfo/sonic-build-ca-bundle.pem').read_bytes())

    def test_opt_in_installs_validated_trust_before_native_first_run(self):
        result, dockerfile = self.prepare()
        self.assert_success(result)
        content = dockerfile.read_text(encoding='utf-8')
        self.assertEqual(self.bundle.read_bytes(),
                         (dockerfile.parent / 'buildinfo/sonic-build-ca-bundle.pem').read_bytes())
        certificates = dockerfile.parent / 'buildinfo/sonic-build-ca-certificates'
        der = ssl.PEM_cert_to_DER_cert(self.bundle.read_text(encoding='ascii'))
        certificate_name = hashlib.sha256(der).hexdigest() + '.crt'
        self.assertEqual([certificate_name], [path.name for path in certificates.iterdir()])
        self.assertEqual(self.bundle.read_bytes(), (certificates / certificate_name).read_bytes())
        self.assertIn('"/usr/local/share/ca-certificates/sonic-build-trust/"', content)
        self.assertEqual(1, content.count(BEGIN))
        self.assertLess(content.index(BEGIN), content.index('\nRUN '))
        self.assertLess(content.index(END), content.index('RUN dpkg -i'))
        self.assertIn(hashlib.sha256(self.bundle.read_bytes()).hexdigest(), content)
        for name in ('SSL_CERT_FILE', 'GIT_SSL_CAINFO', 'CURL_CA_BUNDLE',
                     'REQUESTS_CA_BUNDLE', 'PIP_CERT'):
            self.assertIn('ENV ' + name + '=' + TRUST_PATH, content)
        self.assertIn('ENV WGETRC=/usr/local/share/sonic-build-trust/wgetrc', content)
        self.assertIn('> /usr/local/share/sonic-build-trust/wgetrc', content)
        self.assertNotIn('/etc/wgetrc', content)
        self.assertIn('Acquire::https::CaInfo "' + TRUST_PATH + '";', content)
        self.assertIn('check_certificate = on', content)
        self.assertIn("printf '%s\\n'", content)
        self.assertIn('&& \\\n', content)
        self.assertNotIn('update-ca-certificates', content)
        self.assertNotIn('Verify-Peer=false', content)
        self.assertNotIn('sslVerify=false', content)
        self.assertNotIn('-----BEGIN CERTIFICATE-----', result.stdout + result.stderr + content)
        self.assert_shared_source_unchanged()

    def test_runtime_contexts_and_non_slave_builds_cannot_receive_ca_bytes(self):
        for image, slave in (('docker-swss', 'y'), ('docker-swss', 'n'),
                             ('sonic-slave-trixie', 'n')):
            with self.subTest(image=image, slave=slave):
                result, dockerfile = self.prepare(image=image, slave=slave)
                self.assert_success(result)
                self.assertNotIn(TRUST_PATH, dockerfile.read_text(encoding='utf-8'))
                self.assertFalse((dockerfile.parent / 'buildinfo/sonic-build-ca-bundle.pem').exists())
                self.assertFalse((dockerfile.parent / 'buildinfo/sonic-build-ca-certificates').exists())
        self.assert_shared_source_unchanged()

    def test_disabling_opt_in_removes_previous_trust_and_changes_cache_identity(self):
        result, dockerfile = self.prepare()
        self.assert_success(result)
        original = dockerfile.read_bytes()
        result, _ = self.prepare(enabled=False)
        self.assert_success(result)
        disabled = dockerfile.read_bytes()
        self.assertNotEqual(hashlib.sha256(original).digest(), hashlib.sha256(disabled).digest())
        self.assertNotIn(BEGIN.encode(), disabled)
        self.assertNotIn(TRUST_PATH.encode(), disabled)
        self.assertFalse((dockerfile.parent / 'buildinfo/sonic-build-ca-bundle.pem').exists())
        self.assertFalse((dockerfile.parent / 'buildinfo/sonic-build-ca-certificates').exists())
        self.assertIn(b'RUN echo fixture-build\n', disabled)
        result, _ = self.prepare(enabled=False)
        self.assert_success(result)
        self.assertEqual(disabled, dockerfile.read_bytes())
        self.assert_shared_source_unchanged()

    def test_changed_ca_rotates_native_dockerfile_identity_without_duplicate_hooks(self):
        result, dockerfile = self.prepare()
        self.assert_success(result)
        original = dockerfile.read_bytes()
        self.bundle.write_bytes(certificate_fixture('SYSTEM_CA_PEM'))
        result, _ = self.prepare()
        self.assert_success(result)
        current = dockerfile.read_bytes()
        self.assertNotEqual(original, current)
        self.assertEqual(1, current.count(BEGIN.encode()))
        self.assertIn(hashlib.sha256(self.bundle.read_bytes()).hexdigest().encode(), current)
        self.assertEqual(self.bundle.read_bytes(),
                         (dockerfile.parent / 'buildinfo/sonic-build-ca-bundle.pem').read_bytes())

    def test_invalid_ca_or_private_material_fails_before_injection(self):
        for data in (b'not a certificate', b'-----BEGIN PRIVATE KEY-----\nprivate\n',
                     certificate_fixture('TEST_CA_PEM') + b'PRIVATE DATA', b''):
            with self.subTest(data=data[:20]):
                self.bundle.write_bytes(data)
                result, dockerfile = self.prepare()
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn(BEGIN, dockerfile.read_text(encoding='utf-8'))
                self.assertFalse((dockerfile.parent / 'buildinfo/sonic-build-ca-bundle.pem').exists())
        self.assert_shared_source_unchanged()

    def test_unbalanced_generated_trust_block_is_rejected(self):
        result, dockerfile = self.prepare(enabled=False)
        self.assert_success(result)
        original = dockerfile.read_bytes() + BEGIN.encode() + b'\nRUN malformed\n'
        dockerfile.write_bytes(original)
        result, _ = self.prepare()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(original, dockerfile.read_bytes())


if __name__ == '__main__':
    unittest.main()
