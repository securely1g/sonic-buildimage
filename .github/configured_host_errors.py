"""Create publishable diagnostics from a private native Make invocation.

Only parsed target names, recognized failure classes, statuses and receipt
fields leave the private checkout. No raw shell commands or log lines do.
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import stat

ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07)')
STAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+')
MAKE = re.compile(r'^g?make(?:\[\d+\])?:\s+\*\*\*\s+\[(?:(?P<rule>[\w./-]+):(?P<line>\d+):\s+)?(?P<target>[^\]]+)\]\s*(?:Error (?P<exit>\d+)|(?P<signal>Killed|Terminated|Interrupt|Segmentation fault))', re.I)
MARK = re.compile(r'^\[\s*(building|finished|cached|FAIL LOG START|FAIL LOG END)\s*\]\s*\[\s*([^\]]+?)\s*\]',re.I)
REL = re.compile(r'target/(?:[a-zA-Z0-9_][a-zA-Z0-9._:+%~@-]*/)*[a-zA-Z0-9._][a-zA-Z0-9._:+%~@-]{0,155}')
SECRET = re.compile(r'(?:password|passwd|shadow|secret|credential|authorization|bearer|private[-_. ]?key|access[-_. ]?token|api[-_. ]?key|/\.ssh|[A-Z]+_TOKEN)',re.I)
LABEL = re.compile(r'//(?:dockers|tools/bazel|platform)/[\w./+:-]{1,128}')
MODULE = re.compile(r'\b(?:go\.opentelemetry\.io|github\.com/open-telemetry)/[\w./-]+@v[\w.+-]+')
DIGEST = re.compile(r'[0-9a-f]{64}')
CLASSES = (
    (r'(?:no space left on device|disk quota exceeded)', 'storage: out of disk space'),
    (r'(?:TLS handshake timeout|TLS handshake timed out)', 'network: TLS handshake timeout'),
    (r'(?:connection (?:timed out|reset by peer|refused)|temporary failure resolving|network is unreachable|i/o timeout|temporary failure in name resolution)', 'network: connection or DNS failure'),
    (r'HTTP(?:/\d(?:\.\d)?| Error| status(?: code)?)?[: ]+([45]\d\d)\b', 'HTTP request failed'),
    (r'(?:killed signal terminated program|out of memory|cannot allocate memory|oom.kill)', 'process: memory exhausted'),
    (r'(?:Access denied|Permission denied|Operation not permitted)', 'filesystem: permission denied'),
    (r'(?:No such file or directory|cannot stat|cannot find|can.t open file|missing (?:file|path|archive)|not a regular file)', 'filesystem: missing input'),
    (r'(?:file (?:already )?exists|already mounted|device or resource busy|target is busy)', 'filesystem: existing or busy resource'),
    (r'(?:read.only file system|input/output error|stale file handle)', 'filesystem: read/write failed'),
    (r'(?:No rule to make target|recipe for target .* failed)', 'Make prerequisite or recipe failed'),
    (r'(?:command not found|executable file not found|: not found\s*$)', 'missing executable or file'),
    (r'(?:No matching distribution found|Could not find a version that satisfies)', 'pip dependency unavailable'),
    (r'(?:unmet dependencies|dependency problems|Unable to locate package|has no installation candidate)', 'APT dependency unavailable'),
    (r'(?:dpkg: error processing package|error processing archive|dpkg-deb: error)', 'dpkg package operation failed'),
    (r'frozen (?:input|OCI receipt) changed|modified benchmark input was replaced|another build replaced the diagnostic package', 'frozen producer artifact changed'),
    (r'host prefix changed across A/B|host program identity mismatch', 'rendered host program changed'),
    (r'wrong configured-host cache outcome|disabled control unexpectedly used cache|seed must produce a cold configured host', 'configured cache hit/miss differs from expected'),
    (r'wrong configured-host input identity|unexpected input sections:|different first-stage image', 'configured host identity changed'),
    (r'incorrect released version|release mismatch|stale image tag|release tag missing', 'incorrect release metadata or OCI tag'),
    (r'package sentinel missing|package marker (?:missing|already present)|unexpected package marker', 'installed package marker incorrect'),
    (r'missing native OCI layout|unusable native OCI handoff|incomplete native OCI handoff|missing current OCI handoff', 'direct OCI handoff validation failed'),
    (r'installer direct prerequisite missing|missing selected host DEB', 'native installer prerequisite missing'),
    (r'did not produce|produced no installer|missing installer|installer marker missing', 'missing final installer'),
    (r'(?:does not match|(?:sha256|checksum|digest|inventory|identity|handoff|marker) mismatch)', 'artifact identity mismatch'),
    (r'(?:validation failed|expected .* (?:installer|OCI|native|cache)|incomplete .* (?:OCI|cache|handoff))', 'artifact validation failed'),
    (r'(?:Analysis of target .* failed|Analysis failed)', 'Bazel analysis failed'),
    (r'(?:An error occurred during the fetch of repository|fetch_repo:|download failed)', 'Bazel or repository fetch failed'),
    (r'(?:failed to (?:solve|build)|Build did NOT complete successfully)', 'build failed'),
    (r'(?:not supported|unsupported|unrecognized option|unknown (?:option|target|command)|invalid argument)', 'unsupported or invalid argument'),
    (r'(?:fatal error:|compilation terminated|undefined reference|linker command failed)', 'compiler or linker failed'),
    (r'(?:Traceback \(most recent call last\)|^[A-Za-z]*Error:)', 'Python exception'),
)
CLASSES = [(re.compile(p,re.I),s) for p,s in CLASSES]
PROGRAMS = re.compile(r'^(?:(?:[^\s:]+/)?(?:bash|sh):\s+)?(?:[^\s:]+/)?(bash|sh|python3?|go|pip3?|apt-get|apt|dpkg(?:-deb)?|cp|mv|rm|install|ln|mkdir|chmod|chown|mount|umount|mksquashfs|unsquashfs|tar|jq|awk|sed|git|docker|bazel|gcc|g\+\+|ld):')
ERRORISH = re.compile(r'\b(?:error|failed|fatal|failure|cannot|unable|killed|exception|panic|invalid|missing|denied|mismatch|not found)\b',re.I)
STATIC = ('sonic-vs.bin.log','vs-container-images.log','vs-configured-host-native.log')
FIXTURE = ('A.json','B.json','ready.json','connection.json','selected','handoff')
RECEIPTS = ('inner-make-seconds.json','make-attempts.json','cache-receipt.json','phases.json',
            'observed-oci.json','metrics.json','transfer.json','package-perturbation.json','seed-boundary.json')
PUBLIC_TERMS = set('expected missing archived archive file files checksum identity digest receipt installer native selected action registry package packages ca bundle configuration config build builder container image tags tag version images OCI host cache installation control prepared frozen inputs input source rule metric output stage return status target filesystem failed error execute permission access denied unable cannot read write open directory not found exists unavailable invalid command stale symlink mismatch hit miss seed restored B A transfer'.lower().split())

def target_name(raw):
    raw = raw.strip().removeprefix('./').removeprefix('/sonic/')
    if len(raw)<210 and REL.fullmatch(raw) and not SECRET.search(raw) and not any(x in ('.','..') for x in raw.split('/')):
        return raw

def lines(path, root, start=0):
    try:
        # Even a path printed by Make may point outside through a symlink.
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            return
        with path.open('rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mtime_ns<start:
                return
            if st.st_size>8*1024*1024:
                f.seek(st.st_size-8*1024*1024)
                f.readline(16384)
            for raw in f:
                if len(raw)>32768:
                    continue
                text=STAMP.sub('', ANSI.sub('',raw.decode('utf-8','replace'))).strip()
                # Skip xtrace, explicit commands, env and private data. URLs are
                # never exported, even on actual errors.
                if not text or text[0] in '+$' or re.match(r'^(?:export\s+|sudo\s+|[\w]+\s*=)',text):
                    continue
                yield text
    except (OSError,ValueError):
        return

def reason(line):
    if SECRET.search(line):
        return
    for pattern, summary in CLASSES:
        m=pattern.search(line)
        if not m: continue
        item={'error':summary}
        if summary=='HTTP request failed': item['status']=int(m[1])
        if program:=PROGRAMS.match(line): item['program']=program[1]
        # Constrain all supplemental output to recognized namespaces.
        public_text=re.sub(r'(?:[A-Za-z][A-Za-z0-9+.-]*://|file:)[^\s\x22\x27<>]+','[location]',line)
        if label:=LABEL.search(public_text): item['target']=label[0]
        if module:=MODULE.search(public_text): item['module']=module[0][:150]
        if loc:=re.search(r'(?<![\w./])(?:/sonic/)?(target/[^\s\x22\x27\](),;]+)',public_text):
            if name:=target_name(loc[1]): item['path']=name
        if summary=='pip dependency unavailable' and (p:=re.search(r'(?:No matching distribution found for|requirement) ([a-zA-Z0-9][\w.-]{0,60})(?:[<>=!~\s]|$)',line)):
            item['package']=p[1]
        if summary=='dpkg package operation failed' and (p:=re.search(r'error processing package ([a-z0-9][a-z0-9+.-]{0,70})(?:[:\s]|$)',line)):
            item['package']=p[1]
        if program:=re.search(r'\b([A-Za-z][A-Za-z]*Error): ',line):
            item['exception']=program[1][:40]
        return item
    if ERRORISH.search(line):
        # An unfamiliar native exception still has a searchable signature and
        # bounded vocabulary; avoid copying free text from shell commands.
        words=[s for s in re.findall(r'[a-zA-Z]+',line.lower()) if s in PUBLIC_TERMS]
        return {'error':'unclassified error output','keywords':list(dict.fromkeys(words))[:8],
                'line_sha256':hashlib.sha256(line.encode()).hexdigest()}

def safe_json(path,root):
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size>131072: return None
        return json.loads(path.read_bytes())
    except (OSError,ValueError): return None

def current(path,start):
    try: return not path.is_symlink() and path.is_file() and path.stat().st_mtime_ns>=start
    except OSError:return False

def collect(root, arm, error, work, log, *, started_ns=0, stage=None):
    """Return a JSON-serializable report. Pass time.time_ns() before each attempt."""
    root=Path(root).resolve(); work=Path(work).absolute(); log=Path(log).absolute()
    start=int(started_ns)
    report={'exception':type(error).__name__, 'reason':reason(str(error)) or {'error':'arm failed'},
            'receipts_present':{name:current(work/name,start) for name in RECEIPTS},
            'native_fixture_present':{name:current(root/'.configured-host-bench'/name,0) for name in FIXTURE},
            'sources':{},'make_errors':[]}
    if stage in ('launch','bootstrap-b','seed-a','restored-b','paired-control-b','changed-deb-b','native-make','verify','restore','mutate','capture','snapshot'):
        report['stage']=stage
    else: report['stage']=arm if arm in ('start-live','stop-live','bootstrap-b','version-b-control','paired-control-b','seed-a','restored-b','changed-deb-b') else 'unknown'
    for name,key in (('inner-make-seconds.json','inner_make_exit'),('make-attempts.json','last_attempt_exit')):
        if current(work/name,start):
            val=safe_json(work/name,root)
            if isinstance(val,list) and val: val=val[-1]
            if isinstance(val,dict) and isinstance(val.get('exit'),int): report[key]=val['exit']
    if isinstance(getattr(error,'returncode',None),int): report['exception_returncode']=error.returncode
    actions=root/'artifacts/vs'
    reports=[('arm',work/'observed-oci.json'),('bootstrap',work.parent/'bootstrap-b/observed-oci.json'),('native_b',work.parent/'version-b-control/observed-oci.json')]
    for label,path in reports:
        j=safe_json(path,root)
        if (isinstance(j,dict) and type(j.get('images')) is int and 0<=j['images']<=256
                and isinstance(j.get('receipt_sha256'),str) and DIGEST.fullmatch(j['receipt_sha256'])):
            report.setdefault('verified_oci',{})[label]={'images':j['images'],'receipt_sha256':j['receipt_sha256']}
    cache=actions/'configured-host-cache.json'
    j=safe_json(cache,root) if current(cache,start) else None
    if isinstance(j,dict) and type(j.get('cache_hit')) is bool and isinstance(j.get('identity_sha256'),str) and DIGEST.fullmatch(j['identity_sha256']):
        report['configured_host']={'cache_hit':j['cache_hit'],'identity_sha256':j['identity_sha256']}
    active=[]; explicit=[]; any_target=[]; source=root/'target'
    def scan(path,label,main=False):
        issues=collections.deque(maxlen=12); count=0
        for line in lines(path,root,start):
            count+=1
            if match:=MAKE.match(line):
                item={'target':target_name(match['target']) or '[non-output target]'}
                if match['exit']:item['exit']=int(match['exit'])
                elif match['signal']:item['signal']=match['signal'].lower()
                fn=match['rule']
                if fn and not any(x in ('..','') for x in fn.split('/')) and re.fullmatch(r'(?:[\w.-]+/)*(?:[\w.-]+\.mk|Makefile(?:\.work)?)',fn) and not SECRET.search(fn):
                    item.update(rule=fn,line=int(match['line']))
                if item not in report['make_errors']:report['make_errors'].append(item)
                if name:=target_name(match['target']): explicit.append(name)
                continue
            if match:=MARK.match(line):
                name=target_name(match[2])
                if name:
                    any_target.append(name)
                    what=match[1].lower()
                    if what=='building':active.append(name)
                    elif 'fail' in what:explicit.append(name)
                    elif name in active: active.remove(name)
                continue
            if detail:=reason(line):
                if detail in issues: issues.remove(detail)
                issues.append(detail)
        if count:report['sources'][label]={'recognized_errors':list(issues),'lines_examined':count}

    scan(log,'arm Make output',True)
    if arm=='start-live':scan(log.parent/'launch.log','launcher output')
    targets=list(dict.fromkeys(explicit))
    if targets:report['failed_targets']=targets[:10]
    if not targets and active:report['suspected_target']=active[-1]
    if active:report['last_active_targets']=list(dict.fromkeys(active))[-6:]
    candidates=[root/(p+'.log') for p in (targets+list(reversed(active[-6:]))+list(reversed(any_target[-3:])))]
    candidates.extend(source/n for n in STATIC)
    if not targets:
        recent=[]
        for directory in (source,source/'debs/trixie',source/'python-wheels/trixie',source/'files/trixie'):
            if directory.is_dir() and directory.resolve().is_relative_to(root):
                recent.extend(p for p in directory.glob('*.log') if current(p,start) and target_name(str(p.relative_to(root))))
        recent.sort(key=lambda p:p.stat().st_mtime_ns,reverse=True)
        candidates.extend(recent[:5])
    for p in list(dict.fromkeys(candidates))[:14]:
        if p.resolve().is_relative_to(root):scan(p,str(p.relative_to(root)))
    report['make_errors']=report['make_errors'][:12]
    return report
