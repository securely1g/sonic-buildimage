#!/usr/bin/env python3
"""Qualify the K3s credential/launcher path with a tiny real remote-cache hit.

This is a copy-only genrule, not a SONiC build or native action-key portability
test. Each selected Bazel version gets two fresh output bases and no disk cache.
Private command and execution logs are never included in the public receipt.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time

from tools.bazel.ci import source_image_worker as worker
from tools.bazel.ci import source_worker_preflight as preflight

ROOT = Path(__file__).resolve().parents[3]
POOL = "sonic-slave-container-k3s"
IMAGE = "sha256:52cc68070154c5d9132995341a1800f1aa80f59d089f951d274a725693bff732"
MANAGER = "c650551885f8ad22cbc104082d1f1b6171e67eef"
VERSIONS = ("6.4.0", "8.5.1")
TARGET = "//:cache_probe"
OUTPUT = "cache-probe.txt"
LOG_CAP = 4 * 1024 * 1024
STDOUT_CAP = STDERR_CAP = 2 * 1024 * 1024
PROFILE = {"jobs": 1, "cpu": 1, "scheduler_memory_mb": 1024, "jvm_heap_gib": 2}
sha = lambda data: hashlib.sha256(data).hexdigest()
now = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()


class ProbeFailure(ValueError):
    """Only fixed messages authored at this module's guard sites are public."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def require(condition, reason):
    if not condition:
        raise ProbeFailure(reason)


def write_json(path, value):
    write_fresh(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def write_fresh(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def json_decoder():
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ProbeFailure("Nonfinite JSON value")
    return json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant)


def read_under(root, relative, cap):
    """Read one regular file through held, no-follow directory descriptors."""
    parts = Path(relative).parts
    require(parts and not Path(relative).is_absolute() and
            all(part not in (".", "..") for part in parts), "Invalid selected relative path")
    descriptors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(fd)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= cap,
                "Selected file type or size differs")
        raw = bytearray()
        while len(raw) <= cap:
            block = os.read(fd, min(65536, cap + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        fields = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(len(raw) == before.st_size and fields(before) == fields(os.fstat(fd)) and
                fields(before) == fields(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)),
                "Selected file changed during observation")
        return bytes(raw)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def check_identity(environment):
    expected = {"GITHUB_REPOSITORY": "securely1g/sonic-buildimage",
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "securely1g",
                "GITHUB_TRIGGERING_ACTOR": "securely1g"}
    require(all(environment.get(key) == value for key, value in expected.items()),
            "Unexpected manual probe identity")
    require(all(re.fullmatch(r"[1-9][0-9]*", environment.get(key, ""))
                for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")), "Invalid run identity")
    require(re.fullmatch(r"[0-9a-f]{40}", environment.get("GITHUB_SHA", "")),
            "Missing immutable controller")
    require(preflight.worker_matches_pool(environment.get("RUNNER_NAME", ""), POOL),
            "Different worker pool")
    require((os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (1000, 1000, 1000, 1000)
            and platform.machine() == "x86_64", "Probe requires the AMD64 worker user")
    marker_path = Path("/etc/sonic-slave-github-runner.json")
    require(preflight.marker_identity(marker_path) == {"base_image": IMAGE}, "Different worker image")
    marker = json_decoder().decode(read_under(marker_path.parent, marker_path.name, 16384).decode())
    require(marker.get("manager_revision") == MANAGER, "Different worker manager")
    return {"run": int(environment["GITHUB_RUN_ID"]), "attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
            "controller": environment["GITHUB_SHA"], "worker": environment["RUNNER_NAME"],
            "pool": POOL, "base_image": IMAGE, "manager_revision": MANAGER}


def fresh_root(parent, identity):
    require(parent.is_absolute() and parent.resolve() == parent and
            parent.is_relative_to(Path("/actions/_work")), "Different temporary root")
    for path in (parent, *parent.parents):
        require(stat.S_ISDIR(path.lstat().st_mode), "Temporary parent must not be a symlink")
    result = parent / ("remote-cache-smoke-%s-%s" % (identity["run"], identity["attempt"]))
    result.mkdir(mode=0o700)
    return result


def proto_field(record, snake, default=None):
    parts = snake.split("_")
    camel = parts[0] + "".join(part.title() for part in parts[1:])
    names = tuple(dict.fromkeys((snake, camel)))
    present = [key for key in names if key in record]
    require(len(present) <= 1, "Ambiguous protobuf JSON field spelling")
    return record[present[0]] if present else default


def spawn_evidence(raw, version, cached):
    """Consume the documented delimited SpawnExec JSON, not terminal messages."""
    require(version in VERSIONS and 0 < len(raw) <= LOG_CAP, "Invalid execution log bound")
    text, decoder, records, offset = raw.decode("utf-8"), json_decoder(), [], 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        value, offset = decoder.raw_decode(text, offset)
        require(type(value) is dict and len(records) < 4, "Unexpected execution log shape")
        records.append(value)
    require(len(records) == 1, "Expected only the copy genrule spawn")
    value = records[0]
    require(value.get("mnemonic") == "Genrule" and proto_field(value, "target_label") in (TARGET, "@@" + TARGET),
            "Unexpected executed target")
    require(type(proto_field(value, "exit_code", 0)) is int and
            proto_field(value, "exit_code", 0) == 0 and value.get("status", "") == "",
            "Copy action did not succeed")
    flag = "remote_cache_hit" if version == "6.4.0" else "cache_hit"
    hit = proto_field(value, flag, False)
    require(type(hit) is bool and hit is cached, "Cache result differs from the required phase")
    runner = value.get("runner")
    require(runner == "remote cache hit" if cached else runner in ("linux-sandbox", "processwrapper-sandbox", "local"),
            "Unexpected action runner")
    digest = value.get("digest")
    if digest is None:
        require(version == "6.4.0" and not cached, "Missing action cache digest")
        action_sha256 = None
    else:
        require(type(digest) is dict and re.fullmatch(r"[0-9a-f]{64}", digest.get("hash", "")),
                "Invalid action cache digest")
        action_sha256 = digest["hash"]
    outputs = proto_field(value, "actual_outputs", [])
    listed = proto_field(value, "listed_outputs", [])
    require(type(outputs) is list and len(outputs) == 1 and type(outputs[0]) is dict,
            "Unexpected actual outputs")
    path = outputs[0].get("path", "")
    require(type(path) is str and path.startswith("bazel-out/") and Path(path).name == OUTPUT and
            not Path(path).is_absolute() and all(part not in (".", "..") for part in Path(path).parts) and str(Path(path)) == path and
            listed == [path], "Unexpected copy output path")
    # Only these authored/validated scalar fields leave the private log directory.
    return {"target": TARGET, "mnemonic": "Genrule", "runner": runner, "cache_hit": hit,
            "action_sha256": action_sha256, "output_path": path,
            "execution_log_sha256": sha(raw), "execution_log_bytes": len(raw)}


def build_arguments(version, log_path, cached, user_root):
    require(version in VERSIONS, "Unsupported Bazel version")
    return ["--output_user_root=" + str(user_root), "--batch", "--nosystem_rc", "--nohome_rc", "--noworkspace_rc", "build", TARGET,
            "--enable_bzlmod=false", *(["--enable_workspace=true"] if version == "8.5.1" else []),
            "--remote_executor=", "--disk_cache=", "--remote_download_outputs=all",
            "--remote_accept_cached=" + str(cached).lower(),
            "--remote_upload_local_results=" + str(not cached).lower(),
            "--execution_log_json_file=" + str(log_path), "--jobs=1",
            "--action_env=PATH=/usr/bin:/bin", "--action_env=LANG=C.UTF-8",
            "--action_env=LC_ALL=C.UTF-8", "--color=no", "--curses=no", "--noshow_progress"]


def version_environment(inherited, directory, version):
    # Credential helper receives the existing interface. It is never an action_env.
    env = {key: inherited[key] for key in ("HOME", "BAZEL_REMOTE_AUTH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
           "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in inherited}
    env.update(PATH="/usr/local/bin:/usr/bin:/bin", LANG="C.UTF-8", LC_ALL="C.UTF-8",
               USE_BAZEL_VERSION=version, BAZELISK_HOME=str(directory / "bazelisk"))
    return env


def project_child_result(value):
    require(type(value) is dict, "Child control record is not an object")
    require(value.get("status") in ("CAPTURED", "FAILED_STOP_NO_RETRY"), "Unknown child status")
    reasons = (None, "CLIENT_DEADLINE", "CLIENT_OUTPUT_CAP", "INTERRUPTED_OR_CLIENT_EXCEPTION",
               "DESCENDANTS_AFTER_CLIENT_EXIT", "CHILD_NOT_REAPED", "CHILD_GROUP_NOT_CLOSED")
    require(value.get("reason") in reasons, "Unknown child control reason")
    code = value.get("returncode")
    require(code is None or type(code) is int and -255 <= code <= 255, "Invalid child return code")
    result = {"control_record_available": True, "status": value["status"],
              "returncode": code, "control_reason": value.get("reason")}
    for key in ("leader_reaped", "group_closed"):
        require(type(value.get(key)) is bool, "Invalid child closure metadata")
        result[key] = value[key]
    for stream, cap in (("stdout", STDOUT_CAP), ("stderr", STDERR_CAP)):
        size, digest = value.get(stream + "_bytes"), value.get(stream + "_sha256")
        require(type(size) is int and 0 <= size <= cap and type(digest) is str and
                re.fullmatch(r"[0-9a-f]{64}", digest), "Invalid bounded child stream metadata")
        result[stream + "_bytes"], result[stream + "_sha256"] = size, digest
    return result


def run_command(directory, argv, env, *, cwd, seconds, record):
    result = None
    try:
        result = observe_client(directory, argv, env, cwd=cwd, seconds=seconds)
        return result
    finally:
        interrupted = sys.exc_info()[0] is not None
        try:
            if result is None:
                result = json_decoder().decode(read_under(directory, "result.json", 16384).decode())
            safe = project_child_result(result)
        except (OSError, ValueError, TypeError):
            safe = {"control_record_available": False}
        record(safe)
        if not interrupted:
            require(safe["control_record_available"], "Child control record is unavailable or invalid")


def verify_version_output(raw, version):
    lines = raw.splitlines()
    if lines and lines[0].startswith(b"Bazelisk "):
        lines = lines[1:]
    require(b"\n".join(lines).strip() == ("bazel " + version).encode(), "Selected Bazel version differs")


def qualify_version(directory, binary, version, configuration, inherited, nonce,
                    progress=lambda stage: None, record=lambda value: None):
    directory.mkdir(mode=0o700)
    workspace = directory / "workspace"
    workspace.mkdir(mode=0o700)
    write_fresh(workspace / "WORKSPACE", b'workspace(name = "remote_cache_smoke")\n')
    write_fresh(workspace / ".bazelversion", (version + "\n").encode())
    write_fresh(workspace / "BUILD.bazel", b'genrule(name="cache_probe", srcs=["input.txt"], outs=["cache-probe.txt"], cmd="cat $< > $@")\n')
    payload = (nonce + "\n").encode()
    write_fresh(workspace / "input.txt", payload)
    (directory / "bazelisk").mkdir(mode=0o700)
    env = version_environment(inherited, directory, version)
    user_root = directory / "user-root"
    require(not os.path.lexists(user_root), "Output user root must be fresh")
    progress("version")
    version_dir = directory / "version"
    version_dir.mkdir(mode=0o700)
    result = run_command(version_dir, [binary, "--output_user_root=" + str(user_root), "--batch",
                         "--nosystem_rc", "--nohome_rc", "--noworkspace_rc", "version", "--gnu_format"],
                         env, cwd=workspace, seconds=90, record=record)
    require(result["status"] == "CAPTURED", "Bazel version command failed")
    verify_version_output(read_under(version_dir, "stdout.data", 4096), version)
    phases = []
    for name, cached in (("seed", False), ("reuse", True)):
        progress(name)
        phase = directory / name
        phase.mkdir(mode=0o700)
        base = directory / (name + "-output-base")
        require(not os.path.lexists(base), "Output base must be fresh")
        launcher = phase / "bazel"
        if version == "6.4.0":
            rc = phase / "owner.bazelrc"
            write_fresh(rc, b"# Empty smoke rc; actual owner launcher supplies scoped remote cache.\n")
            text = worker.owner_launcher_text(binary, base, rc, directory, configuration)
        else:
            text = worker.launcher_text(binary, base, PROFILE, configuration)
        write_fresh(launcher, text.encode(), 0o700)
        log = phase / "spawns.json"
        result = run_command(phase, [str(launcher), *build_arguments(version, log, cached, user_root)],
                             env, cwd=workspace, seconds=180, record=record)
        require(result["status"] == "CAPTURED", "Bazel copy phase failed")
        evidence = spawn_evidence(read_under(phase, "spawns.json", LOG_CAP), version, cached)
        output = read_under(base, "execroot/remote_cache_smoke/" + evidence.pop("output_path"), 4096)
        require(output == payload, "Copy output differs from the original input")
        evidence.update(phase=name, output_sha256=sha(output), output_bytes=len(output),
                        fresh_output_base=True, disk_cache="disabled", remote_executor="disabled")
        phases.append(evidence)
    comparison = "unavailable_for_6.4.0_seed"
    if phases[0]["action_sha256"] is not None:
        require(phases[0]["action_sha256"] == phases[1]["action_sha256"], "Action digests differ")
        comparison = "equal"
    return {"bazel_version": version, "phases": phases, "same_output": True,
            "action_digest_comparison": comparison}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-revision", required=True)
    parser.add_argument("--worker-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(re.fullmatch(r"[0-9a-f]{40}", args.native_revision) and
            re.fullmatch(r"[0-9a-f]{64}", args.worker_sha256), "Invalid source binding")
    require(Path(worker.__file__).resolve() == ROOT / "tools/bazel/ci/source_image_worker.py",
            "Imported worker is outside the selected checkout")
    require(sha(read_under(ROOT, "tools/bazel/ci/source_image_worker.py", 256 * 1024)) == args.worker_sha256,
            "Selected worker source differs")
    identity = check_identity(os.environ)
    root = fresh_root(Path(os.environ["RUNNER_TEMP"]), identity)
    require(args.output.parent.resolve() == root.parent and not os.path.lexists(args.output),
            "Public receipt must be fresh in runner temp")
    old_umask = os.umask(0o077)
    receipt = {"status": "FAILED_NOT_QUALIFIED", "identity": identity,
               "native_revision": args.native_revision, "worker_sha256": args.worker_sha256,
               "scope": "TINY_COPY_REMOTE_CACHE_ONLY_NOT_NATIVE_BUILD_ACCEPTANCE", "versions": [], "commands": []}
    try:
        receipt["stage"] = "credential_setup"
        configuration = worker.remote_cache_configuration(ROOT)
        binary = shutil.which("bazel", path="/usr/local/bin:/usr/bin:/bin")
        require(binary is not None, "Bazel launcher is unavailable")
        executable = Path(binary).resolve()
        info = executable.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022 and
                info.st_mode & 0o111, "Untrusted installed Bazel launcher")
        binary = str(executable)
        receipt["bazel_launcher_sha256"] = sha(read_under(executable.parent, executable.name, 128 * 1024 * 1024))
        auth = root / "credential-probe"
        auth.mkdir(mode=0o700)
        auth_output = auth / "remote-cache-access.json"
        env = version_environment(os.environ, root, VERSIONS[0])
        receipt["stage"] = "credential_probe"
        result = run_command(auth, [configuration["helper"], "probe", "--output", str(auth_output)],
                             env, cwd=ROOT, seconds=45,
                             record=lambda value: receipt["commands"].append(dict(stage="credential_probe", **value)))
        require(result["status"] == "CAPTURED", "Credential probe command failed")
        access = json_decoder().decode(read_under(auth, auth_output.name, 4096).decode())
        require(set(access) == {"endpoint", "authenticated", "status"} and
                access["endpoint"] == configuration["endpoint"] and access["authenticated"] is True and
                type(access["status"]) is int and access["status"] == 200, "Credential probe did not pass")
        receipt["credential_helper_sha256"] = configuration["helper_sha256"]
        receipt["authenticated_probe"] = True
        for version in VERSIONS:
            nonce = "%s/%s/%s/%s" % (identity["run"], identity["attempt"], args.native_revision, version)
            receipt["versions"].append(qualify_version(root / version, binary, version, configuration, os.environ, nonce,
                progress=lambda stage: receipt.update(stage=version + "/" + stage),
                record=lambda value: receipt["commands"].append(dict(stage=receipt["stage"], **value))))
        receipt["stage"] = "complete"
        receipt["status"] = "TINY_COPY_REMOTE_CACHE_QUALIFIED"
    except BaseException as error:
        receipt["failure_reason"] = error.reason if isinstance(error, ProbeFailure) else "Unhandled runtime or I/O exception"
        receipt["error_type"] = type(error).__name__ if type(error).__name__ in (
            "ProbeFailure", "ValueError", "OSError", "FileNotFoundError", "TimeoutError", "InterruptedError", "JSONDecodeError") else "ProbeFailure"
        raise SystemExit(1) from None
    finally:
        receipt["at"] = now()
        write_json(args.output, receipt)
        os.umask(old_umask)


def gone(pid):
    try:
        os.killpg(pid, 0)
        return False
    except ProcessLookupError:
        return True


def observe_client(directory, argv, env, *, cwd, seconds):
    # The process/stream/deadline/descendant lifecycle below is retained from the
    # reviewed anonymous upstream client donor. No retry is performed here.
    D = directory
    plan = {"stdout_cap": STDOUT_CAP, "stderr_cap": STDERR_CAP}
    def save(name, value):
        write_json(D / name, value)
    proc=None;sel=selectors.DefaultSelector();buffers={'stdout':bytearray(),'stderr':bytearray()};reason=None;signals=[];started=time.monotonic()
    def interrupted(signum,frame):
     raise InterruptedError('Interrupted during bounded upstream client observation')
    handled=(signal.SIGALRM,signal.SIGINT,signal.SIGTERM)
    original_handlers={sig:signal.signal(sig,interrupted) for sig in handled}
    pending_signal=[None]
    def deferred(signum,frame):
     if pending_signal[0] is None:pending_signal[0]=signum
    try:
     signal.alarm(seconds + 15)
     previous={sig:signal.getsignal(sig) for sig in handled}
     try:
      for sig in handled:signal.signal(sig,deferred)
      proc=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
     finally:
      for sig,handler in previous.items():signal.signal(sig,handler)
     if pending_signal[0] is not None:raise InterruptedError('Interrupted during retained client creation')
     for key,stream in [('stdout',proc.stdout),('stderr',proc.stderr)]:
      os.set_blocking(stream.fileno(),False);sel.register(stream,selectors.EVENT_READ,key)
     deadline=started+seconds
     while sel.get_map():
      if time.monotonic()>=deadline:reason='CLIENT_DEADLINE';break
      for key,_ in sel.select(min(.25,max(0,deadline-time.monotonic()))):
       try:chunk=os.read(key.fileobj.fileno(),65536)
       except BlockingIOError:continue
       if not chunk:sel.unregister(key.fileobj);continue
       target=buffers[key.data];cap=plan[key.data+'_cap'];available=max(0,cap-len(target))
       target.extend(chunk[:available])
       if len(chunk)>available:reason='CLIENT_OUTPUT_CAP';break
      if reason:break
     if reason is None:
      try:proc.wait(timeout=max(.001,deadline-time.monotonic()))
      except subprocess.TimeoutExpired:reason='CLIENT_DEADLINE'
    except BaseException:
     if reason is None:reason='INTERRUPTED_OR_CLIENT_EXCEPTION'
     raise
    finally:
     signal.alarm(0)
     for sig in handled:signal.signal(sig,signal.SIG_IGN)
     if proc is not None:
      if not gone(proc.pid):
       if reason is None:reason='DESCENDANTS_AFTER_CLIENT_EXIT'
       try:os.killpg(proc.pid,signal.SIGKILL);signals.append('KILL')
       except ProcessLookupError:pass
      try:proc.wait(timeout=5)
      except subprocess.TimeoutExpired:reason='CHILD_NOT_REAPED'
      end=time.monotonic()+2
      while not gone(proc.pid) and time.monotonic()<end:time.sleep(.05)
      if not gone(proc.pid):reason='CHILD_GROUP_NOT_CLOSED'
      for stream in (proc.stdout,proc.stderr):
       if stream is not None:stream.close()
     for key,raw in buffers.items():
      with (D/(key+'.data')).open('xb') as f:f.write(raw)
     sel.close()
     result={'status':'CAPTURED' if proc is not None and proc.returncode==0 and reason is None else 'FAILED_STOP_NO_RETRY','returncode':None if proc is None else proc.returncode,'reason':reason,'elapsed_seconds':time.monotonic()-started,'leader_reaped':proc is None or proc.poll() is not None,'group_closed':proc is None or gone(proc.pid),'signals':signals,'client_invocations':int(proc is not None),'deferred_spawn_signal':pending_signal[0],'stdout_bytes':len(buffers['stdout']),'stdout_sha256':sha(buffers['stdout']),'stderr_bytes':len(buffers['stderr']),'stderr_sha256':sha(buffers['stderr']),'at':now()}
     save('result.json',result)
     for sig,handler in original_handlers.items():signal.signal(sig,handler)
    return result


if __name__ == "__main__":
    main()
