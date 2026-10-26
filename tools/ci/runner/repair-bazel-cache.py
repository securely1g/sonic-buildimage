"""Repair the 13 verified stale root-owned CAS directories; preserve cache files."""
import json,os,pathlib,re,stat,tempfile
root=pathlib.Path('/bazel_cache/repository_cache/content_addressable/sha256')
assert root.stat().st_uid == 1001 and not root.is_symlink()
selected=[]
for p in root.iterdir():
 s=p.lstat()
 if s.st_uid==1001:continue
 assert s.st_uid==0 and s.st_gid==0 and stat.S_ISDIR(s.st_mode) and stat.S_IMODE(s.st_mode)==0o755 and re.fullmatch('[0-9a-f]{64}',p.name),(str(p),s)
 selected.append(p)
assert len(selected)==13, len(selected)
for p in sorted(selected):
 s=p.lstat();print(json.dumps({'path':str(p),'uid':s.st_uid,'gid':s.st_gid,'mode':oct(stat.S_IMODE(s.st_mode)),'mtime':s.st_mtime,'ctime':s.st_ctime}),flush=True)
 os.chown(p,1001,1003,follow_symlinks=False)
os.setgroups([]);os.setgid(1003);os.setuid(1001)
for p in selected:
 with tempfile.NamedTemporaryFile(prefix='sonic-cache-access-probe-',dir=p) as f:
  f.write(b'permission probe\n');f.flush()
 for child in p.iterdir():
  if child.is_file():
   with child.open('rb') as stream:stream.read(1)
print(json.dumps({'repaired_directories':len(selected),'probe_uid':os.getuid(),'write_and_read_access':'passed','cache_blobs_changed':False}),flush=True)
