"""Keep native error diagnostics bounded; never publish raw shell lines."""
import json,os,re,sys
from pathlib import Path
root=Path.cwd();role=sys.argv[1];assert role in ('a','b')
out=Path(os.environ['PAIR_ROOT'])/'evidence'/role;out.mkdir(parents=True,exist_ok=True)
report={}
for p in [root/'artifacts/vs/build.log',root/'artifacts/vs/legacy-device.log',root/'target/sonic-vs.bin.log',root/'target/vs-configured-host-native.log']:
 if not p.is_file():continue
 signals=set()
 with p.open(errors='replace') as f:
  for line in f:
   line=line.strip()
   if line.startswith(('+','$')):continue
   if (m:=re.search(r'make(?:\[\d+\])?: \*\*\* \[([a-zA-Z0-9._/-]+):([0-9]+): (target/[a-zA-Z0-9._+/:@-]+)\] Error (\d+)',line)):
    signals.add('make '+':'.join(m.groups()))
   for pat, label in [('TLS handshake timeout','transport TLS timeout'),('No space left on device','disk full'),('No matching distribution found','offline pip wheel missing'),('unexpected host package','invalid host package'),('VS configured host cache unavailable','configured action fell back'),('VS configured host producer failed','configured producer failed'),('checksum mismatch','checksum mismatch'),('Could not resolve','DNS failed'),('Could not resolve host','DNS failed'),('Unable to locate package','APT package missing'),('Could not connect','transport unavailable'),('FAILED: Build did NOT complete successfully','Bazel build failure')]:
    if pat.lower() in line.lower():signals.add(label)
 report[p.name]=sorted(signals)
for name in ('host-base-cache.json','configured-host-cache.json'):
 f=root/'artifacts/vs'/name
 if f.is_file():
  raw=json.loads(f.read_bytes());report[name]={k:raw[k] for k in ('cache_hit','identity_sha256','input_sha256','outputs') if k in raw}
(out/'failure.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n');print(json.dumps(report))
