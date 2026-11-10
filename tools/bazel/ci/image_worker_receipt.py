#!/usr/bin/env python3
"""Bind operator-run image evidence to the original normal-CI merge checkout.

Only JSON metadata is consumed. Artifact transport belongs to the pinned
download-artifact action; this module never extracts archives or executes data.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

from tools.bazel.ci.public_artifacts import safe_json, load_json
from tools.bazel.ci import p4rt_receipt_semantics, p4rt_sdk_receipt_semantics

REPOSITORY = 'securely1g/sonic-buildimage'
WORKFLOW = '.github/workflows/bazel-oci.yml'
MEMBER_LIMIT = 2 * 1024**2
TOTAL_LIMIT = 16 * 1024**2
PHASES = p4rt_receipt_semantics.PHASES
PUBLIC_JSON_NAMES = frozenset('''base.packages.json action-audit.json build-summary.json
config-engine.packages.json debug-apt.json docker-sonic-p4rt-dbg.runtime.json
docker-sonic-p4rt-dbg.validation.json docker-sonic-p4rt.runtime.json
docker-sonic-p4rt.validation.json module-graph.json incremental.json owner-action-audit.json
owner-source.json owner-installed-packages.json owner-test-action-audit.json
owner-test-results.json owner-test-summary.json owner-tests.json p4rt-debug.packages.json
p4rt-runtime.packages.json p4rt-symbols.packages.json p4rt_runtime_contract.json
p4rt_runtime_debug_contract.json public-receipt.json runtime-apt.json
sairedis.library-packages.json symbols-apt.json sdk-preparation.json validation.json
worker-build.json worker-execution-tools.json worker-incremental.json worker-loaded.json
worker-input-run.json worker-owner-tests.json worker-preflight.json
worker-prepare-owner-runtime.json worker-prepare-owner.json worker-source-downloads.json
worker-validate.json worker.json'''.split())
STEPS = (
    'Prepare private Docker and persistent worker caches',
    'Prepare the P4RT Common SDK and sealed owner TARs',
    'Prepare the P4RT owner test runtime',
    'Test all original and installed P4RT owner targets',
    'Build source runtime and debug images',
    'Validate image contents and symbols',
    'Verify incremental rebuild and restoration',
    'Validate the installed services in private containers',
    'Prepare declared artifacts and public evidence',
    "Stop only this job's private Docker daemon",
    'Package the normal CI acceptance receipts',
    'Upload the normal CI acceptance receipts',
    'Post Checkout the exact reviewed source',
    'Post Checkout reviewed dispatcher',
    'Complete job',
)


def image(value):
    selected = value.get('image', 'p4rt')
    require(selected in ('p4rt', 'syncd', 'gearbox'), 'Unsupported image receipt profile')
    return selected


def phases(value):
    return PHASES if image(value) == 'p4rt' else ('build', 'validate', 'incremental', 'loaded')


def public_json_names(value):
    if image(value) == 'p4rt':
        return PUBLIC_JSON_NAMES
    from tools.bazel.ci.syncd_receipt_semantics import public_json_names
    return public_json_names(image(value))


def required_steps(value):
    if image(value) == 'p4rt':
        return STEPS
    return tuple(name for name in STEPS if name not in (
        'Prepare the P4RT Common SDK and sealed owner TARs',
        'Prepare the P4RT owner test runtime',
        'Test all original and installed P4RT owner targets',
    ))


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def revision(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value), 'Invalid revision')
    return value


def number(value):
    require(type(value) is int and value > 0, 'Invalid numeric identity')
    return value


def read(path, *, public=True):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MEMBER_LIMIT,
            'Missing or oversized JSON')
    value = load_json(path.read_text())
    if public:
        safe_json(value)
    return value


def write(path, value, *, public=True):
    if public:
        safe_json(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2) + '\n')


def read_evidence(path, request):
    value = read(path, public=False)
    checked = value
    if image(request) in ('syncd', 'gearbox') and Path(path).name == 'policy.json':
        # The producer preserves these original bytes. Permit its one checked-in
        # image constant only in the safety view, retaining the original digest.
        checked = copy.deepcopy(value)
        require(checked['image_validation'].pop('environment') ==
                {'DEBIAN_FRONTEND': 'noninteractive'}, 'Unexpected public image environment')
    safe_json(checked)
    return value


def git(workspace, *args):
    return subprocess.check_output(['git', '-C', str(workspace), *args], text=True,
                                   env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'}).strip()


def commit_parents(workspace, commit):
    # Read the commit object itself: checkout's shallow boundary hides %P.
    header = git(workspace, 'cat-file', 'commit', commit).split('\n\n', 1)[0]
    return [line.removeprefix('parent ') for line in header.splitlines() if line.startswith('parent ')]


def api(endpoint):
    require(endpoint.startswith('/') and not endpoint.startswith('//'), 'Unexpected API endpoint')
    request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + endpoint,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(8 * 1024**2 + 1)
    require(len(data) <= 8 * 1024**2, 'Metadata response too large')
    return json.loads(data)


def output(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in values.items():
            require('\n' not in str(value), 'Invalid workflow output')
            stream.write(key + '=' + str(value) + '\n')


def inventory(response, key):
    rows = response[key]
    require(type(rows) is list and response['total_count'] == len(rows) <= 100,
            'Metadata inventory is incomplete')
    return rows


def origin_name(run, selected='p4rt'):
    require(selected in ('p4rt', 'syncd', 'gearbox'), 'Unsupported request profile')
    return selected + '-worker-request-' + str(number(run))


def acceptance_name(request):
    return image(request) + '-ci-' + str(request['normal_run']) + '-' + request['merge']


def unique_artifact(rows, name, *, optional=False):
    matches = [row for row in rows if row['name'] == name and not row['expired']]
    require(len(matches) == 1 or optional and not matches, 'Missing or duplicate eligible artifact')
    if not matches:
        return None
    item = matches[0]
    require(0 < item['size_in_bytes'] <= TOTAL_LIMIT, 'Artifact exceeds JSON envelope')
    number(item['id'])
    return item


def original_artifact(rows, run, *, optional=False, selected='p4rt'):
    name = origin_name(run, selected)
    # An expired or duplicated origin is not replaceable on a later attempt.
    matches = [row for row in rows if row['name'] == name]
    require(len(matches) <= 1, 'Duplicate original CI request')
    item = unique_artifact(matches, name, optional=optional and not matches)
    if item is not None:
        require(item['size_in_bytes'] <= MEMBER_LIMIT, 'Original request is too large')
    return item


def validate_request(value):
    selected = image(value)
    fields = {'schema', 'repository', 'workflow', 'normal_run', 'request_attempt',
              'pr', 'head', 'base', 'merge', 'tree', 'catalog', 'controller', 'contract_sha256'}
    fields |= {'owner'} if selected == 'p4rt' else {'image'}
    require(set(value) == fields, 'Unexpected request fields')
    require(type(value['schema']) is int and value['schema'] == (1 if selected == 'p4rt' else 2) and
            value['repository'] == REPOSITORY and value['workflow'] == WORKFLOW,
            'Unexpected normal CI request')
    for key in ('normal_run', 'request_attempt', 'pr'):
        number(value[key])
    for key in ('head', 'base', 'merge', 'tree', 'catalog', 'controller'):
        revision(value[key])
    if selected == 'p4rt':
        revision(value['owner'])
    require(re.fullmatch('[0-9a-f]{64}', value['contract_sha256']), 'Invalid contract digest')
    return value


def checkout_request(workspace, contract, *, event, run, attempt):
    pull = event['pull_request']
    require(pull['head']['repo']['full_name'] == pull['base']['repo']['full_name'] == REPOSITORY,
            'Fork source requires a separate reviewed validation')
    merge = revision(git(workspace, 'rev-parse', 'HEAD'))
    head, base = revision(pull['head']['sha']), revision(pull['base']['sha'])
    require(commit_parents(workspace, merge) == [base, head],
            'CI checkout is not the exact PR merge')
    selected = image(contract)
    if selected == 'p4rt':
        require(git(workspace, 'ls-tree', merge, 'src/sonic-p4rt/sonic-pins').split()[:3] ==
                ['160000', 'commit', contract['owner']], 'Selected owner changed')
    request = {'schema': 1 if selected == 'p4rt' else 2, 'repository': REPOSITORY, 'workflow': WORKFLOW,
        'normal_run': number(run), 'request_attempt': number(attempt), 'pr': number(pull['number']),
        'head': head, 'base': base, 'merge': merge,
        'tree': git(workspace, 'rev-parse', 'HEAD^{tree}'), 'catalog': contract['catalog'],
        'controller': contract['controller'],
        'contract_sha256': sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode())}
    request.update({'owner': contract['owner']} if selected == 'p4rt' else {'image': selected})
    validate_request(request)
    reviewed_worker_pool(request, contract)
    return request


def same_checkout(original, current):
    validate_request(original)
    validate_request(current)
    require(original['request_attempt'] <= current['request_attempt'] and
            {k: v for k, v in original.items() if k != 'request_attempt'} ==
            {k: v for k, v in current.items() if k != 'request_attempt'},
            'Receipt request belongs to another CI checkout or run')


def normal_origin(args):
    require(os.environ['GITHUB_REPOSITORY'] == REPOSITORY and
            os.environ['GITHUB_EVENT_NAME'] == 'pull_request', 'Expected normal same-repository PR CI')
    contract = read(args.contract)
    current = checkout_request(args.workspace, contract, event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()),
        run=int(os.environ['GITHUB_RUN_ID']), attempt=int(os.environ['GITHUB_RUN_ATTEMPT']))
    rows = inventory(api('/actions/runs/' + str(current['normal_run']) + '/artifacts?per_page=100'), 'artifacts')
    existing = original_artifact(rows, current['normal_run'], optional=True, selected=image(current))
    if existing is None:
        require(not args.existing_only and current['request_attempt'] == 1 and
                not any(row['name'] == origin_name(current['normal_run'], image(current)) for row in rows),
                'A rerun cannot mint a replacement request; start a new normal workflow run')
        write(args.directory / 'request.json', current)
        output(new_request='true', request_name=origin_name(current['normal_run'], image(current)))
    else:
        require(existing['workflow_run']['id'] == current['normal_run'] and
                existing['workflow_run']['head_sha'] == current['head'], 'Request artifact run differs')
        output(new_request='false', request_artifact=existing['id'], request_run=current['normal_run'])


def reviewed_worker_pool(request, contract):
    """Choose policy only from the entire checked-in, request-bound contract."""
    validate_request(request)
    require(sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()) == request['contract_sha256']
            and contract['controller'] == request['controller'] and image(contract) == image(request)
            and contract.get('owner') == request.get('owner') and contract['catalog'] == request['catalog'],
            'Reviewed source contract differs')
    pool = contract.get('worker_pool', 'sonic-slave-container-dpool')
    require(type(pool) is str and pool in ('sonic-slave-container-dpool', 'sonic-slave-container-k3s'),
            'Unsupported reviewed worker pool')
    return pool


def assigned_worker_job(request, pool, jobs, *, run, attempt, completed):
    """Bind the pool policy to GitHub's actual assigned job, never artifact hints."""
    number(run)
    number(attempt)
    selected = [j for j in jobs if j['name'] == 'Source image (' + image(request) + ')']
    require(len(selected) == 1, 'Ambiguous source-image job')
    job = selected[0]
    require(job['run_id'] == run and job['run_attempt'] == attempt and
            job['head_sha'] == request['controller'] and
            job['status'] == ('completed' if completed else 'in_progress') and
            (job['conclusion'] == 'success' if completed else job['conclusion'] is None),
            'Worker job identity or state differs')
    number(job['id'])
    number(job['runner_id'])
    name, labels = job['runner_name'], job['labels']
    require(type(name) is str and type(labels) is list and all(type(v) is str for v in labels),
            'Invalid assigned worker identity')
    if pool == 'sonic-slave-container-dpool':
        require(re.fullmatch('sonic-slave-lgh114-[1-4]', name) and pool in labels and name in labels,
                'Worker identity differs')
    elif pool == 'sonic-slave-container-k3s':
        require(re.fullmatch('ks-[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?', name) and pool in labels,
                'Worker identity differs')
    else:
        require(False, 'Unsupported reviewed worker pool')
    return job


def verify_k3s_preflight(preflight, job):
    """Verify the pinned K3s cache layout and measured, assigned worker identity."""
    facts, checks = preflight['facts'], preflight['checks']
    require(facts['pool'] == 'sonic-slave-container-k3s' and facts['worker'] == job['runner_name'] and
            all(checks.get(k) is True for k in ('cache_mount', 'cache_storage', 'cache_writable')),
            'K3s assigned worker or cache checks differ')
    root, storage = facts['cache_root'], facts['cache_storage']
    expected = {'namespace': 'actions_work_source_images',
                'worker_directory': 'source-images/' + job['runner_name'],
                'backing_mount': 'actions', 'shared_with_workspace': True,
                'lifetime': 'current_worker_mount', 'cross_pod_reuse': 'not_established',
                'uid': 1000, 'gid': 1000, 'mode': '0o700', 'writable': True, 'write_probe_closed': True}
    require(set(root) == set(expected) | {'work_root_uid', 'work_root_gid', 'work_root_mode'} and
            all(type(root[k]) is type(v) and root[k] == v for k, v in expected.items()) and
            all(type(root[k]) is int and root[k] >= 0 for k in ('work_root_uid', 'work_root_gid')) and
            type(root['work_root_mode']) is str and re.fullmatch('0o[0-7]{3,4}', root['work_root_mode']),
            'K3s private cache directory differs')
    require(set(storage) == {'filesystem', 'free_bytes', 'backing_mount', 'shared_with_workspace'} and
            storage['filesystem'] in ('ext4', 'xfs') and storage['backing_mount'] == 'actions' and
            storage['shared_with_workspace'] is True and type(storage['free_bytes']) is int and
            storage['free_bytes'] >= 50 * 1024**3, 'K3s cache storage admission differs')


def verify_worker_profile(worker, preflight, job, pool):
    profile = worker['build_profile']
    require(profile == preflight['build_profile'] and profile['jobs'] == profile['cpu'] == 2 and
            profile['jvm_heap_gib'] == 3 and profile['scheduler_memory_mb'] == 8000 and
            profile['loaded_container_memory_gib'] == 4 and
            preflight['facts']['memory_limit_bytes'] == 20 * 1024**3 and
            preflight['facts']['cpu_limit'] == (6 if pool == 'sonic-slave-container-k3s' else 4),
            'Reviewed resource profile changed')
    if pool == 'sonic-slave-container-k3s':
        verify_k3s_preflight(preflight, job)
    return profile


def worker_metadata(request, artifact, run, jobs, contract):
    pool = reviewed_worker_pool(request, contract)
    require(artifact['name'] == acceptance_name(request) and artifact['expired'] is False and
            0 < artifact['size_in_bytes'] <= TOTAL_LIMIT, 'Unexpected worker artifact')
    require(run['id'] == artifact['workflow_run']['id'] and run['head_sha'] == request['controller'] and
            artifact['workflow_run']['head_sha'] == request['controller'] and
            run['repository']['full_name'] == REPOSITORY and run['path'] == WORKFLOW and
            run['head_repository']['full_name'] == REPOSITORY and
            artifact['workflow_run']['repository_id'] == artifact['workflow_run']['head_repository_id'] == run['repository']['id'] and
            run['event'] == 'workflow_dispatch' and run['actor']['login'] == 'securely1g' and
            run['triggering_actor']['login'] == 'securely1g' and
            run['status'] == 'completed' and run['conclusion'] == 'success', 'Untrusted or incomplete worker run')
    number(run['run_attempt'])
    require(all(j['status'] == 'completed' and j['conclusion'] in ('success', 'skipped') for j in jobs),
            'Worker lifecycle is incomplete or adverse')
    job = assigned_worker_job(request, pool, jobs, run=run['id'], attempt=run['run_attempt'], completed=True)
    steps = job['steps']
    require(all(s['status'] == 'completed' and s['conclusion'] in ('success', 'skipped') for s in steps),
            'Incomplete worker cleanup or phase')
    for name in required_steps(request):
        matches = [s for s in steps if s['name'] == name]
        require(len(matches) == 1 and matches[0]['conclusion'] == 'success', 'Required worker step did not pass: ' + name)
    return job


def discover(args):
    original = read(args.directory / 'request.json')
    contract = read(args.contract)
    current = checkout_request(args.workspace, contract,
        event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()),
        run=int(os.environ['GITHUB_RUN_ID']), attempt=int(os.environ['GITHUB_RUN_ATTEMPT']))
    same_checkout(original, current)
    origin = original_artifact(inventory(api('/actions/runs/' + str(original['normal_run']) +
        '/artifacts?per_page=100'), 'artifacts'), original['normal_run'], selected=image(original))
    require(origin['workflow_run']['id'] == original['normal_run'] and
            origin['workflow_run']['head_sha'] == original['head'], 'Original artifact provenance changed')
    name = acceptance_name(original)
    rows = inventory(api('/actions/artifacts?per_page=100&name=' + urllib.parse.quote(name, safe='')), 'artifacts')
    artifact = unique_artifact(rows, name)
    run = api('/actions/runs/' + str(artifact['workflow_run']['id']))
    jobs = inventory(api('/actions/runs/' + str(run['id']) + '/jobs?filter=latest&per_page=100'), 'jobs')
    job = worker_metadata(original, artifact, run, jobs, contract)
    write(args.directory / 'provenance.json', {'artifact': artifact, 'run': run, 'job': job,
                                             'jobs': jobs, 'origin': origin}, public=False)
    output(artifact=artifact['id'], worker_run=run['id'])


def worker_origin(args):
    require(os.environ['GITHUB_REPOSITORY'] == REPOSITORY and os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch'
            and os.environ['GITHUB_ACTOR'] == 'securely1g', 'Expected reviewed manual dispatch')
    artifact = api('/actions/artifacts/' + str(number(args.artifact)))
    run = api('/actions/runs/' + str(artifact['workflow_run']['id']))
    require(not artifact['expired'] and artifact['name'] == origin_name(run['id'], args.image) and
            0 < artifact['size_in_bytes'] <= MEMBER_LIMIT and run['repository']['full_name'] == REPOSITORY and
            run['event'] == 'pull_request' and run['path'] == WORKFLOW and
            run['head_repository']['full_name'] == REPOSITORY and
            artifact['workflow_run']['repository_id'] == artifact['workflow_run']['head_repository_id'] == run['repository']['id'] and
            artifact['workflow_run']['head_sha'] == run['head_sha'], 'Untrusted request artifact')
    selected = original_artifact(inventory(api('/actions/runs/' + str(run['id']) +
        '/artifacts?per_page=100'), 'artifacts'), run['id'], selected=args.image)
    require(selected['id'] == artifact['id'], 'Original artifact identity changed')
    write(args.directory / 'origin.json', {'artifact': artifact, 'run': run}, public=False)
    output(request_artifact=artifact['id'], request_run=run['id'])


def bind_worker(args):
    payload = args.directory / 'payload'
    require(payload.is_dir() and not payload.is_symlink() and
            {p.name for p in payload.iterdir()} == {'request.json'}, 'Unexpected request artifact files')
    request = validate_request(read(payload / 'request.json'))
    require(image(request) == args.image == os.environ['SELECTED_IMAGE'], 'Dispatch image differs from CI request')
    origin = read(args.directory / 'origin.json', public=False)
    require(origin['run']['id'] == request['normal_run'] and origin['run']['head_sha'] == request['head'] and
            origin['run']['run_attempt'] >= request['request_attempt'] and
            origin['artifact']['name'] == origin_name(request['normal_run'], image(request)), 'Original request run changed')
    require(request['merge'] == os.environ['SOURCE_REVISION'] and request['controller'] == os.environ['GITHUB_SHA'] and
            request['catalog'] == os.environ['SOURCE_REGISTRY_REVISION'], 'Dispatch differs from CI request')
    require(git(args.workspace, 'rev-parse', 'HEAD') == request['merge'] and
            git(args.workspace, 'rev-parse', 'HEAD^{tree}') == request['tree'] and
            commit_parents(args.workspace, 'HEAD') == [request['base'], request['head']],
            'Worker checkout differs from original normal CI merge')
    contract = read(args.workspace / ('tools/bazel/ci/' + image(request) + '_worker_contract.json'))
    pool = reviewed_worker_pool(request, contract)
    require(os.environ.get('WORKER_POOL', 'sonic-slave-container-dpool') == pool,
            'Dispatched pool differs from reviewed source contract')
    require(os.environ['GITHUB_ACTOR'] == os.environ['GITHUB_TRIGGERING_ACTOR'] == 'securely1g',
            'Untrusted worker dispatch actor')
    pull = api('/pulls/' + str(request['pr']))
    require(pull['state'] == 'open' and pull['head']['sha'] == request['head'] and pull['base']['sha'] == request['base']
            and pull['head']['repo']['full_name'] == pull['base']['repo']['full_name'] == REPOSITORY,
            'PR head/base changed before worker admission')
    worker_run, worker_attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    jobs = inventory(api('/actions/runs/' + str(number(worker_run)) + '/jobs?filter=latest&per_page=100'), 'jobs')
    job = assigned_worker_job(request, pool, jobs, run=worker_run, attempt=worker_attempt, completed=False)
    require(job['runner_name'] == os.environ['RUNNER_NAME'], 'Assigned worker differs from executing runner')
    write(args.directory / 'binding.json', {'request': request, 'request_artifact': origin['artifact']['id'],
                                           'request_sha256': sha((args.directory / 'payload' / 'request.json').read_bytes())})


def bundle(args):
    binding = read(args.directory / 'binding.json')
    request = validate_request(binding['request'])
    require(sha((args.directory / 'payload' / 'request.json').read_bytes()) == binding['request_sha256'], 'Request changed')
    worker = read(args.public / 'worker.json')
    require(worker['source_revision'] == request['merge'] and worker['dispatcher_revision'] == request['controller'] and
            worker['review_catalog'] == request['catalog'] and worker['image'] == image(request), 'Worker selection changed')
    for phase in phases(request):
        row = read(args.public / ('worker-' + phase + '.json'))
        require(row['phase'] == phase and row['status'] == 'passed' and row['exit_code'] == 0 and
                row['source_revision'] == request['merge'] and row['image'] == image(request) and row['no_oom_events'] is True,
                'Only completed native acceptance may be bundled')
    rows = {}
    total = 0
    for path in sorted(args.public.glob('*.json')):
        require(not path.is_symlink() and path.is_file() and path.name in public_json_names(request), 'Unsafe receipt file')
        read_evidence(path, request)
        total += path.stat().st_size
        require(total <= TOTAL_LIMIT, 'JSON acceptance exceeds bound')
        rows[path.name] = {'sha256': sha(path.read_bytes()), 'bytes': path.stat().st_size}
    require(public_json_names(request) - {'worker-input-run.json'} <= rows.keys(), 'Incomplete public acceptance')
    args.output.mkdir(exist_ok=False)
    for name in rows:
        shutil.copyfile(args.public / name, args.output / name)
    write(args.output / 'ci-binding.json', {'schema': 1, **binding, 'files': rows,
        'worker_run': int(os.environ['GITHUB_RUN_ID']), 'worker_attempt': int(os.environ['GITHUB_RUN_ATTEMPT'])})
    require(sum(p.stat().st_size for p in args.output.iterdir()) <= TOTAL_LIMIT, 'Envelope exceeds bound')
    output(acceptance_name=acceptance_name(request))


def verify_evidence(directory, request, provenance, contract):
    """Pure JSON verification; callers separately establish the normal checkout."""
    require(__debug__, 'Receipt verification requires Python assertions enabled')
    pool = reviewed_worker_pool(request, contract)
    require(directory.is_dir() and not directory.is_symlink(), 'Unsafe acceptance directory')
    job = worker_metadata(request, provenance['artifact'], provenance['run'], provenance['jobs'], contract)
    origin = provenance['origin']
    require(not origin['expired'] and origin['name'] == origin_name(request['normal_run'], image(request)) and
            origin['workflow_run']['id'] == request['normal_run'] and
            origin['workflow_run']['head_sha'] == request['head'], 'Original normal request provenance differs')
    binding = read(directory / 'ci-binding.json')
    require(set(binding) == {'schema', 'request', 'request_artifact', 'request_sha256', 'files',
                            'worker_run', 'worker_attempt'} and type(binding['schema']) is int and
            binding['schema'] == 1 and binding['request'] == request and
            binding['request_artifact'] == provenance['origin']['id'] and
            binding['worker_run'] == provenance['run']['id'] and
            binding['worker_attempt'] == provenance['run']['run_attempt'], 'CI receipt binding differs')
    require(binding['request_sha256'] == provenance['request_sha256'], 'Original request bytes changed')
    rows = binding['files']
    require(type(rows) is dict and public_json_names(request) - {'worker-input-run.json'} <= rows.keys() <= public_json_names(request),
            'Invalid selected JSON inventory')
    paths = list(directory.iterdir())
    require({p.name for p in paths} == set(rows) | {'ci-binding.json'} and
            sum(p.stat().st_size for p in paths) <= TOTAL_LIMIT, 'Unexpected acceptance files or size')
    for name, row in rows.items():
        require(Path(name).name == name and name.endswith('.json') and
                set(row) == {'bytes', 'sha256'}, 'Unexpected receipt member')
        path = directory / name
        read_evidence(path, request)
        require(type(row['bytes']) is int and row['bytes'] == path.stat().st_size and
                sha(path.read_bytes()) == row['sha256'], 'Receipt bytes changed')
    load = lambda name: read_evidence(directory / name, request)
    raw = lambda name: (directory / name).read_bytes()
    worker, preflight = load('worker.json'), load('worker-preflight.json')
    require(worker['source_revision'] == request['merge'] and worker['dispatcher_revision'] == request['controller']
            and worker['review_catalog'] == request['catalog'] and worker['image'] == image(request)
            and worker['worker'] == job['runner_name'] and worker['architecture'] == 'amd64'
            and worker['distribution'] == 'trixie' and worker['cache_mode'] == 'persistent_worker_local'
            and worker['docker_network'] == 'none', 'Worker receipt identity differs')
    require(preflight['status'] == 'passed' and preflight['checks'] and
            all(v is True for v in preflight['checks'].values()) and
            preflight['image_build_executed'] is False and preflight['deb_generation_executed'] is False,
            'Worker preflight did not pass')
    profile = verify_worker_profile(worker, preflight, job, pool)
    owner_profile = {'revision': request.get('owner'), 'bazel': '6.4.0', 'jobs': 1, 'jvm_heap_gib': 2,
                     'scheduler_memory_mb': 4096, 'cache_mode': 'separate_persistent_worker_local'}
    if image(request) == 'p4rt':
        require(worker['owner_profile'] == owner_profile, 'Owner resource profile differs')
    for phase in phases(request):
        receipt = load('worker-' + phase + '.json')
        require(receipt['phase'] == phase and receipt['status'] == 'passed' and receipt['exit_code'] == 0
                and receipt['source_revision'] == request['merge'] and receipt['image'] == image(request)
                and receipt['no_oom_events'] is True and receipt['build_profile'] == profile
                and (image(request) != 'p4rt' or receipt['owner_profile'] == owner_profile),
                'Native phase did not pass: ' + phase)
        for moment in ('memory_before', 'memory_after'):
            require(receipt[moment]['limit'] == str(20 * 1024**3) and
                    all(receipt[moment]['events'][key] == 0 for key in ('oom', 'oom_kill', 'oom_group_kill')),
                    'Native memory limit or OOM result changed')
    if image(request) == 'p4rt':
        require(len(contract['test_targets']) == len(set(contract['test_targets'])) == 59,
                'The original 59-test inventory changed')
        observed = p4rt_receipt_semantics.verify(load, raw, source=request['merge'], owner=request['owner'],
            outputs=contract['outputs'], test_targets=contract['test_targets'],
            owner_test_helper_sha256=contract['owner_test_helper_sha256'])
        sdk = p4rt_sdk_receipt_semantics.verify(load, source=request['merge'], owner=request['owner'],
            preparation_targets=contract['preparation_outputs'], common_revision=contract['common_revision'])
    else:
        from tools.bazel.ci import syncd_receipt_semantics
        observed = syncd_receipt_semantics.verify(load, raw, source=request['merge'],
            outputs=contract['outputs'], image=image(request))
        require(observed['fixture_sources'] == contract['fixture_sources'], 'Installed-service fixture sources changed')
        require(observed['tested_revision_parents'] == [request['base'], request['head']],
                'Installed-service validation tested another merge')
        sdk = None
    audit = load('action-audit.json')
    require(audit['deb_outputs'] == audit['packaging_wrappers'] == [] and
            len(audit['targets']) == len(set(audit['targets'])) == (31 if image(request) == 'p4rt' else 25) and
            set(audit['targets']) == set(contract['audit_labels']), 'Image no-DEB scope changed')
    summary = load('build-summary.json')['build-events.jsonl']
    require(summary['finished'] == {'success': True, 'exit_code': 0} and summary['bazel_version'] == '8.5.1'
            and summary['truncated'] is False and summary['tests'] == {}, 'Image build summary differs')
    normalize = lambda label: '@sonic_sairedis//' + label.split('//', 1)[1] if label.startswith('@@sonic-sairedis+//') else label
    require(len(summary['targets']) == (27 if image(request) == 'p4rt' else 22) and set(summary['targets'].values()) == {True} and
            {normalize(label) for label in summary['targets']} == set(contract['outputs'].values()),
            'Image build output inventory changed')
    modules = {}
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get('name'), str) and isinstance(value.get('version'), str):
                modules.setdefault(value['name'], set()).add(value['version'])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(load('module-graph.json'))
    require(all(modules.get(name) == {version} for name, version in contract['selected_modules'].items()),
            'Resolved dependency selection changed')
    for name, row in load('public-receipt.json')['artifacts'].items():
        if name in rows:
            require(rows[name] == {'bytes': row['bytes'], 'sha256': row['sha256']}, 'Declared JSON output changed')
    return {'status': 'passed', 'request': request, 'worker_run': provenance['run']['id'],
            'worker_attempt': provenance['run']['run_attempt'], 'job': job['id'],
            'artifact': provenance['artifact']['id'], 'observed': observed, 'sdk': sdk,
            'limits': 'Selected JSON independently hashed; native payload bytes and runtime checks remain CI-reported.'}


def verify(args):
    original = read(args.directory / 'request.json')
    current = checkout_request(args.workspace, read(args.contract),
        event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()),
        run=int(os.environ['GITHUB_RUN_ID']), attempt=int(os.environ['GITHUB_RUN_ATTEMPT']))
    same_checkout(original, current)
    provenance = read(args.directory / 'provenance.json', public=False)
    provenance['request_sha256'] = sha((args.directory / 'request.json').read_bytes())
    result = verify_evidence(args.public, original, provenance, read(args.contract))
    write(args.output, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('origin', 'discover', 'worker-origin', 'bind-worker', 'bundle', 'verify'))
    parser.add_argument('--image', choices=('p4rt', 'syncd', 'gearbox'), default='p4rt')
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--contract', type=Path)
    parser.add_argument('--artifact', type=int)
    parser.add_argument('--public', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--existing-only', action='store_true')
    args = parser.parse_args()
    {'origin': normal_origin, 'discover': discover, 'worker-origin': worker_origin,
     'bind-worker': bind_worker, 'bundle': bundle, 'verify': verify}[args.mode](args)


if __name__ == '__main__':
    main()
