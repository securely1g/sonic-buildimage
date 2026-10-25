#!/usr/bin/env python3
"""Check declared P4RT schemas and caches through the existing DEB recipe."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class P4rtSchemaCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'Makefile').write_bytes((ROOT / 'src/sonic-p4rt/Makefile').read_bytes())
        (self.root / 'sonic-pins').mkdir()
        self.dest = self.root / 'debs'
        self.dest.mkdir()
        self.calls = self.root / 'calls.jsonl'
        self.runtime = self.dest / 'sonic-p4rt_0.0.1_amd64.deb'
        self.debug = self.dest / 'sonic-p4rt-dbgsym_0.0.1_amd64.deb'
        self.schema = self.root / 'installed/cfg_schema.h'
        self.schema.parent.mkdir()
        self.schema.write_text('installed schema')
        self.schema_repo = self.root / 'schema-repository'
        self.cache = self.root / 'shared-cache'
        self.cache.mkdir()
        self.cache_marker = self.cache / 'retained-download'
        self.cache_marker.write_text('retain downloaded inputs')
        fake = self.root / 'bazel'
        fake.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
import sys
args = sys.argv[1:]
with Path(os.environ['P4RT_TEST_CALLS']).open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if args[0] == 'build':
    assert args[-2:] == ['//p4rt_app:p4rt_deb', '//p4rt_app:p4rt_dbg_deb']
    output = Path('bazel-bin/p4rt_app')
    output.mkdir(parents=True, exist_ok=True)
    for name in ('sonic-p4rt_0.0.1_amd64.deb', 'sonic-p4rt-dbgsym_0.0.1_amd64.deb'):
        (output / name).write_text('existing DEB recipe output')
elif args[0] == 'test':
    assert args[-1] == '//p4rt_app/...'
elif args[0] != 'shutdown':
    raise SystemExit('Unexpected Bazel operation')
''')
        fake.chmod(0o755)

    def make(self):
        environment = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                           P4RT_TEST_CALLS=str(self.calls))
        return subprocess.run([
            'make', '--no-print-directory', '-j2', 'BAZEL_OPTS=',
            f'CFG_SCHEMA_H={self.schema}', f'CFG_SCHEMA_REPO={self.schema_repo}',
            f'BAZEL_CACHE={self.cache}', f'DEST={self.dest}',
            f'SONIC_P4RT={self.runtime.name}', f'SONIC_P4RT_DBG={self.debug.name}',
            str(self.runtime), str(self.debug),
        ], cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)

    def recorded(self, operation):
        return [args for args in map(json.loads, self.calls.read_text().splitlines()) if args[0] == operation]

    def test_schema_and_caches_are_explicit_and_noop_preserves_outputs(self):
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.schema_repo / 'cfg_schema.h').read_bytes(), self.schema.read_bytes())
        for operation in ('build', 'test'):
            calls = self.recorded(operation)
            self.assertEqual(len(calls), 1)
            for args in calls:
                self.assertIn('--override_repository=swss_cfg_schema=' + str(self.schema_repo), args)
                self.assertIn('--@sonic_swss_common//tools/bazel:cfg_schema=@swss_cfg_schema//:cfg_schema.h', args)
                self.assertIn('--repository_cache=' + str(self.cache / 'repository_cache'), args)
                self.assertIn('--disk_cache=' + str(self.cache / 'disk_cache'), args)
                self.assertFalse(any(arg.startswith('--output_user_root') for arg in args))
        self.assertEqual(self.runtime.read_text(), 'existing DEB recipe output')
        self.assertEqual(self.debug.read_text(), 'existing DEB recipe output')
        self.assertTrue(self.cache_marker.exists())
        before = [path.stat().st_mtime_ns for path in (self.runtime, self.debug)]
        call_count = len(self.calls.read_text().splitlines())
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, [path.stat().st_mtime_ns for path in (self.runtime, self.debug)])
        self.assertEqual(call_count, len(self.calls.read_text().splitlines()))

    def test_missing_schema_stops_before_bazel(self):
        self.schema.unlink()
        result = self.make()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not found: is libswsscommon-dev installed?', result.stderr)
        self.assertFalse(self.calls.exists())


class P4rtLegacyDockerTest(unittest.TestCase):
    def test_make_registers_existing_runtime_and_debug_debs(self):
        makefile = '''SRC_PATH = src
DOCKERS_PATH = dockers
CONFIGURED_ARCH = amd64
LIBSWSSCOMMON = libswsscommon.deb
LIBSWSSCOMMON_DEV = libswsscommon-dev.deb
LIBSWSSCOMMON_DBG = libswsscommon-dbg.deb
LIBSAIREDIS_DBG = libsairedis-dbg.deb
include rules/functions
include rules/p4rt.mk
include rules/docker-p4rt.mk
include rules/docker-p4rt.dep
.PHONY: print
print:
\t@printf '%s\\n' '$(SONIC_MAKE_FILES)' '$(SONIC_MAKE_DEBS)' '$($(DOCKER_P4RT)_DEPENDS)' '$($(DOCKER_P4RT)_DBG_DEPENDS)' '$($(DOCKER_P4RT)_FILES)'
'''
        output = subprocess.check_output(['make', '--no-print-directory', '-s', '-f', '-', 'print'],
                                         input=makefile, text=True, cwd=ROOT).splitlines()
        self.assertEqual(output[:3], ['', 'sonic-p4rt_0.0.1_amd64.deb', 'sonic-p4rt_0.0.1_amd64.deb'])
        self.assertEqual(output[3].split(), ['sonic-p4rt-dbgsym_0.0.1_amd64.deb',
                                          'libswsscommon-dbg.deb', 'libsairedis-dbg.deb'])
        self.assertEqual(output[4], '')

    def test_runtime_template_installs_debs(self):
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
        environment = Environment(loader=FileSystemLoader(ROOT), undefined=StrictUndefined)
        template = environment.get_template('dockers/docker-sonic-p4rt/Dockerfile.j2')
        rendered = template.render(DOCKER_USERNAME='test', DOCKER_USERTAG='latest',
                                   docker_sonic_p4rt_debs='sonic-p4rt_0.0.1_amd64.deb')
        self.assertIn('debs/sonic-p4rt_0.0.1_amd64.deb', rendered)
        self.assertIn('dpkg -i', rendered)
        self.assertNotIn('ADD files/', rendered)

    def test_debug_template_keeps_existing_deb_interface(self):
        from jinja2 import Environment, StrictUndefined
        template = subprocess.check_output([
            'bash', str(ROOT / 'build_debug_docker_j2.sh'), 'p4rt:latest', 'debug_debs', 'image_debs',
        ], text=True)
        rendered = Environment(undefined=StrictUndefined).from_string(template).render(
            debug_debs='sonic-p4rt-dbgsym_0.0.1_amd64.deb', image_debs='libc6-dbg')
        self.assertIn('debs/sonic-p4rt-dbgsym_0.0.1_amd64.deb', rendered)
        self.assertIn('libc6-dbg', rendered)
        self.assertIn('rsync -axAX', rendered)
        self.assertNotIn('ADD files/', rendered)


if __name__ == '__main__':
    unittest.main()
