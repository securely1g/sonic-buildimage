#!/usr/bin/env python3
"""Verify configured source discovery with the maintained owner and public guards."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import container, public_artifacts


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ConfiguredOwnerSourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = container
        cls.public = public_artifacts
        cls.owner_path = Path(container.__file__)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'product'
        self.source.mkdir()
        for name in (*self.owner.SOURCE_FILES, 'app/BUILD.bazel', 'app/input.txt',
                     'defs/transitive.bzl', 'extra-input.txt'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('retained source input\n')
        self.records = [
            {'type': 'RULE', 'rule': {'name': '@@//app:alias', 'ruleClass': 'alias'}},
            {'type': 'RULE', 'rule': {'name': '@@//lib:actual', 'ruleClass': 'filegroup'}},
            {'type': 'SOURCE_FILE', 'sourceFile': {'name': '@@//app:input.txt'}},
            {'type': 'RULE', 'rule': {'name': '@@selected+//pkg:dependency', 'ruleClass': 'filegroup'}},
        ]
        self.rows = json.dumps({'target': self.records})
        self.input_rows = ('@@//app:input.txt\n@@//app:BUILD.bazel\n'
                           '@@//defs:transitive.bzl\n@@selected+//pkg:BUILD.bazel\n')
        self.options = ['--config=syncd-dash', '--platforms=@sonic_build_infra//platforms:x86_64_trixie',
                        '--host_platform=@sonic_build_infra//platforms:x86_64_trixie',
                        '--//dockers/docker-snmp:image_version=bazel-ci']
        self.calls = []
        self.receipt = {}

    def collect(self, *, rows=None, input_rows=None, configured=True, extra=(), fail_query=False):
        artifacts = Path(tempfile.mkdtemp(dir=self.root))
        def execute(command, directory, receipt, name, **kwargs):
            self.calls.append((list(command), name, kwargs))
            if command[1] == 'cquery':
                kwargs['output_path'].write_text(self.rows if rows is None else rows)
                if fail_query:
                    raise subprocess.CalledProcessError(7, command)
                return ''
            self.expression = (Path(command[-1].split('=', 1)[1]).read_text()
                               if command[-1].startswith('--query_file=') else command[-1])
            return self.input_rows if input_rows is None else input_rows
        value = self.owner.declared_sources(
            ['//app:alias'], 'fixed-proxy', self.source, artifacts, self.receipt, execute, extra,
            options=self.options if configured else None)
        return value, artifacts

    def test_original_alias_external_and_null_source_labels_are_preserved(self):
        names, artifacts = self.collect()
        command = self.calls[0][0]
        self.assertEqual(command[:2], ['fixed-proxy', 'cquery'])
        self.assertEqual(command[2:2 + len(self.options)], self.options)
        for option in ('--implicit_deps', '--tool_deps', '--include_aspects',
                       '--consistent_labels', '--output=jsonproto', '--noproto:include_configurations',
                       '--proto:output_rule_attrs=', '--noproto:rule_inputs_and_outputs',
                       '--noproto:locations', '--transitions=none'):
            self.assertIn(option, command)
        self.assertEqual(command[-1], 'deps(set(//app:alias))')
        for label in ('//app:alias', '//lib:actual', '@@selected+//pkg:dependency'):
            self.assertIn(label, self.expression)
        self.assertIn('defs/transitive.bzl', names)
        self.assertEqual((artifacts / 'source-configured-targets.raw.json').read_text(), self.rows)

    def test_multiple_configurations_and_duplicate_rows_form_one_source_union(self):
        rows = json.dumps({'target': self.records + [self.records[0], self.records[0]]})
        _, artifacts = self.collect(rows=rows)
        self.assertEqual(self.receipt['source_discovery']['configured_rows'], 6)
        self.assertEqual(self.receipt['source_discovery']['configured_labels'], 4)
        self.assertEqual((artifacts / 'source-configured-targets.raw.json').read_text(), rows)
        self.assertEqual(self.expression.count('//app:alias'), 2)

    def test_malformed_or_injected_rows_fail_before_buildfiles(self):
        for bad in ('@@//app:alias (abc1234)\n', '{', '[',
                    '{"target": [{"type": "RULE", "rule": {"name": "@@//app:alias) union //other:target"}}]}'):
            with self.subTest(row=bad):
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.collect(rows=bad)
                self.assertEqual(len(self.calls), 1)

    def test_missing_selected_local_root_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'omitted a selected local target'):
            self.collect(rows=json.dumps({'target': [self.records[1]]}))
        self.assertEqual(len(self.calls), 1)

    def test_configured_query_failure_preserves_original_and_stops(self):
        with self.assertRaises(subprocess.CalledProcessError) as result:
            self.collect(fail_query=True)
        self.assertEqual(result.exception.returncode, 7)
        self.assertEqual(len(self.calls), 1)
        path = self.calls[0][2]['output_path']
        self.assertEqual(path.read_text(), self.rows)
        self.assertNotIn('source_discovery', self.receipt)

    def test_second_query_uses_exact_configured_set_without_deps(self):
        _, artifacts = self.collect()
        command = self.calls[1][0]
        self.assertEqual(command[:2], ['fixed-proxy', 'query'])
        self.assertEqual(command[-1], '--query_file=' + str(artifacts / 'source-buildfiles.query'))
        self.assertNotIn('deps(', self.expression)
        self.assertTrue(self.expression.startswith('kind("source file", set('))
        self.assertIn(' union buildfiles(set(', self.expression)
        self.assertIn('@@selected+//pkg:dependency', self.expression)
        self.assertNotIn('@@unselected', self.expression)
        self.assertEqual(len(self.calls), 2)

    def test_missing_and_escaping_local_sources_are_rejected(self):
        for labels in ('@@//app:absent.txt\n', '@@//app:../escape\n', ''):
            with self.subTest(labels=labels):
                with self.assertRaises(ValueError):
                    self.collect(input_rows=labels)

    def test_fixed_owner_inputs_and_explicit_extra_inputs_are_retained(self):
        names, _ = self.collect(extra=('extra-input.txt',))
        self.assertTrue(set(self.owner.SOURCE_FILES).issubset(names))
        self.assertIn('extra-input.txt', names)
        self.assertIn('tools/bazel/ci/container.py', names)
        self.assertNotIn('selected+/pkg/BUILD.bazel', names)
        self.assertEqual(names, sorted(set(names)))

    def test_legacy_source_discovery_remains_the_original_single_query(self):
        names, _ = self.collect(configured=False)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], ['fixed-proxy', 'query', *self.owner.QUERY_OPTIONS,
            '--output=label', 'kind("source file", deps(set(//app:alias))) union buildfiles(deps(set(//app:alias)))'])
        self.assertNotIn('source_discovery', self.receipt)
        self.assertIn('defs/transitive.bzl', names)

    def test_public_discovery_record_contains_actual_code_and_raw_evidence_hashes(self):
        _, artifacts = self.collect()
        record = self.receipt['source_discovery']
        self.assertEqual(record['controller_sha256'], sha(self.owner_path))
        self.assertEqual(record['configured_metadata'], {
            'bytes': (artifacts / 'source-configured-targets.raw.json').stat().st_size,
            'sha256': sha(artifacts / 'source-configured-targets.raw.json')})
        self.assertEqual(record['buildfiles_query']['sha256'], sha(artifacts / 'source-buildfiles.query'))
        self.public.safe_json({'source_discovery': record})
        self.assertNotIn(str(self.source), json.dumps(record))

    def test_workspace_selection_rejects_relative_missing_and_symlink_paths(self):
        link = self.root / 'source-link'
        link.symlink_to(self.source, target_is_directory=True)
        for path in ('relative', str(self.root / 'missing'), str(link)):
            with self.subTest(path=path), mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit) as result:
                    self.owner.source_workspace_argument(['--source-workspace', path])
                self.assertEqual(result.exception.code, 2)
        self.assertEqual(self.owner.source_workspace_argument(['--source-workspace', str(self.source)]),
                         self.source.resolve())
        self.assertIsNone(self.owner.source_workspace_argument(['--container', '//app']))

    def test_controller_bootstrap_imports_product_helpers_before_main(self):
        import_log = self.root / 'imported.jsonl'
        fake = self.root / 'fake-product'
        modules = {'build': '', 'cache_reuse': '', 'command_log': '', 'public_artifacts': '',
                   'resolution': '', 'artifact_validation': 'require = lambda *args: None\nsha = lambda p: "unused"\n',
                   'bazel_commands': 'inspect_actions = lambda p: None\n'}
        for prefix in ('tools', 'tools/bazel', 'tools/bazel/ci', 'tools/bazel/gzip'):
            directory = fake / prefix
            directory.mkdir(parents=True, exist_ok=True)
            (directory / '__init__.py').write_text('')
        marker = 'from pathlib import Path\nwith Path(' + repr(str(import_log)) + ').open("a") as f: f.write(__file__ + "\\n")\n'
        for name, body in modules.items():
            (fake / 'tools/bazel/ci' / (name + '.py')).write_text(marker + body)
        (fake / 'tools/bazel/gzip/source_archive.py').write_text(marker + 'check_versions = lambda p: None\n')
        result = subprocess.run([sys.executable, '-I', '-S', '-B', str(self.owner_path),
                                 '--source-workspace', str(fake), '--help'],
                                text=True, capture_output=True, check=False, timeout=10,
                                env=os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--source-workspace', result.stdout)
        imported = import_log.read_text().splitlines()
        self.assertEqual(len(imported), 8)
        self.assertTrue(all(Path(name).is_relative_to(fake) for name in imported))

    def test_all_five_target_variants_preserve_original_names(self):
        additions = [
            {'type': 'GENERATED_FILE', 'generatedFile': {'name': '@@//gen:output', 'generatingRule': '@@//gen:producer'}},
            {'type': 'PACKAGE_GROUP', 'packageGroup': {'name': '@@//visibility:group'}},
            {'type': 'ENVIRONMENT_GROUP', 'environmentGroup': {'name': '@@//env:group'}},
        ]
        self.collect(rows=json.dumps({'target': self.records + additions}))
        for label in ('//gen:output', '//visibility:group', '//env:group', '//app:input.txt'):
            self.assertEqual(self.expression.count(label), 2)
        self.assertEqual(self.receipt['source_discovery']['configured_rows'], 7)

    def test_target_discriminator_and_exclusive_body_must_match(self):
        for bad in ({'type': 'OTHER', 'rule': {'name': '@@//app:alias'}},
                    {'type': 'RULE', 'sourceFile': {'name': '@@//app:alias'}},
                    {'type': 'RULE', 'rule': {'name': '@@//app:alias'}, 'sourceFile': {'name': '@@//x:y'}},
                    {'type': 'RULE', 'rule': {'name': 123}},
                    {'type': 'RULE', 'rule': None}):
            with self.subTest(row=bad):
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, 'configured source target'):
                    self.collect(rows=json.dumps({'target': [bad]}))
                self.assertEqual(len(self.calls), 1)

    def test_strict_json_duplicate_keys_and_nonfinite_numbers_reject(self):
        for bad in ('{"target": [], "target": []}',
                    '{"target": [{"type": "RULE", "rule": {"name": "@@//app:alias", "name": "@@//other:alias"}}]}',
                    '{"target": [], "extra": NaN}'):
            with self.subTest(data=bad):
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.collect(rows=bad)
                self.assertEqual(len(self.calls), 1)

    def test_structured_names_still_use_the_unchanged_label_guard(self):
        for bad in ('@@//app:alias) union //other:target', '@@//app:alias\n',
                    'not-a-label', '@//app:alias'):
            with self.subTest(label=bad):
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, 'invalid-label'):
                    self.collect(rows=json.dumps({'target': [{'type': 'RULE', 'rule': {'name': bad}}]}))
                self.assertEqual(len(self.calls), 1)

    def test_aspect_base_rows_and_implicit_inputs_preserve_raw_evidence(self):
        aspect_base = {'type': 'RULE', 'rule': {'name': '@@//app:alias', 'ruleClass': 'alias',
            'attribute': [{'name': '_marker', 'type': 'LABEL', 'stringValue': '@@//aspect_inputs:marker'}]}}
        implicit = {'type': 'SOURCE_FILE', 'sourceFile': {'name': '@@//aspect_inputs:marker.txt'}}
        raw = json.dumps({'target': self.records + [aspect_base, implicit]})
        _, artifacts = self.collect(rows=raw)
        self.assertEqual(self.expression.count('//app:alias'), 2)
        self.assertEqual(self.expression.count('//aspect_inputs:marker.txt'), 2)
        self.assertEqual(self.receipt['source_discovery']['configured_rows'], 6)
        self.assertEqual(self.receipt['source_discovery']['configured_labels'], 5)
        self.assertEqual((artifacts / 'source-configured-targets.raw.json').read_text(), raw)
        self.assertNotIn('attribute', json.dumps(self.receipt['source_discovery']))
        self.public.safe_json(self.receipt['source_discovery'])

    def test_empty_and_wrong_query_result_shapes_reject_before_buildfiles(self):
        for bad in ({}, {'target': []}, {'target': {}}, [], {'results': self.records},
                    {'target': self.records, 'extra': []}):
            with self.subTest(value=bad):
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.collect(rows=json.dumps(bad))
                self.assertEqual(len(self.calls), 1)

    def test_all_genuine_external_name_occurrences_are_retained_and_quoted(self):
        # Names/occurrences come from original diagnostic.json15c9e78f. These
        # synthetic typed wrappers make no claim about each original row's kind.
        observed = ('@@rules_python++pip+scapy_test_pip_313_setuptools//:site-packages/setuptools/_vendor/jaraco/text/Lorem ipsum.txt', '@@rules_python++pip+scapy_test_pip_313_setuptools//:site-packages/setuptools/command/launcher manifest.xml', '@@rules_python++pip+scapy_test_pip_313_setuptools//:site-packages/setuptools/script (dev).tmpl', '@@rules_python++pip+config_engine_pip_313_setuptools//:site-packages/setuptools/script (dev).tmpl', '@@rules_python++pip+config_engine_pip_313_setuptools//:site-packages/setuptools/_vendor/jaraco/text/Lorem ipsum.txt', '@@rules_python++pip+config_engine_pip_313_setuptools//:site-packages/setuptools/command/launcher manifest.xml', '@@rules_python++python+python_3_11_x86_64-unknown-linux-gnu//:lib/python3.11/site-packages/setuptools/command/launcher manifest.xml', '@@rules_python++python+python_3_11_x86_64-unknown-linux-gnu//:lib/python3.11/site-packages/setuptools/_vendor/jaraco/text/Lorem ipsum.txt', '@@rules_python++python+python_3_11_x86_64-unknown-linux-gnu//:lib/python3.11/site-packages/setuptools/script (dev).tmpl', '@@libyang+//:models/ietf-yang-library@2019-01-04.yang', '@@libyang+//:models/ietf-yang-schema-mount@2019-01-14.yang', '@@libyang+//:models/yang@2025-01-29.yang', '@@libyang+//:models/ietf-yang-metadata@2016-08-05.yang', '@@libyang+//:models/ietf-datastores@2018-02-14.yang', '@@libyang+//:models/ietf-inet-types@2013-07-15.yang', '@@libyang+//:models/ietf-yang-types@2013-07-15.yang', '@@libyang+//:models/ietf-yang-structure-ext@2020-06-17.yang', '@@rules_python++pip+sonic_yang_models_pip_313_setuptools//:site-packages/setuptools/script (dev).tmpl', '@@rules_python++pip+sonic_yang_models_pip_313_setuptools//:site-packages/setuptools/_vendor/jaraco/text/Lorem ipsum.txt', '@@libyang+//:models/ietf-yang-library@2019-01-04.h', '@@libyang+//:models/ietf-yang-types@2013-07-15.h', '@@libyang+//:models/ietf-yang-schema-mount@2019-01-14.h', '@@libyang+//:models/ietf-yang-metadata@2016-08-05.h', '@@libyang+//:models/ietf-inet-types@2013-07-15.h', '@@libyang+//:models/yang@2025-01-29.h', '@@rules_python++pip+sonic_yang_models_pip_313_setuptools//:site-packages/setuptools/command/launcher manifest.xml', '@@libyang+//:models/ietf-datastores@2018-02-14.h', '@@libyang+//:models/ietf-yang-structure-ext@2020-06-17.h', '@@rules_distroless++apt+trixie_libboost1.83-dev-amd64_1.83.0-4.2//:usr/include/boost/serialization/collection_size_type copy.hpp', '@@rules_distroless++apt+trixie_libboost1.83-dev-amd64_1.83.0-4.2//:usr/include/boost/serialization/collection_size_type copy.hpp', '@@rules_distroless++apt+trixie_libboost1.83-dev-amd64_1.83.0-4.2//:usr/include/boost/serialization/collection_size_type copy.hpp')
        self.assertEqual(len(observed), 31)
        self.assertEqual(len(set(observed)), 29)
        fields = {'RULE': 'rule', 'SOURCE_FILE': 'sourceFile', 'GENERATED_FILE': 'generatedFile',
                  'PACKAGE_GROUP': 'packageGroup', 'ENVIRONMENT_GROUP': 'environmentGroup'}
        for kind, field in fields.items():
            with self.subTest(kind=kind):
                self.calls.clear()
                raw = json.dumps({'target': self.records + [
                    {'type': kind, field: {'name': label}} for label in observed]})
                _, artifacts = self.collect(rows=raw)
                for label in set(observed):
                    self.assertEqual(self.expression.count('"' + label + '"'), 2)
                self.assertEqual(self.receipt['source_discovery']['configured_rows'], 35)
                self.assertEqual(self.receipt['source_discovery']['configured_labels'], 33)
                self.assertEqual((artifacts / 'source-configured-targets.raw.json').read_text(), raw)

    def test_only_reviewed_canonical_external_target_punctuation_is_extended(self):
        for label in ('@@repo+//:Lorem ipsum.txt', '@@repo+//:script (dev).tmpl',
                      '@@libyang+//:models/ietf-yang-library@2019-01-04.yang'):
            self.assertEqual(self.owner.configured_source_label(label), label)
            with self.assertRaisesRegex(ValueError, 'invalid-label'):
                self.public.label(label)
        for label in ('//app:Lorem ipsum.txt', '@repo//:Lorem ipsum.txt', '@@//app:Lorem ipsum.txt',
                      '@@repo//:a"b', "@@repo//:a'b", '@@repo//:a\\b', '@@repo//:a\n',
                      '@@repo//:a\t', '@@repo//:a\x00', '@@repo//:a\x7f', '@@repo//:a[b]',
                      '@@repo//:a;b', '@@repo//:a|b', '@@repo//:a$b', '@@repo//:a:b',
                      '@@repo//:café'):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, 'invalid-label'):
                self.owner.configured_source_label(label)

    def test_every_flat_query_name_is_a_single_quoted_word(self):
        _, _ = self.collect()
        selected = self.expression.split('set(', 1)[1].split(')', 1)[0]
        expected = ['//app:alias', '//app:input.txt', '//lib:actual', '@@selected+//pkg:dependency']
        self.assertEqual(selected, ' '.join('"' + label + '"' for label in sorted(expected)))
        self.assertNotIn('deps(', self.expression)


if __name__ == "__main__":
    unittest.main()
