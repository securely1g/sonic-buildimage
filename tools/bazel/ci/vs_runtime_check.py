#!/usr/bin/env python3
"""Qualify two runtime capabilities on one disposable K3s worker, without Make."""
import argparse
from contextlib import contextmanager
import grp
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import vs_worker_inspection as inspection

MANAGER = "c650551885f8ad22cbc104082d1f1b6171e67eef"
BASE_IMAGE = "sha256:52cc68070154c5d9132995341a1800f1aa80f59d089f951d274a725693bff732"
KERNEL_HELPER_SHA256 = "de63743c6c8bde35b8326fe48adaeb5783851504855a722aafe1875f156eff47"
NESTED_HELPER_SHA256 = "30994e9b75d4906373bbb789da66350417d1bb105828a2d3476b0a59cddf7ac0"
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
CAP = 2 * 1024 * 1024
LITERAL = b"SONiC stopped-container archive probe\n"
LITERAL_PATH = "probe.txt"
NEVER_RUN = "/never-execute-vs-runtime-probe"
STOPPED_LOGS = ("stopped-import", "stopped-create", "stopped-inspect-before",
                "stopped-archive", "stopped-inspect-after", "stopped-remove-container",
                "stopped-remove-image")


class ProbeFailure(ValueError):
    """A public-safe message authored here, never tool stderr."""


class SupervisorUnclosed(ProbeFailure):
    pass


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def identity(worker, environment):
    require(environment.get("GITHUB_REPOSITORY") == "securely1g/sonic-buildimage"
            and environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and environment.get("GITHUB_ACTOR") == environment.get("GITHUB_TRIGGERING_ACTOR") == "securely1g",
            "Unexpected manual workflow identity")
    require(worker == environment.get("RUNNER_NAME") and inspection.resources.worker_matches_pool(worker, inspection.POOL),
            "Assigned worker identity differs")
    values = {key: environment[name] for key, name in (("run", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT"), ("controller", "GITHUB_SHA"))}
    require(all(re.fullmatch(r"[1-9][0-9]{0,19}", values[k]) for k in ("run", "attempt"))
            and re.fullmatch(r"[0-9a-f]{40}", values["controller"]), "Invalid immutable execution identity")
    return dict(values, worker=worker, pool=inspection.POOL)


def token(worker, run, attempt):
    require(inspection.resources.worker_matches_pool(worker, inspection.POOL)
            and all(re.fullmatch(r"[1-9][0-9]{0,19}", x) for x in (run, attempt)), "Invalid scope")
    return hashlib.sha256(json.dumps([worker, run, attempt], separators=(",", ":")).encode()).hexdigest()[:24]


def state_paths(row, temporary):
    temporary = Path(temporary)
    require(temporary.is_absolute() and str(temporary) == os.path.normpath(str(temporary))
            and temporary.is_relative_to("/actions/_work"), "Unexpected worker temporary namespace")
    scope = token(row["worker"], row["run"], row["attempt"])
    return {"state": temporary / ("vs-runtime-" + scope), "exec": Path("/run") / ("sonic-vs-runtime-" + scope)}


def no_links(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink in private state path")


def reviewed_observation(value):
    inspection.check_reviewed_requirements(value, MANAGER)
    require(value["vs_observations"]["marker"]["value"]["base_image"] == BASE_IMAGE,
            "Runner image differs from the reviewed K3s image")


def checked_helper(path, expected):
    require(path.is_file() and not path.is_symlink()
            and hashlib.sha256(path.read_bytes()).hexdigest() == expected, "Pinned helper bytes differ")
    return path


def kernel_command(root):
    helper = checked_helper(root / "tools/bazel/ci/check_kernel_sandbox.py", KERNEL_HELPER_SHA256)
    version = root / "tools/bazel/kernel/.bazelversion"
    require(version.is_file() and not version.is_symlink() and version.read_bytes() == b"8.5.1\n", "Kernel Bazel pin differs")
    return [sys.executable, "-B", str(helper)]


@contextmanager
def cleanup_window():
    """Do not interrupt child creation or bounded reaping with a second signal."""
    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    def deferred(signum, frame):
        return
    for sig in signals:
        # Caught handlers reset on exec; SIG_IGN would leak into child helpers.
        signal.signal(sig, deferred)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def group_alive(pid):
 try:os.killpg(pid,0);return True
 except ProcessLookupError:return False

def wait_group_gone(pid,seconds):
 deadline=time.monotonic()+seconds
 while group_alive(pid) and time.monotonic()<deadline:time.sleep(0.05)
 return not group_alive(pid)


def stop_owned_session(child):
    """Close the owned session even when its leader has already been reaped."""
    if group_alive(child.pid):
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    closed = wait_group_gone(child.pid, 5)
    if not closed:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        closed = wait_group_gone(child.pid, 5)
    require(child.poll() is not None and closed, "Owned helper process group did not close")


def root_lifeline_tick(deadline, descriptor=0):
    require(time.monotonic() < deadline, "Root supervisor deadline expired")
    try:
        value = os.read(descriptor, 1)
    except BlockingIOError:
        return
    require(value != b"", "Caller lifeline closed")
    raise ProbeFailure("Unexpected lifeline input")


def safe_failure(error):
    result = {"error_type": type(error).__name__}
    if isinstance(error, ProbeFailure):
        result["reason"] = str(error)
    for key in ("errno", "returncode", "timeout"):
        value = getattr(error, key, None)
        if type(value) in (int, float):
            result[key] = value
    if isinstance(error, SupervisorUnclosed):
        result["root_supervisor_reaped"] = False
    original = getattr(error, "_probe_original_failure", None)
    if isinstance(original, dict):
        result["original_failure"] = original
    return result


def diagnostics(state):
    """Publish fixed error classes and bounded-tail hashes, never raw tool text."""
    patterns = {
        "permission_denied": (b"permission denied",),
        "operation_not_permitted": (b"operation not permitted",),
        "no_space": (b"no space left",),
        "read_only_filesystem": (b"read-only file system",),
        "overlay_failure": (b"failed to mount overlay", b"error initializing graphdriver"),
        "cgroup_failure": (b"failed to create cgroup", b"cgroup mountpoint does not exist"),
        "address_in_use": (b"address already in use",),
        "invalid_argument": (b"invalid argument", b"unknown flag", b"unknown option"),
        "daemon_start_failure": (b"failed to start daemon",),
        "network_unreachable": (b"network is unreachable", b"connection refused"),
        "download_failure": (b"failed to download", b"error downloading"),
        "certificate_failure": (b"certificate verify failed", b"certificate signed by unknown authority"),
        "timed_out": (b"timed out", b"timeout"),
        "namespace_failure": (b"unshare failed", b"failed to create user namespace"),
        "lifeline_not_pipe": (b"lifeline must be a pipe",),
    }
    records = []
    names = ("kernel-sandbox.stdout", "kernel-sandbox.stderr", "private-cgroup.stdout",
             "private-cgroup.stderr", "dockerd.log", "private-docker.stdout", "private-docker.stderr")
    names += tuple(label + suffix for label in STOPPED_LOGS for suffix in (".stdout", ".stderr"))
    for name in names:
        path = state / name
        try:
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(max(0, size - 65536))
                tail = stream.read(65536)
            lower = tail.lower()
            records.append({"stage_log": name, "bytes": size, "tail_bytes": len(tail),
                            "tail_sha256": hashlib.sha256(tail).hexdigest(),
                            "known_errors": [key for key, needles in patterns.items() if any(x in lower for x in needles)],
                            "raw_text_published": False})
        except OSError as error:
            records.append({"stage_log": name, "read_error": safe_failure(error)})
    return records


def bounded(argv, state, label, seconds, environment=None, root_supervisor=False):
    """Root owns its deadline/cleanup; the worker holds only a pipe lifeline."""
    with (state / (label + ".stdout")).open("xb") as out, (state / (label + ".stderr")).open("xb") as err:
        with cleanup_window():
            child = subprocess.Popen(argv, stdin=subprocess.PIPE if root_supervisor else subprocess.DEVNULL,
                                     stdout=out, stderr=err, env=environment,
                                     start_new_session=True, close_fds=True)
        try:
            code = child.wait(timeout=seconds)
        except BaseException as failure:
            try:
                with cleanup_window():
                    if root_supervisor:
                        # Only this parent holds the write end. Parent death also causes EOF.
                        child.stdin.close()
                        try:
                            # An interrupted client, two owned-object removals and
                            # the daemon each retain their bounded cleanup budget.
                            child.wait(timeout=135)
                        except subprocess.TimeoutExpired as error:
                            raise SupervisorUnclosed("Root supervisor did not reap after lifeline closure") from error
                    else:
                        stop_owned_session(child)
            except BaseException as cleanup_error:
                cleanup_error._probe_original_failure = safe_failure(failure)
                raise
            raise
        finally:
            if root_supervisor and not child.stdin.closed:
                child.stdin.close()
    if not root_supervisor and not wait_group_gone(child.pid, 1):
        with cleanup_window():
            stop_owned_session(child)
        raise ProbeFailure("Helper leader exited with surviving descendants")
    if code != 0:
        error = ProbeFailure(label + " did not pass")
        error.returncode = code
        raise error
    require(all((state / (label + suffix)).stat().st_size <= CAP for suffix in (".stdout", ".stderr")),
            "Oversized helper diagnostics")
    return (state / (label + ".stdout")).read_bytes()


def daemon_arguments(executable, paths):
    state, execution = paths["state"], paths["exec"]
    return [executable, "--config-file", str(state / "daemon.json"),
            "--host", "unix://" + str(execution / "docker.sock"), "--group", "docker",
            "--data-root", str(state / "docker-data"), "--exec-root", str(execution / "exec"),
            "--pidfile", str(execution / "dockerd.pid"), "--storage-driver", "overlay2",
            "--exec-opt", "native.cgroupdriver=cgroupfs", "--bridge=none", "--iptables=false",
            "--ip6tables=false", "--ip-forward=false", "--ip-masq=false"]


def stop_owned_daemon(child):
    """Popen retains ownership until wait; never signal a PID read from a file."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    return child.poll() is not None


def docker_information(executable, environment):
    template = '{"root":{{json .DockerRootDir}},"driver":{{json .Driver}},"version":{{json .ServerVersion}}}'
    with cleanup_window():
        child = subprocess.Popen([executable, "info", "--format", template], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
                                 user=1000, group=1000, extra_groups=[grp.getgrnam("docker").gr_gid],
                                 start_new_session=True, close_fds=True)
    try:
        output, unused = child.communicate(timeout=3)
    except BaseException as failure:
        try:
            with cleanup_window():
                stop_owned_session(child)
        except BaseException as cleanup_error:
            cleanup_error._probe_original_failure = safe_failure(failure)
            raise
        raise
    if not wait_group_gone(child.pid, 1):
        with cleanup_window():
            stop_owned_session(child)
        raise ProbeFailure("Docker client left surviving descendants")
    require(len(output) <= 65536, "Oversized Docker response")
    return json.loads(output) if child.returncode == 0 else None


def validate_daemon(observed, paths, owned_pid, pid_text):
    require(pid_text.strip() == str(owned_pid), "Daemon PID differs from our child")
    require(observed["root"] == str(paths["state"] / "docker-data") and observed["driver"] == "overlay2",
            "Unexpected private Docker root or storage driver")
    require(isinstance(observed["version"], str) and re.fullmatch(r"[0-9][0-9A-Za-z.+~_-]{0,63}", observed["version"]),
            "Unexpected daemon version")
    return observed["version"]


def literal_rootfs():
    """One nonexecutable file; no base image, command or project build."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        member = tarfile.TarInfo(LITERAL_PATH)
        member.size, member.mode, member.mtime = len(LITERAL), 0o444, 0
        member.uid = member.gid = 0
        archive.addfile(member, io.BytesIO(LITERAL))
    return output.getvalue()


def docker_operation(executable, arguments, environment, state, label, payload=None):
    """Bound each nonroot client and close its session before daemon cleanup."""
    require(label in STOPPED_LOGS and (payload is None or len(payload) <= 16384),
            "Unexpected stopped-container operation")
    child = None
    with (state / (label + ".stdout")).open("xb") as out, (state / (label + ".stderr")).open("xb") as err:
        try:
            with cleanup_window():
                child = subprocess.Popen([executable, *arguments],
                                         stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                         stdout=out, stderr=err, env=environment, user=1000, group=1000,
                                         extra_groups=[grp.getgrnam("docker").gr_gid],
                                         start_new_session=True, close_fds=True)
            child.communicate(input=payload, timeout=10)
        except BaseException as failure:
            if child is not None:
                try:
                    with cleanup_window():
                        stop_owned_session(child)
                except BaseException as cleanup_error:
                    cleanup_error._probe_original_failure = safe_failure(failure)
                    raise
            raise
    if not wait_group_gone(child.pid, 1):
        with cleanup_window():
            stop_owned_session(child)
        raise ProbeFailure("Stopped-container client left surviving descendants")
    if child.returncode != 0:
        error = ProbeFailure(label + " did not pass")
        error.returncode = child.returncode
        raise error
    require(all((state / (label + suffix)).stat().st_size <= CAP for suffix in (".stdout", ".stderr")),
            "Oversized stopped-container response")
    return (state / (label + ".stdout")).read_bytes()


def stopped_identity(data, name, container, image, result):
    observed = json.loads(data)
    state = observed["State"]
    started = state.get("Running") is not False or state.get("Pid") != 0 or state.get("StartedAt") != "0001-01-01T00:00:00Z"
    result["ever_started_observed"] |= started
    require(observed["Id"] == container and observed["Name"] == "/" + name
            and observed["Image"] == image and observed["Config"]["Cmd"] == [NEVER_RUN],
            "Stopped-container identity differs")
    require(not started and state["Status"] == "created", "Probe container did not remain never started")
    mode = observed["HostConfig"]["NetworkMode"]
    require(isinstance(mode, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mode),
            "Unexpected default network mode")
    result["reported_network_mode"] = mode


def literal_archive(data):
    require(len(data) <= 65536, "Oversized stopped-container archive")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        require(len(members) == 1 and members[0].name == LITERAL_PATH and members[0].isfile()
                and members[0].size == len(LITERAL), "Unexpected stopped-container archive member")
        stream = archive.extractfile(members[0])
        require(stream is not None and stream.read(len(LITERAL) + 1) == LITERAL,
                "Stopped-container literal differs")


def stopped_container(executable, environment, state):
    """Exercise only import/create/archive/remove on our fresh private daemon."""
    require(re.fullmatch(r"vs-runtime-[0-9a-f]{24}", state.name), "Unexpected stopped-container scope")
    scope = state.name.removeprefix("vs-runtime-")
    name, tag = "sonic-vs-stopped-" + scope, "sonic-vs-stopped:" + scope
    image, container = tag, name
    imported = attempted_create = False
    result = {"status": "failed", "container_start_requested": False, "ever_started_observed": False,
              "default_network_requested": True, "network_workload_executed": False,
              "owned_container_removed": None, "owned_image_removed": None}
    def operation(arguments, label, payload=None):
        return docker_operation(executable, arguments, environment, state, label, payload)
    try:
        payload = literal_rootfs()
        result["literal_rootfs_sha256"] = hashlib.sha256(payload).hexdigest()
        result["stage"] = "import"
        imported = True  # Even an interrupted reply can follow a completed import.
        value = operation(["import", "-", tag], "stopped-import", payload).decode().strip()
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", value), "Unexpected imported image identity")
        image = value
        result["image"] = image
        result["stage"] = "create"
        attempted_create = True  # Reserve our unique name before the possible side effect.
        value = operation(["create", "--pull=never", "--name", name, image, NEVER_RUN], "stopped-create").decode().strip()
        require(re.fullmatch(r"[0-9a-f]{64}", value), "Unexpected stopped-container identity")
        container = value
        result["container"] = container
        result["stage"] = "inspect-before"
        stopped_identity(operation(["inspect", "--format", "{{json .}}", container], "stopped-inspect-before"),
                         name, container, image, result)
        result["stage"] = "archive"
        data = operation(["cp", container + ":/" + LITERAL_PATH, "-"], "stopped-archive")
        literal_archive(data)
        result["literal_sha256"] = hashlib.sha256(LITERAL).hexdigest()
        result["stage"] = "inspect-after"
        stopped_identity(operation(["inspect", "--format", "{{json .}}", container], "stopped-inspect-after"),
                         name, container, image, result)
        result["status"] = "passed"
    except BaseException as error:
        result["failure"] = safe_failure(error)
    finally:
        # Parent root supervisor still owns the daemon. Mask its alarm only for
        # these bounded cleanup clients, then let its existing finally reap it.
        with cleanup_window():
            for attempted, arguments, label, key in (
                (attempted_create, ["rm", "--force", container], "stopped-remove-container", "owned_container_removed"),
                (imported, ["image", "rm", "--force", image], "stopped-remove-image", "owned_image_removed"),
            ):
                if attempted:
                    try:
                        operation(arguments, label)
                        result[key] = True
                    except BaseException as error:
                        result[key] = False
                        result["status"] = "failed"
                        result.setdefault("cleanup_failures", []).append(dict(operation=label, **safe_failure(error)))
    return result


def private_daemon(paths):
    """Root supervisor owns exactly one foreground daemon and always waits for it."""
    require(os.geteuid() == 0 and Path("/.dockerenv").is_file(), "Private daemon requires its disposable slave")
    require(inspection.marker() == {"base_image": BASE_IMAGE, "manager_revision": MANAGER}, "Runner marker differs")
    state, execution = paths["state"], paths["exec"]
    for path in (state, execution):
        no_links(path)
    info = state.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 1000
            and stat.S_IMODE(info.st_mode) == 0o700, "Private scope ownership differs")
    require(not execution.exists() and not execution.is_symlink(), "Preserve existing daemon scope")
    helper = checked_helper(ROOT / "tools/bazel/ci/prepare_nested_docker.py", NESTED_HELPER_SHA256)
    daemon = shutil.which("dockerd", path=PATH)
    docker = shutil.which("docker", path=PATH)
    require(daemon and docker, "Missing reviewed Docker tools")
    for name in ("daemon.json", "docker-data", "client", "dockerd.log"):
        require(not (state / name).exists() and not (state / name).is_symlink(), "Preserve existing private daemon state")
    result = {"status": "failed", "daemon_started": False, "daemon_closed": True,
              "container_workload_executed": False, "networking_exercised": False,
              "daemon_network_mutation_disabled": True, "data_lifetime": "current_worker_run_attempt"}
    child = None
    def interrupted(signum, frame):
        raise ProbeFailure("Private runtime probe interrupted")
    require(stat.S_ISFIFO(os.fstat(0).st_mode), "Root supervisor lifeline must be a pipe")
    os.set_blocking(0, False)
    deadline = time.monotonic() + 160
    previous = {s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT)}
    previous[signal.SIGALRM] = signal.signal(signal.SIGALRM, lambda signum, frame: root_lifeline_tick(deadline))
    signal.setitimer(signal.ITIMER_REAL, 1, 1)
    try:
        root_lifeline_tick(deadline)
        result["stage"] = "private-cgroup"
        bounded([sys.executable, "-I", "-S", "-B", str(helper)], state, "private-cgroup", 30)
        execution.mkdir(mode=0o700)
        os.chown(execution, 1000, 1000)
        (state / "docker-data").mkdir(mode=0o711)
        (state / "client").mkdir(mode=0o700)
        os.chown(state / "client", 1000, 1000)
        with (state / "daemon.json").open("x") as stream:
            stream.write("{}\n")
        with (state / "dockerd.log").open("xb") as log:
            result["stage"] = "daemon-start"
            with cleanup_window():
                child = subprocess.Popen(daemon_arguments(daemon, paths), stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, env={"PATH": PATH, "HOME": "/root"},
                                         start_new_session=True, close_fds=True)
            result["daemon_started"] = True
            result["daemon_closed"] = False
            client_env = {"PATH": PATH, "HOME": str(state / "client"),
                          "DOCKER_CONFIG": str(state / "client"),
                          "DOCKER_HOST": "unix://" + str(execution / "docker.sock")}
            readiness_deadline = time.monotonic() + 120
            result["stage"] = "daemon-readiness"
            observed = None
            while time.monotonic() < readiness_deadline and child.poll() is None:
                try:
                    observed = docker_information(docker, client_env)
                except (subprocess.TimeoutExpired, json.JSONDecodeError):
                    observed = None
                if observed is not None:
                    break
                time.sleep(1)
            require(observed is not None, "Private Docker did not become ready")
            version = validate_daemon(observed, paths, child.pid, (execution / "dockerd.pid").read_text())
            result["stage"] = "stopped-container"
            result["stopped_container"] = stopped_container(docker, client_env, state)
            result["container_workload_executed"] = result["stopped_container"]["ever_started_observed"]
            require(result["stopped_container"]["status"] == "passed", "Stopped-container qualification or cleanup failed")
            result.update(status="passed", driver="overlay2", exact_private_root=True,
                          nonroot_client=True, server_version=version)
    except BaseException as error:
        result.update(status="failed", failure=safe_failure(error))
    finally:
        # Disarm before cleanup; another alarm cannot interrupt exact-child reaping.
        signal.setitimer(signal.ITIMER_REAL, 0)
        with cleanup_window():
            if child is not None:
                try:
                    result["daemon_closed"] = stop_owned_daemon(child)
                    result["daemon_exit_code"] = child.returncode
                    require(result["daemon_closed"] and child.returncode == 0, "Private daemon did not close cleanly")
                except BaseException as error:
                    result.update(status="failed", cleanup_failure=safe_failure(error))
                    result["daemon_closed"] = child.returncode is not None
            result["diagnostics"] = diagnostics(state)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return result


def run_probe(worker, output):
    require(os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 1000, "Probe must start as the worker user")
    row = identity(worker, os.environ)
    require(not output.exists() and not output.is_symlink(), "Preserve existing runtime receipt")
    paths = state_paths(row, os.environ["RUNNER_TEMP"])
    no_links(paths["state"])
    require(not paths["state"].exists() and not paths["exec"].exists() and not paths["exec"].is_symlink(), "Preserve existing runtime scope")
    value = dict(schema=1, status="failed", scope="current_worker_runtime_capabilities_only", **row)
    value.update(run=int(row["run"]), attempt=int(row["attempt"]))
    value.update(build_or_package_generation=False, host_kernel_configuration_changed=False,
                 container_workload_executed=False, full_VS_or_kernel_package_acceptance=False)
    def interrupted(signum, frame):
        raise ProbeFailure("Runtime probe interrupted")
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        value["stage"] = "canonical-resource-inspection"
        observed = inspection.collect(worker)
        value["prerequisites"] = observed
        reviewed_observation(observed)
        paths["state"].mkdir(mode=0o700)
        (paths["state"] / "tmp").mkdir(mode=0o700)
        cache = Path("/actions/_work/.source-image-cache/source-images") / worker / "bazelisk"
        no_links(cache)
        cache.mkdir(mode=0o700, exist_ok=True)
        info = cache.stat()
        require(info.st_uid == info.st_gid == 1000 and stat.S_IMODE(info.st_mode) == 0o700, "Bazelisk cache ownership differs")
        environment = dict(os.environ, BAZEL="bazel", BAZELISK_HOME=str(cache), TMPDIR=str(paths["state"] / "tmp"))
        value["stage"] = "kernel-sandbox"
        bounded(kernel_command(ROOT), paths["state"], "kernel-sandbox", 300, environment)
        value["kernel_sandbox"] = {"status": "passed", "bazel": "8.5.1", "helper_sha256": KERNEL_HELPER_SHA256,
                                    "empty_workspace": True, "project_build_executed": False}
        value["stage"] = "private-docker"
        data = bounded(["sudo", "-n", sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                        "--docker-only", "--worker", worker, "--run", row["run"], "--attempt", row["attempt"],
                        "--temporary", os.environ["RUNNER_TEMP"]], paths["state"], "private-docker", 230, root_supervisor=True)
        value["private_docker"] = json.loads(data)
        require(value["private_docker"]["status"] == "passed" and value["private_docker"]["daemon_started"] is True
                and value["private_docker"]["daemon_closed"] is True, "Private Docker qualification or closure failed")
        value["status"] = "passed"
    except BaseException as error:
        value["failure"] = safe_failure(error)
    finally:
        value["diagnostics"] = diagnostics(paths["state"])
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    with output.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return value


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--docker-only", action="store_true")
    parser.add_argument("--run")
    parser.add_argument("--attempt")
    parser.add_argument("--temporary")
    args = parser.parse_args()
    if args.docker_only:
        require(args.output is None and args.run and args.attempt and args.temporary, "Incomplete private daemon scope")
        paths = state_paths({"worker": args.worker, "run": args.run, "attempt": args.attempt}, args.temporary)
        value = private_daemon(paths)
        print(json.dumps(value, sort_keys=True))
        return 0  # The caller retains the failed result as well as successful closure.
    require(args.output is not None and not any((args.run, args.attempt, args.temporary)), "Unexpected probe arguments")
    value = run_probe(args.worker, args.output)
    print(value["status"])
    return 0 if value["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
