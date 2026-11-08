"""Exercise standalone download steps with inert readers and isolated caches."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from tools.bazel.tests.source_workflow_test import shell, steps


ROOT = Path(__file__).resolve().parents[3]
FAMILIES = {
    "database-image": "database", "eventd-image": "eventd", "frr-image": "frr",
    "lldp-image": "lldp", "macsec": "macsec", "mgmt-framework": "mgmt-framework",
    "mux-image": "mux", "radv-image": "radv", "snmp": "snmp", "sysmgr": "sysmgr",
}


def native_job(family):
    text = (ROOT / f".github/workflows/bazel-{family}.yml").read_text()
    return re.search(r"^  native:\n.*?(?=^  [\w-]+:\n|\Z)", text,
                     re.MULTILINE | re.DOTALL)[0]


def fixture(directory, reader):
    """Only the fixture interpreter and recording chmod are on PATH."""
    commands = directory / "commands"
    commands.mkdir()
    (commands / "python3").symlink_to(sys.executable)
    inputs = directory / ".bazel-inputs/github-reader/ci"
    (inputs / "github_sources").mkdir(parents=True)
    (inputs / "github_sources.py").write_text(textwrap.dedent(reader))
    for name in ("bazelisk-1.29.0-linux-amd64", "sonic-buildimage-rule-inputs"):
        (inputs / "github_sources" / (name + ".json")).write_text("{}\n")
    return commands


class StandaloneRuleWorkflowTest(unittest.TestCase):
    def test_native_only_order_cache_and_existing_upload_layout(self):
        for family, output in FAMILIES.items():
            with self.subTest(family=family):
                job = native_job(family)
                self.assertIn("runs-on: ubuntu-24.04\n", job)
                self.assertNotIn("matrix.", job)
                selected = steps(job)
                for name in ("Check out the pinned GitHub input reader",
                             "Prepare checksum-pinned Bazelisk",
                             "Prepare checksum-pinned Bazel rule archives"):
                    self.assertNotIn("if:", selected[name])
                    self.assertNotIn("continue-on-error", selected[name])
                checkout = selected["Check out the pinned GitHub input reader"]
                self.assertIn("3a7e32d09e511735f36a47b276d6524cf9c2a5c4", checkout)
                self.assertIn("persist-credentials: false", checkout)
                self.assertIn("sparse-checkout-cone-mode: false", checkout)
                for path in ("ci/github_sources.py",
                             "ci/github_sources/bazelisk-1.29.0-linux-amd64.json",
                             "ci/github_sources/sonic-buildimage-rule-inputs.json"):
                    self.assertIn("            " + path + "\n", checkout)
                if family == "sysmgr":
                    self.assertIn("ci/github_sources/cosmos-4.0.2.json", checkout)
                self.assertLess(job.index("Prepare checksum-pinned Bazelisk"),
                                job.index("bazel-contrib/setup-bazel@"))
                self.assertLess(job.index("bazel-contrib/setup-bazel@"),
                                job.index("Prepare checksum-pinned Bazel rule archives"))
                self.assertIn('bazelisk-version: ""', job)
                self.assertIn("bazelisk-cache: true", job)
                self.assertIn("repository-cache: true", job)
                self.assertIn("steps.download_receipts.outcome == 'success'", job)
                upload = job[job.index("uses: actions/upload-artifact@") :]
                self.assertIn("artifacts/" + output, upload)
                self.assertNotIn("artifacts/source-downloads", upload)
                self.assertNotIn("RUNNER_TEMP", upload)
                self.assertNotIn("path: artifacts\n", upload)

    def test_bootstrap_failure_never_exposes_partial_binary(self):
        reader = r'''
            import json, os, sys
            from pathlib import Path
            options = dict(zip(sys.argv[1:][::2], sys.argv[1:][1::2]))
            assert options['--github-token-env'] == 'GITHUB_TOKEN'
            assert os.environ['GITHUB_TOKEN'] == 'fixture-token'
            assert options['--manifest'].endswith('/bazelisk-1.29.0-linux-amd64.json')
            assert options['--receipt'] == os.environ['EXPECTED_RECEIPT']
            binary = Path(options['--distdir']) / 'bazel'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'fixture only; never execute')
            binary.chmod(0o600)
            if int(os.environ['READER_EXIT']):
                raise SystemExit(int(os.environ['READER_EXIT']))
            receipt = Path(options['--receipt'])
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({'verified': True}))
        '''
        chmod = "#!" + sys.executable + "\n" + textwrap.dedent(r'''
            import os, sys
            from pathlib import Path
            binary = Path(os.environ['RUNNER_TEMP']) / 'sonic-bazelisk/bin/bazel'
            assert sys.argv[1:] == ['0755', str(binary)]
            assert Path(os.environ['EXPECTED_RECEIPT']).is_file()
            assert Path(os.environ['GITHUB_PATH']).read_text() == ''
            binary.chmod(0o755)
        ''')
        for family, output in FAMILIES.items():
            script = shell(steps(native_job(family))["Prepare checksum-pinned Bazelisk"])
            for code in (23, 0):
                with self.subTest(family=family, code=code), tempfile.TemporaryDirectory() as temp:
                    directory = Path(temp)
                    commands = fixture(directory, reader)
                    (commands / "chmod").write_text(chmod)
                    (commands / "chmod").chmod(0o755)
                    path_file = directory / "github-path"
                    path_file.write_text("")
                    result = subprocess.run(
                        ["/bin/bash", "-e", "-o", "pipefail", "-c", script], cwd=directory,
                        env={"PATH": str(commands), "HOME": str(directory),
                             "RUNNER_TEMP": str(directory / "runner-temp"),
                             "GITHUB_PATH": str(path_file), "GITHUB_TOKEN": "fixture-token",
                             "EXPECTED_RECEIPT": f"artifacts/{output}/bazelisk-bootstrap.json",
                             "READER_EXIT": str(code)},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    binary = directory / "runner-temp/sonic-bazelisk/bin/bazel"
                    self.assertEqual(binary.stat().st_mode & 0o777, 0o600 if code else 0o755)
                    self.assertEqual(path_file.read_text(), "" if code else str(binary.parent) + "\n")

    def test_rule_reader_preserves_restored_cache_pigz_and_startup_flags(self):
        reader = r'''
            import json, os, sys
            from pathlib import Path
            options = dict(zip(sys.argv[1:][::2], sys.argv[1:][1::2]))
            assert set(options) == {'--manifest', '--distdir', '--repository-cache',
                                    '--cache-dir', '--receipt', '--github-token-env'}
            assert options['--manifest'].endswith('/sonic-buildimage-rule-inputs.json')
            assert options['--github-token-env'] == 'GITHUB_TOKEN'
            assert os.environ['GITHUB_TOKEN'] == 'fixture-token'
            repository = Path(os.environ['HOME']) / '.cache/bazel-repo'
            assert Path(options['--repository-cache']) == repository
            assert Path(options['--cache-dir']) == Path(os.environ['HOME']) / '.cache/sonic-github-sources'
            assert options['--receipt'] == os.environ['EXPECTED_RECEIPT']
            distdir = Path(options['--distdir'])
            assert distdir == Path(os.environ['EXPECTED_DISTDIR'])
            assert (distdir / 'pigz-2.8.tar.gz').read_bytes() == b'existing pigz'
            data = (repository / 'fixture-archive').read_bytes()
            if int(os.environ['READER_EXIT']):
                raise SystemExit(int(os.environ['READER_EXIT']))
            (distdir / 'rules_cc-0.2.22.tar.gz').write_bytes(data)
            receipt = Path(options['--receipt'])
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({'origin': 'repository-cache'}))
        '''
        for family, output in FAMILIES.items():
            job = native_job(family)
            script = shell(steps(job)["Prepare checksum-pinned Bazel rule archives"])
            for code in (23, 0):
                with self.subTest(family=family, code=code), tempfile.TemporaryDirectory() as temp:
                    directory = Path(temp)
                    commands = fixture(directory, reader)
                    runner = directory / "runner-temp"
                    if family in ("frr-image", "lldp-image", "macsec", "snmp"):
                        distdir = runner / "pigz-distdir"
                    elif family == "sysmgr":
                        distdir = runner / "sysmgr-github-inputs/distdir"
                    else:
                        distdir = directory / "artifacts/source-downloads"
                    distdir.mkdir(parents=True)
                    (distdir / "pigz-2.8.tar.gz").write_bytes(b'existing pigz')
                    repository = directory / ".cache/bazel-repo"
                    repository.mkdir(parents=True)
                    (repository / "fixture-archive").write_bytes(b'cached fixture')
                    prior = "build --disk_cache=/fixture/disk\ncommon --repository_cache=/fixture/repository\n"
                    rc = directory / ".bazelrc.user"
                    rc.write_text(prior)
                    startup = runner / "sysmgr-github-inputs/downloads.bazelrc"
                    startup.parent.mkdir(parents=True, exist_ok=True)
                    startup.write_text("common --distdir=" + str(distdir) + "\n")
                    result = subprocess.run(
                        ["/bin/bash", "-e", "-o", "pipefail", "-c", script], cwd=directory,
                        env={"PATH": str(commands), "HOME": str(directory), "PWD": str(directory),
                             "RUNNER_TEMP": str(runner), "GITHUB_TOKEN": "fixture-token",
                             "EXPECTED_RECEIPT": f"artifacts/{output}/rule-sources.json",
                             "EXPECTED_DISTDIR": str(distdir), "READER_EXIT": str(code)},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    self.assertEqual((repository / "fixture-archive").read_bytes(), b'cached fixture')
                    self.assertEqual((distdir / "pigz-2.8.tar.gz").read_bytes(), b'existing pigz')
                    self.assertEqual((distdir / "rules_cc-0.2.22.tar.gz").exists(), code == 0)
                    extra = "common --distdir=" + str(distdir) + "\n" if family == "mux-image" and not code else ""
                    self.assertEqual(rc.read_text(), prior + extra)
                    self.assertEqual(startup.read_text(), "common --distdir=" + str(distdir) + "\n")
                    if family == "sysmgr":
                        self.assertIn('--startup-arg="--bazelrc=$RUNNER_TEMP/sysmgr-github-inputs/downloads.bazelrc"', job)

    def test_sysmgr_explicit_rc_keeps_distdir_and_verified_mirrors(self):
        reader = r'''
            import os, sys
            from pathlib import Path
            options = dict(zip(sys.argv[1:][::2], sys.argv[1:][1::2]))
            assert options['--manifest'].endswith('/cosmos-4.0.2.json')
            assert options['--github-token-env'] == 'GITHUB_TOKEN'
            assert os.environ['GITHUB_TOKEN'] == 'fixture-token'
            cache = Path(os.environ['HOME']) / '.cache/bazel-repo'
            assert Path(options['--repository-cache']) == cache
            assert (cache / 'retained-input').read_bytes() == b'existing input'
            raise SystemExit(int(os.environ['READER_EXIT']))
        '''
        job = native_job('sysmgr')
        self.assertIn('--startup-arg="--bazelrc=$RUNNER_TEMP/sysmgr-github-inputs/downloads.bazelrc"', job)
        self.assertIn('            ci/source_mirrors.cfg\n', job)
        script = shell(steps(job)['Prepare verified GitHub build inputs'])
        original = (ROOT / 'tools/bazel/tests/fixtures/source-mirrors-3a7e32d0.cfg').read_bytes()
        for reader_exit, corrupt in ((23, False), (0, True), (0, False)):
            with self.subTest(reader_exit=reader_exit, corrupt=corrupt), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                commands = fixture(directory, reader)
                (commands / 'mkdir').symlink_to('/bin/mkdir')
                source = directory / '.bazel-inputs/github-reader/ci/source_mirrors.cfg'
                source.write_bytes(original + (b'corrupt' if corrupt else b''))
                runner = directory / 'runner-temp'
                inputs = runner / 'sysmgr-github-inputs'
                inputs.mkdir(parents=True)
                rc = inputs / 'downloads.bazelrc'
                rc.write_text('prior configuration\n')
                cache = directory / '.cache/bazel-repo'
                cache.mkdir(parents=True)
                (cache / 'retained-input').write_bytes(b'existing input')
                result = subprocess.run(
                    ['/bin/bash', '-e', '-o', 'pipefail', '-c', script], cwd=directory,
                    env={'PATH': str(commands), 'HOME': str(directory), 'PWD': str(directory),
                         'RUNNER_TEMP': str(runner), 'GITHUB_TOKEN': 'fixture-token',
                         'READER_EXIT': str(reader_exit)},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, reader_exit or int(corrupt), result.stdout + result.stderr)
                self.assertEqual((cache / 'retained-input').read_bytes(), b'existing input')
                self.assertFalse((directory / '.bazelrc.user').exists())
                config = inputs / 'source-mirrors.cfg'
                if reader_exit or corrupt:
                    self.assertFalse(config.exists())
                    self.assertEqual(rc.read_text(), 'prior configuration\n')
                else:
                    self.assertEqual(config.read_bytes(), original)
                    self.assertEqual(rc.read_text(), 'common --distdir=' + str(inputs / 'distdir') + '\n'
                                     'common --experimental_downloader_config=' + str(config) + '\n')

    def test_receipt_gate_rejects_malformed_or_private_evidence(self):
        previous = Path.cwd()
        for family, output in FAMILIES.items():
            script = shell(steps(native_job(family))["Validate public download receipts"]).splitlines()
            code = "\n".join(script[1:-1])
            filenames = ("rule-sources.json", "github-sources.json") if family == "sysmgr" else ("rule-sources.json",)
            for filename in filenames:
                for value in (None, b'{"files": [], "sources": []}',
                              b'{"authorization": "private-sentinel"}', b'{"malformed":'):
                    with self.subTest(family=family, filename=filename, value=value), tempfile.TemporaryDirectory() as temp:
                        directory = Path(temp)
                        root = directory / "artifacts" / output
                        root.mkdir(parents=True)
                        if value is not None:
                            (root / filename).write_bytes(value)
                        captured = io.StringIO()
                        try:
                            os.chdir(directory)
                            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured), \
                                    mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process denied")), \
                                    mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network denied")):
                                if value in (None, b'{"files": [], "sources": []}'):
                                    exec(compile(code, "<actual-receipt-step>", "exec"), {})
                                else:
                                    with self.assertRaises(ValueError):
                                        exec(compile(code, "<actual-receipt-step>", "exec"), {})
                            self.assertEqual(captured.getvalue(), "")
                        finally:
                            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
