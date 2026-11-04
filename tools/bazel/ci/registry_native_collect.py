#!/usr/bin/env python3
"""Collect retained DASH outputs using fresh configured queries, never a build."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import registry_native_export as origin
from tools.bazel.ci import registry_native_worker as worker
from tools.bazel.ci.source_image_worker import mirror_configuration

require = worker.require
BAZEL_SHA256 = "61d89402f0368e64b6c827be5de79d8e65382e8124c3cbb97325611a1851392e"
BASE_NAME = "ad4b2533ed21d3c399b32fd29ec82494"
SPEC_SHA256 = "09fa3e6fe03937c85d8eef6ce52c1a9786b18c70095486aa0681c668f673f229"
CONSUMER_SHA256 = "a17b044a77b260a566d6b1d32b102439a0e7437af2916054aa7e8bac99cbd961"
SOURCE_SHA256 = "2e15f1ef3d95a65b10a66c097319f0411118d98db32834e1f9e3172670a8598d"
TEST_SHA256 = {
    "runtime_test/test.xml": "0068bec68665b33a3ade1fb46892ca7e04257137fda3c10a37159dfa1c772d74",
    "runtime_test/test.log": "f9efe205458e866477cb25311f33869badf94622df3ebd02b49364b5161a483b",
    "make_flags_test/test.xml": "144206e328cd30cee8b7639e0ec3d0208df4114ef9bd2b558edd8949f9b94224",
    "make_flags_test/test.log": "d14ea8a6cd2a8cc607c47950cbdceced583fc224b854c23c8b8c51b5553dc4c3",
}
BUILDS = ["@sonic-dash//:runtime", "@sonic-dash//:runtime.debug_symbols",
          "@sonic-dash//:sdk", "@sonic-dash//:runtime_packages.json"]


def stable_file(path):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), "Expected a confined regular file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    require(identity(before) == identity(after) == identity(path.lstat()), "File changed during measurement")
    return {"sha256": digest, "bytes": before.st_size, "mode": stat.S_IMODE(before.st_mode),
            "uid": before.st_uid, "gid": before.st_gid, "mtime_ns": before.st_mtime_ns}


def confined(path, root):
    require(path.resolve().is_relative_to(root.resolve()), "Retained path escapes its fixed root")
    return path


def source_fence(work, base):
    """Hash BUILD/Starlark definitions without executing retained helper scripts."""
    paths = [work/"consumer/MODULE.bazel", work/"consumer/MODULE.bazel.lock",
             work/"consumer/BUILD.bazel", work/"consumer/.bazelversion"]
    external = base/"external"
    total = 0
    for parent, directories, files in os.walk(external, followlinks=False):
        directories[:] = sorted(d for d in directories if not (Path(parent)/d).is_symlink())
        for name in sorted(files):
            if name in ("BUILD", "BUILD.bazel", "MODULE.bazel", "REPO.bazel", "WORKSPACE", "WORKSPACE.bazel") or name.endswith(".bzl"):
                paths.append(Path(parent)/name)
    require(len(paths) <= 50000, "Source definition inventory exceeds bound")
    result = {}
    for path in paths:
        confined(path, work)
        total += path.stat().st_size
        require(total <= 512*1024**2, "Source definition bytes exceed bound")
        result[path.relative_to(work).as_posix()] = stable_file(path)
    return result


def query_binary(cache):
    # The binary itself is pinned to the official 8.5.1 Linux x86_64 SHA256.
    # No retained wrapper or Bazelisk code is executed, and no download is attempted.
    candidates = [cache/"bazelisk/downloads/sha256"/BAZEL_SHA256/"bin/bazel",
                  Path("/usr/local/bin/bazel"), Path("/usr/bin/bazel")]
    matches = []
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            measured = stable_file(path)
            if measured["sha256"] == BAZEL_SHA256:
                require(measured["uid"] == 0 and not measured["mode"] & 0o022,
                        "Pinned query binary must be protected")
                matches.append(path)
    require(matches, "Pinned installed Bazel8.5.1 binary is unavailable; no download permitted")
    return matches[0]


def query_command(binary, spec, base, workspace, label):
    require(label in BUILDS and spec["config"]["build_targets"] == BUILDS, "Unexpected configured target")
    require(spec["profile"] == {'name': 'two_actions', 'status': 'pilot', 'minimum_memory_bytes': 21474836480, 'minimum_cpu': 4, 'measured_memory_limit_bytes': 21474836480, 'measured_cpu_limit': 4.0, 'jvm_heap_gib': 3, 'jobs': 2, 'cpu': 2, 'scheduler_memory_mb': 8000, 'loaded_container_memory_gib': 4}, "Original resource profile differs")
    flags = [f"--registry={workspace.as_uri()}", "--registry=https://bcr.bazel.build/",
             "--platforms=@sonic-build-infra//platforms:x86_64_trixie",
             "--host_platform=@sonic-build-infra//platforms:x86_64_trixie",
             "--lockfile_mode=error", "--nofetch",
             "--remote_download_regex=(^|.*/)tools/build_tools/kernel_runtime_(amd64|arm64)(/.*)?$",
             "--experimental_downloader_config="+spec["downloader_config"],
             *["--distdir="+x for x in spec["distdirs"]],
             "--repository_cache="+spec["repository_cache"], "--disk_cache="+spec["disk_cache"],
             "--remote_cache=", "--jobs=2", "--local_resources=cpu=2", "--local_resources=memory=8000"]
    expression = 'json.encode({"label": '+json.dumps(label)+', "resolved_label": str(target.label), "files": [{"path": f.path, "tree": f.is_directory} for f in target.files.to_list()]})'
    return [str(binary), "--ignore_all_rc_files", "--output_user_root="+spec["work"]+"/bazel",
            "--output_base="+str(base), "--host_jvm_args=-Xmx3g",
            "--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts",
            "cquery", *flags, "--output=starlark", "--starlark:expr="+expression,
            "config("+label+", target)"]


def parse_query(text, label, execution_root):
    rows = [json.loads(x) for x in text.splitlines() if x.strip()]
    require(len(rows) == 1 and set(rows[0]) == {"label", "resolved_label", "files"},
            "Configured query must emit one exact target")
    row = rows[0]
    require(row["label"] == label and isinstance(row["resolved_label"], str), "Query target differs")
    require(isinstance(row["files"], list) and row["files"] and len(row["files"]) <= 128,
            "Unexpected default output inventory")
    paths = set()
    for value in row["files"]:
        require(type(value) is dict and set(value) == {"path", "tree"} and
                type(value["path"]) is str and type(value["tree"]) is bool, "Malformed configured output")
        path = PurePosixPath(value["path"])
        require(path.parts and path.parts[0] == "bazel-out" and ".." not in path.parts and
                "\\" not in value["path"] and str(path) == value["path"] and path not in paths,
                "Unsafe or duplicate configured output")
        paths.add(path)
        source = confined(execution_root/path, execution_root)
        require(not source.is_symlink() and (source.is_dir() if value["tree"] else source.is_file()),
                "Missing retained configured output; no build permitted")
    return row


def stop_query(binary, spec, base, consumer):
    pid_file = base/"server/server.pid.txt"
    if not pid_file.exists():
        return {"status": "not_started"}
    confined(pid_file, base)
    pid = int(pid_file.read_text().strip())
    before = worker.server_process(pid)
    if before is None or (before["state"] == "Z" and before["rss_pages"] == 0):
        return {"status": "already_stopped", "pid": pid}
    prefix = query_command(binary, spec, base, consumer, BUILDS[0])
    startup = prefix[:prefix.index("cquery")]
    result = subprocess.run([*startup, "shutdown"], cwd=consumer, check=False, timeout=90)
    require(result.returncode in (0,36), "Query server shutdown failed")
    after = worker.server_process(pid)
    require(after is None or (after["started"] == before["started"] and after["state"] == "Z" and
                              after["rss_pages"] == 0), "Exact query server remains live")
    return {"status": "stopped", "pid": pid, "before": before, "after": after, "exit_code": result.returncode}


def output_snapshot(rows, execution_root):
    result = {}
    total = 0
    def members(path, tree):
        yield path
        if tree:
            for parent, directories, files in os.walk(path, followlinks=False):
                directories.sort(); files.sort()
                for name in [*directories,*files]:
                    yield Path(parent)/name
    for row in rows:
        for value in row["files"]:
            path = execution_root/value["path"]
            for member in members(path,value["tree"]):
                confined(member, path if value["tree"] else execution_root)
                name = member.relative_to(execution_root).as_posix()
                if name in result: continue
                require(len(result) < 100000, "Declared tree inventory exceeds bound")
                info = member.lstat()
                mode = info.st_mode
                if stat.S_ISREG(mode):
                    total += info.st_size
                    require(total <= 8*1024**3, "Declared output bytes exceed bound before hashing")
                    result[name] = {"kind":"file", **stable_file(member)}
                elif stat.S_ISDIR(mode):
                    result[name]={"kind":"directory","mode":stat.S_IMODE(mode),"uid":info.st_uid,"gid":info.st_gid}
                else:
                    require(stat.S_ISLNK(mode), "Special file in declared output")
                    target=os.readlink(member)
                    require(not PurePosixPath(target).is_absolute(), "Absolute link in declared tree")
                    result[name]={"kind":"symlink","target":target}
    return result


def archive_outputs(rows, execution_root, destination, snapshot):
    destination.mkdir(exist_ok=False)
    total=sum(x.get("bytes",0) for x in snapshot.values())
    require(total <= 8*1024**3 and shutil.disk_usage(destination).free >= total*2+2*1024**3,
            "Insufficient capacity for bounded retained output collection")
    index={}
    for row in rows:
        records=[]
        for item in row["files"]:
            source=execution_root/item["path"]
            key=hashlib.sha256(item["path"].encode()).hexdigest()
            target=destination/(key+(".tar" if item["tree"] else ".file"))
            if not target.exists():
                if item["tree"]:
                    with tarfile.open(target,"x",format=tarfile.PAX_FORMAT,dereference=False) as archive:
                        names=[name for name in snapshot if name==item["path"] or name.startswith(item["path"]+"/")]
                        for name in sorted(names):
                            member=execution_root/name
                            relative=Path(name).relative_to(item["path"]).as_posix()
                            info=archive.gettarinfo(str(member),arcname=relative)
                            if info.isreg():
                                with member.open("rb") as stream:archive.addfile(info,stream)
                            else:archive.addfile(info)
                else:
                    shutil.copyfile(source,target,follow_symlinks=False)
                target.chmod(0o644)
            records.append({"source":item["path"],"tree":item["tree"],"path":"outputs/"+target.name,
                            **stable_file(target)})
        index[row["label"]]=records
    require(output_snapshot(rows,execution_root)==snapshot,"Retained output changed during collection")
    return index


def collect(args, output):
    require(os.geteuid()==0 and Path("/.dockerenv").is_file(),"Collection requires real disposable root")
    worker.marker_identity(Path("/etc/sonic-slave-github-runner.json"))
    require(platform.machine()=="x86_64" and "VERSION_CODENAME=trixie" in Path("/etc/os-release").read_text().splitlines(),
            "Expected actual native AMD64 Trixie")
    worker.clean_source(args.workspace,origin.REVISION)
    private=origin.PRIVATE; work=private/"work"; base=work/"bazel"/BASE_NAME
    require(private.resolve()==private and work.resolve()==work and base.resolve()==base,
            "Original work must remain confined")
    require(stable_file(private/"spec.json")["sha256"]==SPEC_SHA256,"Original spec changed")
    spec=json.loads((private/"spec.json").read_text())
    require(spec["work"]==str(work) and spec["artifacts"]==str(origin.ORIGINAL_ARTIFACTS),
            "Original spec paths changed")
    profile=worker.PROFILES["dash"]
    config=json.loads((args.workspace/"modules"/profile["module"]/profile["version"]/"presubmit.json").read_text())
    require(config==spec["config"] and worker.digest(args.workspace/"modules"/profile["module"]/profile["version"]/"presubmit.json")
            ==profile["presubmit_sha256"],"Original complete consumer selection changed")
    require(stable_file(work/"consumer/MODULE.bazel")["sha256"]==CONSUMER_SHA256 and
            stable_file(base/"external/sonic-dash+/MODULE.bazel")["sha256"]==SOURCE_SHA256,
            "Original consumer/source identity changed")
    require(not (base/"server/server.pid.txt").exists(),"Original server must be stopped before collection")
    cache=private.parents[1]
    for name in ("repository","disk"):
        require(spec[name+"_cache"]==str(cache/name) and (cache/name).resolve()==cache/name,"Original cache differs")
    require(spec["downloader_config"]==str(private/"source-mirrors.cfg") and
            (private/"source-mirrors.cfg").resolve()==private/"source-mirrors.cfg","Original mirror path differs")
    require(all(type(x) is str and Path(x).resolve().is_relative_to(cache/"distdir") for x in spec["distdirs"]),
            "Original distdir differs")
    expected_mirrors=mirror_configuration((args.workspace/"ci/source_mirrors.cfg").read_bytes())
    require((private/"source-mirrors.cfg").read_bytes()==expected_mirrors,"Original mirror configuration changed")
    memory_before=worker.memory()
    require(worker.select_build_profile(int(memory_before["memory.max"]),worker.cpu_limit(memory_before["cpu.max"]))
            ==spec["profile"],"Original worker resource profile changed")
    binary=query_binary(cache)
    require(subprocess.check_output([str(binary),"--version"],text=True,timeout=15).strip()=="bazel 8.5.1",
            "Pinned binary version mismatch")
    fence=source_fence(work,base)
    tests=base/"execroot/_main/bazel-out/k8-fastbuild/testlogs/external/sonic-dash+/bazel"
    for name,digest in TEST_SHA256.items():
        require(stable_file(confined(tests/name,work))["sha256"]==digest,"Original test bytes changed")
    fresh=output/"collected";fresh.mkdir(exist_ok=False)
    receipt={"status":"running","kind":"new-collection-of-retained-DASH-outputs",
             "original_run":origin.ORIGINAL_RUN,"collection_run":int(os.environ["GITHUB_RUN_ID"]),
             "registry_revision":origin.REVISION,"bazel_sha256":BAZEL_SHA256,
             "original_bep_present":False,"original_output_index_present":False,
             "native_execution":False,"configured_query_executed":False,
             "build_invocations":0,"test_invocations":0,"apt_invocations":0,
             "fetch_enabled":False,"lockfile_mode":"error","original_spec_sha256":SPEC_SHA256,
             "downloader_config_sha256":hashlib.sha256(expected_mirrors).hexdigest(),
             "memory_before":memory_before,
             "definitions_evaluated":"verified retained BUILD and Starlark; no repository fetching permitted",
             "source_fence_sha256":hashlib.sha256(json.dumps(fence,sort_keys=True).encode()).hexdigest()}
    rows=[]
    try:
        for number,label in enumerate(BUILDS):
            command=query_command(binary,spec,base,args.workspace,label)
            with (fresh/f"query-{number}.jsonl").open("w") as out, (fresh/f"query-{number}.log").open("w") as err:
                receipt["configured_query_executed"]=True
                receipt["query_attempts"]=number+1
                result=subprocess.run(command,cwd=work/"consumer",stdout=out,stderr=err,timeout=300)
            require(result.returncode==0,"Configured query failed; no fetch or build fallback permitted")
            rows.append(parse_query((fresh/f"query-{number}.jsonl").read_text(),label,base/"execroot/_main"))
        require(source_fence(work,base)==fence,"Source or lock changed during configured query")
        snapshot=output_snapshot(rows,base/"execroot/_main")
        index=archive_outputs(rows,base/"execroot/_main",fresh/"outputs",snapshot)
        worker.write(fresh/"collected-outputs.json",index)
        worker.write(fresh/"collected-members.json",snapshot)
        for name,digest in TEST_SHA256.items():
            require(stable_file(tests/name)["sha256"]==digest,"Original test changed during query")
            target=fresh/"original-tests"/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(tests/name,target);target.chmod(0o644)
            require(stable_file(target)["sha256"]==digest,"Copied original test differs")
        lock=work/"consumer/MODULE.bazel.lock"
        worker.validate_registry_lock(json.loads(lock.read_text()),args.workspace,origin.REVISION)
        shutil.copyfile(lock,fresh/"MODULE.bazel.lock")
        shutil.copyfile(work/"consumer/MODULE.bazel",fresh/"consumer.MODULE.bazel")
        shutil.copyfile(base/"external/sonic-dash+/MODULE.bazel",fresh/"fetched.MODULE.bazel")
        receipt.update(status="passed",new_output_index_sha256=worker.digest(fresh/"collected-outputs.json"),
                       original_test_sha256=TEST_SHA256,retained_lock_sha256=worker.digest(lock),
                       original_build_targets=BUILDS,source_and_lock_unchanged=True)
    except BaseException as error:
        receipt.update(status="failed",error_class=type(error).__name__)
        raise
    finally:
        try:
            receipt["query_server_shutdown"]=stop_query(binary,spec,base,work/"consumer")
            require(source_fence(work,base)==fence,"Source or lock changed at collection boundary")
            worker.clean_source(args.workspace,origin.REVISION)
            require(stable_file(binary)["sha256"]==BAZEL_SHA256,"Pinned query binary changed")
            receipt["memory_after"]=worker.memory()
            counts=lambda value:dict(line.split() for line in value["memory.events"].splitlines())
            require(all(counts(receipt["memory_after"])[key]==counts(memory_before)[key] for key in ("oom","oom_kill")),
                    "Query collection observed OOM events")
            receipt["no_oom_events"]=True
        except BaseException as error:
            receipt.update(status="failed",final_fence_error=type(error).__name__)
            raise
        finally:
            worker.write(output/"worker-output-collection.json",receipt)
    # Logs are private diagnostics. Public evidence contains only original declared
    # outputs, original two test logs/XMLs, and safe new collection records.
    with tarfile.open(output/"retained-dash-collection.tar","x",format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(fresh.rglob("*")):
            if path.is_file() and not (path.parent==fresh and path.suffix==".log"):
                archive.add(path,arcname=path.relative_to(fresh).as_posix(),recursive=False)
        archive.add(output/"worker-output-collection.json",arcname="worker-output-collection.json",recursive=False)
    (output/"retained-dash-collection.tar").chmod(0o644)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=("collect","root-collect"))
    parser.add_argument("--consumer",required=True)
    parser.add_argument("--revision",required=True)
    parser.add_argument("--worker",required=True)
    parser.add_argument("--workspace",required=True,type=Path)
    args=parser.parse_args()
    worker.selection(args.consumer,args.revision,args.worker,os.environ)
    require(args.consumer=="dash" and args.worker==origin.WORKER and args.revision==origin.REVISION,
            "Only the exact original DASH work is admitted")
    require(args.workspace.resolve()==args.workspace and args.workspace.is_relative_to("/actions/_work"),
            "Registry workspace must be confined")
    output=ROOT/"artifacts/registry-export";output.mkdir(parents=True,exist_ok=True)
    if args.phase=="root-collect":
        collect(args,output)
        return
    require(os.getuid()==os.getgid()==1000,"Outer collector must be UID/GID1000")
    expected={"status":"passed","original_run":origin.ORIGINAL_RUN,"original_job":origin.ORIGINAL_JOB,
              "original_control":origin.ORIGINAL_CONTROL,"registry_revision":origin.REVISION,
              "worker":origin.WORKER,"native_phase":"success","original_publication":"failure"}
    require(json.loads((output/"worker-export-origin.json").read_text())==expected,"Original phase proof missing")
    measured=worker.measure(origin.WORKER);worker.write(output/"worker-collection-preflight.json",measured)
    require(measured["status"]=="passed","Collection isolation/resource preflight failed")
    env=worker.child_environment(os.environ,args.workspace,Path("/bazel_cache/registry-native")/origin.WORKER)
    result=subprocess.run(["sudo","-n","/usr/bin/env","-i",*[k+"="+v for k,v in env.items()],
                           sys.executable,"-B",str(Path(__file__).resolve()),"root-collect",*sys.argv[2:]])
    require(result.returncode==0,"Root collection failed; original native evidence is not relabeled")


if __name__=="__main__":
    try:
        main()
    except BaseException as error:
        diagnostic=ROOT/"artifacts/registry-export"
        if diagnostic.is_dir():
            worker.write(diagnostic/"worker-collection-failure.json",
                         {"status":"failed","stage":"configured-output-collection",
                          "error_class":type(error).__name__,"native_execution":False,
                          "original_run":origin.ORIGINAL_RUN})
        raise
