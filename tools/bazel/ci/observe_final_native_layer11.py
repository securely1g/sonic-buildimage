"""Fixed original layer11 metadata audit; no build or image admission."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tarfile
import tempfile

PLAN_SHA256 = '6f4cf23ed7f5d6d0c817c32fa8a9a54bc68f23b2c5e12097d3b1dd568d6772b8'
MODULES = {
    'observe_original_archive.py': (11696, '19f55eef22052643ca4eadd62af2620e97821511d932ba8f700a00724d09e20f'),
    'observe_final_native_provenance.py': (7698, '4faa375fced471d30574c984e79c4433bd27d5d1abf87b44cd75a5b6e0c16a9c'),
    'recover_vs_original_inputs.py': (26407, '6699855d87b685f9b23a4b4c0a12084ec3100b45a59a8b575a06925f447988a5'),
    'recover_final_native_archives.py': (13320, '8440a5cb118f8eb51971039d91aff5944cee785c34def52b3a2129a86616b86c'),
    'observe_vs_original_installed_policy.py': (15427, 'dba576f3229df618e1b3afbfa33bcd1a70f861cba1bd41c45b072811520fcfa6'),
}
MAX_REPORT = 16 * 1024**2


def require(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def write_report(path, value):
    data = encoded(value)
    require(len(data) <= MAX_REPORT, 'Metadata report exceeds bound')
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def verify_sources(root, rows, original):
    for relative, expected in rows.items():
        path = Path(relative)
        require(not path.is_absolute() and '..' not in path.parts and
                len(path.parts) == 4 and path.parts[:2] == ('tools', 'bazel') and
                path.parts[2] in ('ci', 'oci'), 'Unexpected fixed source path')
        with original.HeldFile(root / path) as held:
            require(held.initial.st_size == expected['bytes'] and
                    original.hash_range(held.fd, 0, held.initial.st_size) == expected['sha256'],
                    'Fixed source owner differs')
            held.verify()


def operator_identity():
    require(os.environ.get('GITHUB_REPOSITORY') == 'securely1g/sonic-buildimage' and
            os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'Unexpected audit operator')
    values = {key: os.environ[key] for key in (
        'GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
        'GITHUB_WORKFLOW_REF', 'GITHUB_JOB', 'GITHUB_EVENT_NAME')}
    require(re.fullmatch('[0-9a-f]{40}', values['GITHUB_SHA']) and
            values['GITHUB_RUN_ID'].isdigit() and int(values['GITHUB_RUN_ID']) > 0 and
            values['GITHUB_RUN_ATTEMPT'].isdigit() and int(values['GITHUB_RUN_ATTEMPT']) > 0,
            'Invalid actual operator identity')
    return values


def layer_inventory(view, checks, original, *, tar_info, maximum_headers=20000, maximum_bytes=12*1024**2):
    require(len(view.layers) > 11, 'Original layer11 is absent')
    layer = view.layers[11]
    headers, occurrences, used = [], {}, 0
    with tarfile.open(layer, 'r:*', tarinfo=tar_info) as archive:
        for member in archive:
            name = original.clean_name(member.name, member.isdir(), tar=True)
            require(not member.issparse(), 'Sparse selected layer member is unsupported')
            require(len(headers) < maximum_headers, 'Selected header inventory exceeds bound')
            occurrence = occurrences.get(name, 0)
            occurrences[name] = occurrence + 1
            record = {'declared_name': member.name, 'normalized_name': name,
                      'type': member.type.decode('ascii'), 'bytes': member.size,
                      'offset_data': member.offset_data, 'occurrence': occurrence}
            record.update(order=len(headers), mode=member.mode, uid=member.uid, gid=member.gid,
                          mtime=member.mtime, linkname=member.linkname,
                          uname=member.uname, gname=member.gname,
                          pax_header_count=len(member.pax_headers),
                          pax_ordered_sha256=hashlib.sha256(json.dumps(
                              list(member.pax_headers.items()), separators=(',', ':'),
                              allow_nan=False).encode()).hexdigest())
            used += len(encoded(record)) + 1
            require(used <= maximum_bytes, 'Selected header inventory exceeds byte bound')
            headers.append(record)
    files = {}
    checks.apply_layer(layer, files)
    require(len(files) <= maximum_headers and len(encoded(files)) <= maximum_bytes,
            'Selected file inventory exceeds bound')
    return {'files': files, 'headers': headers,
            'ordered_headers_sha256': hashlib.sha256(encoded(headers)).hexdigest()}, {}


def differences(left, right):
    result = {'only_left': sorted(set(left)-set(right)),
              'only_right': sorted(set(right)-set(left)), 'changed': []}
    used = len(encoded(result))
    for name in sorted(set(left) & set(right)):
        if left[name] == right[name]:
            continue
        row = {'name': name, 'left': left[name], 'right': right[name]}
        used += len(encoded(row)) + 1
        require(used <= MAX_REPORT, 'Difference report exceeds bound')
        result['changed'].append(row)
    require(used <= MAX_REPORT, 'Difference report exceeds bound')
    return result


def inspect_two(plan, inputs, archives, scratch, output, owners, library, report, *, tar_info=None):
    original, archive_owner, checks = owners
    require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), 'An unrelated timer is active')
    def expired(_signum, _frame):
        raise TimeoutError('Original layer inventory deadline')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, plan['inventory_deadline_seconds'])
    inventories = {}
    try:
        for row in plan['images']:
            report['active_original'] = row['name']
            proof = library.original_proof(row, inputs, original)
            require(proof['rootfs']['diff_ids'][11] == row['expected_diff_id'],
                    'Original selected layer differs from frozen validation')
            with tempfile.TemporaryDirectory(dir=scratch, prefix=row['name']+'-') as directory:
                observation = library.inspect_archive(row, archives, Path(directory), None, proof, owners,
                    audit_only=True, inventory_reader=lambda view: layer_inventory(view, checks, original,
                        tar_info=tar_info, maximum_headers=plan['maximum_layer_members'],
                        maximum_bytes=plan['maximum_header_inventory_bytes']))
            require(observation['audit_only'] is True and
                    observation['Make_manifest_equivalence_claimed'] is False,
                    'Observation scope changed')
            inventory = observation['files']
            write_report(output/(row['family']+'-files.json'), inventory['files'])
            write_report(output/(row['family']+'-headers.json'), inventory['headers'])
            inventories[row['family']] = inventory
            report['images'].append({'name': row['name'], 'original_run': row['evidence']['producer']['run_id'],
                'original_artifact': row['evidence']['producer']['artifact_id'], 'archive': row['archive'],
                'config_digest': observation['snapshot']['config_digest'],
                'layer_index': 11, 'stored_layer': observation['snapshot']['layers'][11],
                'uncompressed_layer_diff_id': observation['snapshot']['rootfs'][11],
                'file_count': len(inventory['files']), 'header_count': len(inventory['headers']),
                'ordered_headers_sha256': inventory['ordered_headers_sha256']})
        left, right = inventories['syncd'], inventories['gearbox']
        comparisons = {
            'file': differences(left['files'], right['files']),
            'header': differences(
                {encoded([x['normalized_name'], x['occurrence']]).decode(): x for x in left['headers']},
                {encoded([x['normalized_name'], x['occurrence']]).decode(): x for x in right['headers']})}
        report['differences'] = {}
        for kind, comparison in comparisons.items():
            filename = kind+'-differences.json'
            write_report(output/filename, comparison)
            fields = {}
            for row in comparison['changed']:
                for key in set(row['left']) | set(row['right']):
                    if row['left'].get(key) != row['right'].get(key):
                        fields[key] = fields.get(key, 0) + 1
            report['differences'][kind] = {
                'member': filename, 'sha256': hashlib.sha256(encoded(comparison)).hexdigest(),
                'bytes': len(encoded(comparison)),
                'only_left_count': len(comparison['only_left']),
                'only_right_count': len(comparison['only_right']),
                'changed_count': len(comparison['changed']), 'changed_fields': fields,
                'first_twenty_changed_names': [row['name'] for row in comparison['changed'][:20]]}
        report.update(status='TWO_ORIGINAL_LAYER11_INVENTORIES_OBSERVED', active_original=None)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run(control, source, inputs, scratch, output):
    require(not sys.flags.optimize, 'Inherited archive guards require ordinary Python')
    require(control.is_dir() and source.is_dir() and inputs.is_dir() and scratch.is_dir(),
            'Fixed input directories are missing')
    output.mkdir(mode=0o700)
    report = {'status': 'INCOMPLETE', 'images': [], 'source_image_installed_policy_admission': False,
              'Make_manifest_equivalence_claimed': False, 'actual_operator': operator_identity()}
    try:
        # The job seals these regular control files before starting this caller.
        # Repeat the fixed byte identities before importing any sibling module.
        for name, (size, digest) in MODULES.items():
            path = control/name
            require(path.is_file() and not path.is_symlink() and path.stat().st_size == size and
                    hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'Fixed control module differs')
        raw = (control/'vs_final_native_layer11_audit.json').read_bytes()
        require(hashlib.sha256(raw).hexdigest() == PLAN_SHA256, 'Fixed audit plan differs')
        plan = json.loads(raw)
        report['source_revision'] = plan['source_revision']
        report['source_tree'] = plan['source_tree']
        report['native_controller'] = plan['native_controller']
        usage = os.statvfs(scratch)
        require(usage.f_bavail * usage.f_frsize >= plan['minimum_temporary_free_bytes'],
                'Insufficient temporary observation space')
        sys.path.insert(0, str(control))
        original = importlib.import_module('recover_vs_original_inputs')
        transport = importlib.import_module('recover_final_native_archives')
        library = importlib.import_module('observe_vs_original_installed_policy')
        provenance_owner = importlib.import_module('observe_final_native_provenance')
        verify_sources(source, plan['source_owners'], original)
        api_raw = (control/'vs_native_api_provenance_inputs.json').read_bytes()
        require(hashlib.sha256(api_raw).hexdigest() == plan['api_owner_plan_sha256'],
                'Fixed API owner plan differs')
        worker = provenance_owner.load_owner(source, json.loads(api_raw))
        archive_owner = importlib.import_module('tools.bazel.ci.vs_archive_reuse')
        checks = importlib.import_module('tools.bazel.oci.image_validation')
        metadata_owner = importlib.import_module('observe_original_archive')
        for row in plan['downloads']:
            actual = worker.api('/actions/artifacts/'+str(row['id']))
            require(all(actual.get(key) == row[key] for key in ('id', 'name', 'size_in_bytes', 'digest')) and
                    actual.get('expired') is False and actual['workflow_run']['id'] == row['run_id'],
                    'Fixed original evidence artifact differs')
        for row in plan['images']:
            library.original_proof(row, inputs, original)
        transfer_plan = transport.load_plan(control/'vs_final_native_layer11_inputs.json')
        report['transport'] = {}
        archives = scratch/'original-archives'
        transport.recover_two(transfer_plan, worker.api, os.environ['GH_TOKEN'], archives, original,
                              report['transport'])
        inspect_two(plan, inputs, archives, scratch, output, (original, archive_owner, checks), library, report,
                    tar_info=metadata_owner.ObservationTarInfo)
        verify_sources(source, plan['source_owners'], original)
    except Exception as error:
        report['failure_type'] = type(error).__name__
        report['status'] = 'INCOMPLETE'
        raise RuntimeError('Fixed layer11 observation failed; inspect bounded report') from None
    finally:
        write_report(output/'layer11-comparison.json', report)


def main():
    parser = argparse.ArgumentParser()
    for name in ('control', 'source', 'inputs', 'scratch', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args.control.absolute(), args.source.absolute(), args.inputs.absolute(),
            args.scratch.absolute(), args.output.absolute())
    except Exception:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
