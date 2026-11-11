#!/usr/bin/env python3
"""Observe one immutable management-image configuration with the upstream client.

The installed client owns registry authentication and metadata retrieval. This
records version provenance and checks its fixed metadata projection; it does not
pull image layers, start a daemon or establish package/source compatibility.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

MANIFEST_DIGEST = "sha256:e15502a1cb4ff66f9a7de1b566f1a056ca703a72f69b0f0a9943ef92d20f2124"
MANIFEST_SIZE = 6179
IMAGE = "sonicdev-microsoft.azurecr.io/docker-sonic-mgmt@" + MANIFEST_DIGEST
CONFIG_DESCRIPTOR = {
    "mediaType": "application/vnd.docker.container.image.v1+json",
    "digest": "sha256:a7bf7704ab8afed8d2769dc6d6ded17b439b09de335c0a229e612441314a88a4",
    "size": 16670,
}
LAYER_COUNT = 28
REVIEWED_BUILDX_REVISION = "0b265a9f62db554fa9aba6dd19e1bd5704bc7d8a"
FORMAT = ('{"manifest":{{json .Manifest}},"architecture":{{json .Image.Architecture}},'
          '"os":{{json .Image.OS}},"created":{{json .Image.Created}},'
          '"labels":{{json .Image.Config.Labels}},"rootfs":{{json .Image.RootFS}},'
          '"history":{{json .Image.History}}}')
DOCKER = Path("/usr/bin/docker")
BUILDX = Path("/usr/libexec/docker/cli-plugins/docker-buildx")
TOOL_CAP = 256 * 1024 * 1024
STDOUT_CAP = 256 * 1024
STDERR_CAP = 64 * 1024
sha = lambda data: hashlib.sha256(data).hexdigest()
now = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def gone(pid):
    try:
        os.killpg(pid, 0)
        return False
    except ProcessLookupError:
        return True


def file_identity(path, *, require_root=True):
    """Hash one fixed installed executable; no discovery, execution or package query."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= TOOL_CAP:
            raise ValueError("Installed executable type or size differs")
        if require_root and (before.st_uid != 0 or before.st_mode & 0o022):
            raise ValueError("Installed executable ownership or permissions differ")
        if not before.st_mode & 0o111:
            raise ValueError("Installed tool is not executable")
        digest = hashlib.sha256()
        count = 0
        deadline = time.monotonic() + 10
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Installed executable read deadline")
            chunk = os.read(fd, min(1024 * 1024, TOOL_CAP + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > TOOL_CAP:
                raise ValueError("Installed executable byte cap")
            digest.update(chunk)
        fields = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if count != before.st_size or fields(before) != fields(os.fstat(fd)) or fields(before) != fields(path.lstat()):
            raise ValueError("Installed executable changed during read")
        return {"path": str(path), "bytes": count, "sha256": digest.hexdigest(),
                "device": before.st_dev, "inode": before.st_ino, "mode": stat.S_IMODE(before.st_mode),
                "uid": before.st_uid, "gid": before.st_gid}
    finally:
        os.close(fd)


def client_context(directory, inherited):
    config, buildx = directory / "docker-config", directory / "buildx-config"
    config.mkdir(mode=0o700)
    buildx.mkdir(mode=0o700)
    # The empty registry entry prevents implicit default credential-store discovery.
    # There are no auth bytes, credential helpers or imported Docker configuration.
    write_json(config / "config.json", {"auths": {"sonicdev-microsoft.azurecr.io": {}},
               "cliPluginsExtraDirs": [str(BUILDX.parent)]})
    endpoint = directory / "no-daemon.sock"
    if os.path.lexists(endpoint):
        raise ValueError("Preserve existing client-only endpoint")
    env = {key: inherited[key] for key in ("HOME",) if key in inherited}
    env.update(PATH="/usr/bin:/bin", LANG="C.UTF-8", LC_ALL="C.UTF-8", TERM="dumb",
               DOCKER_CONFIG=str(config), BUILDX_CONFIG=str(buildx), DOCKER_HOST="unix://" + str(endpoint))
    argv = [str(DOCKER), "--config", str(config), "buildx", "imagetools", "inspect", "--format", FORMAT, IMAGE]
    return argv, env


def observe_client(directory, argv, env, *, stdout_cap=None, seconds=30):
    # The process/stream/deadline/descendant lifecycle below is retained from the
    # reviewed anonymous upstream client donor. No retry is performed here.
    D = directory
    cap = STDOUT_CAP if stdout_cap is None else stdout_cap
    if type(cap) is not int or not 0 < cap <= 32 * 1024 * 1024 or type(seconds) is not int or not 0 < seconds <= 120:
        raise ValueError("Upstream client observation bounds differ")
    plan = {"stdout_cap": cap, "stderr_cap": STDERR_CAP}
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
     signal.alarm(seconds + 30)
     previous={sig:signal.getsignal(sig) for sig in handled}
     try:
      for sig in handled:signal.signal(sig,deferred)
      proc=subprocess.Popen(argv,cwd=D,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
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



def version_metadata(raw):
    """Record upstream version text without making a version difference a gate."""
    match = re.fullmatch(rb"github\.com/docker/buildx ([A-Za-z0-9_.+-]{1,80}) ([0-9a-f]{7,40})", raw.strip())
    return {"stdout_bytes": len(raw), "stdout_sha256": sha(raw),
            "reported_version": match[1].decode() if match else None,
            "reported_revision": match[2].decode() if match else None,
            "reviewed_reference_revision": REVIEWED_BUILDX_REVISION,
            "installed_binary_source_equivalence_established": False,
            "version_difference_blocks_metadata": False}


def validate_projection(raw):
    """Check only the documented upstream template's fixed metadata projection."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate metadata key")
            value[key] = item
        return value
    def nonfinite(value):
        raise ValueError("Nonfinite metadata value")
    if not 0 < len(raw) <= STDOUT_CAP:
        raise ValueError("Metadata projection byte bound")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    if not isinstance(value, dict) or set(value) != {"manifest", "architecture", "os", "created", "labels", "rootfs", "history"}:
        raise ValueError("Unexpected metadata projection fields")
    descriptor = value["manifest"]
    if (not isinstance(descriptor, dict) or descriptor.get("digest") != MANIFEST_DIGEST
            or type(descriptor.get("size")) is not int or descriptor["size"] != MANIFEST_SIZE
            or descriptor.get("mediaType") != "application/vnd.docker.distribution.manifest.v2+json"):
        raise ValueError("Resolved immutable manifest descriptor differs")
    if value["architecture"] != "amd64" or value["os"] != "linux":
        raise ValueError("Resolved image is not Linux AMD64")
    rootfs = value["rootfs"]
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise ValueError("Unexpected rootfs metadata")
    diff_ids = rootfs.get("diff_ids")
    if (not isinstance(diff_ids, list) or len(diff_ids) != LAYER_COUNT
            or any(not isinstance(item, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in diff_ids)):
        raise ValueError("Rootfs diff-ID cardinality or format differs")
    history = value["history"]
    if (not isinstance(history, list) or not 1 <= len(history) <= 256
            or any(not isinstance(item, dict) or type(item.get("empty_layer", False)) is not bool for item in history)):
        raise ValueError("Invalid image history metadata")
    populated = [item for item in history if not item.get("empty_layer", False)]
    if len(populated) != LAYER_COUNT:
        raise ValueError("History and layer cardinality differ")
    for item in history:
        if any(key in item and not isinstance(item[key], str) for key in ("created_by", "author", "comment")):
            raise ValueError("Unexpected image history field type")
    labels = value["labels"]
    if labels is not None and (not isinstance(labels, dict) or len(labels) > 256
            or any(not isinstance(key, str) or not isinstance(item, str) for key, item in labels.items())):
        raise ValueError("Invalid producer label metadata")
    return {"manifest_descriptor": descriptor, "architecture": value["architecture"], "os": value["os"],
            "history_entries": len(history), "nonempty_history_entries": len(populated),
            "rootfs_diff_ids": len(diff_ids), "producer_label_count": len(labels or {}),
            "original_manifest_config_descriptor": CONFIG_DESCRIPTOR,
            "configuration_output_is_reencoded_projection": True,
            "original_config_bytes_or_digest_independently_verified": False,
            "device_data_layer_identified": False, "package_or_source_compatibility_established": False}


def collect_metadata(directory, argv, env):
    version_dir = directory / "version"
    version_dir.mkdir(mode=0o700)
    version_result = observe_client(version_dir, argv[:4] + ["version"], env)
    if version_result["status"] != "CAPTURED":
        raise ValueError("Installed upstream version observation failed")
    version = version_metadata((version_dir / "stdout.data").read_bytes())
    result = observe_client(directory, argv, env)
    metadata = None
    if result["status"] == "CAPTURED":
        metadata = validate_projection((directory / "stdout.data").read_bytes())
        write_json(directory / "metadata-summary.json", metadata)
    return result, version_result, version, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run, attempt = (os.environ[name] for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
    if (not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run, attempt))
            or os.environ.get("GITHUB_REPOSITORY") != "securely1g/sonic-buildimage"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or any(os.environ.get(name) != "securely1g" for name in ("GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR"))
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.environ.get("RUNNER_OS") != "Linux" or os.environ.get("RUNNER_ARCH") != "X64"):
        raise ValueError("Unexpected hosted observation identity")
    parent = Path(os.environ["RUNNER_TEMP"])
    directory = parent / f"management-image-metadata-{run}-{attempt}"
    if not parent.is_absolute() or parent.is_symlink() or args.output != directory:
        raise ValueError("Unexpected fresh observation path")
    directory.mkdir(mode=0o700)
    phase = "installed_tool_identity"
    try:
        identity = [file_identity(path) for path in (DOCKER, BUILDX)]
        write_json(directory / "tool-identity.json", {"tools": identity,
                   "runner": os.environ["RUNNER_NAME"], "run": int(run), "attempt": int(attempt),
                   "controller": os.environ["GITHUB_SHA"], "image_os": os.environ.get("ImageOS"),
                   "image_version": os.environ.get("ImageVersion"), "identity_scope": "observed_installed_files_not_local_tool_proof"})
        phase = "fresh_anonymous_configuration"
        argv, env = client_context(directory, os.environ)
        write_json(directory / "client-intent.json", {"argv": argv, "at": now(), "seconds": 30,
                   "maximum_client_invocations": 2, "maximum_registry_client_invocations": 1,
                   "version_argv": argv[:4] + ["version"], "credentials_supplied": False,
                   "credential_or_proxy_environment_forwarded": False,
                   "image_or_layer_pull": False, "daemon_started": False})
        phase = "upstream_version_and_immutable_configuration"
        result, version_result, version, metadata = collect_metadata(directory, argv, env)
        phase = "installed_tool_stability"
        after = [file_identity(path) for path in (DOCKER, BUILDX)]
        if after != identity:
            raise ValueError("Installed client identity changed across observation")
        write_json(directory / "observation.json", {"status": result["status"], "image": IMAGE,
                   "upstream_result": result, "version_result": version_result, "version": version,
                   "metadata_summary": metadata, "installed_tools_unchanged": True,
                   "raw_stdout_parsed": result["status"] == "CAPTURED",
                   "configuration_is_reencoded_projection": True,
                   "historical_image_identity_established": False,
                   "device_data_package_acquired": False, "source_compatibility_established": False})
        print(json.dumps({key: result[key] for key in ("status", "returncode", "reason", "elapsed_seconds", "leader_reaped", "group_closed")}))
        return 0 if result["status"] == "CAPTURED" else 1
    except BaseException as error:
        write_json(directory / "failure.json", {"phase": phase, "error_type": type(error).__name__,
                   "status": "FAILED_STOP_NO_RETRY", "at": now()})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
