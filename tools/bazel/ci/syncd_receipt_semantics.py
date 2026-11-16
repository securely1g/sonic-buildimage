"""Check Syncd and Gearbox public JSON evidence without executing or reading image payloads.

The caller binds exact source/parents/catalog, module selection, fixture hashes,
worker provenance and transfer integrity. These checks preserve the existing
Syncd producer and installed-validator contracts. Large payload hashes remain
CI-reported; restored hashes do not establish action-level cache reuse.
"""
import hashlib
import re

IMAGES = ('docker-syncd-vs', 'docker-syncd-vs-dbg')
OWNER = '//platform/vs/docker-syncd-vs:'
PARENT = '//dockers/docker-config-engine-trixie:docker-config-engine-trixie'
PHASES = ('build', 'validate', 'incremental', 'loaded')
BUILD_PROFILE = {'jobs': 2, 'cpu': 2, 'jvm_heap_gib': 3,
                 'scheduler_memory_mb': 8000, 'loaded_container_memory_gib': 4}
PUBLIC_JSON_NAMES = frozenset({
    'action-audit.json', 'base-packages.json', 'build-summary.json',
    'config-engine-packages.json', 'debug-apt.json', 'debug-packages.json',
    'incremental.json', 'loaded-validation.json', 'module-graph.json',
    'policy.json', 'public-receipt.json', 'runtime-apt.json',
    'runtime-packages.json', 'source-contract.json', 'source-receipt.json',
    'validation.json', 'worker.json', 'worker-execution-tools.json',
    'worker-input-run.json', 'worker-preflight.json', 'worker-source-downloads.json',
    'worker-tap.json', *(name + '.runtime.json' for name in IMAGES),
    *('worker-' + phase + '.json' for phase in PHASES),
})
SOURCE_MODULES = frozenset({'libnl3', 'p4c', 'p4lang-bmv2', 'p4lang-pi',
                            'sonic-dash', 'sonic-fips', 'sonic-sairedis',
                            'sonic-swss-common'})
DEBUG_PATHS = frozenset({'usr/bin/sonic-db-cli', 'usr/bin/pi_CLI_dummy',
                        'usr/bin/syncd', 'usr/bin/syncd_dash'})
FIXTURE_NAMES = frozenset({'sai.profile', 'lanemap.ini', 'fabriclanemap.ini'})


def image_profile(image='syncd'):
    assert image in ('syncd', 'gearbox'), 'Unsupported image receipt profile'
    if image == 'syncd':
        return IMAGES, OWNER, 'source_runtime_payloads', 'debug_packages'
    return ('docker-gbsyncd-vs', 'docker-gbsyncd-vs-dbg'), '//platform/vs/docker-gbsyncd-vs:', 'source_runtime', 'all_debug_symbols'


def public_json_names(image='syncd'):
    images, _, _, _ = image_profile(image)
    if image == 'syncd':
        return PUBLIC_JSON_NAMES
    return (PUBLIC_JSON_NAMES - {'worker-tap.json', *(name + '.runtime.json' for name in IMAGES)}
            | {name + '.runtime.json' for name in images})


def digest(value, prefix=''):
    assert isinstance(value, str) and re.fullmatch(re.escape(prefix) + r'[0-9a-f]{64}', value)
    return value


def module_versions(value):
    result = {}

    def visit(item):
        if isinstance(item, dict):
            if item.get('name') in SOURCE_MODULES:
                result.setdefault(item['name'], set()).add(item['version'])
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return result


def verify_pool_resources(facts, expected_cpus):
    assert facts['memory_limit_bytes'] == 20 * 1024**3 and facts['cpu_limit'] == expected_cpus


def verify(read, raw, *, source, outputs, image='syncd', expected_cpus=4):
    """Return observations after the selected fixed image receipt checks pass."""
    image_names, owner, runtime_target, debug_target = image_profile(image)
    json_names = public_json_names(image)
    public, validation = read('public-receipt.json'), read('validation.json')
    assert public['status'] == 'built' and validation['status'] == 'passed'
    for document in (public, validation):
        assert document['revision'] == source and document['architecture'] == 'amd64'
    assert public['tested_revision_parents'] == validation['tested_revision_parents']
    artifacts = public['artifacts']
    assert artifacts == validation['artifacts'] and len(artifacts) == len(outputs) == 22
    assert {name: item['target'] for name, item in artifacts.items()} == outputs
    assert len(set(outputs.values())) == 22 and not any(name.endswith('.deb') for name in artifacts)
    for name, item in artifacts.items():
        digest(item['sha256'])
        assert isinstance(item['bytes'], int) and item['bytes'] > 0
        if name.endswith('.json'):
            assert name in json_names
            content = raw(name)
            assert len(content) == item['bytes'] and hashlib.sha256(content).hexdigest() == item['sha256']
    summary = read('build-summary.json')['build-events.jsonl']
    assert summary['finished'] == {'success': True, 'exit_code': 0}
    assert summary['bazel_version'] == '8.5.1' and summary['truncated'] is False
    assert set(summary['targets']) == set(outputs.values()) and all(summary['targets'].values())
    audit = read('action-audit.json')
    expected_audit = set(outputs.values()) | {owner + n for n in image_names} | {PARENT}
    assert len(expected_audit) == len(audit['targets']) == 25 and set(audit['targets']) == expected_audit
    assert audit['deb_outputs'] == audit['packaging_wrappers'] == [] and audit['action_count'] > 0

    worker, preflight = read('worker.json'), read('worker-preflight.json')
    assert worker['image'] == image and worker['source_revision'] == source
    assert worker['status'] == 'prepared' and worker['architecture'] == 'amd64'
    assert worker['distribution'] == 'trixie' and worker['docker_network'] == 'none'
    assert worker['cache_mode'] == 'persistent_worker_local'
    profile = worker['build_profile']
    assert profile == preflight['build_profile'] and all(profile[k] == v for k, v in BUILD_PROFILE.items())
    assert preflight['status'] == 'passed' and preflight['checks'] and all(v is True for v in preflight['checks'].values())
    assert preflight['image_build_executed'] is False and preflight['deb_generation_executed'] is False
    verify_pool_resources(preflight['facts'], expected_cpus)
    for phase in PHASES:
        record = read('worker-' + phase + '.json')
        assert record['phase'] == phase and record['status'] == 'passed' and record['exit_code'] == 0
        assert record['source_revision'] == source and record['image'] == image
        assert record['no_oom_events'] is True and record['build_profile'] == profile
        for moment in ('memory_before', 'memory_after'):
            assert record[moment]['limit'] == str(20 * 1024**3)
            assert all(record[moment]['events'][k] == 0 for k in ('oom', 'oom_kill', 'oom_group_kill'))

    contract, receipt = read('source-contract.json'), read('source-receipt.json')
    assert receipt['contract_sha256'] == hashlib.sha256(raw('source-contract.json')).hexdigest()
    packages = {}
    for item in contract['packages']:
        assert item['package'] not in packages
        packages[item['package']] = item
    assert len(receipt['packages']) == len(packages) and {p['package'] for p in receipt['packages']} == set(packages)
    versions = module_versions(read('module-graph.json'))
    assert set(versions) == SOURCE_MODULES and all(len(v) == 1 for v in versions.values())
    seen = set()
    for document in (contract, receipt):
        assert document['architecture'] == 'amd64' and document['distribution'] == 'trixie'
        assert document['image'] == image_names[0]
        for package in document['packages']:
            origin = package['source']; name = origin['module']
            assert name in SOURCE_MODULES and versions[name] == {origin['version']}
            if 'commit' in origin:
                assert re.fullmatch(r'[0-9a-f]{40}', origin['commit']) and origin['version'].endswith('-' + origin['commit'])
            seen.add(name)
    assert seen == SOURCE_MODULES
    for package in receipt['packages']:
        for key in ('architecture', 'control_fields', 'source', 'version'):
            assert package[key] == packages[package['package']][key]
    assert receipt['kind'] == 'bazel_source'
    assert set(receipt['module_file_sha256']) == SOURCE_MODULES
    for value in receipt['module_file_sha256'].values():
        digest(value)
    policy = read('policy.json')
    assert policy['image'] == image_names[0] and policy['architecture'] == 'amd64' and policy['distribution'] == 'trixie'
    assert policy['native']['require_source'] is True and policy['native']['gap_packages'] == []
    assert policy['image_validation']['environment'] == {'DEBIAN_FRONTEND': 'noninteractive'}
    assert set(policy['native']['required_packages']) <= set(packages)
    assert validation['source_native']['source_packages'] == receipt
    assert validation['source_native']['source_receipt_sha256'] == artifacts['source-receipt.json']['sha256']
    assert validation['p4']['contract_sha256'] == artifacts['source-contract.json']['sha256']
    assert validation['p4']['receipt_sha256'] == artifacts['source-receipt.json']['sha256']

    native, pairs = validation['native'], validation['source_debug_pairs']
    assert DEBUG_PATHS <= set(native) and len(pairs) == len(native)
    assert {p['path'] for p in pairs} == set(native)
    assert all(p['elf_machine'] == 62 for p in native.values())
    by_path = {p['path']: p for p in pairs}
    for path, pair in by_path.items():
        digest(native[path]['sha256'])
        assert pair.get('machine', native[path]['elf_machine']) == 62 and pair['build_id']
        assert pair['has_debuglink'] is True or (path == 'usr/lib/x86_64-linux-gnu/libsymcrypt.so.103.13.0' and pair['package'] == 'symcrypt-openssl' and pair.get('lookup') == 'build-id only: sealed source SymCrypt')
        if 'source' in pair:
            assert pair['package'] in packages and pair['source'] == packages[pair['package']]['source']
            assert pair['source_debug_tar_sha256'] == artifacts['source-debug.tar']['sha256']
        assert pair['debug_path'] == 'usr/lib/debug/.build-id/' + pair['build_id'][:2] + '/' + pair['build_id'][2:] + '.debug'
    if image == 'syncd':
        assert validation['accounts']['ordered_rows_and_metadata_preserved'] is True
    else:
        accounts = validation['accounts']
        assert isinstance(accounts['base_groups'], int) and accounts['base_groups'] > 0
        assert accounts['runtime_groups'] in (accounts['base_groups'], accounts['base_groups'] + 1)
        assert accounts['debug_groups'] == accounts['runtime_groups']
        assert isinstance(validation['image_version'], str) and validation['image_version'] and '\n' not in validation['image_version']
    assert validation['accounts']['declared_addition'] == '_ssh:x:104:'
    images = validation['images']; assert set(images) == set(image_names)
    runtime_layers, debug_layers = (images[n]['rootfs']['diff_ids'] for n in image_names)
    assert runtime_layers and debug_layers[:len(runtime_layers)] == runtime_layers and len(debug_layers) > len(runtime_layers)
    for image_record in images.values():
        digest(image_record['manifest_digest'], 'sha256:'); digest(image_record['config_digest'], 'sha256:')
        assert image_record['rootfs']['type'] == 'layers'
        for layer in image_record['rootfs']['diff_ids']: digest(layer, 'sha256:')
        assert len(image_record['native_paths']) == len(native) and set(image_record['native_paths']) == set(native)

    inc = read('incremental.json'); assert set(inc) == {'baseline', 'noop', 'edit', 'restore'}
    labels = {owner + n + suffix for n in image_names for suffix in ('', '.gz')}
    expected = labels | {PARENT, owner + runtime_target, owner + debug_target}
    baseline = inc['baseline']['sha256']
    for state in inc.values():
        assert state['seconds'] >= 0 and set(state['sha256']) == expected
        for label, value in state['sha256'].items(): digest(value, 'sha256:' if label in {PARENT, *(owner+n for n in image_names)} else '')
    assert baseline == inc['noop']['sha256'] == inc['restore']['sha256']
    assert {k for k, v in inc['edit']['sha256'].items() if v != baseline[k]} == labels
    assert baseline[owner + runtime_target] == artifacts['source-runtime.tar']['sha256']
    # debug_packages is the intermediate payload, not debug_package_metadata JSON.
    assert baseline[PARENT] == receipt['base_manifest_digest']

    loaded = read('loaded-validation.json')
    assert loaded['status'] == 'passed' and loaded['revision'] == source and set(loaded['images']) == set(image_names)
    fixtures = None if image == 'syncd' else {}
    private_redis = None
    for name in image_names:
        record = read(name + '.runtime.json'); assert record == loaded['images'][name]
        assert record['status'] == 'passed' and record['variant'] == name and record['native_files'] == len(native)
        assert record['archive_sha256'] == artifacts[name + '.gz']['sha256'] == baseline[owner + name + '.gz']
        assert images[name]['manifest_digest'] == baseline[owner + name]
        common_checks = ('config_and_layers_verified', 'actual_entrypoint', 'actual_syncd', 'profile_copy_and_render')
        specific_checks = (('original_core_port_map_absent', 'stable_process') if image == 'syncd'
                           else ('actual_manager', 'main_database_unchanged', 'child_exit_observed'))
        for key in common_checks + specific_checks:
            assert record[key] is True
        redis = record['private_redis']
        # This is the extracted Redis executable hash, not the helper TAR hash.
        digest(redis['sha256']); digest(redis['database_config_sha256'])
        if image == 'syncd':
            assert redis['loopback_tcp_and_unix_same_process'] is True
            redis_identity = redis
            services = {'start', 'dependent-startup', 'supervisor-proc-exit-listener', 'rsyslogd', 'syncd'}
        else:
            assert redis['unix_socket'] == '/var/run/redis/redis.sock'
            assert redis['tcp_disabled'] is False and redis['tcp_port'] == 6379 and redis['tcp_bind'] == '127.0.0.1'
            assert isinstance(redis['process_id'], int) and redis['process_id'] > 0
            redis_identity = {k: v for k, v in redis.items() if k != 'process_id'}
            services = {'dependent-startup', 'supervisor-proc-exit-listener', 'rsyslogd', 'gbsyncdmgrd'}
        if private_redis is None: private_redis = redis_identity
        assert redis_identity == private_redis
        assert set(record['service_states']) == services
        for service, state in record['service_states'].items():
            assert state['exitstatus'] == 0
            assert state['state'] in (('EXITED',) if service == 'start' else ('EXITED', 'RUNNING') if service == 'dependent-startup' else ('RUNNING',))
        if image == 'syncd':
            assert record['fixture_absent'] == ['coreportindexmap.ini']
            assert record['startup_arguments'] == ['/usr/bin/syncd', '-u', '-s', '-B', 'null', '-l', '-p', '/usr/share/sonic/hwsku/sai.profile', '-w', '30000000']
            assert set(record['fixture_sources']) == FIXTURE_NAMES
            for value in record['fixture_sources'].values(): digest(value)
            if fixtures is None: fixtures = record['fixture_sources']
            assert record['fixture_sources'] == fixtures
        else:
            assert record['context'] == 1 and record['gb_databases'] == [13, 15]
            if name.endswith('-dbg'):
                assert record['full_vim']['returncode'] == 0 and record['full_vim']['huge_version'] is True
            else:
                assert record['full_vim'] is None
        checks = record['debug_checks']
        assert len(checks) == (4 if name.endswith('-dbg') else 0)
        if checks:
            assert {c['path'].lstrip('/') for c in checks} == DEBUG_PATHS
        for check in checks:
            assert check['debug_path'].lstrip('/') == by_path[check['path'].lstrip('/')]['debug_path']
            assert check['without_symbols']['found'] is False and check['with_symbols']['found'] is True
            assert check['with_symbols']['line'] > 0 and check['with_symbols']['source'] and check['with_symbols']['type']
    if image == 'syncd':
        tap = read('worker-tap.json')
        assert tap['status'] == 'passed' and tap['private_network_namespace'] is True and tap['tap_created_and_closed'] is True
        assert (tap['device_major'], tap['device_minor']) == (10, 200)
    return {'exports': 22, 'audit_roots': 25, 'native_files': len(native),
            'source_debug_pairs': len(pairs), 'debug_checks': 4,
            'source_modules': {k: next(iter(v)) for k, v in versions.items()},
            'fixture_sources': fixtures, 'tested_revision_parents': public['tested_revision_parents'],
            'scope': ('CI-reported regular VS Syncd startup, private Redis/TAP' if image == 'syncd' else 'CI-reported Gearbox manager startup, context 1 databases and private loopback Redis') + ', native/debug and exact incremental restoration; no hardware/forwarding/full-VS or action-level cache acceptance'}
