"""Observe four fixed original ELF files; never execute, alter or admit them."""
import argparse
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time

PLAN_SHA256 = 'e7911eefea11aafd287a273936220f4d3ca9f4145cf6e9d6a89a2a49f27dfb53'
MODULES = {'observe_final_native_provenance.py': (7698, '4faa375fced471d30574c984e79c4433bd27d5d1abf87b44cd75a5b6e0c16a9c'), 'recover_vs_original_inputs.py': (26407, '6699855d87b685f9b23a4b4c0a12084ec3100b45a59a8b575a06925f447988a5'), 'recover_final_native_archives.py': (13320, '8440a5cb118f8eb51971039d91aff5944cee785c34def52b3a2129a86616b86c'), 'observe_vs_original_installed_policy.py': (15427, 'dba576f3229df618e1b3afbfa33bcd1a70f861cba1bd41c45b072811520fcfa6'), 'observe_vs_host_binaries.py': (21581, 'ad0af004e1d84ca178798b4d16fee7062275d1d36b1e7dfa8cd61771a271304c'), 'observe_original_socat.py': (9579, 'da41bb8bd78caa23aa4ac57ee4a5be63ce4f294647987a5714d66fda2bf11efe')}
MIB = 1024**2
MAX_REPORT = 16 * MIB
MAX_BINARY = 2 * MIB


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

def run_upstream(program, arguments, source, scratch, maximum, seconds=45):
    """One fixed upstream reader, with file/CPU/memory limits and owned cleanup."""
    require(program in ("/usr/bin/dpkg-deb", "/usr/bin/readelf"), "Unexpected reader")
    source.seek(0)
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    output = tempfile.TemporaryFile(dir=scratch)
    try:
        with tempfile.TemporaryFile(dir=scratch) as errors:
            environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
            if "HOME" in os.environ:
                environment["HOME"] = os.environ["HOME"]
            if os.environ.get("RUNNER_TRACKING_ID"):
                environment["RUNNER_TRACKING_ID"] = os.environ["RUNNER_TRACKING_ID"]
            process = subprocess.Popen(
                [program, *arguments, "/proc/self/fd/" + str(source.fileno())],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                pass_fds=(source.fileno(),), start_new_session=True, preexec_fn=limits,
                env=environment)
            # Reuse the qualified native APT reader's waitid/WNOWAIT pattern:
            # keep the leader unreaped until its owned group has been closed.
            leader_owned = True
            try:
                deadline = time.monotonic() + seconds + 5
                while time.monotonic() < deadline:
                    state = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if state is not None:
                        code = state.si_status if state.si_code == os.CLD_EXITED else -state.si_status
                        break
                    time.sleep(0.05)
                else:
                    raise subprocess.TimeoutExpired(program, seconds + 5)
            except ChildProcessError:
                leader_owned = False
                raise
            finally:
                if leader_owned:
                    # These readers own no transactional state. Close all decoder
                    # children before reaping, including on successful exit.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
            require(code == 0, "Upstream reader failed")
        require(os.fstat(output.fileno()).st_size <= maximum, "Upstream output exceeds bound")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise


def sections_with_flags(listing, binary, elf_owner):
    rows = elf_owner.section_metadata(listing, binary)
    flags = {}
    for line in listing.decode('ascii', 'strict').splitlines():
        if not re.match(r'^\s*\[\s*[0-9]+\]', line):
            continue
        fields = line.split(']', 1)[1].split()
        if fields and fields[0] in {row['name'] for row in rows}:
            require(len(fields) in (9, 10), 'Unexpected ELF section flags shape')
            value = fields[6] if len(fields) == 10 else ''
            require(re.fullmatch(r'[A-Za-z]{0,32}', value) is not None and
                    fields[0] not in flags, 'Invalid ELF section flags')
            flags[fields[0]] = value
    require(set(flags) == {row['name'] for row in rows}, 'Missing ELF section flags')
    for row in rows:
        row['flags'] = flags[row['name']]
        row['category'] = ('allocated' if 'A' in row['flags'] else
                           'debug' if row['name'].startswith(('.debug', '.zdebug')) else 'other')
    return rows


def section_names(sections, offset, size):
    return sorted(row['name'] for row in sections if row['type'] != 'NOBITS' and
                  offset < row['offset'] + row['bytes'] and offset + size > row['offset'])


def string_classifications(binary, sections):
    require(len(binary) <= MAX_BINARY, 'ELF classification exceeds bound')
    paths, compilers = [], []
    markers = {
        'temporary_build_path': (b'/tmp/', b'/var/tmp/', b'/pip-build-', b'/pip-install-',
                                 b'/pip-req-build-', b'/pip-build-env-'),
        'bazel_path': (b'/external/', b'/execroot/', b'/_bazel_'),
        'python_header_path': (b'/include/python3.13', b'/cpython/', b'/internal/pycore_'),
        'compiler_header_path': (b'/include/c++/', b'/lib/gcc/'),
    }
    for match in re.finditer(rb'[\x20-\x7e]{8,}', binary):
        value = match.group()
        classes = [kind for kind, needles in markers.items() if any(x in value for x in needles)]
        if re.search(rb'/[A-Za-z0-9_.+-]+\.(?:c|h|cc|cpp)$', value):
            classes.append('source_filename_path')
        if classes:
            require(len(value) <= 4096 and len(paths) < 64, 'ELF path classification exceeds bound')
            paths.append({'offset': match.start(), 'bytes': len(value),
                          'sha256': hashlib.sha256(value).hexdigest(), 'classes': sorted(classes),
                          'sections': section_names(sections, match.start(), len(value))})
        if (value.startswith(b'GCC:') or value.startswith(b'clang version')) and \
                '.comment' in section_names(sections, match.start(), len(value)):
            require(len(value) <= 512 and len(compilers) < 8, 'Compiler classification exceeds bound')
            compilers.append({'kind': 'gcc' if value.startswith(b'GCC:') else 'clang',
                              'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value),
                              'version_tokens': [x.decode('ascii') for x in
                                                 re.findall(rb'[0-9]+(?:\.[0-9]+)+', value)[:4]]})
    return {'paths': paths, 'compilers': compilers, 'raw_path_values_retained': False}


def inspect_elf_ranges(view, row, scratch, original, elf_owner):
    require(len(row['binaries']) == 2 and len(view.layers) > 11, 'Fixed ELF selection differs')
    layer = Path(view.layers[11])
    records, bodies = [], {}
    with original.HeldFile(layer) as held:
        require(held.initial.st_size == row['layer_bytes'] and
                original.hash_range(held.fd, 0, held.initial.st_size) == row['expected_diff_id'][7:],
                'Original uncompressed layer differs')
        for selected in row['binaries']:
            require(0 < selected['bytes'] <= MAX_BINARY and selected['offset'] >= 0 and
                    selected['offset'] + selected['bytes'] <= held.initial.st_size and
                    selected['name'] not in bodies, 'Fixed ELF range exceeds original layer')
            with tempfile.TemporaryFile(dir=scratch) as content:
                source = io.BytesIO(os.pread(held.fd, selected['bytes'], selected['offset']))
                facts = original.copy_member(source, content, selected['bytes'], MAX_BINARY)
                require(facts['sha256'] == selected['sha256'], 'Original ELF hash differs')
                content.seek(0)
                binary = content.read(MAX_BINARY + 1)
                require(binary[:6] == b'\x7fELF\x02\x01' and binary[16:20] == b'\x03\x00\x3e\x00',
                        'Original ELF is not AMD64 little-endian shared code')
                with run_upstream('/usr/bin/readelf', ['--wide', '--section-headers'],
                                  content, scratch, MIB) as output:
                    sections = sections_with_flags(output.read(MIB + 1), binary, elf_owner)
                abi = elf_metadata(content, scratch)
                abi['rpath'] = [{'bytes': len(value.encode()),
                                 'sha256': hashlib.sha256(value.encode()).hexdigest()}
                                for value in abi['rpath']]
                abi['defined_global_pyinit_exports'] = pyinit_exports(content, scratch)
                records.append({**selected, 'sections': sections, 'abi': abi,
                                'classifications': string_classifications(binary, sections)})
                bodies[selected['name']] = binary
        held.verify()
    return {'records': records, 'bodies': bodies}


def elf_inventory(view, row, scratch, original, elf_owner):
    """Preserve the existing archive owner's (inventory, diagnostics) callback contract."""
    return inspect_elf_ranges(view, row, scratch, original, elf_owner), {}


def compare_pair(left, right, left_body, right_body, elf_owner):
    raw = elf_owner.differences(left_body, right_body, left['sections'])
    spans = []
    for span in raw['reported_ranges']:
        offset, size = span['offset'], span['bytes']
        def inside(record):
            return any(p['offset'] <= offset and offset + size <= p['offset'] + p['bytes']
                       for p in record['classifications']['paths'])
        spans.append({'offset': offset, 'bytes': size, 'sections': span['sections'],
                      'right_sections': section_names(right['sections'], offset, size),
                      'within_classified_path_string_both': inside(left) and inside(right)})
    lmap = {x['name']: x for x in left['sections']}
    rmap = {x['name']: x for x in right['sections']}
    sections = []
    for name in sorted(set(lmap) | set(rmap)):
        a, b = lmap.get(name), rmap.get(name)
        sections.append({'name': name, 'identical': a == b, 'left': a, 'right': b})
    return {'name': left['name'], 'left_sha256': left['sha256'], 'right_sha256': right['sha256'],
            'different_bytes': raw['different_bytes'], 'ranges': raw['ranges'],
            'reported_ranges': spans, 'ranges_truncated': raw['ranges_truncated'],
            'sections': sections,
            'changed_section_counts': {kind: sum(not x['identical'] and
                any(v is not None and v['category'] == kind for v in (x['left'], x['right']))
                for x in sections) for kind in ('allocated', 'debug', 'other')},
            'raw_binary_context_retained': False, 'byte_equivalence_claimed': False}


def pyinit_exports(source, scratch):
    with run_upstream('/usr/bin/readelf', ['--wide', '--dyn-syms'], source, scratch, MIB) as stream:
        raw = stream.read(MIB + 1)
    require(len(raw) <= MIB, 'Dynamic symbol report exceeds bound')
    result = []
    pattern = re.compile(r'^\s*[0-9]+:\s+[0-9a-fA-F]+\s+[0-9]+\s+FUNC\s+GLOBAL\s+DEFAULT\s+'
                         r'(?!UND\b)\S+\s+(PyInit_[A-Za-z0-9_]+)(?:\s|$)', re.M)
    for match in pattern.finditer(raw.decode('ascii', 'strict')):
        require(len(result) < 32 and len(match.group(1)) <= 128, 'PyInit export report exceeds bound')
        result.append(match.group(1))
    return sorted(set(result))


def inspect_pairs(plan, inputs, archives, scratch, owners, library, elf_owner, report):
    original, _archive_owner, _checks = owners
    require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), 'An unrelated timer is active')
    def expired(_signum, _frame):
        raise TimeoutError('Original ELF observation deadline')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, plan['inventory_deadline_seconds'])
    observations = {}
    try:
        require([x['family'] for x in plan['images']] == ['syncd', 'gearbox'], 'Fixed family pair differs')
        for row in plan['images']:
            report['active_original'] = row['name']
            proof = library.original_proof(row, inputs, original)
            require(proof['rootfs']['diff_ids'][11] == row['expected_diff_id'],
                    'Selected layer differs from original validation')
            with tempfile.TemporaryDirectory(dir=scratch, prefix=row['name']+'-') as directory:
                observation = library.inspect_archive(row, archives, Path(directory), None, proof, owners,
                    audit_only=True, inventory_reader=lambda view: elf_inventory(
                        view, row, Path(directory), original, elf_owner))
            require(observation['audit_only'] is True and
                    observation['Make_manifest_equivalence_claimed'] is False, 'Observation scope changed')
            observations[row['family']] = observation['files']
            report['images'].append({'name': row['name'], 'archive': row['archive'],
                'original_run': row['evidence']['producer']['run_id'],
                'original_artifact': row['evidence']['producer']['artifact_id'],
                'config_digest': observation['snapshot']['config_digest'], 'layer_index': 11,
                'stored_layer': observation['snapshot']['layers'][11],
                'uncompressed_layer_diff_id': observation['snapshot']['rootfs'][11],
                'binaries': observation['files']['records']})
        left, right = observations['syncd'], observations['gearbox']
        lmap = {x['name']: x for x in left['records']}
        rmap = {x['name']: x for x in right['records']}
        require(set(lmap) == set(rmap) and len(lmap) == 2, 'Original ELF pairs differ')
        report['pairs'] = [compare_pair(lmap[name], rmap[name], left['bodies'][name],
                                       right['bodies'][name], elf_owner) for name in sorted(lmap)]
        report.update(status='FOUR_ORIGINAL_BITARRAY_ELF_FILES_OBSERVED', active_original=None)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)



def elf_metadata(source, scratch):
    with run_upstream("/usr/bin/readelf", ["--wide", "--file-header", "--program-headers",
                      "--dynamic", "--version-info"], source, scratch, 2 * MIB) as stream:
        raw = stream.read(2 * MIB + 1)
    require(len(raw) <= 2 * MIB, "ELF report exceeds bound")
    text = raw.decode("utf-8", "strict")
    fields = {}
    for key in ("Class", "Data", "Type", "Machine", "OS/ABI"):
        match = re.search(r"^\s*" + re.escape(key) + r":\s*(.+)$", text, re.M)
        require(match is not None, "ELF header field missing")
        fields[key] = match.group(1).strip()
    fields["needed"] = sorted(re.findall(r"\(NEEDED\).*?\[([^]\n]+)\]", text))
    fields["soname"] = sorted(re.findall(r"\(SONAME\).*?\[([^]\n]+)\]", text))
    fields["rpath"] = sorted(re.findall(r"\((?:RPATH|RUNPATH)\).*?\[([^]\n]+)\]", text))
    fields["interpreter"] = re.findall(r"Requesting program interpreter: ([^]\n]+)", text)
    needs = text.split("Version needs section", 1)
    fields["required_symbol_versions"] = sorted(set(re.findall(
        r"Name: ([A-Za-z0-9_.+:-]+)", needs[1] if len(needs) == 2 else "")))
    return fields

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
        raw = (control/'vs_final_bitarray_elf_inputs.json').read_bytes()
        require(hashlib.sha256(raw).hexdigest() == PLAN_SHA256, 'Fixed audit plan differs')
        plan = json.loads(raw)
        report['source_revision'] = plan['source_revision']
        report['source_tree'] = plan['source_tree']
        report['native_controller'] = plan['native_controller']
        report['prior_audit'] = plan['prior_audit']
        report['parent_HOME_modified'] = False
        report['readelf_HOME_policy'] = 'actual_value_preserved_or_absent'
        report['binaries_uploaded'] = False
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
        elf_owner = importlib.import_module('observe_original_socat')
        for row in plan['downloads']:
            actual = worker.api('/actions/artifacts/'+str(row['id']))
            require(all(actual.get(key) == row[key] for key in ('id', 'name', 'size_in_bytes', 'digest')) and
                    actual.get('expired') is False and actual['workflow_run']['id'] == row['run_id'],
                    'Fixed original evidence artifact differs')
        prior = worker.api('/actions/artifacts/'+str(plan['prior_audit']['artifact_id']))
        require(prior['id'] == plan['prior_audit']['artifact_id'] and
                prior['digest'] == plan['prior_audit']['artifact_digest'] and prior['expired'] is False and
                prior['workflow_run']['id'] == plan['prior_audit']['run_id'], 'Original audit identity differs')
        for row in plan['images']:
            library.original_proof(row, inputs, original)
        transfer_plan = transport.load_plan(control/'vs_final_native_layer11_inputs.json')
        report['transport'] = {}
        archives = scratch/'original-archives'
        transport.recover_two(transfer_plan, worker.api, os.environ['GH_TOKEN'], archives, original,
                              report['transport'])
        inspect_pairs(plan, inputs, archives, scratch, (original, archive_owner, checks), library, elf_owner, report)
        verify_sources(source, plan['source_owners'], original)
    except Exception as error:
        report['failure_type'] = type(error).__name__
        report['status'] = 'INCOMPLETE'
        raise RuntimeError('Fixed original ELF observation failed; inspect bounded report') from None
    finally:
        write_report(output/'bitarray-elf-comparison.json', report)

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
