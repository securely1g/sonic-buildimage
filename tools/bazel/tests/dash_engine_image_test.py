"""Guard actual DASH engine metadata and installed-image acceptance boundaries."""

from contextlib import ExitStack
from types import SimpleNamespace
from copy import deepcopy
import ast
import hashlib
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


def foreign_record(instance='first', target=None):
    digest = {'hash': 'a' * 64, 'sizeBytes': '12', 'hashFunctionName': 'SHA-256'}
    output = 'bazel-out/' + instance + '/foreign-sdk'
    return {'targetLabel': target or '@@p4lang-pi++_repo_rules+pi_source//:sdk', 'mnemonic': 'CcConfigureMakeRule',
            'runner': 'processwrapper-sandbox', 'cacheHit': False, 'cacheable': True, 'remoteCacheable': True,
            'digest': digest, 'commandArgs': ['/home/private/compiler', 'private-argument'],
            'environmentVariables': [{'name': 'PRIVATE_CONTEXT', 'value': 'private-environment'}],
            'platform': {'properties': [{'name': 'os', 'value': 'linux'}]},
            'inputs': [{'path': '/home/private/source.c', 'digest': digest, 'isTool': False}],
            'listedOutputs': [output], 'actualOutputs': [{'path': output + '/lib.so', 'digest': digest}]}


def foreign_summary(records=()):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'execution.json'
        path.write_text('\n'.join(json.dumps(record) for record in records))
        hits = sum(record.get('cacheHit', False) for record in records)
        return driver.foreign_diagnostics(path, {'action_count': len(records), 'cache_hit_count': hits,
            'executed_count': len(records) - hits}, {name: None for name in driver.FOREIGN_PAYLOADS})


def input_differences(before, after):
    with tempfile.TemporaryDirectory() as temporary:
        paths = [Path(temporary) / name for name in ('initial.json', 'baseline.json')]
        for path, records in zip(paths, (before, after)):
            path.write_text('\n'.join(json.dumps(record) for record in records))
        return driver.foreign_input_differences(*paths)


def empty_archive_comparison():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths = {name: root / name for name in driver.FOREIGN_PAYLOADS}
        for path in paths.values():
            with tarfile.open(path, "w"):
                pass
        return driver.compare_foreign_archives(root, paths)


class DashEngineImageTest(unittest.TestCase):
    def test_archives_and_live_layouts_share_one_build_before_export(self):
        # A separate layout build can replace a source package after its archive
        # was exported. Exercise the real collector with one simulated action
        # invocation, including failure before any prior publication is replaced.
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / "evidence"
                directory.mkdir()
                execution = root / "execution"
                layouts = [driver.BASE, *(driver.OWNER + name for name in driver.IMAGES)]
                outputs = {label: "outputs/" + name for name, label in driver.TARGETS.items()}
                outputs.update({label: "layouts/" + label.rsplit(":", 1)[1] for label in layouts})
                previous = directory / (driver.IMAGES[0] + ".gz")
                previous.write_bytes(b"previous publication")
                calls = []

                def execute(argv, _directory, _receipt, _name, **_kwargs):
                    calls.append(argv)
                    if argv[1] == "build":
                        self.assertEqual(sum(call[1] == "build" for call in calls), 1)
                        self.assertEqual(set(argv[2:]) - {arg for arg in argv[2:] if arg.startswith("--")},
                                         set(driver.TARGETS.values()) | set(layouts))
                        if fail:
                            raise subprocess.CalledProcessError(1, argv)
                        for label, relative in outputs.items():
                            path = execution / relative
                            path.parent.mkdir(parents=True, exist_ok=True)
                            if label in layouts:
                                path.mkdir()
                            else:
                                path.write_bytes(("same invocation " + label).encode())
                        events = Path(next(arg.split("=", 1)[1] for arg in argv
                                           if arg.startswith("--build_event_json_file=")))
                        events.write_text('\n'.join(json.dumps(value) for value in [
                            {"id": {"workspace": {}}, "workspaceInfo": {"localExecRoot": str(execution)}},
                            *successful_build_events()]) + '\n')
                        Path(next(arg.split('=', 1)[1] for arg in argv
                                  if arg.startswith('--execution_log_json_file='))).write_text('')
                        return ""
                    self.assertEqual(argv[1], "cquery")
                    return outputs[argv[-1]] + "\n"

                with ExitStack() as stack:
                    for target, value in (("clean_source", None), ("prepare_manifests", None),
                                          ("source_identity", {"revision": "a" * 40})):
                        stack.enter_context(mock.patch.object(driver, target, return_value=value))
                    stack.enter_context(mock.patch.object(driver.platform, "machine", return_value="x86_64"))
                    stack.enter_context(mock.patch.object(driver.platform, "freedesktop_os_release",
                                                          return_value={"VERSION_CODENAME": "trixie"}))
                    stack.enter_context(mock.patch.object(driver.command_log, "execute", side_effect=execute))
                    stack.enter_context(mock.patch.object(driver, "root_options", return_value=["--jobs=2"]))
                    stack.enter_context(mock.patch.object(driver.bazel_commands, "audit_targets", return_value={}))
                    stack.enter_context(mock.patch.object(driver.resolution, "collect", return_value={}))
                    args = SimpleNamespace(bazel="must-not-execute-bazel", image_version="ci")
                    if fail:
                        with self.assertRaises(subprocess.CalledProcessError):
                            driver.build_images(args, directory)
                        self.assertEqual(previous.read_bytes(), b"previous publication")
                    else:
                        driver.build_images(args, directory)
                        receipt = json.loads((directory / "build-receipt.json").read_text())
                        self.assertEqual(receipt["status"], "built")
                        self.assertEqual(set(receipt["artifacts"]), set(driver.TARGETS))
                        for name, item in receipt["artifacts"].items():
                            self.assertEqual(item["sha256"], hashlib.sha256((directory / name).read_bytes()).hexdigest())
                        for path in [receipt["base_layout"], *receipt["layouts"].values()]:
                            self.assertTrue((execution / path).is_dir())
                self.assertEqual(sum(call[1] == "build" for call in calls), 1)

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


    def test_packaging_probes_distinguish_foreign_rebuild_from_cache_materialization(self):
        # Execute the real probe controller and log reader. A simulated foreign
        # build leaves every owner TAR byte unchanged; matching payload hashes
        # must not conceal native execution during a startup-only probe.
        for probe in ('launcher-source-edit', 'restored'):
            for cache_hit in (False, True):
                with self.subTest(probe=probe, cache_hit=cache_hit), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    directory = root / 'artifacts'; directory.mkdir()
                    source = root / 'platform/vs/docker-dash-engine/start.sh'
                    source.parent.mkdir(parents=True)
                    source.write_bytes(b'#!/bin/sh\nexit 0\n'); source.chmod(0o755)
                    saved = source.read_bytes()
                    revision = {'revision': 'a' * 40, 'tested_revision_parents': []}
                    names = ('engine.source.tar', 'base.source.tar', *driver.FOREIGN_PAYLOADS)
                    owners = {name: root / name for name in names}
                    for path in owners.values():
                        with tarfile.open(path, 'w'):
                            pass
                    owner_hashes = {name: driver.check.sha(path) for name, path in owners.items()}
                    for name in driver.FOREIGN_PAYLOADS:
                        (directory / name).write_bytes(owners[name].read_bytes())
                    archives = {name: root / (name + '.gz') for name in driver.IMAGES}
                    def archive_bytes(name):
                        return name.encode() + b'\n' + source.read_bytes()
                    for name, path in archives.items(): path.write_bytes(archive_bytes(name))
                    (directory / 'build.execution.json').write_text('')
                    (directory / 'build-receipt.json').write_text(json.dumps({
                        **revision, 'status': 'built',
                        'artifacts': {name + '.gz': {'sha256': driver.check.sha(path)} for name, path in archives.items()},
                        'layouts': {name: name for name in driver.IMAGES},
                        'foreign_diagnostics': foreign_summary(),
                    }))
                    outputs = {driver.TARGETS[name]: name for name in names}
                    outputs.update({driver.OWNER + name + '.gz': name + '.gz' for name in driver.IMAGES})
                    outputs[driver.BASE] = 'base-layout'
                    phases = []
                    def run(argv, **kwargs):
                        execution = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--execution_log_json_file=')))
                        phase = execution.name.removeprefix('incremental-').removesuffix('.execution.json')
                        phases.append(phase)
                        records = []
                        if phase == probe:
                            record = foreign_record()
                            record.update(cacheHit=cache_hit, runner='disk cache hit' if cache_hit else 'processwrapper-sandbox')
                            records.append(record)
                        execution.write_text('\n'.join(json.dumps(record) for record in records))
                        events = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--build_event_json_file=')))
                        events.write_text('\n'.join(json.dumps(event) for event in successful_build_events(
                            processes=len(records), runners={record['runner']: 1 for record in records})) + '\n')
                        kwargs['stdout'].write('INFO: 0 processes.\n')
                        for name, path in archives.items(): path.write_bytes(archive_bytes(name))
                        return subprocess.CompletedProcess(argv, 0)
                    def layout(path, platform):
                        digest = 'base' if path.name == 'base-layout' else hashlib.sha256(source.read_bytes()).hexdigest()
                        return SimpleNamespace(descriptor={'digest': digest})
                    with ExitStack() as stack:
                        stack.enter_context(mock.patch.object(driver, 'ROOT', root))
                        stack.enter_context(mock.patch.object(driver, 'clean_source'))
                        stack.enter_context(mock.patch.object(driver, 'source_identity', return_value=revision))
                        stack.enter_context(mock.patch.object(driver.build, 'execution_root', return_value=root))
                        stack.enter_context(mock.patch.object(driver.subprocess, 'check_output', side_effect=lambda argv, **kw: outputs[argv[-1]] + '\n'))
                        stack.enter_context(mock.patch.object(driver.subprocess, 'run', side_effect=run))
                        stack.enter_context(mock.patch.object(driver.oci_layout, 'validate_layout', side_effect=layout))
                        if cache_hit:
                            driver.incremental(SimpleNamespace(bazel='must-not-run'), directory)
                        else:
                            with self.assertRaisesRegex(ValueError, 'image-only probe unexpectedly compiled foreign source owners'):
                                driver.incremental(SimpleNamespace(bazel='must-not-run'), directory)
                    self.assertEqual(phases, ['baseline', 'no-op', 'launcher-source-edit', 'restored'])
                    self.assertEqual(source.read_bytes(), saved)
                    self.assertEqual(source.stat().st_mode & 0o777, 0o755)
                    self.assertEqual({name: driver.check.sha(path) for name, path in owners.items()}, owner_hashes)
                    result = json.loads((directory / 'incremental.json').read_text())
                    self.assertEqual(result['status'], 'passed' if cache_hit else 'failed')
                    self.assertEqual(result['spawn_cache_evidence'][probe]['executed_count'], 0 if cache_hit else 1)
                    self.assertEqual(result['spawn_cache_evidence'][probe]['cache_hit_count'], 1 if cache_hit else 0)

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

    def test_installed_mismatch_identifies_image_and_exact_changed_fields(self):
        path = 'usr/bin/pi_CLI_dummy'
        expected = {'kind': 'file', 'mode': 0o755, 'uid': 0, 'gid': 0,
                    'sha256': 'a' * 64, 'size': 14448, 'elf_machine': 62}
        actual = {**expected, 'sha256': 'b' * 64, 'uid': 1}
        with self.assertRaises(ValueError) as error:
            validate.require_item({path: actual}, path, expected, image='docker-dash-engine')
        prefix = 'installed payload differs: ' + path + ' '
        self.assertTrue(str(error.exception).startswith(prefix))
        detail = json.loads(str(error.exception)[len(prefix):])
        self.assertEqual(detail, {
            'image': 'docker-dash-engine',
            'differences': {
                'sha256': {'expected': 'a' * 64, 'actual': 'b' * 64},
                'uid': {'expected': 0, 'actual': 1},
            },
        })
        self.assertEqual(expected['uid'], 0)
        validate.require_item({path: expected}, path, expected, image='docker-dash-engine')

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

    def test_layout_evidence_survives_later_validation_failure_without_config_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = {"revision": "a" * 40}
            layout = SimpleNamespace(
                descriptor={"digest": "sha256:" + "b" * 64},
                manifest={"config": {"digest": "sha256:" + "c" * 64},
                          "layers": [{"digest": "sha256:" + "d" * 64, "size": 1}]},
                config={"rootfs": {"type": "layers", "diff_ids": ["sha256:" + "d" * 64]},
                        "config": {"Env": ["PRIVATE_PATH=/home/person/.cache/private"],
                                   "Entrypoint": ["/usr/bin/start.sh"]}})
            artifacts = {}
            for name in driver.TARGETS:
                (root / name).write_bytes(b"x")
                artifacts[name] = {"bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
            (root / "build-receipt.json").write_text(json.dumps({**identity, "status": "built",
                "artifacts": artifacts, "base_layout": "base",
                "layouts": {name: name for name in driver.IMAGES}}))
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(validate, "clean_source"))
                stack.enter_context(mock.patch.object(validate, "source_identity", return_value=identity))
                stack.enter_context(mock.patch.object(validate.build, "execution_root", return_value=root))
                stack.enter_context(mock.patch.object(validate.oci_layout, "validate_layout", return_value=layout))
                stack.enter_context(mock.patch.object(validate.base_images, "installed_inventory",
                                                     side_effect=ValueError("later image check failed")))
                with self.assertRaisesRegex(ValueError, "later image check failed"):
                    validate.validate(root)
            evidence = root / "layout-identities.json"
            data = json.loads(evidence.read_text())
            self.assertEqual(data["source_revision"], identity["revision"])
            self.assertEqual(data["scope"], "OCI identities only; not image acceptance")
            self.assertEqual(set(data["images"]), {"docker-base-trixie", *driver.IMAGES})
            self.assertEqual(data["images"][driver.IMAGES[0]]["rootfs"], layout.config["rootfs"])
            self.assertNotIn("/home/person", evidence.read_text())
            expected = hashlib.sha256(json.dumps(layout.config["config"]["Env"],
                separators=(",", ":")).encode()).hexdigest()
            self.assertIn({"name": "Env", "sha256": expected},
                          data["images"][driver.IMAGES[0]]["config_fields"])
            public.safe_json(data)
            # The normal public-artifact review must include the new evidence.
            evidence.write_text('{"unexpected":"/home/person/.cache/private"}')
            for name in driver.TARGETS:
                (root / name).unlink()
            with self.assertRaises(ValueError):
                public.review(root)

    def test_layout_evidence_projects_only_valid_rootfs_digests(self):
        layout = SimpleNamespace(
            descriptor={"digest": "sha256:" + "a" * 64},
            manifest={"config": {"digest": "sha256:" + "b" * 64}, "layers": []},
            config={"rootfs": {"type": "layers", "diff_ids": ["sha256:" + "c" * 64],
                               "private_location": "/home/person/.cache/private"}})
        result = validate.layout_identity(layout)
        self.assertEqual(result["rootfs"], {"type": "layers", "diff_ids": ["sha256:" + "c" * 64]})
        self.assertNotIn("private", json.dumps(result))
        public.safe_json(result)
        for value in ("/home/person/.cache/private", "sha256:short", None, 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid OCI rootfs digest"):
                layout.config["rootfs"]["diff_ids"] = [value]
                validate.layout_identity(layout)
        layout.config["rootfs"]["diff_ids"] = {"sha256:" + "c" * 64: "/home/person/.cache/private"}
        with self.assertRaisesRegex(ValueError, "invalid OCI rootfs digest"):
            validate.layout_identity(layout)

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
            (evidence / 'build.execution.json').write_text('')
            source = root / 'platform/vs/docker-dash-engine/start.sh'
            source.parent.mkdir(parents=True); source.write_bytes(b'#!/bin/sh\nexit 0\n'); source.chmod(0o755)
            original = source.read_bytes()
            revision = {'revision': 'a'*40, 'tested_revision_parents': []}
            inventory = {name + '.gz': {'sha256': 'original'} for name in driver.IMAGES}
            (evidence / 'build-receipt.json').write_text(json.dumps({**revision, 'status': 'built',
                'artifacts': inventory, 'layouts': {name: name for name in driver.IMAGES},
                'foreign_diagnostics': foreign_summary()}))
            calls = []
            def run(argv, **kwargs):
                execution = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--execution_log_json_file=')))
                phase = execution.name.removeprefix('incremental-').removesuffix('.execution.json')
                calls.append(phase)
                self.assertEqual(any(arg.startswith('--explain=') for arg in argv), phase == 'baseline')
                self.assertNotIn('--verbose_explanations', argv)
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
                stack.enter_context(mock.patch.object(driver, 'compare_foreign_archives', return_value=empty_archive_comparison()))
                retained = stack.enter_context(mock.patch.object(driver, 'retain_baseline_archives'))
                with self.assertRaises(subprocess.CalledProcessError):
                    driver.incremental(SimpleNamespace(bazel='bazel'), evidence)
                retained.assert_not_called()
            self.assertEqual(calls, ['baseline', 'no-op', 'launcher-source-edit', 'restored'])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(source.stat().st_mode & 0o777, 0o755)
            receipt = json.loads((evidence / 'incremental.json').read_text())
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['restored'], receipt['baseline'])


    def test_foreign_projection_keeps_instances_and_only_public_fingerprints(self):
        records = [foreign_record('one'), foreign_record('two'),
                   foreign_record('bmv2', '@@p4lang-bmv2++_repo_rules+bmv2_source//:bmv2_build')]
        unrelated = {**foreign_record('unrelated'), 'targetLabel': '@@other+//:build'}
        summary = foreign_summary([*records, unrelated])
        self.assertEqual(len(summary['observations']), 3)
        self.assertEqual(summary['spawn_counts']['action_count'], 4)
        self.assertEqual(summary['unobserved_targets'], [])
        text = json.dumps(summary)
        for private in ('/home/private', 'private-argument', 'PRIVATE_CONTEXT', 'private-environment', 'source.c', 'lib.so'):
            self.assertNotIn(private, text)
        self.assertEqual(summary, foreign_summary([unrelated, *reversed(records)]))
        self.assertEqual(len({row['listed_outputs_sha256'] for row in summary['observations']}), 3)
        comparison = driver.compare_foreign_diagnostics(summary, summary)
        self.assertEqual([row['status'] for row in comparison['instances']], ['matched'] * 3)
        self.assertTrue(all(not row['changed_recorded_fields'] for row in comparison['instances']))
        # More initial configured instances are evidence, not a presumed error.
        self.assertEqual(len(foreign_summary([foreign_record(str(n)) for n in range(16)])['observations']), 16)
        with self.assertRaisesRegex(ValueError, 'too many configured'):
            foreign_summary([foreign_record(str(n)) for n in range(17)])

    def test_foreign_fingerprints_distinguish_definition_inputs_and_outputs(self):
        original = foreign_record()
        initial = foreign_summary([original])
        cases = ('arguments_sha256', 'environment_sha256', 'platform_sha256', 'inputs_sha256',
                 'actual_outputs_sha256', 'action_digest', 'unrecorded_listed_outputs')
        for field in cases:
            with self.subTest(field=field):
                record = deepcopy(original)
                if field == 'arguments_sha256': record['commandArgs'].append('changed')
                elif field == 'environment_sha256': record['environmentVariables'][0]['value'] = 'changed'
                elif field == 'platform_sha256': record['platform']['properties'][0]['value'] = 'changed'
                elif field == 'inputs_sha256': record['inputs'][0]['digest']['hash'] = 'b' * 64
                elif field == 'actual_outputs_sha256': record['actualOutputs'][0]['digest']['hash'] = 'b' * 64
                elif field == 'action_digest': record['digest']['hash'] = 'b' * 64
                else: record['actualOutputs'] = []
                comparison = driver.compare_foreign_diagnostics(initial, foreign_summary([record]))
                row = comparison['instances'][0]
                self.assertEqual(row['status'], 'matched')
                self.assertIn(field, row['changed_recorded_fields'])
        first, second = foreign_summary([original]), foreign_summary([original])
        first['payloads'] = {name: 'a' * 64 for name in driver.FOREIGN_PAYLOADS}
        second['payloads'] = {name: ('b' if name.startswith('pi.') else 'a') * 64 for name in driver.FOREIGN_PAYLOADS}
        self.assertEqual(driver.compare_foreign_diagnostics(first, second)['payloads'],
                         {'pi.source.tar': 'changed', 'bmv2.source.tar': 'same-reported-sha256'})

    def test_foreign_absent_spawns_digests_outputs_and_ambiguous_instances_are_explicit(self):
        empty = foreign_summary()
        self.assertEqual(empty['unobserved_targets'], sorted(driver.FOREIGN_TARGETS))
        record = foreign_record()
        observed = foreign_summary([record])
        comparison = driver.compare_foreign_diagnostics(empty, observed)
        self.assertEqual(comparison['instances'][0]['status'], 'unobserved-initial')
        self.assertEqual(driver.compare_foreign_diagnostics(observed, empty)['instances'][0]['status'],
                         'unobserved-baseline')
        duplicate = foreign_summary([record, record])
        row = driver.compare_foreign_diagnostics(duplicate, observed)['instances'][0]
        self.assertEqual(row['status'], 'ambiguous')
        self.assertEqual(row['changed_recorded_fields'], [])
        record.pop('digest'); record['inputs'][0].pop('digest'); record['actualOutputs'][0].pop('digest')
        partial = foreign_summary([record])
        row = driver.compare_foreign_diagnostics(partial, partial)['instances'][0]
        self.assertEqual(row['unobserved_fields'], ['action_digest', 'input_digest_unobserved', 'output_digest_unobserved'])
        self.assertEqual(driver.compare_foreign_diagnostics(partial, partial)['payloads'],
                         {name: 'unobserved' for name in driver.FOREIGN_PAYLOADS})
        other_configuration = foreign_summary([foreign_record('different-output-configuration')])
        self.assertEqual({row['status'] for row in driver.compare_foreign_diagnostics(observed, other_configuration)['instances']},
                         {'unobserved-initial', 'unobserved-baseline'})

    def test_foreign_public_schema_rejects_unknown_private_fields_and_bad_counts(self):
        valid = foreign_summary([foreign_record()])
        for change in ('extra', 'private', 'target', 'count', 'digest', 'cache', 'payload', 'schema', 'missing'):
            with self.subTest(change=change), self.assertRaises(ValueError):
                value = deepcopy(valid)
                if change == 'extra': value['raw'] = 'not allowed'
                elif change == 'private': value['observations'][0]['argv'] = ['/home/private/bin']
                elif change == 'target': value['observations'][0]['target'] = '@@other+//:build'
                elif change == 'count': value['spawn_counts']['action_count'] = True
                elif change == 'digest': value['observations'][0]['inputs_sha256'] = 'not-a-hash'
                elif change == 'cache': value['observations'][0]['cache_hit'] = True
                elif change == 'payload': value['payloads']['pi.source.tar'] = '/home/private/output'
                elif change == 'schema': value['schema'] = True
                else: value['observations'][0]['input_digest_unobserved'] = 2
                driver.validate_foreign_diagnostics(value)
        size = len((json.dumps(valid, indent=2) + '\n').encode())
        with mock.patch.object(driver, 'DIAGNOSTIC_LIMIT', size): driver.validate_foreign_diagnostics(valid)
        with mock.patch.object(driver, 'DIAGNOSTIC_LIMIT', size - 1), self.assertRaises(ValueError):
            driver.validate_foreign_diagnostics(valid)

    def test_foreign_projection_reuses_strict_streaming_parser_and_digest_validation(self):
        for raw in ('{"targetLabel":"one","targetLabel":"two"}', '{"metric":NaN}', '{'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'execution.json'; path.write_text(raw)
                with self.assertRaises(ValueError):
                    driver.foreign_diagnostics(path, {'action_count': 0, 'cache_hit_count': 0, 'executed_count': 0},
                                               {name: None for name in driver.FOREIGN_PAYLOADS})
        for bad in (True, '-1', '1.2'):
            record = foreign_record(); record['digest']['sizeBytes'] = bad
            with self.subTest(size=bad), self.assertRaises(ValueError): foreign_summary([record])
        record = foreign_record(); record['environmentVariables'].append(record['environmentVariables'][0])
        with self.assertRaisesRegex(ValueError, 'duplicate foreign key-value'): foreign_summary([record])

    def test_public_review_checks_diagnostic_schema_and_derived_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            initial = foreign_summary([foreign_record()]); baseline = foreign_summary([foreign_record()])
            receipt = {'status': 'built', 'foreign_diagnostics': initial, 'commands': [{'argv': ['/home/private']}]}
            (directory / 'build-receipt.json').write_text(json.dumps(receipt))
            incremental = {'status': 'failed', 'foreign_diagnostics': {'initial': initial, 'baseline': baseline},
                           'foreign_comparison': driver.compare_foreign_diagnostics(initial, baseline),
                           'foreign_input_differences': input_differences([foreign_record()], [foreign_record()]),
                           'foreign_explain': driver.foreign_explain(directory / 'absent.log'),
                           'foreign_payloads_before_baseline': {name: None for name in driver.FOREIGN_PAYLOADS},
                           'foreign_archive_comparison': empty_archive_comparison()}
            path = directory / 'incremental.json'; path.write_text(json.dumps(incremental))
            public.review(directory)
            self.assertEqual(json.loads((directory / 'public-receipt.json').read_text())['foreign_diagnostics'], initial)
            self.assertNotIn('/home/private', (directory / 'public-receipt.json').read_text())
            incremental['foreign_comparison']['instances'][0]['status'] = 'invented-match'
            path.write_text(json.dumps(incremental))
            with self.assertRaisesRegex(ValueError, 'diagnostic comparison'): public.review(directory)

    def test_baseline_drift_keeps_original_error_and_no_edit_even_if_diagnostics_fail(self):
        for diagnostic_failure, matching_archives in ((None, False), ('projection', False), ('input-diff', False), ('input-diff', True), ('archive-members', False), ('archive-members', True), ('retention', False)):
            with self.subTest(diagnostic_failure=diagnostic_failure, matching_archives=matching_archives), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); directory = root / 'evidence'; directory.mkdir()
                (directory / 'build.execution.json').write_text('')
                for name in driver.FOREIGN_PAYLOADS:
                    with tarfile.open(root / name, 'w'):
                        pass
                    (directory / name).write_bytes((root / name).read_bytes())
                payload_sha = driver.check.sha(root / driver.FOREIGN_PAYLOADS[0])
                revision = {'revision': 'a' * 40, 'tested_revision_parents': []}
                (directory / 'build-receipt.json').write_text(json.dumps({**revision, 'status': 'built',
                    'artifacts': {name + '.gz': {'sha256': ('a' if matching_archives else 'b') * 64} for name in driver.IMAGES},
                    'layouts': {name: name for name in driver.IMAGES}, 'foreign_diagnostics': foreign_summary()}))
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--execution_log_json_file='))).write_text('')
                    Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--build_event_json_file='))).write_text(
                        '\n'.join(json.dumps(value) for value in successful_build_events()) + '\n')
                    kwargs['stdout'].write('INFO: 0 processes.\n')
                    return subprocess.CompletedProcess(argv, 0)
                with ExitStack() as stack:
                    if diagnostic_failure == 'archive-members':
                        stack.enter_context(mock.patch.object(driver, 'compare_foreign_archives', return_value={
                            'schema': 1, 'scope': driver.ARCHIVE_COMPARISON_SCOPE, 'status': 'unavailable', 'archives': {}, 'reason': 'deadline'}))
                    stack.enter_context(mock.patch.object(driver, 'clean_source'))
                    stack.enter_context(mock.patch.object(driver, 'source_identity', return_value=revision))
                    stack.enter_context(mock.patch.object(driver.build, 'execution_root', return_value=root))
                    def query(argv, **kwargs):
                        return next((name for name in driver.FOREIGN_PAYLOADS if driver.TARGETS[name] == argv[-1]),
                                    argv[-1].split(':')[-1]) + '\n'
                    stack.enter_context(mock.patch.object(driver.subprocess, 'check_output', side_effect=query))
                    stack.enter_context(mock.patch.object(driver.subprocess, 'run', side_effect=run))
                    stack.enter_context(mock.patch.object(driver.check, 'sha', side_effect=lambda path:
                        payload_sha if Path(path).name in (*driver.FOREIGN_PAYLOADS, *driver.BASELINE_PAYLOADS.values()) else 'a' * 64))
                    stack.enter_context(mock.patch.object(driver.oci_layout, 'validate_layout', return_value=SimpleNamespace(descriptor={'digest': 'a' * 64})))
                    if diagnostic_failure:
                        method = ('retain_baseline_archives' if diagnostic_failure == 'retention' else
                                  'foreign_diagnostics' if diagnostic_failure == 'projection' else 'foreign_input_differences')
                        stack.enter_context(mock.patch.object(driver, method, side_effect=ValueError('/home/private/failure')))
                    expected_error = 'foreign diagnostics unavailable' if matching_archives else 'incremental baseline differs from verified archives'
                    with self.assertRaisesRegex(ValueError, '^' + expected_error + '$'):
                        driver.incremental(SimpleNamespace(bazel='must-not-run'), directory)
                self.assertEqual(len(calls), 1)
                self.assertEqual([arg for arg in calls[0] if arg.startswith('--explain=')],
                                 ['--explain=' + str(directory / 'incremental-baseline.explain.log')])
                expected = {driver.OWNER + name + suffix for name in driver.IMAGES for suffix in ('', '.gz')}
                expected.update([driver.BASE, driver.TARGETS['engine.source.tar'], driver.TARGETS['base.source.tar']])
                self.assertEqual({arg for arg in calls[0][2:] if not arg.startswith('--')}, expected)
                result = json.loads((directory / 'incremental.json').read_text())
                self.assertEqual(result['error'], expected_error)
                self.assertNotIn('/home/private', json.dumps(result))
                self.assertEqual(result['foreign_payloads_before_baseline'], {name: payload_sha for name in driver.FOREIGN_PAYLOADS})
                if diagnostic_failure:
                    self.assertEqual(result['foreign_diagnostics_error'], 'foreign-diagnostics-unavailable')
                else:
                    self.assertIn('baseline', result['foreign_diagnostics'])
                    self.assertEqual(result['foreign_diagnostics']['baseline']['payloads'],
                                     {name: payload_sha for name in driver.FOREIGN_PAYLOADS})
                public.review(directory)

    def test_input_differences_identify_public_files_and_hash_private_paths(self):
        before = foreign_record()
        public_path = 'external/p4lang-pi++_repo_rules+pi_source/src/pi.c'
        before['inputs'] = [{'path': public_path, 'digest': before['digest']},
                            {'path': 'host/toolchain/private-name', 'symlinkTargetPath': '/home/private/target'},
                            {'path': '/home/private/removed', 'digest': before['digest']}]
        after = deepcopy(before)
        after['inputs'][0]['digest'] = {**before['digest'], 'hash': 'b' * 64}
        after['inputs'][1]['isTool'] = True
        after['inputs'][2] = {'path': 'tools/bazel/new.h'}
        row = input_differences([before], [after])[0]
        self.assertEqual((row['status'], row['changed_input_count'], row['truncated']), ('matched', 4, False))
        by_hash = {change['path_sha256']: change for change in row['changes']}
        changed = by_hash[driver._fingerprint(public_path)]
        self.assertEqual(changed['public_path'], public_path)
        self.assertEqual(changed['initial']['digest']['hash'], 'a' * 64)
        self.assertEqual(changed['baseline']['digest']['hash'], 'b' * 64)
        self.assertIsNone(by_hash[driver._fingerprint('/home/private/removed')]['baseline'])
        self.assertIsNone(by_hash[driver._fingerprint('tools/bazel/new.h')]['initial'])
        private = by_hash[driver._fingerprint('host/toolchain/private-name')]
        self.assertIsNone(private['public_path']); self.assertIsNone(private['initial']['digest'])
        self.assertNotEqual(private['initial']['identity_sha256'], private['baseline']['identity_sha256'])
        for value in ('/home/private', 'host/toolchain/private-name'):
            self.assertNotIn(value, json.dumps(row))
        for path in ('external/p4lang-pi++_repo_rules+pi_source/../secret',
                     'tools/bazel//host', 'tools/bazel/./host', 'tools/bazel/../../host',
                     '/tools/bazel/host', 'arbitrary/relative/name', 'tools/bazel/a\x00b'):
            with self.subTest(path=path): self.assertIsNone(driver._public_input_path(path))

    def test_input_differences_bound_rows_and_preserve_unobserved_instances(self):
        before, after = foreign_record(), foreign_record()
        before['inputs'] = []
        after['inputs'] = [{'path': 'tools/bazel/input-%03d' % n, 'digest': after['digest']} for n in range(65)]
        row = input_differences([before], [after])[0]
        self.assertEqual((row['changed_input_count'], len(row['changes']), row['truncated']), (65, 64, True))
        for left, right, status in (([], [after], 'unobserved-initial'), ([before], [], 'unobserved-baseline'),
                                     ([before, before], [after], 'ambiguous')):
            row = input_differences(left, right)[0]
            self.assertEqual(row['status'], status); self.assertIsNone(row['changed_input_count'])
            self.assertEqual(row['changes'], [])
        valid = input_differences([before], [after])
        for field in ('raw', 'count', 'truncated', 'path', 'digest', 'identity'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                value = deepcopy(valid)
                if field == 'raw': value[0]['argv'] = []
                elif field == 'count': value[0]['changed_input_count'] = 64
                elif field == 'truncated': value[0]['truncated'] = False
                elif field == 'path': value[0]['changes'][0]['public_path'] = 'host/private'
                elif field == 'digest': value[0]['changes'][0]['baseline']['digest']['hash'] = 'bad'
                else: value[0]['changes'][0]['baseline']['raw_path'] = 'private'
                driver.validate_foreign_input_differences(value)

    def test_private_explain_projects_fixed_reasons_without_configuration_attribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'baseline.explain.log'
            self.assertEqual(driver.foreign_explain(path)['status'], 'unobserved')
            header = 'Build options: --client_env=SECRET=/home/private\n'
            rows = ['Executing Foreign Cc - Configure: Building sdk: ' + reason + '.\n' for reason in driver.EXPLAIN_REASONS]
            rows += ['Executing Foreign Cc - Configure: Building bmv2_build: One of the files has changed.\n',
                     'Executing Foreign Cc - Configure: Building sdk: unknown /home/private.\n',
                     'Executing unrelated /home/private: One of the files has changed.\n']
            path.write_text(header + ''.join(rows))
            value = driver.foreign_explain(path)
            self.assertEqual((value['status'], value['matched_lines'], value['unrecognized_lines']), ('complete', 9, 1))
            self.assertNotIn('/home/private', json.dumps(value)); self.assertNotIn('SECRET', json.dumps(value))
            self.assertEqual(value['scope'], 'fixed-progress-descriptions-unbound-configurations')
            with mock.patch.object(driver, 'EXPLAIN_LIMIT', len((header + rows[0]).encode())):
                partial = driver.foreign_explain(path)
            self.assertEqual(partial['status'], 'partial'); self.assertEqual(partial['matched_lines'], 1)
            for field in ('extra', 'reason', 'count', 'status'):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    invalid = deepcopy(value)
                    if field == 'extra': invalid['raw'] = header
                    elif field == 'reason': invalid['reasons'][0]['reason'] = 'private reason'
                    elif field == 'count': invalid['matched_lines'] += 1
                    else: invalid['status'] = 'cache-hit'
                    driver.validate_foreign_explain(invalid)

    def test_public_review_retains_partial_diagnostics_only_on_failed_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); path = directory / 'incremental.json'
            value = {'status': 'failed', 'foreign_diagnostics_error': 'foreign-diagnostics-unavailable',
                     'foreign_diagnostics': {'initial': foreign_summary(), 'baseline': foreign_summary()}}
            path.write_text(json.dumps(value)); public.review(directory)
            value['status'] = 'passed'; path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'diagnostic error'): public.review(directory)
            value['status'] = 'failed'; value['foreign_comparison'] = {'raw': '/home/private'}
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'diagnostic comparison'): public.review(directory)

    def test_public_member_bound_counts_actual_indentation_and_newline(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); path = directory / 'incremental.json'
            value = {'status': 'failed', 'foreign_diagnostics': {'initial': foreign_summary()}}
            path.write_text(json.dumps(value, indent=2) + '\n')
            emitted = len(path.read_bytes())
            self.assertGreater(emitted, len(json.dumps(value).encode()))
            with mock.patch.object(public, 'DIAGNOSTIC_LIMIT', emitted): public.review(directory)
            with mock.patch.object(public, 'DIAGNOSTIC_LIMIT', emitted - 1), self.assertRaisesRegex(ValueError, 'member exceeds'):
                public.review(directory)
            path.unlink()
            value = {'status': 'built', 'foreign_diagnostics': foreign_summary()}
            (directory / 'build-receipt.json').write_text(json.dumps(value))
            emitted = len((json.dumps(value, indent=2) + '\n').encode())
            with mock.patch.object(public, 'DIAGNOSTIC_LIMIT', emitted): public.review(directory)
            self.assertEqual((directory / 'public-receipt.json').stat().st_size, emitted)
            with mock.patch.object(public, 'DIAGNOSTIC_LIMIT', emitted - 1), self.assertRaisesRegex(ValueError, 'receipt exceeds'):
                public.review(directory)

    def test_resource_options_keep_platform_fixed(self):
        with mock.patch.dict('os.environ', {'SONIC_CONTAINER_BAZEL_OPTIONS': '--jobs=1 --local_resources=memory=4000'}):
            options = driver.root_options()
        self.assertIn('--jobs=1', options)
        self.assertEqual(options[-2:], driver.OPTIONS[-2:])


class RecordedToolInputPathTest(unittest.TestCase):
    def differences(self, old, new):
        before, after = foreign_record(), foreign_record()
        digest = {'hash': '4' * 64, 'sizeBytes': '1476', 'hashFunctionName': 'SHA-256'}
        for record, path in ((before, old), (after, new)):
            record['inputs'] = [{'path': path, 'digest': digest, 'isTool': True,
                                 'symlinkTargetPath': ''}]
        return input_differences([before], [after])

    def test_recorded_tool_owner_paths_keep_exact_hash_and_digest_states(self):
        for owner in ('sonic-build-infra+', 'rules_foreign_cc+', 'bazel_tools'):
            old = 'external/' + owner + '/tools/old.runfiles/_repo_mapping'
            new = 'bazel-out/k8-opt-exec-ST-abc/bin/external/' + owner + '/tools/new.runfiles/_repo_mapping'
            with self.subTest(owner=owner):
                result = self.differences(old, new)
                row = result[0]
                self.assertEqual(row['changed_input_count'], 2)
                self.assertFalse(row['truncated'])
                changes = {change['public_path']: change for change in row['changes']}
                self.assertEqual(set(changes), {old, new})
                for path, state, absent in ((old, 'initial', 'baseline'), (new, 'baseline', 'initial')):
                    change = changes[path]
                    self.assertEqual(change['path_sha256'], driver._fingerprint(path))
                    self.assertIsNone(change[absent])
                    self.assertTrue(change[state]['is_tool'])
                    self.assertEqual(change[state]['digest']['hash'], '4' * 64)
                    self.assertEqual(change[state]['digest']['size_bytes'], 1476)
                    self.assertEqual(change[state]['symlink_target_sha256'], driver._fingerprint(''))

    def test_private_unknown_and_noncanonical_paths_remain_hash_only(self):
        rejected = [
            '/home/private/tool', 'external/private-owner/tool',
            'external/sonic-build-infra+private/tool', 'external/bazel_tools_evil/tool',
            'external/sonic-build-infra+/../private', 'external/sonic-build-infra+//tool',
            'external/sonic-build-infra+/./tool', 'external/sonic-build-infra+/tool\nsecret',
            'external/sonic-build-infra+/tool?token=secret',
            'external/sonic-build-infra+/' + 'x' * 1024,
        ]
        for path in rejected:
            with self.subTest(path=path):
                result = self.differences(path, '/other/private/tool')
                self.assertTrue(all(change['public_path'] is None for change in result[0]['changes']))
                self.assertNotIn(path, json.dumps(result))
                self.assertIn(driver._fingerprint(path), json.dumps(result))

    def test_public_schema_rejects_forged_path_hash_and_private_text(self):
        result = self.differences('external/bazel_tools/old', 'external/bazel_tools/new')
        bad_hash = deepcopy(result)
        bad_hash[0]['changes'][0]['path_sha256'] = '0' * 64
        bad_path = deepcopy(result)
        bad_path[0]['changes'][0]['public_path'] = '/private/tool'
        bad_field = deepcopy(result)
        bad_field[0]['changes'][0]['raw_command'] = 'private command'
        for invalid in (bad_hash, bad_path, bad_field):
            with self.assertRaises(ValueError):
                driver.validate_foreign_input_differences(invalid)

    def test_actual_serialized_bound_includes_public_tool_paths(self):
        result = self.differences('external/bazel_tools/old', 'external/bazel_tools/new')
        size = len((json.dumps(result, indent=2) + '\n').encode())
        with mock.patch.object(driver, 'DIAGNOSTIC_LIMIT', size):
            driver.validate_foreign_input_differences(result)
        with mock.patch.object(driver, 'DIAGNOSTIC_LIMIT', size - 1), self.assertRaises(ValueError):
            driver.validate_foreign_input_differences(result)


class ForeignArchiveComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.initial, self.live = self.root / 'initial', self.root / 'live'
        self.initial.mkdir(); self.live.mkdir()
        self.paths = {name: self.live / name for name in driver.FOREIGN_PAYLOADS}

    def archive(self, path, entries):
        import io
        with tarfile.open(path, 'w', format=tarfile.PAX_FORMAT) as archive:
            for name, content, options in entries:
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                for key, value in options.items(): setattr(member, key, value)
                if member.isfile(): member.size = len(content)
                archive.addfile(member, io.BytesIO(content) if member.isfile() else None)

    def pair(self, before=None, after=None):
        before = before if before is not None else [('usr/lib/example.so', b'payload', {})]
        after = after if after is not None else before
        for name in driver.FOREIGN_PAYLOADS:
            self.archive(self.initial / name, before)
            self.archive(self.live / name, after)
        return driver.compare_foreign_archives(self.initial, self.paths)

    def test_identical_payloads_headers_and_order(self):
        result = self.pair()
        self.assertEqual(result['status'], 'complete')
        for row in result['archives'].values():
            self.assertTrue(all(row[k] for k in ('same_file_contents', 'same_installation_metadata', 'same_header_metadata', 'same_order')))
            self.assertEqual(row['changed_member_count'], 0)

    def test_content_change_is_distinct_from_headers(self):
        result = self.pair(after=[('usr/lib/example.so', b'PAYLOAD', {})])
        for row in result['archives'].values():
            self.assertFalse(row['same_file_contents'])
            self.assertTrue(row['same_installation_metadata']); self.assertTrue(row['same_header_metadata'])
            self.assertEqual(row['changed_member_count'], 1)

    def test_mtime_only_change_is_not_payload_drift(self):
        result = self.pair(after=[('usr/lib/example.so', b'payload', {'mtime': 123})])
        for row in result['archives'].values():
            self.assertTrue(row['same_file_contents']); self.assertTrue(row['same_installation_metadata'])
            self.assertFalse(row['same_header_metadata'])

    def test_mode_change_is_installation_metadata(self):
        result = self.pair(after=[('usr/lib/example.so', b'payload', {'mode': 0o755})])
        for row in result['archives'].values():
            self.assertTrue(row['same_file_contents']); self.assertFalse(row['same_installation_metadata'])
            self.assertFalse(row['same_header_metadata'])

    def test_order_change_is_separate_from_members(self):
        entries = [('usr/a', b'a', {}), ('usr/b', b'b', {})]
        result = self.pair(entries, list(reversed(entries)))
        for row in result['archives'].values():
            self.assertTrue(row['same_file_contents']); self.assertTrue(row['same_header_metadata'])
            self.assertFalse(row['same_order']); self.assertEqual(row['changed_member_count'], 0)

    def test_private_names_links_and_pax_are_hashes_only(self):
        private = 'private-component-name'
        before = [(private, b'payload', {'uname': 'private-user', 'pax_headers': {'comment': 'private-pax'}}),
                  ('link', b'', {'type': tarfile.SYMTYPE, 'linkname': '/private/link/target'})]
        after = [(private, b'changed', {'uname': 'other-user', 'pax_headers': {'comment': 'other-pax'}}),
                 ('link', b'', {'type': tarfile.SYMTYPE, 'linkname': '/other/link/target'})]
        result = self.pair(before, after)
        self.assertEqual(result['status'], 'complete')
        output = json.dumps(result)
        for secret in (private, 'private-user', 'private-pax', '/private/link/target', 'other-user', 'other-pax', '/other/link/target'):
            self.assertNotIn(secret, output)

    def test_unsafe_path_duplicate_file_and_special_member_fail_closed(self):
        for entries in [[('../escape', b'x', {})], [('x', b'1', {}), ('x', b'2', {})],
                        [('device', b'', {'type': tarfile.CHRTYPE})]]:
            with self.subTest(entries=entries):
                result = self.pair(entries)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['reason'], 'invalid-archive')
                self.assertNotIn('escape', json.dumps(result))

    def test_duplicate_compatible_directories_preserve_order_count(self):
        entry = ('usr', b'', {'type': tarfile.DIRTYPE})
        result = self.pair([entry, entry, ('usr/file', b'a', {})])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['archives']['pi.source.tar']['initial']['member_count'], 3)

    def test_member_limit_exact_boundary_and_overflow(self):
        with mock.patch.object(driver, 'ARCHIVE_MEMBER_LIMIT', 1):
            self.assertEqual(self.pair()['status'], 'complete')
            result = self.pair([('a', b'1', {}), ('b', b'2', {})])
            self.assertEqual(result['reason'], 'member-limit')

    def test_archive_byte_limit_exact_boundary_and_overflow(self):
        self.pair()
        size = (self.initial / driver.FOREIGN_PAYLOADS[0]).stat().st_size
        with mock.patch.object(driver, 'ARCHIVE_BYTE_LIMIT', size):
            self.assertEqual(driver.compare_foreign_archives(self.initial, self.paths)['status'], 'complete')
        with mock.patch.object(driver, 'ARCHIVE_BYTE_LIMIT', size - 1):
            self.assertEqual(driver.compare_foreign_archives(self.initial, self.paths)['reason'], 'archive-byte-limit')

    def test_total_deadline_does_not_accept_partial_work(self):
        self.pair()
        with mock.patch.object(driver.time, 'monotonic', side_effect=[0, 121]):
            result = driver.compare_foreign_archives(self.initial, self.paths)
        self.assertEqual(result, {'schema': 1, 'scope': driver.ARCHIVE_COMPARISON_SCOPE, 'status': 'unavailable', 'archives': {}, 'reason': 'deadline'})

    def test_changed_rows_are_bounded_and_total_is_explicit(self):
        before = [(f'usr/{i}', b'a', {}) for i in range(65)]
        after = [(f'usr/{i}', b'b', {}) for i in range(65)]
        result = self.pair(before, after)
        for row in result['archives'].values():
            self.assertEqual(row['changed_member_count'], 65)
            self.assertEqual(len(row['changes']), 64); self.assertTrue(row['truncated'])

    def test_public_schema_rejects_extra_fields_and_false_flags(self):
        result = self.pair()
        cases = []
        bad = deepcopy(result); bad['raw_path'] = '/private'; cases.append(bad)
        bad = deepcopy(result); bad['archives']['pi.source.tar']['same_header_metadata'] = False; cases.append(bad)
        bad = deepcopy(result); bad['archives']['pi.source.tar']['initial']['raw_pax'] = 'private'; cases.append(bad)
        for bad in cases:
            with self.assertRaises(ValueError): driver.validate_foreign_archive_comparison(bad)

    def test_actual_serialized_public_bound(self):
        result = self.pair()
        size = len((json.dumps(result, indent=2) + '\n').encode())
        with mock.patch.object(driver, 'DIAGNOSTIC_LIMIT', size): driver.validate_foreign_archive_comparison(result)
        with mock.patch.object(driver, 'DIAGNOSTIC_LIMIT', size - 1), self.assertRaisesRegex(ValueError, 'public bound'):
            driver.validate_foreign_archive_comparison(result)

    def test_incomplete_diagnostic_needs_failed_public_envelope(self):
        initial = foreign_summary()
        comparison = {'schema': 1, 'scope': driver.ARCHIVE_COMPARISON_SCOPE, 'status': 'unavailable', 'archives': {}, 'reason': 'deadline'}
        data = {'status': 'failed', 'foreign_diagnostics': {'initial': initial},
                'foreign_diagnostics_error': 'foreign-diagnostics-unavailable', 'foreign_archive_comparison': comparison}
        path = self.initial / 'incremental.json'
        path.write_text(json.dumps(data)); public.review(self.initial)
        data.pop('foreign_diagnostics_error'); path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'incomplete archive comparison'): public.review(self.initial)


class BaselineArchiveRetentionTest(unittest.TestCase):
    setUp = ForeignArchiveComparisonTest.setUp
    archive = ForeignArchiveComparisonTest.archive
    pair = ForeignArchiveComparisonTest.pair

    def prepare(self):
        comparison = self.pair(after=[('usr/lib/example.so', b'changed', {})])
        payloads = {name: driver.check.sha(path) for name, path in self.paths.items()}
        result = driver.retain_baseline_archives(self.initial, self.paths, 'a' * 40, payloads)
        initial = foreign_summary(); baseline = foreign_summary(); baseline['payloads'] = payloads
        data = {'status': 'failed', 'error': 'incremental baseline differs from verified archives',
                'baseline': {'archives': {name: 'c' * 64 for name in driver.IMAGES}},
                'foreign_diagnostics': {'initial': initial, 'baseline': baseline},
                'foreign_comparison': driver.compare_foreign_diagnostics(initial, baseline),
                'foreign_input_differences': [],
                'foreign_explain': {'scope': 'fixed-progress-descriptions-unbound-configurations',
                    'status': 'unobserved', 'matched_lines': 0, 'unrecognized_lines': 0, 'reasons': []},
                'foreign_archive_comparison': comparison,
                'foreign_payloads_before_baseline': {name: driver.check.sha(self.initial / name) for name in driver.FOREIGN_PAYLOADS},
                'foreign_baseline_archives': result}
        (self.initial / 'build-receipt.json').write_text(json.dumps({'status': 'built', 'revision': 'a' * 40,
            'artifacts': {name + '.gz': {'sha256': 'a' * 64} for name in driver.IMAGES}}))
        return data, payloads

    def review(self, data):
        (self.initial / 'incremental.json').write_text(json.dumps(data))
        public.review(self.initial)

    def test_retains_two_atomic_copies_without_touching_initial_or_live(self):
        data, payloads = self.prepare()
        self.review(data)
        for name, output in driver.BASELINE_PAYLOADS.items():
            path = self.initial / output
            self.assertEqual(path.read_bytes(), self.paths[name].read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertNotEqual(driver.check.sha(self.initial / name), payloads[name])
        self.assertEqual(set(data['foreign_baseline_archives']['artifacts']), set(driver.BASELINE_PAYLOADS.values()))

    def test_exact_size_boundary_and_overflow_reject_before_copy(self):
        self.pair(); payloads = {name: driver.check.sha(path) for name, path in self.paths.items()}
        size = next(iter(self.paths.values())).stat().st_size
        with mock.patch.object(driver, 'BASELINE_ARCHIVE_LIMIT', size - 1), mock.patch.object(driver.build, 'export_archive') as copy:
            with self.assertRaisesRegex(ValueError, 'bound'): driver.retain_baseline_archives(self.initial, self.paths, 'a' * 40, payloads)
            copy.assert_not_called()
        with mock.patch.object(driver, 'BASELINE_TOTAL_LIMIT', 2 * size - 1), mock.patch.object(driver.build, 'export_archive') as copy:
            with self.assertRaisesRegex(ValueError, 'bound'): driver.retain_baseline_archives(self.initial, self.paths, 'a' * 40, payloads)
            copy.assert_not_called()
        with mock.patch.object(driver, 'BASELINE_ARCHIVE_LIMIT', size), mock.patch.object(driver, 'BASELINE_TOTAL_LIMIT', 2 * size):
            driver.retain_baseline_archives(self.initial, self.paths, 'a' * 40, payloads)

    def test_partial_copy_and_hash_drift_remove_only_fresh_diagnostics(self):
        self.pair(); payloads = {name: driver.check.sha(path) for name, path in self.paths.items()}
        original = {p: p.read_bytes() for p in [*self.paths.values(), *[self.initial / n for n in driver.FOREIGN_PAYLOADS]]}
        copy = driver.build.export_archive; calls = []
        def fail_second(source, destination):
            calls.append(destination)
            if len(calls) == 2: raise OSError('private path failure')
            copy(source, destination)
        with mock.patch.object(driver.build, 'export_archive', side_effect=fail_second):
            with self.assertRaises(OSError): driver.retain_baseline_archives(self.initial, self.paths, 'a' * 40, payloads)
        self.assertFalse(any(self.initial.glob('*.baseline.source.tar')))
        with self.assertRaisesRegex(ValueError, 'digest'):
            driver.retain_baseline_archives(self.initial, self.paths, 'a' * 40, {n: 'b' * 64 for n in driver.FOREIGN_PAYLOADS})
        self.assertFalse(any(self.initial.glob('*.baseline.source.tar')))
        self.assertEqual({p: p.read_bytes() for p in original}, original)

    def test_existing_or_dangling_destination_is_never_overwritten(self):
        self.pair(); payloads = {name: driver.check.sha(path) for name, path in self.paths.items()}
        output = self.initial / next(iter(driver.BASELINE_PAYLOADS.values()))
        for dangling in (False, True):
            if dangling: output.symlink_to('missing')
            else: output.write_bytes(b'keep prior evidence')
            with self.assertRaisesRegex(ValueError, 'fresh'):
                driver.retain_baseline_archives(self.initial, self.paths, 'a' * 40, payloads)
            if not dangling: self.assertEqual(output.read_bytes(), b'keep prior evidence')
            else: self.assertTrue(output.is_symlink())
            output.unlink()

    def test_public_review_requires_failed_state_revision_target_hash_and_files(self):
        data, _ = self.prepare(); output = next(iter(driver.BASELINE_PAYLOADS.values()))
        variants = []
        for key, value in [('status', 'passed'), ('error', 'different failure')]:
            bad = deepcopy(data); bad[key] = value; variants.append(bad)
        bad = deepcopy(data); bad['baseline']['archives'] = {name: 'a' * 64 for name in driver.IMAGES}; variants.append(bad)
        for key, value in [('revision', 'b' * 40), ('phase', 'initial'), ('purpose', 'accepted-image')]:
            bad = deepcopy(data); bad['foreign_baseline_archives'][key] = value; variants.append(bad)
        for key, value in [('target', '@other//:runtime'), ('sha256', 'c' * 64), ('bytes', 0)]:
            bad = deepcopy(data); bad['foreign_baseline_archives']['artifacts'][output][key] = value; variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad['foreign_baseline_archives']):
                with self.assertRaises(ValueError): self.review(bad)
        path = self.initial / output; content = path.read_bytes(); path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'file differs'): self.review(data)
        path.unlink()
        with self.assertRaises(ValueError): self.review(data)
        path.write_bytes(content); self.review(data)

    def test_public_review_rejects_orphan_and_unknown_diagnostic_files(self):
        data, _ = self.prepare()
        data.pop('foreign_baseline_archives')
        with self.assertRaisesRegex(ValueError, 'baseline diagnostic'): self.review(data)
        (self.initial / 'unknown.baseline.source.tar').write_bytes(b'not allowed')
        with self.assertRaisesRegex(ValueError, 'unexpected'): self.review(data)

    def test_observed_gcc_extension_only_preserves_exact_public_hash(self):
        prefix = 'bazel-out/k8-opt-exec-ST-fa012fe6b892/bin/external/'
        owner = 'sonic-build-infra++gcc+gcc_toolchains/'
        path = prefix + owner + 'host_x86_64_binutils_14.2.0_readelf.runfiles/tool'
        before = foreign_record(); after = deepcopy(before)
        after['inputs'][0]['path'] = path
        rows = input_differences([before], [after])[0]['changes']
        row = next(r for r in rows if r['public_path'] is not None)
        self.assertEqual(row['public_path'], path)
        self.assertEqual(row['path_sha256'], driver._fingerprint(path))
        for bad in [path.replace('gcc_toolchains/', 'gcc_toolchains_private/'),
                    path.replace('++gcc+', '++unknown+'), path.replace('/tool', '/../tool'),
                    '/home/private/' + path, path.replace('/bin/', '//bin/')]:
            with self.subTest(path=bad): self.assertIsNone(driver._public_input_path(bad))

    def test_failed_baseline_build_never_exports_stale_payloads(self):
        self.pair()
        receipt = {'status': 'built', 'revision': 'a' * 40,
                   'layouts': {name: name for name in driver.IMAGES}, 'foreign_diagnostics': foreign_summary()}
        (self.initial / 'build-receipt.json').write_text(json.dumps(receipt))
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(driver, 'clean_source'))
            stack.enter_context(mock.patch.object(driver, 'source_identity', return_value={'revision': 'a' * 40}))
            stack.enter_context(mock.patch.object(driver.build, 'execution_root', return_value=self.live))
            def query(argv, **kwargs):
                return next((name for name in driver.FOREIGN_PAYLOADS if driver.TARGETS[name] == argv[-1]), 'unused') + '\n'
            stack.enter_context(mock.patch.object(driver.subprocess, 'check_output', side_effect=query))
            run = stack.enter_context(mock.patch.object(driver.subprocess, 'run', return_value=subprocess.CompletedProcess(['fake'], 23)))
            retained = stack.enter_context(mock.patch.object(driver, 'retain_baseline_archives'))
            with self.assertRaises(subprocess.CalledProcessError):
                driver.incremental(SimpleNamespace(bazel='must-not-run'), self.initial)
            run.assert_called_once(); retained.assert_not_called()
        self.assertFalse(any(self.initial.glob('*.baseline.source.tar')))
        self.assertEqual(json.loads((self.initial / 'incremental.json').read_text())['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
