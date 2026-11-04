"""Exercise the Teamd worker handoff without compiling image inputs."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from tools.bazel.ci import teamd_image as driver


class TeamdWorkerAdapterTest(unittest.TestCase):
    def test_actual_owned_branch_guards_preserve_other_workflow_events(self):
        workflow = (driver.ROOT / '.github/workflows/bazel-oci.yml').read_text()
        guards = re.findall(r'SOURCE_ONLY_NO_DEBS: \$\{\{ (.*?) \}\}', workflow)
        self.assertEqual(len(guards), 2)
        vs = workflow.split('\n  vs_container:\n', 1)[1].split('    if: >-\n', 1)[1].split('    runs-on:', 1)[0]
        def evaluate(expression, event, branch, same_repo=True, number=140, skip=True):
            github = SimpleNamespace(event_name=event, head_ref=branch, repository='securely1g/sonic-buildimage',
                ref='refs/heads/master', event=SimpleNamespace(pull_request=SimpleNamespace(number=number,
                    head=SimpleNamespace(repo=SimpleNamespace(full_name='securely1g/sonic-buildimage' if same_repo else 'other/sonic-buildimage')))))
            expression = ' '.join(expression.split()).replace('&&', ' and ').replace('||', ' or ')
            expression = re.sub(r'!(?!=)', 'not ', expression)
            return bool(eval(expression, {'__builtins__': {}}, {'github': github, 'inputs': SimpleNamespace(skip_vs=skip)}))
        for event, branch, same_repo, number, skip, source_only, full_vs in (
            ('pull_request', 'codex/teamd-worker-ci-adapter', True, 166, True, True, False),
            ('pull_request', 'codex/teamd-worker-ci-adapter', False, 166, True, False, False),
            ('pull_request', 'codex/image-environment', True, 1, True, True, False),
            ('pull_request', 'ordinary', True, 140, True, False, True),
            ('pull_request', 'ordinary', True, 5, True, False, False),
            ('pull_request', 'ordinary', False, 140, True, False, False),
            ('push', '', True, 0, True, False, True),
            ('workflow_dispatch', '', True, 0, True, False, False),
            ('workflow_dispatch', '', True, 0, False, False, True),
        ):
            with self.subTest(event=event, branch=branch, same_repo=same_repo, number=number, skip=skip):
                arguments = (event, branch, same_repo, number, skip)
                for guard in guards: self.assertEqual(evaluate(guard, *arguments), source_only)
                self.assertEqual(evaluate(vs, *arguments), full_vs)

    def test_real_launcher_preserves_catalog_arguments_and_worker_cache_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ci = root / 'tools/bazel/ci'; ci.mkdir(parents=True)
            for name in ('libteam_draft_bazel.sh', 'review_bazel.sh', 'review_bazel_rc.py'):
                shutil.copy2(driver.ROOT / 'tools/bazel/ci' / name, ci / name)
            shutil.copy2(driver.ROOT / 'tools/bazel/common.bazelrc', ci.parent / 'common.bazelrc')
            (root / '.bazelrc.user').write_text('common --disk_cache=/worker/disk\ncommon --repository_cache=/worker/repository\n')
            home = root / 'home'; home.mkdir()
            (home / '.bazelrc').write_text('common --registry=https://invalid.example\ncommon --distdir=/worker/downloads\n')
            fake = root / 'measured bazel'
            fake.write_text('#!' + sys.executable + '\n' +
                'import json,os,pathlib,sys\n' +
                'args=sys.argv[1:]\n' +
                'rc=[pathlib.Path(a.split("=",1)[1]).read_text() for a in args if a.startswith("--bazelrc=")]\n' +
                'pathlib.Path(os.environ["ADAPTER_RECORD"]).write_text(json.dumps({"argv":args,"rc":rc}))\n')
            fake.chmod(0o755)
            record = root / 'record.json'
            env = {**os.environ, 'HOME': str(home), 'BAZEL_REAL': str(fake), 'ADAPTER_RECORD': str(record),
                   'SONIC_BAZEL_REGISTRY_REVISION': 'f' * 40}
            arguments = ['--output_base=/worker/output', '--host_jvm_args=-Xmx3g', 'build',
                         '--jobs=1', '--local_resources=memory=4000', '--define=quoted value',
                         '//dockers/docker-teamd:docker-teamd.gz']
            subprocess.run([str(ci / 'libteam_draft_bazel.sh'), *arguments], cwd=root, env=env, check=True)
            result = json.loads(record.read_text())
            for argument in arguments: self.assertIn(argument, result['argv'])
            registry = [a for a in result['argv'] if a.startswith('--registry=')]
            self.assertEqual(registry, [
                '--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/e0bf141cea82764b18d15d7d2c8516cbf3e99290',
                '--registry=https://bcr.bazel.build'])
            self.assertEqual(len(result['rc']), 1)
            for option in ('--disk_cache=/worker/disk', '--repository_cache=/worker/repository', '--distdir=/worker/downloads'):
                self.assertIn(option, result['rc'][0])
            self.assertNotIn('invalid.example', result['rc'][0])
            record.unlink()
            rejected = subprocess.run([str(ci / 'libteam_draft_bazel.sh'), 'build', '--registry=https://invalid.example'],
                                      cwd=root, env=env, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0); self.assertFalse(record.exists())
            subprocess.run([str(ci / 'libteam_draft_bazel.sh'), '--version'], cwd=root, env=env, check=True)
            self.assertEqual(json.loads(record.read_text()), {'argv': ['--version'], 'rc': []})

    def test_actual_git_fence_keeps_reserved_receipts_and_rejects_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = root / 'source.py'; source.write_text('original\n')
            subprocess.run(['git', 'add', 'source.py'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'baseline'], cwd=root, check=True)
            artifact = root / 'artifacts/teamd'; artifact.mkdir(parents=True)
            prior = root / 'artifacts/archive'; prior.mkdir()
            (prior / 'receipt.json').write_text('{}')
            with mock.patch.object(driver, 'ROOT', root):
                driver.clean_source(artifact)
                for directory in (root, root / 'tools', root.parent):
                    with self.assertRaises(ValueError): driver.clean_source(directory)
                rogue = root / 'new_source.py'; rogue.write_text('unexpected')
                with self.assertRaises(ValueError): driver.clean_source(artifact)
                rogue.unlink(); source.write_text('changed\n')
                with self.assertRaises(subprocess.CalledProcessError): driver.clean_source(artifact)
                source.write_text('original\n'); shutil.rmtree(root / 'artifacts')
                (root / 'artifacts').write_text('bare file')
                with self.assertRaises(ValueError): driver.clean_source(root.parent / (root.name + '-evidence'))


if __name__ == '__main__':
    unittest.main()
