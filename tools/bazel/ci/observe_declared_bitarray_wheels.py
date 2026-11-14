"""Fixed post-baseline adapter; owner imports and transport stay with reviewed CI.

No standalone entrypoint: the later fixed job must seal its owners and original
API inputs before calling these functions. It must not alter baseline receipts.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import zipfile

MIB = 1024 ** 2
MAX_WHEEL = 64 * MIB
MAX_ELF = 2 * MIB
MEMBERS = {
    'bitarray/_bitarray.cpython-313-x86_64-linux-gnu.so': 'PyInit__bitarray',
    'bitarray/_util.cpython-313-x86_64-linux-gnu.so': 'PyInit__util',
}
VERIFY_STEP = 'Verify actual C compile cache reuse and original source restoration'


def require(value, message):
    if not value:
        raise ValueError(message)


def read_fixed(path, expected, maximum, original):
    require(type(expected['bytes']) is int and 0 < expected['bytes'] <= maximum and
            re.fullmatch('[0-9a-f]{64}', expected['sha256']), 'Unfrozen fixed input')
    with original.HeldFile(path) as held:
        require(held.initial.st_size == expected['bytes'] and
                original.hash_range(held.fd, 0, held.initial.st_size) == expected['sha256'],
                'Fixed input size or SHA differs')
        raw = os.pread(held.fd, held.initial.st_size, 0)
        require(len(raw) == held.initial.st_size, 'Fixed input was truncated')
        held.verify()
    return raw


def baseline_proof(plan, api_rows, report_path, producer_owner, original):
    """Use original metadata authority, then bind the successful original report."""
    baseline = plan.get('baseline')
    require(isinstance(baseline, dict) and set(api_rows) == {'reports', 'outputs'},
            'Successful original baseline identities are still pending')
    require(set(baseline['transfers']) == set(api_rows), 'Baseline transport scope differs')
    for name, row in baseline['transfers'].items():
        actual = api_rows[name]
        producer_owner.check_producer(row, actual['artifact'], actual['run'], actual['job'])
        require(actual['run']['conclusion'] == actual['job']['conclusion'] == 'success',
                'Baseline qualification did not complete successfully')
        steps = actual['job']['steps']
        verified = [s for s in steps if s['name'] == VERIFY_STEP]
        uploaded = [s for s in steps if s['name'] == row['upload_step']]
        require(len(verified) == len(uploaded) == 1 and
                verified[0]['status'] == 'completed' and verified[0]['conclusion'] == 'success' and
                verified[0]['number'] < uploaded[0]['number'], 'Qualification proof must precede upload')
    report = json.loads(read_fixed(report_path, baseline['report'], 2 * MIB, original))
    expected_operator = baseline['operator']
    for row in baseline['transfers'].values():
        require(row['run_id'] == int(expected_operator['GITHUB_RUN_ID']) and
                row['attempt'] == int(expected_operator['GITHUB_RUN_ATTEMPT']) and
                row['head'] == expected_operator['GITHUB_SHA'], 'Baseline operator domain differs')
    require(report['status'] == 'FIXED_BITARRAY_AMD64_QUALIFICATION_PASSED' and
            report['module'] == 'bitarray' and report['version'] == '2.8.1.sonic.1' and
            report['architecture'] == 'amd64' and report['operator'] == expected_operator and
            report['registry_revision'] == plan['registry_revision'] and
            report['setup_source_revision'] == plan['source_revision'] and
            report['native_setup_owner'] == plan['native_revision'] and
            report['source_archive_sha256'] == plan['source_archive_sha256'] and
            report['full_original_suites'] == 2 and report['declared_header_edit_restored'] is True and
            report['disk_cache'] is False and report['repository_cache'] is False and
            report['HOME_unchanged'] is True, 'Original qualification report differs')
    for phase in ('pass1', 'pass2', 'copy-baseline', 'restored'):
        require(report['outputs'][phase]['@bitarray//:whl'] == baseline['wheel'],
                'Original baseline wheel binding differs')
    return {'report_sha256': baseline['report']['sha256'], 'wheel': baseline['wheel'],
            'original_operator': expected_operator, 'transport': baseline['transfers']}


def abi_checks(name, abi, sections, debug):
    require(abi['Class'] == 'ELF64' and 'little endian' in abi['Data'] and
            abi['Type'].startswith('DYN ') and abi['Machine'] == 'Advanced Micro Devices X86-64' and
            abi['needed'] == ['libc.so.6'] and not abi['interpreter'] and not abi['rpath'] and
            abi['defined_global_pyinit_exports'] == [MEMBERS[name]], 'Unexpected declared extension ABI')
    if debug:
        present = {s['name'] for s in sections if s['bytes'] > 0}
        require(bool(present & {'.debug_info', '.zdebug_info'}) and
                bool(present & {'.debug_line', '.zdebug_line'}), 'Debug information or line table is absent')


def observe_wheel(path, expected, scratch, original, elf, section_owner, *, debug=False):
    """Inspect only two fixed wheel members; use existing parsers and reader limits."""
    require(type(expected['bytes']) is int and 0 < expected['bytes'] <= MAX_WHEEL and
            re.fullmatch('[0-9a-f]{64}', expected['sha256']), 'Wheel identity is not frozen')
    records = []
    with original.HeldFile(path) as held:
        require(held.initial.st_size == expected['bytes'] and
                original.hash_range(held.fd, 0, held.initial.st_size) == expected['sha256'],
                'Original collected wheel differs')
        with os.fdopen(os.dup(held.fd), 'rb') as stream, zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            require(0 < len(infos) <= 128 and len({i.filename for i in infos}) == len(infos) and
                    sum(i.file_size for i in infos) <= MAX_WHEEL, 'Wheel directory exceeds fixed scope')
            selected = {i.filename: i for i in infos if i.filename in MEMBERS}
            require(set(selected) == set(MEMBERS), 'Missing declared extension member')
            for name, info in selected.items():
                require(stat.S_ISREG(info.external_attr >> 16) and
                        0 < info.file_size <= MAX_ELF and not info.flag_bits & 1 and
                        info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                        'Selected wheel member type or bound differs')
                with archive.open(info) as source, tempfile.TemporaryFile(dir=scratch) as content:
                    facts = original.copy_member(source, content, info.file_size, MAX_ELF)
                    require(facts['CRC32'] == format(info.CRC, '08x'), 'Wheel member CRC differs')
                    content.seek(0); binary = content.read(MAX_ELF + 1)
                    require(binary[:6] == b'\x7fELF\x02\x01' and
                            binary[16:20] == b'\x03\x00\x3e\x00', 'Member is not AMD64 shared ELF')
                    with elf.run_upstream('/usr/bin/readelf', ['--wide', '--section-headers'],
                                          content, scratch, MIB) as listing:
                        sections = elf.sections_with_flags(listing.read(MIB + 1), binary, section_owner)
                    abi = elf.elf_metadata(content, scratch)
                    abi['defined_global_pyinit_exports'] = elf.pyinit_exports(content, scratch)
                    abi_checks(name, abi, sections, debug)
                    records.append({'name': name, **facts, 'sections': sections, 'abi': abi,
                                    'classifications': elf.string_classifications(binary, sections)})
        held.verify()
    return {'wheel': expected, 'debug_requested': debug, 'extensions': records,
            'binary_contents_retained': False, 'source_image_admission': False}


def prepare_debug(plan, proof, source, native, registry, scratch, archives, mirrors,
                  support, qualifier):
    require(isinstance(plan.get('baseline'), dict) and
            proof['report_sha256'] == plan['baseline']['report']['sha256'], 'Baseline proof is absent')
    support.source_check(registry, plan['registry_revision'])
    for relative, expected in plan['registry_files'].items():
        qualifier.exact(registry / relative, expected)
    support.prepare_cache(plan, source, native, scratch, archives, mirrors)
    worker = sys.modules['tools.bazel.ci.source_image_worker']
    profile = json.loads((scratch / 'resources.json').read_bytes())['selected_profile']
    binary = shutil.which('bazel', path='/usr/local/bin:/usr/bin:/bin') or shutil.which(
        'bazelisk', path='/usr/local/bin:/usr/bin:/bin')
    require(binary is not None, 'Qualified launcher is missing')
    binary = str(Path(binary).resolve(strict=True))
    configuration = worker.remote_cache_configuration(source)
    require((scratch / 'native-bazel').read_text() == worker.launcher_text(
        binary, scratch / 'output-base', profile, configuration), 'Prepared native launcher differs')
    launcher = scratch / 'debug-bazel'
    text = worker.launcher_text(binary, scratch / 'debug/work/bazel/output-base', profile, configuration)
    with launcher.open('x') as output:
        output.write(text)
    launcher.chmod(0o700)
    directories = sorted((scratch / 'cache-setup/distdir').iterdir())
    require(len(directories) == 5 and all(p.is_dir() and not p.is_symlink() for p in directories),
            'Original source distdirs differ')
    setup = {'launcher': {'bytes': len(text.encode()), 'sha256': hashlib.sha256(text.encode()).hexdigest()},
             'distdirs': [str(p) for p in directories],
             'home_sha256': hashlib.sha256(os.environ['HOME'].encode()).hexdigest(),
             'baseline_report_sha256': proof['report_sha256']}
    qualifier.write(scratch / 'debug-setup.json', setup)


def run_debug(plan, proof, source, registry, scratch, support, qualifier, runner, audit):
    """One original registry run; no source edit, new runner or cache test."""
    require(isinstance(plan.get('baseline'), dict) and
            proof['report_sha256'] == plan['baseline']['report']['sha256'], 'Baseline proof is absent')
    support.source_check(source, plan['source_revision'])
    support.source_check(registry, plan['registry_revision'])
    for relative, expected in plan['registry_files'].items():
        qualifier.exact(registry / relative, expected)
    setup = json.loads((scratch / 'debug-setup.json').read_bytes())
    require(setup['baseline_report_sha256'] == proof['report_sha256'] and
            setup['home_sha256'] == hashlib.sha256(os.environ['HOME'].encode()).hexdigest(),
            'Prepared baseline or actual HOME changed')
    launcher = scratch / 'debug-bazel'; qualifier.exact(launcher, setup['launcher'])
    work, artifacts = scratch / 'debug/work', scratch / 'debug/private'
    require(not work.exists() and not artifacts.exists(), 'Debug run must use fresh directories')
    flags = ['--repository_cache=', '--disk_cache=', '--incompatible_strict_action_env',
             '--experimental_downloader_config=' + str(scratch / 'source-mirrors.cfg'),
             *['--distdir=' + p for p in setup['distdirs']], '--compilation_mode=dbg']
    original_run = runner.run_logged
    phases = set()

    def logged(command, cwd, log):
        prefix, kind, options, targets = qualifier.command_parts(command, launcher, work)
        require(kind not in phases and options.count('--compilation_mode=dbg') == 1,
                'Repeated or non-debug original runner phase')
        phases.add(kind)
        audit.audit_targets(str(launcher), qualifier.safe_options(options), targets, workspace=cwd,
                            output=artifacts / (kind + '-action-audit.json'), startup_options=prefix[1:])
        original_run(command, cwd, log)

    runner.run_logged = logged
    try:
        runner.run(registry, qualifier.MODULE, qualifier.VERSION, 'amd64', work, artifacts,
                   bazel=str(launcher), bazel_startup_args=['--batch'], bazel_build_args=flags)
    finally:
        runner.run_logged = original_run
    require(phases == {'build', 'test'}, 'Original debug phases are incomplete')
    outputs = json.loads((artifacts / 'outputs.json').read_bytes())['@bitarray//:whl']
    require(len(outputs) == 1, 'Debug wheel output is ambiguous')
    row = outputs[0]; relative = Path(row['path'])
    require(not relative.is_absolute() and '..' not in relative.parts, 'Debug wheel escapes retained outputs')
    expected = {'bytes': row['size'], 'sha256': row['sha256']}
    support.source_check(registry, plan['registry_revision'])
    require(setup['home_sha256'] == hashlib.sha256(os.environ['HOME'].encode()).hexdigest(), 'Actual HOME changed')
    return artifacts / relative, expected
