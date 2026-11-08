"""Exercise worker download boundaries with isolated caches and inert payloads."""

import ast
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from tools.bazel.tests.source_image_worker_test import worker, ROOT, SHARED_CONFIG_FIXTURE
from tools.bazel.tests.source_workflow_test import jobs, steps, shell

PIN = "997796400cc3eefc1f0590b227cd9b74c2141424"


def block(first, stop):
    """Compile the unchanged statements from prepare, excluding native work."""
    tree = ast.parse((ROOT / "tools/bazel/ci/source_image_worker.py").read_text())
    prepare = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "prepare")
    starts = {node.targets[0].id: index for index, node in enumerate(prepare.body)
              if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    module = ast.Module(body=prepare.body[starts[first]:starts[stop]], type_ignores=[])
    return compile(ast.fix_missing_locations(module), "<actual-worker-download-statements>", "exec")


def fixture(root, reader):
    commands = root / "commands"
    commands.mkdir()
    (commands / "python3").symlink_to(sys.executable)
    (commands / "chmod").write_text("#!" + sys.executable + "\nimport os,sys\nos.chmod(sys.argv[2],int(sys.argv[1],8))\n")
    (commands / "chmod").chmod(0o755)
    inputs = root / "bazel-public-inputs/ci"
    (inputs / "github_sources").mkdir(parents=True)
    (inputs / "github_sources.py").write_text(textwrap.dedent(reader))
    (inputs / "source_mirrors.cfg").write_text(SHARED_CONFIG_FIXTURE)
    (inputs / "github_sources/bazelisk-1.29.0-linux-amd64.json").write_text("{}\n")
    (inputs / "github_sources/sonic-buildimage-rule-inputs.json").write_text("{}\n")
    (root / "runner-temp").mkdir()
    (root / "github-path").write_text("")
    guards = root / "guards"
    guards.mkdir()
    (guards / "sitecustomize.py").write_text(
        "import sys\n"
        "def deny(event,args):\n"
        " if event.startswith(('socket.','subprocess.','os.exec','os.spawn','os.system')):\n"
        "  raise AssertionError('network/process denied')\n"
        "sys.addaudithook(deny)\n")
    return {"PATH": str(commands), "HOME": str(root), "RUNNER_TEMP": str(root / "runner-temp"),
            "GITHUB_PATH": str(root / "github-path"), "GITHUB_TOKEN": "fixture-only-token",
            "PYTHONPATH": str(guards) + os.pathsep + str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}


class WorkerDownloadsTest(unittest.TestCase):
    def test_input_order_fixed_inventory_and_worker_private_scanner_pin(self):
        all_jobs = jobs()
        inputs = steps(all_jobs["source_image_inputs"])
        names = list(inputs)
        self.assertLess(names.index("Prepare checksum-pinned Bazelisk for source downloads"),
                        names.index("Set up Bazel for repository downloads only"))
        self.assertLess(names.index("Set up Bazel for repository downloads only"),
                        names.index("Prepare verified public rule downloads and mirrors"))
        self.assertLess(names.index("Prepare verified public rule downloads and mirrors"),
                        names.index("Retain the same verified GitLab sources used by the VS workflow"))
        self.assertIn('bazelisk-version: ""', inputs["Set up Bazel for repository downloads only"])
        self.assertIn("repository-cache: true", inputs["Set up Bazel for repository downloads only"])
        self.assertIn("disk-cache: false", inputs["Set up Bazel for repository downloads only"])
        upload = inputs["Upload verified source downloads"]
        self.assertIn("artifacts/source-image-inputs/*/src.tar.gz", upload)
        self.assertIn("artifacts/source-image-inputs/receipt.json", upload)
        self.assertNotIn("source-input-rules", upload)
        native = steps(all_jobs["source_image"])
        self.assertIn("c41c7baf15307ad1c8f94d5dca511ef952d28729", native["Checkout the registry mirror configuration"])
        for selected in (inputs, native):
            checkout = selected["Checkout verified public source inputs"]
            self.assertIn(PIN, checkout)
            self.assertIn("persist-credentials: false", checkout)
            self.assertIn("sparse-checkout-cone-mode: false", checkout)
        self.assertNotIn("bazel-contrib/setup-bazel", all_jobs["source_image"])
        self.assertNotIn("github_sources.py", all_jobs["source_worker_preflight"])
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", native["Prepare private Docker and persistent worker caches"])
        source = (ROOT / "tools/bazel/ci/source_image_worker.py").read_text()
        self.assertLess(source.index('github_token = os.environ.pop("GITHUB_TOKEN", "")'), source.index('preflight = measure(', source.index('def prepare(args):')))
        self.assertLess(source.index('require(preflight["status"] == "passed"'), source.index('public_inputs = ROOT'))
        self.assertLess(source.index('safe_json(rule_sources)'), source.index('run(["sudo", "-n", sys.executable'))

    def test_actual_bazelisk_step_stops_before_path_on_reader_or_receipt_failure(self):
        script = shell(steps(jobs()["source_image_inputs"])["Prepare checksum-pinned Bazelisk for source downloads"])
        reader = r'''
            import json, os, sys
            from pathlib import Path
            options = dict(zip(sys.argv[1:][::2], sys.argv[1:][1::2]))
            assert options['--manifest'].endswith('/bazelisk-1.29.0-linux-amd64.json')
            assert options['--cache-dir'] == str(Path(os.environ['HOME']) / '.cache/sonic-github-sources')
            assert options['--github-token-env'] == 'GITHUB_TOKEN'
            binary = Path(options['--distdir']) / 'bazel'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'inert fixture; never execute')
            binary.chmod(0o600)
            if os.environ['CASE'] == 'reader': raise SystemExit(23)
            value = {'verified': True} if os.environ['CASE'] == 'valid' else {'authorization': 'private-sentinel'}
            if os.environ['CASE'] == 'missing': raise SystemExit(0)
            content = json.dumps(value)
            if os.environ['CASE'] == 'duplicate': content = '{\"files\": [], \"files\": []}'
            if os.environ['CASE'] == 'nonfinite': content = '{\"value\": NaN}'
            Path(options['--receipt']).write_text(content)
        '''
        for case in ("valid", "reader", "unsafe", "duplicate", "nonfinite", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = fixture(root, reader)
                result = subprocess.run(["/bin/bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                                        env={**env, "CASE": case}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode == 0, case == "valid", result.stdout + result.stderr)
                binary = root / "runner-temp/sonic-bazelisk/bin/bazel"
                self.assertEqual(binary.stat().st_mode & 0o777, 0o755 if case == "valid" else 0o600)
                self.assertEqual((root / "github-path").read_text(), str(binary.parent) + "\n" if case == "valid" else "")
                self.assertNotIn("private-sentinel", result.stdout + result.stderr)
                self.assertNotIn("fixture-only-token", result.stdout + result.stderr)

    def test_actual_input_rule_step_preserves_cache_and_rc_and_rejects_bad_config(self):
        script = shell(steps(jobs()["source_image_inputs"])["Prepare verified public rule downloads and mirrors"])
        reader = r'''
            import json, os, sys
            from pathlib import Path
            options = dict(zip(sys.argv[1:][::2], sys.argv[1:][1::2]))
            assert options['--manifest'].endswith('/sonic-buildimage-rule-inputs.json')
            assert options['--github-token-env'] == 'GITHUB_TOKEN'
            repository = Path(os.environ['HOME']) / '.cache/bazel-repo'
            assert Path(options['--repository-cache']) == repository
            assert Path(options['--cache-dir']) == Path(os.environ['HOME']) / '.cache/sonic-github-sources'
            if os.environ['CASE'] == 'reader': raise SystemExit(23)
            destination = Path(options['--distdir']); destination.mkdir()
            (destination / 'archive.tar.gz').write_bytes((repository / 'fixture').read_bytes())
            value = {'origin': 'repository-cache'} if os.environ['CASE'] != 'unsafe' else {'token': 'private-sentinel'}
            if os.environ['CASE'] == 'missing': raise SystemExit(0)
            content = json.dumps(value)
            if os.environ['CASE'] == 'duplicate': content = '{\"files\": [], \"files\": []}'
            if os.environ['CASE'] == 'nonfinite': content = '{\"value\": NaN}'
            Path(options['--receipt']).write_text(content)
        '''
        for case in ("valid", "reader", "unsafe", "bad-config", "duplicate", "nonfinite", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = fixture(root, reader)
                cached = root / ".cache/bazel-repo/fixture"
                cached.parent.mkdir(parents=True); cached.write_bytes(b"existing verified fixture")
                prior = "common --repository_cache=/existing\nbuild --disk_cache=/existing-disk\n"
                (root / ".bazelrc.user").write_text(prior)
                if case == "bad-config": (root / "bazel-public-inputs/ci/source_mirrors.cfg").write_text("corrupt")
                result = subprocess.run(["/bin/bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                                        env={**env, "CASE": case}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode == 0, case == "valid", result.stdout + result.stderr)
                self.assertEqual(cached.read_bytes(), b"existing verified fixture")
                extra = ("common --distdir=" + str(root / "runner-temp/source-input-rules") + "\n"
                         "common --experimental_downloader_config=" + str(root / "runner-temp/source-input-mirrors.cfg") + "\n") if case == "valid" else ""
                self.assertEqual((root / ".bazelrc.user").read_text(), prior + extra)
                self.assertNotIn("private-sentinel", result.stdout + result.stderr)

    def test_actual_worker_download_statements_fail_closed_and_preserve_inventory(self):
        snippet = block("public_inputs", "launch")
        for case in ("valid", "reader", "unsafe", "duplicate", "nonfinite", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); artifacts = root / "artifacts"; artifacts.mkdir()
                cache = root / "cache"; repository = cache / "repository"; repository.mkdir(parents=True)
                (repository / "retained").write_bytes(b"cache fixture")
                distdirs = [cache / "distdir/original-source"]
                def reader(argv, *, env):
                    self.assertEqual(argv[:2], [sys.executable, "-B"])
                    self.assertTrue(argv[2].endswith('/bazel-public-inputs/ci/github_sources.py'))
                    options = dict(zip(argv[3:][::2], argv[3:][1::2]))
                    self.assertEqual(options['--repository-cache'], str(repository))
                    self.assertEqual(options['--cache-dir'], str(cache / 'github-sources'))
                    self.assertEqual(options['--distdir'], str(cache / 'rule-distdir'))
                    self.assertEqual(options['--github-token-env'], 'GITHUB_TOKEN')
                    self.assertEqual(env['GITHUB_TOKEN'], 'fixture-only-token')
                    self.assertNotIn('GITHUB_TOKEN', os.environ)
                    if case == 'reader': raise subprocess.CalledProcessError(23, argv)
                    data = {'files': [{'source': 'repository-cache', 'name': 'fixture', 'sha256': 'a' * 64}]}
                    if case == 'unsafe': data = {'authorization': 'private-sentinel'}
                    if case == 'missing': return
                    content = json.dumps(data)
                    if case == 'duplicate': content = '{\"files\": [], \"files\": []}'
                    if case == 'nonfinite': content = '{\"value\": NaN}'
                    Path(options['--receipt']).write_text(content)
                namespace = {**worker.__dict__, 'cache': cache, 'artifacts': artifacts, 'temporary': root,
                             'distdirs': distdirs, 'github_token': 'fixture-only-token', 'run': reader}
                with mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('process denied')), \
                        mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network denied')):
                    if case == 'valid': exec(snippet, namespace)
                    else:
                        with self.assertRaises((ValueError, OSError, subprocess.CalledProcessError)): exec(snippet, namespace)
                self.assertEqual((repository / 'retained').read_bytes(), b'cache fixture')
                self.assertEqual(distdirs, [cache / 'distdir/original-source'] + ([cache / 'rule-distdir'] if case == 'valid' else []))
                if case == 'valid':
                    receipt = {'image': 'syncd', 'rule_sources': namespace['rule_sources']}
                    worker.safe_json(receipt)
                    (artifacts / 'worker.json').write_text(json.dumps(receipt))
                    worker.publish(type('Args', (), {'image': 'syncd', 'artifacts': artifacts})())
                    self.assertEqual(json.loads((artifacts / 'public/worker.json').read_text()), receipt)
                    self.assertFalse((artifacts / 'public/rule-sources.json').exists())
                    self.assertFalse((artifacts / 'rule-sources.json').exists())

    def test_public_config_updates_without_replacing_private_scanner(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'output.cfg'; artifacts = root / 'artifacts'; artifacts.mkdir()
                public = root / 'bazel-public-inputs/ci/source_mirrors.cfg'
                public.parent.mkdir(parents=True); public.write_text('bad' if corrupt else SHARED_CONFIG_FIXTURE)
                private = mock.Mock()
                private.MIRROR_ROOTS = (('TEST_MIRROR_ROOT', 'unused'),)
                private.selected_mirrors.return_value = []
                with mock.patch.object(worker, 'ROOT', root), mock.patch.object(sys, 'path', list(sys.path)), \
                        mock.patch.dict(sys.modules, {'private_mirrors': private}), \
                        mock.patch.dict(os.environ, {'TEST_MIRROR_ROOT': ''}, clear=True), \
                        mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network denied')), \
                        mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('process denied')):
                    if corrupt:
                        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                            worker.configure_worker_mirrors('prepare-mirrors', artifacts, config=config)
                        private.write_config.assert_not_called()
                    else:
                        worker.configure_worker_mirrors('prepare-mirrors', artifacts, config=config)
                        expected = worker.mirror_configuration(SHARED_CONFIG_FIXTURE.encode()).decode()
                        private.write_config.assert_called_once_with(config, expected)
                    self.assertIn(str(root / '.source-mirror-registry/ci'), sys.path)
                    self.assertNotIn('TEST_MIRROR_ROOT', os.environ)
                    private.Redactor.return_value.mask_github.assert_called_once()


if __name__ == '__main__':
    unittest.main()
