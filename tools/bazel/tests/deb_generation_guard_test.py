#!/usr/bin/env python3
"""Exercise extracted guard prefixes and an inert standalone Make admission block."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / 'slave.mk').read_text()
BUILD_DEBIAN = (ROOT / 'build_debian.sh').read_text()
BOUNDARIES = {
    'MAKE_DEBS': ('$(addprefix $(DEBS_PATH)/, $(SONIC_MAKE_DEBS)) :', '# Remove target to force rebuild'),
    'DPKG_DEBS': ('$(addprefix $(DEBS_PATH)/, $(SONIC_DPKG_DEBS)) :', '# Remove old build logs if they exist'),
    'STDEB': ('$(addprefix $(PYTHON_DEBS_PATH)/, $(SONIC_PYTHON_STDEB_DEBS)) :', '# Apply series of patches if exist'),
}
RECORDS = []
ADMISSION_RECORDS = []
BOUNDARY_RECORDS = []
HOST_RECORDS = []
HOST_MAKE_RECORDS = []

def prefix(source, kind, loaded, policy, target):
    marker, first_producer = BOUNDARIES[kind]
    start = source.index(marker)
    load = source.index('$(call LOAD_CACHE,$*,$@)', start)
    start = source.index("if [ -z '$($*_CACHE_LOADED)' ] ; then", load)
    end = source.index(first_producer, start)
    text = source[start:end]
    # Only three fixed Make values are substituted. This is not a Make parser.
    text = text.replace('$($*_CACHE_LOADED)', loaded).replace('$(SONIC_DEB_BUILD_POLICY)', policy).replace('$@', target)
    assert '$' not in text and '`' not in text
    return text + "printf '%s\\n' producer >> events\nfi\nprintf '%s\\n' continued >> events\n"

def run_case(kind, loaded, policy):
    with tempfile.TemporaryDirectory(prefix='generic-deb-guard-') as temporary:
        directory = Path(temporary)
        target = 'existing-package.input'
        path = directory / target
        path.write_bytes(b'plain text existing-input sentinel, not a Debian archive\n')
        before = path.read_bytes(), path.stat().st_mtime_ns
        command = prefix(SOURCE, kind, loaded, policy, target)
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-e', '-c', command],
                                cwd=directory, env={'PATH': '/nonexistent', 'LC_ALL': 'C'},
                                text=True, capture_output=True, timeout=10, check=False)
        events = (directory / 'events').read_text().splitlines() if (directory / 'events').exists() else []
        unchanged = before == (path.read_bytes(), path.stat().st_mtime_ns)
        record = {'kind': kind, 'cache_status_fixture': loaded, 'policy': policy,
                  'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                  'events': events, 'existing_input_unchanged': unchanged, 'reaped': True,
                  'shell_prefix_sha256': hashlib.sha256(command.encode()).hexdigest()}
        RECORDS.append(record)
        return record

class GenericPackageGuardTests(unittest.TestCase):
    def test_policy_admission_in_standalone_make(self):
        start = SOURCE.index('SONIC_DEB_BUILD_POLICY ?= build\n')
        end = SOURCE.index(BOUNDARIES['MAKE_DEBS'][0], start)
        block = SOURCE[start:end].strip() + '\n'
        # Only the policy conditional is parsed, never the repository Makefile.
        for line in block.splitlines():
            self.assertTrue(line == 'SONIC_DEB_BUILD_POLICY ?= build'
                            or line == 'export SONIC_DEB_BUILD_POLICY'
                            or line == 'endif'
                            or line.startswith(('ifneq (', 'ifeq (', '$(error ')))
        self.assertNotIn('$(shell', block)
        fixture = 'SONIC_BAZEL_KERNEL_PREPARE ?= ensure\n' + block + '.PHONY: all\nall:; @:\n'
        cases = (
            ('default-build', [], None),
            ('explicit-build', ['SONIC_DEB_BUILD_POLICY=build'], None),
            ('reuse-verify', ['SONIC_DEB_BUILD_POLICY=reuse-only', 'SONIC_BAZEL_KERNEL_PREPARE=verify'], None),
            ('reuse-missing-kernel', ['SONIC_DEB_BUILD_POLICY=reuse-only'], 'requires SONIC_BAZEL_KERNEL_PREPARE=verify'),
            ('reuse-ensure', ['SONIC_DEB_BUILD_POLICY=reuse-only', 'SONIC_BAZEL_KERNEL_PREPARE=ensure'], 'requires SONIC_BAZEL_KERNEL_PREPARE=verify'),
            ('invalid-policy', ['SONIC_DEB_BUILD_POLICY=invalid'], 'must be build or reuse-only'),
        )
        for name, assignments, error in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix='deb-policy-make-') as temporary:
                directory = Path(temporary)
                makefile = directory / 'Makefile'
                makefile.write_text(fixture)
                argv = ['/usr/bin/make', '--no-print-directory', '-rR', '-f', 'Makefile', 'all'] + assignments
                result = subprocess.run(argv, cwd=directory,
                                        env={'PATH': '/nonexistent', 'LC_ALL': 'C'},
                                        text=True, capture_output=True, timeout=10, check=False)
                ADMISSION_RECORDS.append({'case': name, 'argv': argv, 'returncode': result.returncode,
                                          'stdout': result.stdout, 'stderr': result.stderr, 'reaped': True,
                                          'makefile_sha256': hashlib.sha256(fixture.encode()).hexdigest()})
                self.assertEqual(result.returncode, 0 if error is None else 2)
                self.assertEqual(result.stdout, '')
                if error is None:
                    self.assertEqual(result.stderr, '')
                else:
                    self.assertIn(error, result.stderr)
                self.assertEqual(makefile.read_text(), fixture)
                self.assertEqual(list(directory.iterdir()), [makefile])

    def test_reuse_misses_stop_before_producer_and_preserve_existing_input(self):
        for kind in BOUNDARIES:
            with self.subTest(kind=kind):
                row = run_case(kind, '', 'reuse-only')
                self.assertEqual(row['returncode'], 1)
                self.assertEqual(row['events'], [])
                self.assertIn('Cannot generate existing-package.input', row['stderr'])
                self.assertTrue(row['existing_input_unchanged'])

    def test_cache_hit_fixture_continues_without_producer(self):
        for kind in BOUNDARIES:
            with self.subTest(kind=kind):
                row = run_case(kind, 'Yes', 'reuse-only')
                self.assertEqual(row['returncode'], 0)
                self.assertEqual(row['events'], ['continued'])
                self.assertTrue(row['existing_input_unchanged'])

    def test_default_and_explicit_build_retain_miss_behavior(self):
        default = next(line.split('?=', 1)[1].strip() for line in SOURCE.splitlines()
                       if line.startswith('SONIC_DEB_BUILD_POLICY ?='))
        for kind in BOUNDARIES:
            for policy in (default, 'build'):
                with self.subTest(kind=kind, policy=policy):
                    row = run_case(kind, '', policy)
                    self.assertEqual(row['returncode'], 0)
                    self.assertEqual(row['events'], ['producer', 'continued'])
                    self.assertTrue(row['existing_input_unchanged'])


    def test_optional_build_hook_admission(self):
        function = BUILD_DEBIAN.index('run_organization_build_hook()\n')
        start = BUILD_DEBIAN.index('    if [ -f "$ORGANIZATION_BUILD_HOOK" ]; then\n', function)
        end = BUILD_DEBIAN.index('        . "$ORGANIZATION_BUILD_HOOK" "$1"\n', start)
        prefix = BUILD_DEBIAN[start:end]
        cases = (('strict-absent', 'reuse-only', False, 0, ['continued']),
                 ('strict-present', 'reuse-only', True, 1, []),
                 ('default-present', None, True, 0, ['producer', 'continued']))
        for name, policy, present, code, events in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix='deb-hook-guard-') as temporary:
                directory = Path(temporary)
                hook = directory / 'hook.input'
                if present:
                    hook.write_text('plain text hook fixture; never sourced\n')
                command = ('unset SONIC_DEB_BUILD_POLICY\n' if policy is None else 'SONIC_DEB_BUILD_POLICY=' + policy + '\n')
                command += 'ORGANIZATION_BUILD_HOOK=hook.input\n' + prefix
                command += "printf '%s\\n' producer >> events\nfi\nprintf '%s\\n' continued >> events\n"
                result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-e', '-c', command],
                                        cwd=directory, env={'PATH': '/nonexistent', 'LC_ALL': 'C'},
                                        text=True, capture_output=True, timeout=10, check=False)
                actual = (directory / 'events').read_text().splitlines() if (directory / 'events').exists() else []
                BOUNDARY_RECORDS.append({'boundary': 'optional-build-hook', 'case': name, 'returncode': result.returncode,
                                         'events': actual, 'stderr': result.stderr, 'reaped': True,
                                         'prefix_sha256': hashlib.sha256(prefix.encode()).hexdigest()})
                self.assertEqual(result.returncode, code)
                self.assertEqual(actual, events)
                self.assertEqual(result.stdout, '')
                if code:
                    self.assertIn('Cannot source hook.input', result.stderr)
                else:
                    self.assertEqual(result.stderr, '')
                if present:
                    self.assertEqual(hook.read_text(), 'plain text hook fixture; never sourced\n')

    def test_optional_template_admission_preserves_prior_render(self):
        loop = SOURCE.index('for organization_template in files/build_templates/sonic_debian_extension_organization_packages.j2 ')
        start = SOURCE.rfind('if [ "$(SONIC_DEB_BUILD_POLICY)" = "reuse-only" ]; then', 0, loop)
        end = SOURCE.index('\t\tj2 files/build_templates/sonic_debian_extension.j2 > sonic_debian_extension.sh\n', loop)
        self.assertGreaterEqual(start, 0)
        prefix = SOURCE[start:end]
        packages = 'files/build_templates/sonic_debian_extension_organization_packages.j2'
        dockers = 'files/build_templates/sonic_debian_extension_organization_docker_images.j2'
        cases = (('strict-absent', 'reuse-only', None, False, 0),
                 ('strict-packages', 'reuse-only', packages, False, 1),
                 ('strict-dockers', 'reuse-only', dockers, False, 1),
                 ('strict-dangling-link', 'reuse-only', packages, True, 1),
                 ('default-present', 'build', packages, False, 0))
        for name, policy, fixture, symlink, code in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix='deb-template-guard-') as temporary:
                directory = Path(temporary)
                output = directory / 'sonic_debian_extension.sh'
                output.write_text('prior render sentinel\n')
                before = output.read_bytes(), output.stat().st_mtime_ns
                if fixture:
                    path = directory / fixture
                    path.parent.mkdir(parents=True)
                    if symlink:
                        path.symlink_to('missing-template')
                    else:
                        path.write_text('plain text template fixture; never rendered\n')
                command = prefix.replace('$(SONIC_DEB_BUILD_POLICY)', policy).replace('$$organization_template', '$organization_template')
                self.assertNotIn('$(', command)
                self.assertNotIn('$$', command)
                command += "printf '%s\\n' producer >> events\nprintf '%s\\n' rendered > sonic_debian_extension.sh\n"
                result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-e', '-c', command],
                                        cwd=directory, env={'PATH': '/nonexistent', 'LC_ALL': 'C'},
                                        text=True, capture_output=True, timeout=10, check=False)
                actual = (directory / 'events').read_text().splitlines() if (directory / 'events').exists() else []
                unchanged = before == (output.read_bytes(), output.stat().st_mtime_ns)
                BOUNDARY_RECORDS.append({'boundary': 'optional-template', 'case': name, 'returncode': result.returncode,
                                         'events': actual, 'stderr': result.stderr, 'prior_render_unchanged': unchanged,
                                         'reaped': True, 'prefix_sha256': hashlib.sha256(prefix.encode()).hexdigest()})
                self.assertEqual(result.returncode, code)
                self.assertEqual(actual, [] if code else ['producer'])
                self.assertEqual(result.stdout, '')
                if code:
                    self.assertTrue(unchanged)
                    self.assertIn('Cannot include ' + fixture, result.stderr)
                else:
                    self.assertEqual(output.read_text(), 'rendered\n')
                    self.assertEqual(result.stderr, '')

    def test_secure_signing_admission(self):
        start = BUILD_DEBIAN.index("if [[ $SECURE_UPGRADE_MODE == 'dev' || $SECURE_UPGRADE_MODE == \"prod\" ]]; then\n")
        end = BUILD_DEBIAN.index('    echo "Secure Boot support build stage: Starting .."\n', start)
        prefix = BUILD_DEBIAN[start:end]
        cases = (('strict-no-sign', 'reuse-only', 'no_sign', 0, ['continued']),
                 ('strict-dev', 'reuse-only', 'dev', 1, []),
                 ('strict-prod', 'reuse-only', 'prod', 1, []),
                 ('default-no-sign', None, 'no_sign', 0, ['continued']),
                 ('default-dev', None, 'dev', 0, ['producer', 'continued']),
                 ('default-prod', None, 'prod', 0, ['producer', 'continued']))
        for name, policy, mode, code, events in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix='deb-signing-guard-') as temporary:
                directory = Path(temporary)
                sentinel = directory / 'existing-package.input'
                sentinel.write_text('plain text existing package sentinel\n')
                before = sentinel.read_bytes(), sentinel.stat().st_mtime_ns
                command = ('unset SONIC_DEB_BUILD_POLICY\n' if policy is None else 'SONIC_DEB_BUILD_POLICY=' + policy + '\n')
                command += 'SECURE_UPGRADE_MODE=' + mode + '\n' + prefix
                command += "printf '%s\\n' producer >> events\nfi\nprintf '%s\\n' continued >> events\n"
                result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-e', '-c', command],
                                        cwd=directory, env={'PATH': '/nonexistent', 'LC_ALL': 'C'},
                                        text=True, capture_output=True, timeout=10, check=False)
                actual = (directory / 'events').read_text().splitlines() if (directory / 'events').exists() else []
                unchanged = before == (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
                BOUNDARY_RECORDS.append({'boundary': 'secure-signing', 'case': name, 'returncode': result.returncode,
                                         'events': actual, 'stderr': result.stderr, 'existing_input_unchanged': unchanged,
                                         'reaped': True, 'prefix_sha256': hashlib.sha256(prefix.encode()).hexdigest()})
                self.assertEqual(result.returncode, code)
                self.assertEqual(actual, events)
                self.assertTrue(unchanged)
                self.assertEqual(result.stdout, '')
                if code:
                    self.assertIn('Cannot prepare secure boot signing in mode ' + mode, result.stderr)
                else:
                    self.assertEqual(result.stderr, '')


    def test_host_build_hooks_miss_admission(self):
        source = (ROOT / 'src/sonic-build-hooks/Makefile').read_text()
        marker = '$(SONIC_BUILD_HOOKS_TARGET): $(DEPENDS)\n'
        start = source.index(marker) + len(marker)
        end = source.index('\t@rm -rf ', start)
        prefix = source[start:end]
        self.assertTrue(prefix.startswith('\t@case "$(SONIC_DEB_BUILD_POLICY)" in '))
        cases = (('default-build', '', 0), ('explicit-build', 'build', 0),
                 ('reuse-only', 'reuse-only', 1), ('invalid-policy', 'invalid', 1))
        for name, policy, code in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix='host-deb-guard-') as temporary:
                directory = Path(temporary)
                target = 'existing-package.input'
                artifact = directory / target
                artifact.write_text('plain text existing-input sentinel, not a Debian archive\n')
                before = artifact.read_bytes(), artifact.stat().st_mtime_ns
                # Only the actual first recipe prefix is executed; all producer lines are excluded.
                command = prefix.replace('\t@', '\t').replace('$(SONIC_DEB_BUILD_POLICY)', policy).replace('$@', target)
                self.assertNotIn('$', command)
                self.assertNotIn('`', command)
                command += "printf '%s\\n' producer >> events\n"
                result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-e', '-c', command],
                                        cwd=directory, env={'PATH': '/nonexistent', 'LC_ALL': 'C'},
                                        text=True, capture_output=True, timeout=10, check=False)
                events = (directory / 'events').read_text().splitlines() if (directory / 'events').exists() else []
                unchanged = before == (artifact.read_bytes(), artifact.stat().st_mtime_ns)
                HOST_RECORDS.append({'case': name, 'policy': policy, 'returncode': result.returncode,
                                     'stdout': result.stdout, 'stderr': result.stderr, 'events': events,
                                     'existing_input_unchanged': unchanged, 'reaped': True,
                                     'shell_prefix_sha256': hashlib.sha256(command.encode()).hexdigest()})
                self.assertEqual(result.returncode, code)
                self.assertEqual(events, [] if code else ['producer'])
                self.assertEqual(result.stdout, '')
                self.assertTrue(unchanged)
                if policy == 'reuse-only':
                    self.assertIn('Cannot generate ' + target, result.stderr)
                elif policy == 'invalid':
                    self.assertIn('must be build or reuse-only', result.stderr)
                else:
                    self.assertEqual(result.stderr, '')


    def test_host_verify_preserves_recursive_make_contract(self):
        source = (ROOT / 'src/sonic-build-hooks/Makefile').read_text()
        work = (ROOT / 'Makefile.work').read_text()
        outer = (ROOT / 'Makefile').read_text()
        marker = '$(SONIC_BUILD_HOOKS_TARGET): $(DEPENDS)\n'
        start = source.index(marker) + len(marker)
        prefix = source[start:source.index('\t@rm -rf ', start)]
        verify = source[source.index('.PHONY: verify\n'):]
        self.assertTrue(verify.startswith('.PHONY: verify\nverify: $(DEPENDS)\n'))
        self.assertNotIn('$(shell', verify)
        recursive = next(line for line in work.splitlines()
                         if 'TRUSTED_GPG_URLS=$(TRUSTED_GPG_URLS) $(MAKE)' in line)
        self.assertIn('$(if $(filter reuse-only,$(SONIC_DEB_BUILD_POLICY)),verify,all)', recursive)
        recursive = recursive.replace('src/sonic-build-hooks', 'child')
        force = next(line for line in work.splitlines() if line == 'MAKEFLAGS += -B')
        forwarding = next(line for line in outer.splitlines()
                          if line == 'override SONIC_OVERRIDE_BUILD_VARS += $(SONIC_BUILD_VARS)')
        parent = ('SHELL := /bin/bash\n.SHELLFLAGS := -e -c\nMAKE := /usr/bin/make\nQ := @\n'
                  + force + '\n' + forwarding + '\nexport SONIC_OVERRIDE_BUILD_VARS\n'
                  + '.PHONY: all\nall:\n' + recursive + '\n')
        child = ('SHELL := /bin/bash\n.SHELLFLAGS := -e -c\n'
                 + 'SONIC_BUILD_HOOKS_TARGET = artifact.input\nDEPENDS = source.input\n'
                 + '$(info CHILD_POLICY=$(SONIC_DEB_BUILD_POLICY))\n'
                 + '$(info CHILD_PROPAGATED=$(PROPAGATED))\n'
                 + '$(info CHILD_INNER=$(SONIC_OVERRIDE_BUILD_VARS))\n'
                 + '$(info CHILD_GPG=$(TRUSTED_GPG_URLS))\n'
                 + '$(SONIC_BUILD_HOOKS_TARGET) all verify: | observe\n.PHONY: observe\n'
                 + "observe:\n\t@printf '%s\\n' 'CHILD_FLAGS=$(MAKEFLAGS)'\n"
                 + marker + prefix + "\t@printf '%s\\n' producer >> events\n"
                 + 'all: $(SONIC_BUILD_HOOKS_TARGET)\n' + verify)
        self.assertNotIn('$(shell', parent + child)
        cases = (('default-force-build', None, 'fresh', True, 0, ['producer']),
                 ('explicit-force-build', 'build', 'fresh', True, 0, ['producer']),
                 ('strict-fresh', 'reuse-only', 'fresh', True, 0, []),
                 ('strict-stale', 'reuse-only', 'stale', True, 2, []),
                 ('strict-missing', 'reuse-only', 'missing', True, 2, []),
                 ('strict-empty', 'reuse-only', 'empty', True, 2, []),
                 ('invalid-policy', 'invalid', 'fresh', True, 2, []),
                 ('strict-missing-source', 'reuse-only', 'fresh', False, 2, []))
        for name, policy, state, source_exists, code, expected_events in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix='host-verify-make-') as temporary:
                directory = Path(temporary)
                child_dir = directory / 'child'
                child_dir.mkdir()
                (directory / 'Makefile').write_text(parent)
                (child_dir / 'Makefile').write_text(child)
                prerequisite = child_dir / 'source.input'
                artifact = child_dir / 'artifact.input'
                if source_exists:
                    prerequisite.write_text('plain text prerequisite\n')
                    os.utime(prerequisite, ns=(1600000000000000000, 1600000000000000000))
                if state != 'missing':
                    artifact.write_text('' if state == 'empty' else 'plain text existing artifact, not a Debian archive\n')
                    stamp = 1599999999000000000 if state == 'stale' else 1600000001000000000
                    os.utime(artifact, ns=(stamp, stamp))
                before = {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in (prerequisite, artifact) if p.exists()}
                inner = 'SONIC_DEB_BUILD_POLICY=reuse-only SONIC_BAZEL_KERNEL_PREPARE=verify'
                argv = ['/usr/bin/make', '--no-print-directory', '-rR', '-Bks', '-j2', '-f', 'Makefile',
                        'all', 'PROPAGATED=sentinel', 'TRUSTED_GPG_URLS=bound-input']
                if policy is not None:
                    argv.append('SONIC_DEB_BUILD_POLICY=' + policy)
                result = subprocess.run(argv, cwd=directory,
                                        env={'PATH': '/nonexistent', 'LC_ALL': 'C', 'SONIC_BUILD_VARS': inner},
                                        text=True, capture_output=True, timeout=10, check=False)
                after = {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns)
                         for p in (prerequisite, artifact) if p.exists()}
                events = (child_dir / 'events').read_text().splitlines() if (child_dir / 'events').exists() else []
                values = dict(line.split('=', 1) for line in result.stdout.splitlines() if line.startswith('CHILD_'))
                flags = values['CHILD_FLAGS']
                HOST_MAKE_RECORDS.append({'case': name, 'argv': argv, 'returncode': result.returncode,
                                          'stdout': result.stdout, 'stderr': result.stderr, 'events': events,
                                          'inputs_unchanged': before == after, 'child_values': values, 'reaped': True,
                                          'parent_sha256': hashlib.sha256(parent.encode()).hexdigest(),
                                          'child_sha256': hashlib.sha256(child.encode()).hexdigest()})
                self.assertEqual(result.returncode, code)
                self.assertEqual(events, expected_events)
                self.assertEqual(before, after)
                self.assertTrue(set('Bks').issubset(set(flags.split()[0])))
                self.assertIn('--jobserver-auth=', flags)
                self.assertIn('-j2', flags)
                self.assertNotIn('jobserver unavailable', result.stderr)
                self.assertEqual(values['CHILD_POLICY'], policy or '')
                self.assertEqual(values['CHILD_PROPAGATED'], 'sentinel')
                self.assertEqual(values['CHILD_INNER'], inner)
                self.assertEqual(values['CHILD_GPG'], 'bound-input')
                self.assertEqual((directory / 'Makefile').read_text(), parent)
                self.assertEqual((child_dir / 'Makefile').read_text(), child)

if __name__ == '__main__':
    unittest.main()
