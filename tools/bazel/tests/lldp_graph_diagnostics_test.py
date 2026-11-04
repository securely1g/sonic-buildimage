#!/usr/bin/env python3
"""Exercise failed graph retention through the existing public-artifact guards."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / '.github/workflows/bazel-lldp-image.yml'
REGISTRY = 'b' * 40
CONTROL = 'c' * 40


def step(name):
    return WORKFLOW.read_text().split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]


def script():
    return textwrap.dedent(step('Prepare public failed graph evidence').split('        run: |\n', 1)[1])


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ('tools/bazel/ci/' + name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class GraphDiagnosticsTest(unittest.TestCase):
    def prepare(self, root, stderr):
        subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
        (root / 'source-marker').write_text('unchanged source\n')
        subprocess.run(['git', 'add', 'source-marker'], cwd=root, check=True, capture_output=True)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'],
                       cwd=root, check=True, capture_output=True)
        helper = root / 'tools/bazel/ci/public_artifacts.py'
        helper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'tools/bazel/ci/public_artifacts.py', helper)
        graph = {'key': '<root>', 'name': 'sonic-buildimage', 'version': '', 'root': True,
                 'dependencies': [{'key': 'fixture@1.0', 'name': 'fixture', 'version': '1.0', 'dependencies': []}]}
        lock = (json.dumps({'lockFileVersion': 24, 'registryFileHashes': {
            'https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/' + REGISTRY +
            '/modules/fixture/1.0/MODULE.bazel': 'a' * 64}, 'moduleExtensions': {}}) + '\n').encode()
        (root / 'MODULE.bazel.lock').write_bytes(lock)
        fake = root / 'bazel'
        fake.write_text('#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n'
                        + 'Path("calls.json").write_text(json.dumps(sys.argv[1:]))\n'
                        + 'print(' + repr(json.dumps(graph)) + ')\n'
                        + 'sys.stderr.write(' + repr(stderr) + ')\nsys.exit(7)\n')
        fake.chmod(0o755)
        artifacts = root / 'artifacts/lldp'
        artifacts.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Unexpected module graph inspection failure'):
            module('resolution').collect(root, artifacts, bazel=[str(fake)])
        self.assertEqual(json.loads((root / 'calls.json').read_text())[:2], ['mod', 'graph'])
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        (artifacts / 'build-receipt.json').write_text(json.dumps({'status': 'failed', 'revision': revision}))
        return artifacts, lock

    def export(self, root, changes=None):
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        environment = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(root), 'SOURCE_REVISION': revision,
                       'REGISTRY_REVISION': REGISTRY, 'CONTROL_REVISION': CONTROL, 'GITHUB_SHA': CONTROL}
        environment.update(changes or {})
        return subprocess.run(['/bin/bash', '-e', '-c', script()], cwd=root,
            env=environment, capture_output=True)

    def test_actual_failed_collection_exports_only_validated_records(self):
        safe = 'WARNING: Download from https://snapshot.debian.org/archive/debian/20260727T143429Z/pool/main/example.deb failed: GET returned 503'
        credential = 'ERROR: github_pat_' + 'x' * 30
        private = 'ERROR: /__w/sonic-buildimage/sonic-buildimage/external/example/BUILD.bazel: failed'
        stderr = '\n'.join([safe, credential, private, 'ERROR: fixture extension download failed', 'private stack context']) + '\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts, lock = self.prepare(root, stderr)
            original_calls = (root / 'calls.json').read_bytes()
            raw = json.loads((artifacts / 'module-graph-inspection.json').read_text())
            guards = module('public_artifacts')
            with self.assertRaisesRegex(guards.PublicArtifactError, 'diagnostic-json-field'):
                guards.safe_json(raw)
            result = self.export(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls.json').read_bytes(), original_calls)
            self.assertEqual((root / 'MODULE.bazel.lock').read_bytes(), lock)
            output = artifacts / 'graph-failure'
            self.assertEqual({p.name for p in output.iterdir()}, {'module-graph.json', 'MODULE.bazel.lock', 'inspection-summary.json'})
            self.assertEqual((output / 'MODULE.bazel.lock').read_bytes(), lock)
            self.assertEqual((output / 'module-graph.json').read_bytes(), (artifacts / 'module-graph.json').read_bytes())
            summary = json.loads((output / 'inspection-summary.json').read_text())
            self.assertEqual(set(summary), {'selection', 'returncode', 'stderr_sha256', 'stderr_bytes', 'safe_error_lines', 'omitted_lines'})
            self.assertEqual(summary['selection'], {'source_revision':
                subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                'registry_revision': REGISTRY, 'control_revision': CONTROL})
            self.assertEqual(summary['returncode'], 7)
            self.assertEqual(summary['safe_error_lines'], [safe, 'ERROR: fixture extension download failed'])
            self.assertEqual(summary['omitted_lines'], 3)
            self.assertEqual(summary['stderr_bytes'], len(stderr.encode()))
            import hashlib
            self.assertEqual(summary['stderr_sha256'], hashlib.sha256(stderr.encode()).hexdigest())
            guards.safe_json(summary)
            self.assertNotIn(credential, (output / 'inspection-summary.json').read_text())
            self.assertNotIn(private, (output / 'inspection-summary.json').read_text())

    def test_source_catalog_and_controller_mismatches_fail_closed(self):
        for change in ({'SOURCE_REVISION': 'd' * 40}, {'REGISTRY_REVISION': 'd' * 40},
                       {'CONTROL_REVISION': 'd' * 40}, {'GITHUB_SHA': 'main'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                artifacts, _ = self.prepare(root, 'ERROR: fixture failure\n')
                self.assertNotEqual(self.export(root, change).returncode, 0)
                self.assertFalse((artifacts / 'graph-failure').exists())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts, _ = self.prepare(root, 'ERROR: fixture failure\n')
            (artifacts / 'build-receipt.json').write_text(json.dumps({'status': 'failed', 'revision': 'd' * 40}))
            self.assertNotEqual(self.export(root).returncode, 0)
            self.assertFalse((artifacts / 'graph-failure').exists())

    def test_default_catalog_is_bound_to_the_existing_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts, _ = self.prepare(root, 'ERROR: fixture failure\n')
            self.assertEqual(self.export(root, {'REGISTRY_REVISION': ''}).returncode, 0)
            value = json.loads((artifacts / 'graph-failure/inspection-summary.json').read_text())
            self.assertEqual(value['selection']['registry_revision'], REGISTRY)

    def test_invalid_graph_or_lock_is_not_published(self):
        for name, value in [('module-graph.json', {'key': '<root>'}),
                            ('MODULE.bazel.lock', {'lockFileVersion': 24, 'registryFileHashes': {},
                                                 'moduleExtensions': {'fixture': {'envVariables': {'PRIVATE': 'value'}}}})]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                artifacts, _ = self.prepare(root, 'ERROR: fixture extension download failed\n')
                target = root / name if name == 'MODULE.bazel.lock' else artifacts / name
                target.write_text(json.dumps(value))
                self.assertNotEqual(self.export(root).returncode, 0)
                self.assertFalse((artifacts / 'graph-failure').exists())

    def test_missing_capture_or_symlink_fails_closed(self):
        for mode in ['missing', 'symlink']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                artifacts, _ = self.prepare(root, 'ERROR: fixture extension download failed\n')
                path = artifacts / 'module-graph.stderr'
                original = path.read_bytes()
                path.unlink()
                if mode == 'symlink':
                    other = root / 'other-stderr'; other.write_bytes(original); path.symlink_to(other)
                self.assertNotEqual(self.export(root).returncode, 0)
                self.assertFalse((artifacts / 'graph-failure').exists())

    def test_failure_only_upload_does_not_accept_the_failed_build(self):
        prepare = step('Prepare public failed graph evidence')
        upload = step('Retain public failed graph evidence')
        for build in ['success', 'failure', 'cancelled', 'skipped']:
            for prepared in ['success', 'failure', 'skipped']:
                with self.subTest(build=build, prepared=prepared):
                    values = {'steps.native_images.outcome': repr(build), 'steps.graph_diagnostics.outcome': repr(prepared)}
                    def evaluate(text):
                        expression = re.search(r'^        if: (.+)$', text, re.MULTILINE).group(1)
                        for name, value in values.items(): expression = expression.replace(name, value)
                        return eval(expression.replace('always()', 'True').replace('&&', ' and '), {'__builtins__': {}}, {})
                    self.assertEqual(evaluate(prepare), build == 'failure')
                    self.assertEqual(evaluate(upload), build == 'failure' and prepared == 'success')
        self.assertNotIn('continue-on-error', WORKFLOW.read_text())
        self.assertIn('        id: native_images\n', step('Build runtime and debug images from source owners'))
        self.assertNotIn('module-graph.stderr', upload)
        self.assertNotIn('module-graph-inspection.json', upload)



if __name__ == '__main__':
    unittest.main()
