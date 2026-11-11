"""Pure review of three historical uploads, never canonical native acceptance.

Callers must authenticate original API responses and the bounded ZIP inventory.
This module does not acquire, extract, execute, repair, adopt, or publish anything.
The complete ZIP has not yet been inspected; logical member paths are explicit
inputs, never inferred from basenames. A passing result still requires reviewed
source equivalence, fresh loaded-runtime/cleanup evidence and a separate consumer
policy before it can be used. Canonical verify_evidence remains unchanged.
"""
import hashlib
from pathlib import PurePosixPath
import re

from tools.bazel.ci import image_worker_receipt as worker
from tools.bazel.ci import syncd_receipt_semantics

MAX_ZIP_BYTES = 16 * 1024**3
MAX_EXPANDED_BYTES = 64 * 1024**3
MAX_MEMBERS = 10000
CLEANUP = "Stop only this job's private Docker daemon"
CASES = {'gearbox': {'artifact': 13793684422,
             'job': 129952774563,
             'request_sha256': 'b01d9b028a89b124ee8c0df9e2ef05255c7dd3ce1082efa553b17661415fe51d',
             'run': 42851406956,
             'runner_name': 'ks-e6fb1976-715102e6996d-3-9700561e1db94bb3916a06a1a5209ad4',
             'skipped_steps': ('Verify Teamd kernel prerequisites without host changes',
                               'Download the verified source archives',
                               'Prepare the P4RT Common SDK and sealed owner TARs',
                               'Prepare the P4RT owner test runtime',
                               'Test all original and installed P4RT owner targets',
                               'Package the normal CI acceptance receipts',
                               'Upload the normal CI acceptance receipts'),
             'success_steps': ('Set up job',
                               'Require fresh native worker paths',
                               'Checkout reviewed dispatcher',
                               'Checkout the registry mirror configuration',
                               'Validate the fixed image selection',
                               'Checkout the exact reviewed source',
                               'Verify the normal CI request artifact provenance',
                               'Download the immutable normal CI request',
                               'Bind the exact normal CI merge before native work',
                               'Verify the existing successful source-download producer',
                               'Reuse the existing source-download artifact',
                               'Configure Debian downloads for the native worker',
                               'Prepare private Docker and persistent worker caches',
                               'Build source runtime and debug images',
                               'Validate image contents and symbols',
                               'Verify incremental rebuild and restoration',
                               'Validate the installed services in private containers',
                               'Prepare declared artifacts and public evidence',
                               'Scan staged source-image artifacts for private mirror locations',
                               'Upload source images and acceptance receipts',
                               "Remove this worker's mirror configuration",
                               'Post Checkout the exact reviewed source',
                               'Post Checkout the registry mirror configuration',
                               'Post Checkout reviewed dispatcher',
                               'Complete job')},
 'p4rt': {'artifact': 13803477455,
          'job': 129985415221,
          'request_sha256': '6b4ba73a07848a00ebfd67230848f3722afaf028e255099bd8daaafb3142b4fb',
          'run': 42860613563,
          'runner_name': 'ks-e6fb1976-715102e6996d-0-9f936d549e9b4e2790c351ad54078bc6',
          'skipped_steps': ('Verify Teamd kernel prerequisites without host changes',
                            'Download the verified source archives',
                            'Package the normal CI acceptance receipts',
                            'Upload the normal CI acceptance receipts'),
          'success_steps': ('Set up job',
                            'Require fresh native worker paths',
                            'Checkout reviewed dispatcher',
                            'Checkout the registry mirror configuration',
                            'Validate the fixed image selection',
                            'Checkout the exact reviewed source',
                            'Verify the normal CI request artifact provenance',
                            'Download the immutable normal CI request',
                            'Bind the exact normal CI merge before native work',
                            'Verify the existing successful source-download producer',
                            'Reuse the existing source-download artifact',
                            'Configure Debian downloads for the native worker',
                            'Prepare private Docker and persistent worker caches',
                            'Prepare the P4RT Common SDK and sealed owner TARs',
                            'Prepare the P4RT owner test runtime',
                            'Test all original and installed P4RT owner targets',
                            'Build source runtime and debug images',
                            'Validate image contents and symbols',
                            'Verify incremental rebuild and restoration',
                            'Validate the installed services in private containers',
                            'Prepare declared artifacts and public evidence',
                            'Scan staged source-image artifacts for private mirror locations',
                            'Upload source images and acceptance receipts',
                            "Remove this worker's mirror configuration",
                            'Post Checkout the exact reviewed source',
                            'Post Checkout the registry mirror configuration',
                            'Post Checkout reviewed dispatcher',
                            'Complete job')},
 'syncd': {'artifact': 13806772663,
           'job': 130052047894,
           'request_sha256': 'cc82a76052fcb8944bcf19a30c781aaa2e62586558e416b5c126c49fc3c077dc',
           'run': 42879609305,
           'runner_name': 'ks-e6fb1976-715102e6996d-2-9b51841e40f640f6ba133d83b2406927',
           'skipped_steps': ('Verify Teamd kernel prerequisites without host changes',
                             'Download the verified source archives',
                             'Prepare the P4RT Common SDK and sealed owner TARs',
                             'Prepare the P4RT owner test runtime',
                             'Test all original and installed P4RT owner targets',
                             'Package the normal CI acceptance receipts',
                             'Upload the normal CI acceptance receipts'),
           'success_steps': ('Set up job',
                             'Set up runner',
                             'Require fresh native worker paths',
                             'Checkout reviewed dispatcher',
                             'Checkout the registry mirror configuration',
                             'Validate the fixed image selection',
                             'Checkout the exact reviewed source',
                             'Verify the normal CI request artifact provenance',
                             'Download the immutable normal CI request',
                             'Bind the exact normal CI merge before native work',
                             'Verify the existing successful source-download producer',
                             'Reuse the existing source-download artifact',
                             'Configure Debian downloads for the native worker',
                             'Prepare private Docker and persistent worker caches',
                             'Build source runtime and debug images',
                             'Validate image contents and symbols',
                             'Verify incremental rebuild and restoration',
                             'Validate the installed services in private containers',
                             'Prepare declared artifacts and public evidence',
                             'Scan staged source-image artifacts for private mirror locations',
                             'Upload source images and acceptance receipts',
                             "Remove this worker's mirror configuration",
                             'Post Checkout the exact reviewed source',
                             'Post Checkout the registry mirror configuration',
                             'Post Checkout reviewed dispatcher',
                             'Complete job')}}

def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def original_request(data, contract):
    require(__debug__, 'Assertions must remain enabled')
    require(type(data) is bytes and 0 < len(data) <= worker.MEMBER_LIMIT,
            'Original request bytes are missing or oversized')
    request = worker.validate_request(worker.load_json(data.decode('utf-8')))
    case = CASES[worker.image(request)]
    require(digest(data) == case['request_sha256'], 'Original request bytes changed')
    require(worker.reviewed_worker_pool(request, contract) == 'sonic-slave-container-k3s',
            'Original reviewed worker pool differs')
    return request, case


def original_artifact(artifact, case, request):
    require(artifact['id'] == case['artifact'] and artifact['expired'] is False and
            artifact['name'] == 'source-image-' + worker.image(request) + '-' + str(case['run']) + '-1' and
            artifact['workflow_run']['id'] == case['run'] and
            artifact['workflow_run']['head_sha'] == request['controller'],
            'Original artifact identity differs')
    require(type(artifact['size_in_bytes']) is int and
            0 < artifact['size_in_bytes'] <= MAX_ZIP_BYTES and
            type(artifact['digest']) is str and
            re.fullmatch(r'sha256:[0-9a-f]{64}', artifact['digest']),
            'Original artifact size or digest is unavailable')


def admit_original(data, contract, run, job, artifact):
    """Admit only inspection of an authenticated original cleanup-only failure."""
    request, case = original_request(data, contract)
    require(run['id'] == case['run'] and type(run['run_attempt']) is int and run['run_attempt'] == 1 and
            run['head_sha'] == request['controller'] and
            run['head_branch'] == 'codex/source-image-worker-p4rt-feature' and
            run['workflow_id'] == 380324045 and run['path'] == worker.WORKFLOW and
            run['event'] == 'workflow_dispatch' and run['status'] == 'completed' and
            run['conclusion'] == 'failure' and
            run['repository']['full_name'] == run['head_repository']['full_name'] == worker.REPOSITORY and
            run['actor']['login'] == run['triggering_actor']['login'] == 'securely1g',
            'Original failed producer run differs')
    require(job['id'] == case['job'] and job['run_id'] == case['run'] and
            type(job['run_attempt']) is int and job['run_attempt'] == 1 and job['head_sha'] == request['controller'] and
            job['name'] == 'Source image (' + worker.image(request) + ')' and
            job['status'] == 'completed' and job['conclusion'] == 'failure' and
            job['runner_name'] == case['runner_name'] and
            type(job['runner_id']) is int and job['runner_id'] > 0 and
            job['labels'] == ['sonic-slave-container-k3s'], 'Original selected job differs')
    steps = job['steps']
    require(type(steps) is list and len(steps) <= 100 and
            len({s['name'] for s in steps}) == len(steps), 'Ambiguous original steps')
    by_name = {s['name']: s for s in steps}
    expected = set(case['success_steps']) | set(case['skipped_steps']) | {CLEANUP}
    require(set(by_name) == expected and all(s['status'] == 'completed' for s in steps),
            'Original step inventory differs')
    require(by_name[CLEANUP]['conclusion'] == 'failure' and
            all(by_name[n]['conclusion'] == 'success' for n in case['success_steps']) and
            all(by_name[n]['conclusion'] == 'skipped' for n in case['skipped_steps']),
            'Cleanup was not the sole original failure')
    original_artifact(artifact, case, request)
    return {'schema': 1, 'kind': 'native_failed_upload_inspection',
            'status': 'ORIGINAL_FAILURE_ADMITTED_FOR_INSPECTION_ONLY',
            'image': worker.image(request), 'original_request_sha256': digest(data),
            'original_request': request, 'original_run': case['run'],
            'original_job': case['job'], 'original_artifact': case['artifact'],
            'original_conclusion': 'failure', 'original_zip_sha256': artifact['digest'][7:],
            'canonical_native_acceptance': False, 'fresh_build_or_runtime': False}


def member_name(name, directory=False):
    require(type(name) is str and 0 < len(name) <= 512 and
            all(32 <= ord(c) < 127 for c in name) and chr(92) not in name,
            'Unsafe ZIP member name')
    value = name[:-1] if directory and name.endswith('/') else name
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.parts and
            all(x not in ('', '.', '..') for x in value.split('/')) and
            str(path) == value, 'Noncanonical ZIP member path')
    return value


def checked_inventory(inventory, artifact):
    require(type(inventory['schema']) is int and inventory['schema'] == 1 and inventory['artifact_id'] == artifact['id'] and
            inventory['zip_sha256'] == artifact['digest'][7:] and
            inventory['zip_bytes'] == artifact['size_in_bytes'] and
            inventory['central_directory_complete'] is True and
            inventory['all_members_crc_verified'] is True,
            'Incomplete or different original ZIP inventory')
    rows = inventory['members']
    require(type(rows) is list and 0 < len(rows) <= MAX_MEMBERS, 'ZIP member bound exceeded')
    result = {}
    total = 0
    for row in rows:
        require(set(row) == {'name', 'type', 'bytes', 'sha256', 'encrypted'} and
                row['type'] in ('regular', 'directory') and row['encrypted'] is False,
                'Unsupported ZIP member')
        name = member_name(row['name'], row['type'] == 'directory')
        require(name not in result and type(row['bytes']) is int and
                0 <= row['bytes'] <= MAX_ZIP_BYTES, 'Duplicate or oversized ZIP member')
        if row['type'] == 'directory':
            require(row['bytes'] == 0 and row['sha256'] is None, 'Directory carries payload')
        else:
            require(type(row['sha256']) is str and re.fullmatch('[0-9a-f]{64}', row['sha256']),
                    'Missing streamed member hash')
        total += row['bytes']
        require(total <= MAX_EXPANDED_BYTES, 'Expanded ZIP bound exceeded')
        result[name] = row
    return result


def public_semantics(request, contract, public):
    """Reuse the existing public software checks, retaining original producer IDs."""
    raw = public.__getitem__
    load = lambda name: worker.load_json(raw(name).decode('utf-8'))
    if worker.image(request) == 'p4rt':
        observed = worker.p4rt_receipt_semantics.verify(load, raw, source=request['merge'],
            owner=request['owner'], outputs=contract['outputs'], test_targets=contract['test_targets'],
            owner_test_helper_sha256=contract['owner_test_helper_sha256'])
        sdk = worker.p4rt_sdk_receipt_semantics.verify(load, source=request['merge'],
            owner=request['owner'], preparation_targets=contract['preparation_outputs'],
            common_revision=contract['common_revision'])
    else:
        observed = syncd_receipt_semantics.verify(load, raw, source=request['merge'],
            outputs=contract['outputs'], image=worker.image(request))
        require(observed['fixture_sources'] == contract['fixture_sources'] and
                observed['tested_revision_parents'] == [request['base'], request['head']],
                'Original installed-service inputs differ')
        sdk = None
    return {'observed': observed, 'sdk': sdk}


def review_public_upload(data, contract, run, job, artifact, inventory, public, logical_paths):
    """Cross-check the bounded reader's inventory; never emit a native CI receipt."""
    admission = admit_original(data, contract, run, job, artifact)
    request = admission['original_request']
    rows = checked_inventory(inventory, artifact)
    names = worker.public_json_names(request)
    require(type(public) is dict and names - {'worker-input-run.json'} <= public.keys() <= names,
            'Public JSON inventory is incomplete or unexpected')
    require(all(type(v) is bytes and 0 < len(v) <= worker.MEMBER_LIMIT for v in public.values()) and
            sum(map(len, public.values())) <= worker.TOTAL_LIMIT, 'Public JSON bound exceeded')
    expected = set(public) | set(contract['outputs'])
    require(type(logical_paths) is dict and set(logical_paths) == expected and
            all(type(v) is str for v in logical_paths.values()) and
            len(set(logical_paths.values())) == len(logical_paths), 'Incomplete or aliased member mapping')
    for logical, path in logical_paths.items():
        member_name(path)
        require(path in rows and rows[path]['type'] == 'regular', 'Mapped original member is missing')
        if logical in public:
            require(rows[path]['bytes'] == len(public[logical]) and
                    rows[path]['sha256'] == digest(public[logical]), 'Original public JSON bytes differ')
            worker.safe_json(worker.load_json(public[logical].decode('utf-8')))
    receipt = worker.load_json(public['public-receipt.json'].decode('utf-8'))
    require(set(receipt['artifacts']) == set(contract['outputs']), 'Declared native outputs differ')
    for logical, declared in receipt['artifacts'].items():
        row = rows[logical_paths[logical]]
        require(row['bytes'] == declared['bytes'] and row['sha256'] == declared['sha256'],
                'Streamed original payload differs from its public receipt')
    semantics = public_semantics(request, contract, public)
    return {**admission, 'status': 'PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED',
            'logical_member_paths': dict(logical_paths), 'original_public_semantics': semantics,
            'complete_ZIP_members': len(rows), 'declared_payloads': len(receipt['artifacts']),
            'limits': 'Caller authenticates original API/ZIP-reader provenance. No private SDK/layout '
                      'availability, full worker-receipt acceptance, current-source equivalence, '
                      'fresh loaded runtime, cleanup, PR206 adoption or rebuilt bytes is established.'}
