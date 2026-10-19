#!/usr/bin/env python3
"""Check P4RT TAR handoff, partial-output recovery and container payload wiring."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.verify_p4rt_debug import extract_member, verify_inventory


class P4rtMakeTarTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'Makefile').write_bytes((ROOT / 'src/sonic-p4rt/Makefile').read_bytes())
        (self.root / 'sonic-pins').mkdir()
        (self.root / 'cfg_schema.h').write_text('// fixture installed schema\n')
        self.cache_marker = self.root / 'sonic-pins/bazel-kept-cache'
        self.cache_marker.write_text('retain existing outputs')
        self.dest = self.root / 'files'
        self.calls = self.root / 'calls.jsonl'
        self.runtime = self.dest / 'sonic-p4rt_0.0.1_amd64.tar.bz2'
        self.debug = self.dest / 'sonic-p4rt-dbgsym_0.0.1_amd64.tar.bz2'
        fake = self.root / 'bazel'
        fake.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
import sys
args = sys.argv[1:]
if os.environ.get('P4RT_STARTUP_FLAG'):
    assert args[0] == os.environ['P4RT_STARTUP_FLAG']
    args = args[1:]
with Path(os.environ['P4RT_TEST_CALLS']).open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
assert not any(arg.endswith((':p4rt_deb', ':p4rt_dbg_deb')) for arg in args)
if args[0] == 'build':
    target = args[-1].split(':')[1]
    assert target in ('p4rt_binaries', 'p4rt_debug')
    output = Path('bazel-bin/p4rt_app') / (target + '.tar.bz2')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(('upstream ' + target).encode())
elif args[0] == 'aquery':
    print(json.dumps({'actions': [{'outputIds': [1]}],
                      'artifacts': [{'id': 1, 'pathFragmentId': 1}],
                      'pathFragments': [{'id': 1, 'label': 'unexpected.deb' if os.environ.get('P4RT_DEB_ACTION') else 'payload.tar.bz2'}]}))
elif args[0] == 'query':
    if os.environ.get('P4RT_EMPTY_TESTS'):
        if os.environ['P4RT_EMPTY_TESTS'] == 'partial':
            print('//p4rt_app:first_test')
        raise SystemExit(1)
    print('//p4rt_app:first_test\\n//p4rt_app:second_test')
elif args[0] == 'test':
    assert '--build_tests_only' in args
    assert args[-2:] == ['//p4rt_app:first_test', '//p4rt_app:second_test']
elif args[0] != 'shutdown':
    raise SystemExit('Unexpected Bazel operation')
''')
        fake.chmod(0o755)

    def make(self, *targets, empty_tests=False, deb_action=False, startup=False):
        startup_flag = '--output_user_root=/cache' if startup else ''
        environment = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                           P4RT_TEST_CALLS=str(self.calls), P4RT_EMPTY_TESTS=str(empty_tests) if empty_tests else '',
                           P4RT_DEB_ACTION='1' if deb_action else '', P4RT_STARTUP_FLAG=startup_flag)
        result = subprocess.run([
            'make', '--no-print-directory', '-j2', 'BAZEL_OPTS=' + startup_flag, 'BAZEL_BUILD_OPTS=',
            f'BAZEL_ACTION_AUDIT={ROOT}/tools/bazel/ci/bazel_commands.py',
            f'CFG_SCHEMA_H={self.root}/cfg_schema.h', f'CFG_SCHEMA_REPO={self.root}/schema-repository',
            f'DEST={self.dest}', f'SONIC_P4RT={self.runtime.name}', f'SONIC_P4RT_DBG={self.debug.name}',
            *(str(target) for target in (targets or (self.runtime, self.debug))),
        ], cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
        return result

    def recorded(self, operation):
        return [args for args in map(json.loads, self.calls.read_text().splitlines()) if args[0] == operation]

    def test_parallel_outputs_and_missing_archive_recovery_preserve_cache(self):
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runtime.read_bytes(), b'upstream p4rt_binaries')
        self.assertEqual(self.debug.read_bytes(), b'upstream p4rt_debug')
        self.assertEqual([args[-1] for args in self.recorded('build')],
                         ['//p4rt_app:p4rt_binaries', '//p4rt_app:p4rt_debug'])
        self.assertEqual(len(self.recorded('test')), 1)
        self.debug.unlink()
        result = self.make(self.debug)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded('build')[-1][-1], '//p4rt_app:p4rt_debug')
        self.assertEqual(len(self.recorded('build')), 3)
        self.assertEqual(len(self.recorded('test')), 1)
        self.assertEqual(self.cache_marker.read_text(), 'retain existing outputs')
        self.runtime.unlink()
        result = self.make(self.debug)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([args[-1] for args in self.recorded('build')][-2:],
                         ['//p4rt_app:p4rt_binaries', '//p4rt_app:p4rt_debug'])
        self.assertEqual(len(self.recorded('test')), 2)

    def test_failed_or_empty_test_selection_does_not_publish_runtime(self):
        for mode in ('empty', 'partial'):
            with self.subTest(mode=mode):
                result = self.make(self.runtime, empty_tests=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.runtime.exists())
                self.assertEqual(self.recorded('test'), [])
                self.assertTrue(self.cache_marker.exists())

    def test_deb_in_selected_action_graph_stops_before_build(self):
        result = self.make(self.runtime, deb_action=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DEB or packaging wrapper', result.stderr)
        self.assertEqual(self.recorded('build'), [])
        self.assertEqual(self.recorded('test'), [])
        self.assertFalse(self.runtime.exists())

    def test_audit_build_and_test_use_the_same_cached_output_root(self):
        result = self.make(startup=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.recorded('aquery')), 2)


class TarPayloadTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / 'payload.tar.bz2'

    def archive(self, *members):
        with tarfile.open(self.package, 'w:bz2') as archive:
            for name, kind, mode in members:
                member = tarfile.TarInfo(name)
                member.mode, member.type = mode, kind
                if kind == tarfile.REGTYPE:
                    member.size = 7
                archive.addfile(member, io.BytesIO(b'payload') if member.isfile() else None)

    def test_copies_regular_owner_payload_without_archive_extraction(self):
        self.archive(('./usr/local/bin/p4rt', tarfile.REGTYPE, 0o755))
        self.assertEqual(verify_inventory(self.package, {'usr/local/bin/p4rt'}), ['usr/local/bin/p4rt'])
        destination = self.root / 'p4rt'
        extract_member(self.package, 'usr/local/bin/p4rt', destination)
        self.assertEqual(destination.read_bytes(), b'payload')

    def test_rejects_missing_duplicate_link_and_wrong_mode(self):
        for members in [(), (('usr/local/bin/p4rt', tarfile.REGTYPE, 0o644),),
                        (('usr/local/bin/p4rt', tarfile.SYMTYPE, 0o755),),
                        (('../p4rt', tarfile.REGTYPE, 0o755),),
                        (('usr/local/bin/p4rt', tarfile.REGTYPE, 0o755),) * 2]:
            with self.subTest(members=members):
                self.archive(*members)
                with self.assertRaises(ValueError):
                    verify_inventory(self.package, {'usr/local/bin/p4rt'})


class DebugDockerTarTest(unittest.TestCase):
    def test_owner_metadata_keeps_runtime_dependencies_and_debug_cache_inputs(self):
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
\t@printf '%s\\n' '$(SONIC_MAKE_FILES)' '$(SONIC_MAKE_DEBS)' '$($(DOCKER_P4RT)_DEPENDS)' '$($(DOCKER_P4RT)_APT_PACKAGES)' '$($(DOCKER_P4RT_DBG)_FILES)' '$($(SONIC_P4RT_DBG)_AFTER_FILES)'
'''
        output = subprocess.check_output(['make', '--no-print-directory', '-s', '-f', '-', 'print'],
                                         input=makefile, text=True, cwd=ROOT).splitlines()
        runtime = 'sonic-p4rt_0.0.1_amd64.tar.bz2'
        debug = 'sonic-p4rt-dbgsym_0.0.1_amd64.tar.bz2'
        self.assertEqual(output[0].split(), [runtime, debug])
        self.assertEqual(output[1], '')
        self.assertEqual(output[2], 'libswsscommon.deb')
        self.assertEqual(output[3], 'libgmpxx4ldbl')
        self.assertEqual(output[4].split(), [runtime, debug])
        self.assertEqual(output[5], runtime)

    def render(self, tars=None):
        from jinja2 import Environment, StrictUndefined
        arguments = ['bash', str(ROOT / 'build_debug_docker_j2.sh'), 'p4rt:latest', 'debug_debs', 'image_debs']
        values = {'debug_debs': 'libswsscommon-dbg.deb', 'image_debs': 'libc6-dbg'}
        if tars is not None:
            arguments.append('debug_tars')
            values['debug_tars'] = tars
        template = subprocess.check_output(arguments, text=True)
        return Environment(undefined=StrictUndefined).from_string(template).render(**values)

    def test_optional_tar_keeps_existing_debug_debs_and_final_stage(self):
        rendered = self.render('sonic-p4rt-dbgsym.tar.bz2')
        self.assertIn('ADD files/sonic-p4rt-dbgsym.tar.bz2 /', rendered)
        self.assertIn('debs/libswsscommon-dbg.deb', rendered)
        self.assertIn('libc6-dbg', rendered)
        self.assertLess(rendered.index('ADD files/'), rendered.rindex('FROM $BASE'))
        self.assertIn('rsync -axAX', rendered)

    def test_existing_three_argument_and_empty_tar_callers_keep_working(self):
        for tars in (None, ''):
            with self.subTest(tars=tars):
                rendered = self.render(tars)
                self.assertNotIn('ADD files/', rendered)
                self.assertIn('debs/libswsscommon-dbg.deb', rendered)


if __name__ == '__main__':
    unittest.main()
