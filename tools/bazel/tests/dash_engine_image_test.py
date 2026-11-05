"""Guard actual DASH engine metadata and installed-image acceptance boundaries."""

from contextlib import ExitStack
from types import SimpleNamespace
from copy import deepcopy
import ast
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import dash_engine_image as driver
from tools.bazel.ci import dash_engine_validate as validate
from tools.bazel.ci import dash_engine_public as public
from tools.bazel.oci import image_validation


def successful_build_events(*, processes=0, action_cache_hits=0, runners=None):
    return [
        {'id': {'started': {}}, 'started': {'command': 'build', 'buildToolVersion': '8.5.1'}},
        {'id': {'targetCompleted': {'label': driver.OWNER + driver.IMAGES[0] + '.gz'}},
         'completed': {'success': True}},
        {'id': {'buildFinished': {}}, 'finished': {'overallSuccess': True, 'exitCode': {'code': 0}}},
        {'id': {'buildMetrics': {}}, 'lastMessage': True,
         'buildMetrics': {'actionSummary': {
             'actionsExecuted': str(processes), 'actionCacheStatistics': {'hits': action_cache_hits},
             'runnerCount': [{'name': name, 'count': count}
                             for name, count in {'total': processes, **(runners or {})}.items()]}}},
    ]


class DashEngineImageTest(unittest.TestCase):
    def test_debug_lock_supplies_source_openssh_dependency_closure(self):
        # Actual openssh-client Depends from source 15fcb926's retained AMD64
        # runtime_packages.json (registry CI run 41958251171). This is a presence
        # regression; installed_packages still validates actual control versions.
        depends = (
            "adduser, init-system-helpers (>= 1.66~), passwd, libc6 (>= 2.38), "
            "libedit2 (>= 2.11-20080614-0), libfido2-1 (>= 1.8.0), "
            "libgssapi-krb5-2 (>= 1.17), libselinux1 (>= 3.1~), "
            "libssl3t64 (>= 3.0.0), zlib1g (>= 1:1.1.4)"
        )
        required = {clause.split()[0] for clause in depends.split(",")}
        base = json.loads((driver.ROOT / "dockers/docker-base-trixie/bazel/apt.lock.json").read_bytes())
        engine = json.loads((driver.ROOT / "platform/vs/docker-dash-engine/bazel/apt.lock.json").read_bytes())

        def closure(lock, group):
            roots = lock["dependency_sets"][group]["sets"]["amd64"]
            pending = [key + "=" + version for key, version in roots.items()]
            records = {}
            while pending:
                key = pending.pop()
                if key not in records:
                    records[key] = lock["packages"][key]
                    pending.extend(records[key]["depends_on"])
            return {record["name"] for record in records.values()}

        inherited = closure(base, "base_debian") | closure(engine, "dash_engine_debian")
        debug = closure(engine, "dash_engine_debug_debian")
        self.assertEqual(required - inherited - debug, set())
        self.assertIn("libcbor0.10", debug)  # libfido2's transitive runtime input.
        for name in ("libedit2", "libfido2-1"):
            with self.subTest(root=name):
                missing_root = deepcopy(engine)
                del missing_root["dependency_sets"]["dash_engine_debug_debian"]["sets"]["amd64"][
                    "/trixie/" + name + ":amd64"]
                # Keeping an unused record in the lock cannot satisfy Depends.
                self.assertEqual(required - inherited - closure(missing_root, "dash_engine_debug_debian"), {name})

    def test_noop_accepts_zero_singular_and_plural_process_summaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execution, events, log = (root / n for n in ('execution.json', 'events.jsonl', 'build.log'))
            events.write_text('\n'.join(json.dumps(v) for v in successful_build_events()) + '\n')
            record = {'mnemonic': 'Unpack', 'targetLabel': '@@owner+//:payload',
                      'runner': 'disk cache hit', 'cacheHit': True}
            for summary, records, expected in [
                ('INFO: 0 processes.\n', [], {'total': 0, 'runners': {}}),
                ('INFO: 1 process: 1 internal.\n', [],
                 {'total': 1, 'runners': {'internal': 1}}),
                ('INFO: 1 process: 1 disk cache hit.\n', [record],
                 {'total': 1, 'runners': {'disk cache hit': 1}}),
                ('INFO: 2 processes: 1 internal, 1 disk cache hit.\n', [record],
                 {'total': 2, 'runners': {'internal': 1, 'disk cache hit': 1}}),
                ('INFO: 1 process: 144 action cache hit, 1 internal.\n', [],
                 {'total': 1, 'runners': {'action cache hit': 144, 'internal': 1}}),
                ('INFO: 2 processes: 144 action cache hit, 1 disk cache hit, 1 internal.\n', [record],
                 {'total': 2, 'runners': {'action cache hit': 144, 'disk cache hit': 1, 'internal': 1}}),
            ]:
                with self.subTest(summary=summary):
                    events.write_text('\n'.join(json.dumps(v) for v in successful_build_events(
                        processes=expected['total'], action_cache_hits=expected['runners'].get('action cache hit', 0),
                        runners={k: v for k, v in expected['runners'].items() if k != 'action cache hit'})) + '\n')
                    execution.write_text('\n'.join(json.dumps(v) for v in records))
                    log.write_text(summary)
                    executed, result = driver.incremental_actions(execution, events, log, no_op=True)
                    self.assertEqual(executed, {})
                    self.assertEqual(result['process_summary'], expected)
                    self.assertEqual(result['executed_count'], 0)
                    self.assertEqual(result['cache_hit_count'], len(records))

    def test_noop_requires_cache_hit_runner_bep_and_process_count_agreement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execution, events, log = (root / n for n in ('execution.json', 'events.jsonl', 'build.log'))
            events.write_text('\n'.join(json.dumps(v) for v in successful_build_events(
                processes=2, runners={'internal': 1, 'disk cache hit': 1})) + '\n')
            record = {'mnemonic': 'Unpack', 'targetLabel': '@@owner+//:payload',
                      'runner': 'disk cache hit', 'cacheHit': True}
            execution.write_text(json.dumps(record))
            log.write_text('INFO: 2 processes: 1 internal, 1 disk cache hit.\n')
            executed, result = driver.incremental_actions(execution, events, log, no_op=True)
            self.assertEqual(executed, {})
            self.assertEqual((result['action_count'], result['cache_hit_count'], result['executed_count']), (1, 1, 0))
            public.safe_json(result)
            failed = {}
            execution.write_text(json.dumps({**record, 'runner': 'processwrapper-sandbox', 'cacheHit': False}))
            with self.assertRaises(ValueError):
                driver.incremental_actions(execution, events, log, no_op=True, evidence=failed)
            self.assertEqual(failed['executed_count'], 1)
            self.assertEqual(failed['actions'][0]['cache_hit'], False)
            public.safe_json(failed)
            for changes in [dict(runner='processwrapper-sandbox', cacheHit=False),
                            dict(cacheHit=False), dict(cacheHit='true'), dict(runner='local'),
                            dict(exitCode=1), dict(status='failed')]:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    execution.write_text(json.dumps({**record, **changes}))
                    driver.incremental_actions(execution, events, log, no_op=True)
            for missing in ('cacheHit', 'runner'):
                bad = dict(record); bad.pop(missing)
                execution.write_text(json.dumps(bad))
                with self.assertRaises(ValueError): driver.incremental_actions(execution, events, log, no_op=True)
            execution.write_text(json.dumps(record))
            for summary in ['INFO: 0 processes.\n', 'INFO: 1 process: 1 local.\n', '',
                            'INFO: 2 processes: 1 disk cache hit.\n',
                            'INFO: 1 process: 1 disk cache hit.\nINFO: 0 processes.\n']:
                log.write_text(summary)
                with self.assertRaises(ValueError): driver.incremental_actions(execution, events, log, no_op=True)
            log.write_text('INFO: 1 process: 1 disk cache hit.\n')
            events.write_text('\n'.join(json.dumps(v) for v in successful_build_events()[:-1]) + '\n')
            with self.assertRaises(ValueError): driver.incremental_actions(execution, events, log, no_op=True)

    def test_empty_noop_and_real_source_actions_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execution, events, log = (root / n for n in ('execution.json', 'events.jsonl', 'build.log'))
            events.write_text('\n'.join(json.dumps(v) for v in successful_build_events()) + '\n')
            execution.write_text(''); log.write_text('INFO: 0 processes.\n')
            executed, result = driver.incremental_actions(execution, events, log, no_op=True)
            self.assertEqual(executed, {}); self.assertEqual(result['action_count'], 0)
            for mnemonic in ('CppCompile', 'CppLink', 'Rustc', 'GoCompilePkg', 'GoLink'):
                execution.write_text(json.dumps({'mnemonic': mnemonic, 'runner': 'processwrapper-sandbox', 'cacheHit': False}))
                with self.assertRaises(ValueError): driver.incremental_actions(execution, events, log)

    def test_noop_requires_matching_structured_cache_and_runner_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execution, events, log = (root / n for n in ('execution.json', 'events.jsonl', 'build.log'))
            execution.write_text('')
            log.write_text('INFO: 1 process: 144 action cache hit, 1 internal.\n')
            baseline = successful_build_events(processes=1, action_cache_hits=144, runners={'internal': 1})
            for change in ('missing-metrics', 'missing-stats', 'missing-runners', 'hits', 'total',
                           'executed', 'duplicate-runner', 'unknown-runner', 'negative', 'boolean',
                           'unknown-runner-field', 'duplicate-metrics'):
                with self.subTest(change=change):
                    records = deepcopy(baseline)
                    metrics = records[-1]['buildMetrics']['actionSummary']
                    if change == 'missing-metrics': records[-1].pop('buildMetrics')
                    elif change == 'missing-stats': metrics.pop('actionCacheStatistics')
                    elif change == 'missing-runners': metrics.pop('runnerCount')
                    elif change == 'hits': metrics['actionCacheStatistics']['hits'] = 143
                    elif change == 'total': metrics['runnerCount'][0]['count'] = 2
                    elif change == 'executed': metrics['actionsExecuted'] = '2'
                    elif change == 'duplicate-runner': metrics['runnerCount'].append({'name': 'internal', 'count': 1})
                    elif change == 'unknown-runner': metrics['runnerCount'][1]['name'] = 'local'
                    elif change == 'negative': metrics['actionCacheStatistics']['hits'] = -1
                    elif change == 'boolean': metrics['actionsExecuted'] = True
                    elif change == 'unknown-runner-field': metrics['runnerCount'][1]['command'] = 'private'
                    else: records.insert(-1, deepcopy(records[-1]))
                    events.write_text('\n'.join(json.dumps(v) for v in records) + '\n')
                    evidence = {}
                    with self.assertRaises(ValueError):
                        driver.incremental_actions(execution, events, log, no_op=True, evidence=evidence)
                    public.safe_json(evidence)
                    if change == 'hits':
                        self.assertEqual(evidence['process_summary']['runners']['action cache hit'], 144)
                        self.assertEqual(evidence['build_action_summary']['action_cache_hits'], 143)
            # Bazel suppresses all details for zero processes, including action
            # cache hits. Keep the separately validated BEP count in the receipt.
            events.write_text('\n'.join(json.dumps(v) for v in successful_build_events(action_cache_hits=144)) + '\n')
            log.write_text('INFO: 0 processes.\n')
            _, result = driver.incremental_actions(execution, events, log, no_op=True)
            self.assertEqual(result['build_action_summary']['action_cache_hits'], 144)
            self.assertEqual(result['process_summary'], {'total': 0, 'runners': {}})

    def test_source_symlink_matches_installed_linux_mode_without_losing_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'source.tar'
            name = 'usr/lib/x86_64-linux-gnu/libpi.so'
            with tarfile.open(archive, 'w') as output:
                member = tarfile.TarInfo(name)
                member.type = tarfile.SYMTYPE
                member.mode = 0
                member.linkname = './libpi.so.0.0.0'
                output.addfile(member)
            raw = validate.check.payload(archive)[name]
            installed = {}
            image_validation.apply_layer(archive, installed)
            self.assertEqual(raw['mode'], 0)
            self.assertEqual(installed[name]['mode'], 0o777)
            validate.require_item(installed, name, raw)
            self.assertEqual(raw['mode'], 0)
            for key, value in [('kind', 'file'), ('mode', 0), ('uid', 1), ('gid', 1),
                               ('linkname', 'different.so')]:
                with self.subTest(key=key):
                    changed = {name: {**installed[name], key: value}}
                    with self.assertRaises(ValueError): validate.require_item(changed, name, raw)

    def test_installed_regular_files_and_hardlinks_keep_exact_modes(self):
        for kind in ('file', 'hardlink'):
            expected = {'kind': kind, 'mode': 0o644, 'uid': 0, 'gid': 0}
            if kind == 'file':
                expected.update(sha256='source-hash', size=1)
            else:
                expected['linkname'] = 'usr/lib/original.so'
            validate.require_item({'member': expected}, 'member', expected)
            for key, value in [('mode', 0o777), ('uid', 1), ('gid', 1)]:
                with self.subTest(kind=kind, key=key):
                    with self.assertRaises(ValueError):
                        validate.require_item({'member': {**expected, key: value}}, 'member', expected)

    def test_actual_metadata_only_make_has_no_image_env_or_owner_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'target'
            commands = []
            def execute(argv, *args):
                commands.append(argv)
                subprocess.run(argv + ['TARGET_PATH=' + str(target)], cwd=driver.ROOT, check=True,
                               capture_output=True, text=True)
            driver.prepare_manifests(execute, Path(temporary), {}, 'engine-test')
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][0], 'make')
            self.assertNotIn('image.env', ' '.join(commands[0]))
            self.assertEqual({p.parent.name for p in target.glob('bazel-manifests/*/manifest.json')},
                             {'docker-base-trixie', 'docker-dash-engine'})
            manifest = json.loads((target / 'bazel-manifests/docker-dash-engine/manifest.json').read_text())
            self.assertEqual(manifest['service']['name'], 'dash_engine')

    def test_config_only_changes_declared_entrypoint_environment_manifest(self):
        parent = {'Env': ['PATH=/usr/bin', 'LANG=C'], 'Entrypoint': ['/base'], 'Cmd': ['base'],
                  'User': '0', 'WorkingDir': '/', 'Labels': {'base': 'yes'}}
        actual = deepcopy(parent)
        actual.update(Entrypoint=['/usr/bin/start.sh'], Cmd=[], Env=parent['Env'] + ['DEBIAN_FRONTEND=noninteractive'])
        actual['Labels']['com.azure.sonic.manifest'] = '{"container_name":"dash_engine"}'
        expected = {'container_name': 'dash_engine'}
        validate.verify_config(parent, actual, expected)
        for key, value in [('User', '1000'), ('Cmd', ['changed']), ('WorkingDir', '/tmp'),
                           ('Env', actual['Env'] + ['EXTRA=yes']), ('Healthcheck', {'Test': ['changed']})]:
            with self.subTest(key=key):
                bad = deepcopy(actual);bad[key] = value
                with self.assertRaises(ValueError): validate.verify_config(parent, bad, expected)

    def test_group_rows_and_metadata_are_exact(self):
        base = {'kind': 'file', 'mode': 0o644, 'uid': 0, 'gid': 0}
        rows = ['root:x:0:', 'user:x:1000:']
        validate.verify_groups(rows, rows, base, base, False)
        validate.verify_groups(rows, rows + ['_ssh:x:104:'], base, base, True)
        for changed in [rows[::-1], rows + ['_ssh:x:104:', 'user:x:1000:'], ['root:x:0:extra', 'user:x:1000:', '_ssh:x:104:']]:
            with self.assertRaises(ValueError): validate.verify_groups(rows, changed, base, base, True)
        for key, value in [('kind', 'symlink'), ('mode', 0o600), ('uid', 1000), ('gid', 1000)]:
            with self.assertRaises(ValueError): validate.verify_groups(rows, rows, base, {**base, key: value}, False)

    def test_public_projection_excludes_private_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'build-receipt.json').write_text(json.dumps({'status': 'built', 'revision': 'a'*40,
                'architecture': 'amd64', 'artifacts': {}, 'commands': [{'argv': ['/home/private/bin']}],
                'invocation': {'bazel': '/home/private/bin'}}))
            public.review(root)
            result = json.loads((root/'public-receipt.json').read_text())
            self.assertEqual(set(result), {'status', 'revision', 'architecture', 'artifacts'})

    def test_public_payload_rejects_private_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'validation.json').write_text('{"oops":"/home/person/.cache/secret"}')
            with self.assertRaises(ValueError): public.review(root)

    def test_archive_names_are_explicit_and_unique(self):
        self.assertEqual(len(driver.TARGETS), 19)
        self.assertEqual(len(set(driver.TARGETS.values())), len(driver.TARGETS))
        for name in ('pi.source.tar', 'bmv2.source.tar', 'nnpy.source.tar'):
            self.assertIn(name, driver.TARGETS)

    def test_retained_runtime_apt_is_the_first_actual_image_layer(self):
        nodes = ast.parse((driver.ROOT / 'platform/vs/docker-dash-engine/BUILD.bazel').read_text())
        image = next(n.value for n in nodes.body if isinstance(n, ast.Expr) and
                     isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and
                     n.value.func.id == 'oci_image' and any(k.arg == 'name' and
                     ast.literal_eval(k.value) == driver.IMAGES[0] for k in n.value.keywords))
        tars = ast.literal_eval(next(k.value for k in image.keywords if k.arg == 'tars'))
        self.assertEqual(driver.TARGETS['engine.runtime-apt.tar'], driver.OWNER.rstrip(':') + tars[0])

    def test_apt_replacements_preserve_exact_payload_and_other_parent_files(self):
        path = 'etc/supervisor/supervisord.conf'
        item = {'kind': 'file', 'mode': 0o644, 'uid': 0, 'gid': 0, 'sha256': 'old', 'size': 3}
        parent = {path: item, 'other': item}
        payload = {path: {**item, 'sha256': 'new'}}
        installed = {**parent, **payload}
        selection = {'selected': [{'package': 'supervisor'}],
                     'changed_non_elf_base_paths': [{'package': 'supervisor', 'path': path}]}
        validate.verify_parent_payload(parent, installed, payload, selection)
        for key, value in [('sha256', 'wrong'), ('size', 4), ('mode', 0o600), ('uid', 1), ('gid', 1), ('kind', 'symlink')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate.verify_parent_payload(parent, {**installed, path: {**payload[path], key: value}}, payload, selection)
        with self.assertRaises(ValueError):
            validate.verify_parent_payload(parent, {**installed, 'other': payload[path]}, payload, selection)
        for records in [[], selection['changed_non_elf_base_paths'] * 2,
                        [{'package': 'unknown', 'path': path}], [{'package': 'supervisor', 'path': 'missing'}]]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                validate.verify_parent_payload(parent, installed, payload, {**selection, 'changed_non_elf_base_paths': records})
        for old, new in [({}, payload), (parent, {}),
                         ({**parent, path: {**item, 'elf_machine': 62}}, payload),
                         (parent, {path: {**payload[path], 'elf_machine': 62}})]:
            with self.assertRaises(ValueError): validate.verify_parent_payload(old, installed, new, selection)

    def test_exported_layer_matches_correct_stage_digest_and_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'layer.tar'; path.write_bytes(b'layer')
            parent = SimpleNamespace(layers=['base'])
            descriptor = {'digest': 'sha256:' + validate.check.sha(path), 'size': path.stat().st_size}
            layout = SimpleNamespace(manifest={'layers': [{'digest': 'base'}, descriptor]})
            validate.verify_layer_binding(layout, parent, path)
            for bad in [{'digest': 'wrong', 'size': 5}, {'digest': descriptor['digest'], 'size': 6}]:
                with self.assertRaises(ValueError):
                    validate.verify_layer_binding(SimpleNamespace(manifest={'layers': [{'digest': 'base'}, bad]}), parent, path)

    def test_package_receipt_binds_lock_and_runtime_or_debug_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / 'platform/vs/docker-dash-engine/bazel/apt.lock.json'
            lock.parent.mkdir(parents=True); lock.write_text('{}')
            apt = root / 'runtime-apt.json'; apt.write_text('selected packages')
            base = SimpleNamespace(descriptor={'digest': 'parent'})
            selection = {'schema': 1, 'image': 'docker-dash-engine', 'group': 'runtime',
                         'apt_lock_sha256': validate.check.sha(lock), 'base_manifest_digest': 'parent',
                         'dependency_check': {'status': 'satisfied'}}
            report = {'base_manifest_digest': 'parent', 'apt_selection_sha256': validate.check.sha(apt),
                      'dependency_check': {'status': 'satisfied'}, 'source_packages': []}
            with mock.patch.object(validate, 'ROOT', root):
                validate.verify_package_binding(report, selection, base, apt, 'runtime')
                for key, value in [('group', 'debug'), ('image', 'other'), ('schema', 2),
                                   ('apt_lock_sha256', 'wrong'), ('base_manifest_digest', 'other')]:
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        validate.verify_package_binding(report, {**selection, key: value}, base, apt, 'runtime')

    def test_source_fence_allows_prior_ci_receipts_but_rejects_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = root / 'source.txt'; source.write_text('original')
            subprocess.run(['git', 'add', 'source.txt'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'baseline'], cwd=root, check=True)
            for name in ('archive', 'config-engine', 'dash-engine'):
                evidence = root / 'artifacts' / name; evidence.mkdir(parents=True)
                (evidence / 'receipt.json').write_text('{}')
            with mock.patch.object(driver, 'ROOT', root):
                driver.clean_source(root / 'artifacts/dash-engine')
                rogue = root / 'new-source.py'; rogue.write_text('unexpected')
                with self.assertRaises(ValueError): driver.clean_source(root / 'artifacts/dash-engine')
                rogue.unlink()
                source.write_text('changed')
                with self.assertRaises(subprocess.CalledProcessError): driver.clean_source(root / 'artifacts/dash-engine')


    def test_incremental_failure_restores_original_source_and_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / 'artifacts' / 'engine'; evidence.mkdir(parents=True)
            source = root / 'platform/vs/docker-dash-engine/start.sh'
            source.parent.mkdir(parents=True); source.write_bytes(b'#!/bin/sh\nexit 0\n'); source.chmod(0o755)
            original = source.read_bytes()
            revision = {'revision': 'a'*40, 'tested_revision_parents': []}
            inventory = {name + '.gz': {'sha256': 'original'} for name in driver.IMAGES}
            (evidence / 'build-receipt.json').write_text(json.dumps({**revision, 'status': 'built',
                'artifacts': inventory, 'layouts': {name: name for name in driver.IMAGES}}))
            calls = []
            def run(argv, **kwargs):
                execution = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--execution_log_json_file=')))
                phase = execution.name.removeprefix('incremental-').removesuffix('.execution.json')
                calls.append(phase)
                execution.write_text('')
                events = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--build_event_json_file=')))
                events.write_text('\n'.join(json.dumps(v) for v in successful_build_events()) + '\n')
                kwargs['stdout'].write('INFO: 0 processes.\n')
                if phase == 'launcher-source-edit':
                    self.assertNotEqual(source.read_bytes(), original)
                    return subprocess.CompletedProcess(argv, 23)
                self.assertEqual(source.read_bytes(), original)
                return subprocess.CompletedProcess(argv, 0)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(driver, 'ROOT', root))
                stack.enter_context(mock.patch.object(driver, 'clean_source'))
                stack.enter_context(mock.patch.object(driver, 'source_identity', return_value=revision))
                stack.enter_context(mock.patch.object(driver.build, 'execution_root', return_value=root))
                stack.enter_context(mock.patch.object(driver.subprocess, 'check_output', side_effect=lambda argv, **kw: argv[-1].split(':')[-1] + '\n'))
                stack.enter_context(mock.patch.object(driver.subprocess, 'run', side_effect=run))
                stack.enter_context(mock.patch.object(driver.check, 'sha', return_value='original'))
                stack.enter_context(mock.patch.object(driver.oci_layout, 'validate_layout', return_value=SimpleNamespace(descriptor={'digest': 'original'})))
                with self.assertRaises(subprocess.CalledProcessError):
                    driver.incremental(SimpleNamespace(bazel='bazel'), evidence)
            self.assertEqual(calls, ['baseline', 'no-op', 'launcher-source-edit', 'restored'])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(source.stat().st_mode & 0o777, 0o755)
            receipt = json.loads((evidence / 'incremental.json').read_text())
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['restored'], receipt['baseline'])


    def test_resource_options_keep_platform_fixed(self):
        with mock.patch.dict('os.environ', {'SONIC_CONTAINER_BAZEL_OPTIONS': '--jobs=1 --local_resources=memory=4000'}):
            options = driver.root_options()
        self.assertIn('--jobs=1', options)
        self.assertEqual(options[-2:], driver.OPTIONS[-2:])


if __name__ == '__main__':
    unittest.main()
