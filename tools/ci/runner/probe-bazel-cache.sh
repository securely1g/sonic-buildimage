#!/usr/bin/env bash
set -euo pipefail
mkdir -p artifacts/kernel-sandbox
exec > >(tee artifacts/kernel-sandbox/cache-permissions.log) 2>&1
image=sonic-slave-trixie:4b8b34e9bb1
cache=/data/sonic-runner/cache/sonic-buildimage/bazel
id
docker ps --format '{{.ID}} {{.Image}} {{.Status}}'
docker run --rm --privileged --mount "type=bind,src=$cache,dst=/bazel_cache,readonly" "$image" python3 -c '
import collections,os,pathlib,stat
root=pathlib.Path("/bazel_cache/repository_cache")
paths=[pathlib.Path("/bazel_cache"),root,root/"content_addressable",root/"content_addressable/sha256",root/"content_addressable/sha256/a1fc64b650ac9a4c72e26524fed72caf80fb4cb4527f2b703668723bc23d60bd"]
for p in paths:
 try:
  s=p.lstat();print(str(p),"uid",s.st_uid,"gid",s.st_gid,"mode",oct(stat.S_IMODE(s.st_mode)),flush=True)
 except OSError as e:print(str(p),str(e),flush=True)
counts=collections.Counter();examples=[]
for directory,dirs,files in os.walk(root,followlinks=False):
 for p in [pathlib.Path(directory)]+[pathlib.Path(directory)/n for n in files]:
  s=p.lstat();kind="dir" if stat.S_ISDIR(s.st_mode) else "file";counts[(s.st_uid,s.st_gid,oct(stat.S_IMODE(s.st_mode)),kind)]+=1
  if kind=="dir" and s.st_uid!=1001 and len(examples)<12:examples.append(str(p))
print("ownership_counts",dict(counts),flush=True);print("different_owner_examples",examples,flush=True)
'

printf '\nRepair only the verified stale root-owned digest directories:\n'
docker run --rm --privileged --mount "type=bind,src=$cache,dst=/bazel_cache" \
  --mount "type=bind,src=$PWD/tools/ci/runner/repair-bazel-cache.py,dst=/repair.py,readonly" \
  "$image" python3 /repair.py
