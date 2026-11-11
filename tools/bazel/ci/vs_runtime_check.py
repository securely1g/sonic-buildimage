#!/usr/bin/env python3
"""Qualify sandbox, stopped/running containers and exact native581 stop without Make."""
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
import struct
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
    state = temporary / ("vs-runtime-" + scope)
    return {"state": state, "exec": Path("/run") / ("sonic-vs-runtime-" + scope),
            "docker_data": state / ("source-image-docker-" + row["run"] + "-" + row["attempt"]),
            "identity": {k: row[k] for k in ("worker", "run", "attempt")}}


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


def traceback_diagnostics(tail, stage_log, leading_fragment=False):
    """Report only bounded traceback facts; command repr and free text are opaque."""
    native_logs = ("native-capture-docker-mount-before.stderr",
                   "native-capture-docker-mount-ready.stderr", "native-stop.stderr")
    before_reasons = (
        "Expected elevated fixed mount action", "Unexpected owned mount scope",
        "Mount table identity/bound", "Mount table exceeds bound", "Invalid mount row",
        "Invalid mount path", "Invalid namespace mountpoint", "Invalid filesystem mount root",
        "Parent components in non-cgroup mount root", "Selected mount root is not canonical",
        "Owned daemon signaling requires pidfd support",
        "Required fixed Docker/umount executable is unavailable",
        "Docker root must be absent before launch", "Private mount directory identity",
        "Missing backing mount", "Ambiguous backing mount", "Private mount proof size",
        "Duplicate private mount proof field",
    )
    types = ("AssertionError", "FileExistsError", "FileNotFoundError", "NotADirectoryError",
             "PermissionError", "OSError", "ValueError", "KeyError", "TypeError",
             "RuntimeError", "TimeoutError", "TimeoutExpired", "JSONDecodeError",
             "UnicodeDecodeError", "IndexError", "AttributeError", "OverflowError")
    errno_categories = {1: "operation_not_permitted", 13: "permission_denied",
                        28: "no_space", 30: "read_only_filesystem", 98: "address_in_use",
                        101: "network_unreachable", 111: "network_unreachable"}
    result = {"exception_facts": [], "source_frames": [], "authored_guard_reasons": [],
              "known_errors": [], "records_truncated": False}

    def add(key, value):
        if value not in result[key]:
            if len(result[key]) < 12:
                result[key].append(value)
            else:
                result["records_truncated"] = True

    lines = tail.splitlines()
    if leading_fragment and lines:
        lines = lines[1:]
    in_traceback = False
    for line in lines:
        if line == b"Traceback (most recent call last):":
            in_traceback = True
            continue
        if not in_traceback:
            continue
        if stage_log in native_logs and len(line) <= 2048:
            frame = re.fullmatch(
                rb'  File "(<string>|[^"\r\n]*/source_image_worker\.py)", line ([1-9][0-9]{0,5})(?:, in (?:[A-Za-z_][A-Za-z0-9_]*|<module>))?', line)
            if frame:
                add("source_frames", {"source": "native_embedded_python" if frame[1] == b"<string>" else "native_worker",
                                      "line": int(frame[2])})
                continue
        # Python source/caret context is indented; never interpret it as an exception.
        if not line or line[:1] in (b" ", b"\t"):
            continue
        in_traceback = False
        # Only the anchored class/errno prefix is inspected, even for a huge repr.
        match = re.match(rb'(?:(?:builtins|subprocess|json\.decoder)\.)?([A-Za-z]+)(?::(?: |$)|$)', line[:128])
        if not match:
            continue
        kind = match[1].decode("ascii")
        if kind not in types:
            continue  # Includes CalledProcessError: its command repr is not evidence.
        fact = {"error_type": kind}
        message = line[match.end():]
        error_number = re.match(rb'\[Errno ([1-9][0-9]{0,3})\](?: |$)', message[:32])
        if error_number:
            number = int(error_number[1])
            fact["errno"] = number
            if number in errno_categories:
                add("known_errors", errno_categories[number])
        if kind in ("TimeoutError", "TimeoutExpired"):
            add("known_errors", "timed_out")
        add("exception_facts", fact)
        if stage_log == "native-capture-docker-mount-before.stderr" and kind == "ValueError":
            for reason in before_reasons:
                if message == reason.encode("ascii"):
                    add("authored_guard_reasons", reason)
    return result


def diagnostics(state):
    """Publish safe traceback facts and bounded-tail hashes, never raw tool text."""
    records = []
    names = ("kernel-sandbox.stdout", "kernel-sandbox.stderr", "private-cgroup.stdout",
             "private-cgroup.stderr", "dockerd.log", "private-docker.stdout", "private-docker.stderr")
    names += tuple(label + suffix for label in STOPPED_LOGS + RUNNING_LOGS + ("native-stop", "native-capture-docker-mount-before", "native-capture-docker-mount-ready", "fixture-program", "fixture-libc", "fixture-loader") for suffix in (".stdout", ".stderr"))
    for name in names:
        path = state / name
        try:
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(max(0, size - 65536))
                tail = stream.read(65536)
            records.append({"stage_log": name, "bytes": size, "tail_bytes": len(tail),
                            "tail_sha256": hashlib.sha256(tail).hexdigest(),
                            **traceback_diagnostics(tail, name, leading_fragment=size > len(tail)),
                            "raw_text_published": False})
        except OSError as error:
            records.append({"stage_log": name, "read_error": safe_failure(error)})
    return records


def bounded(argv, state, label, seconds, environment=None, root_supervisor=False,
            pass_fds=(), worker_user=False):
    """Root owns its deadline/cleanup; the worker holds only a pipe lifeline."""
    with (state / (label + ".stdout")).open("xb") as out, (state / (label + ".stderr")).open("xb") as err:
        with cleanup_window():
            child = subprocess.Popen(argv, stdin=subprocess.PIPE if root_supervisor else subprocess.DEVNULL,
                                     stdout=out, stderr=err, env=environment,
                                     start_new_session=True, close_fds=True, pass_fds=pass_fds,
                                     **({"user": 1000, "group": 1000,
                                         "extra_groups": [grp.getgrnam("docker").gr_gid]} if worker_user else {}))
        try:
            code = child.wait(timeout=seconds)
        except BaseException as failure:
            try:
                with cleanup_window():
                    if root_supervisor:
                        # Only this parent holds the write end. Parent death also causes EOF.
                        child.stdin.close()
                        try:
                            # Interrupted fixture clients/removals, native stop (190s),
                            # and an owned-daemon fallback retain bounded cleanup.
                            child.wait(timeout=ROOT_CLEANUP_SECONDS)
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
            "--data-root=" + str(paths["docker_data"]), "--exec-root", str(execution / "exec"),
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
    require(observed["root"] == str(paths["docker_data"]) and observed["driver"] == "overlay2",
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
    limit = 2 * ELF_TOTAL_CAP if label == "running-import" else 16384
    require(label in STOPPED_LOGS + RUNNING_LOGS and (payload is None or len(payload) <= limit),
            "Unexpected private-container operation")
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


NATIVE_CONTROLLER = "ba575106ea25c3a5f14a428b0326b6a3c891b5ef"
NATIVE_WORKER_SHA256 = "6c491e3daf8cdbdcc64257703742763040d986ed5d69a89cdec5e3366263b26d"
NATIVE_CAPTURE_SECONDS = 40
NATIVE_STOP_SECONDS = 240
ROOT_BODY_SECONDS = 240
ROOT_CLEANUP_SECONDS = 420
ROOT_COMMAND_SECONDS = 660
ELF_FILE_CAP = 8 * 1024 * 1024
ELF_TOTAL_CAP = 16 * 1024 * 1024
INTERPRETER = "/lib64/ld-linux-x86-64.so.2"
SYSTEM_ELFS = (
    ("program", "/usr/bin/sleep", "usr/bin/sleep"),
    ("libc", "/usr/lib/x86_64-linux-gnu/libc.so.6", "lib/x86_64-linux-gnu/libc.so.6"),
    ("loader", "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2", "lib64/ld-linux-x86-64.so.2"),
)
RUNNING_LOGS = ("running-import", "running-create", "running-start", "running-inspect",
                "running-stop", "running-inspect-stopped", "running-remove-container",
                "running-remove-image")


def elf_identity(data, dynamic, role):
    """Admit only this installed AMD64 executable's closed glibc/loader graph."""
    require(64 <= len(data) <= ELF_FILE_CAP and data[:7] == b"\x7fELF\x02\x01\x01" and data[7] in (0, 3),
            "Fixture is not bounded little-endian ELF64")
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    kind, machine, version, entry, offset = header[:5]
    require(kind in (2, 3) and machine == 62 and version == 1 and header[7] == 64
            and header[8] == 56 and 0 < header[9] <= 128
            and 64 <= offset <= len(data) - 56 * header[9], "Unsupported AMD64 ELF header")
    interpreters, executable_entry = [], False
    for index in range(header[9]):
        typ, flags, start, address, unused, size, memory, alignment = struct.unpack_from(
            "<IIQQQQQQ", data, offset + 56 * index)
        require(start <= len(data) and size <= len(data) - start, "ELF segment exceeds fixture")
        if typ == 1:
            require(size <= memory, "Invalid ELF load segment")
            executable_entry |= bool(flags & 1 and address <= entry < address + memory)
        if typ == 3:
            raw = data[start:start + size]
            require(1 < len(raw) <= 128 and raw.endswith(b"\0") and b"\0" not in raw[:-1],
                    "Invalid ELF interpreter")
            interpreters.append(raw[:-1].decode("ascii"))
    require(len(interpreters) <= 1 and executable_entry, "Invalid ELF entry or interpreter count")
    interpreter = interpreters[0] if interpreters else None
    require(interpreter in ((INTERPRETER,) if role == "program" else
                            (None, INTERPRETER) if role == "libc" else (None,)),
            "Unexpected ELF interpreter")
    text = dynamic.decode("ascii")
    forbidden = ("RPATH", "RUNPATH", "AUDIT", "DEPAUDIT", "CONFIG", "FILTER", "AUXILIARY")
    require(not any("(" + tag + ")" in text for tag in forbidden), "Indirect ELF loader input refused")
    values = {}
    for tag, description in (("NEEDED", "Shared library"), ("SONAME", "Library soname")):
        rows = [line for line in text.splitlines() if "(" + tag + ")" in line]
        matches = [re.search(r"\(" + tag + r"\)\s+" + description +
                             r": \[([A-Za-z0-9_.+-]+)\]\s*$", line) for line in rows]
        require(all(matches), "Unrecognized ELF dynamic dependency")
        values[tag] = [match[1] for match in matches]
    expected = {"program": (["libc.so.6"], []),
                "libc": (["ld-linux-x86-64.so.2"], ["libc.so.6"]),
                "loader": ([], ["ld-linux-x86-64.so.2"])}
    require(role in expected and (values["NEEDED"], values["SONAME"]) == expected[role],
            "Installed executable dependency closure differs")
    return {"class": 64, "machine": "AMD64", "interpreter": interpreter,
            "needed": values["NEEDED"], "soname": values["SONAME"]}


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_system_elf(path, role, state):
    """Read three named root-owned files; never execute their bytes on the host."""
    path = Path(path)
    require(str(path) in {source for unused, source, target in SYSTEM_ELFS},
            "Unexpected installed fixture path")
    no_links(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0
                and not before.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
                and 0 < before.st_size <= ELF_FILE_CAP, "Unsafe installed fixture file")
        data = bytearray()
        deadline = time.monotonic() + 10
        while len(data) <= ELF_FILE_CAP:
            require(time.monotonic() < deadline, "Installed fixture read timed out")
            chunk = os.read(descriptor, min(65536, ELF_FILE_CAP + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        require(len(data) == before.st_size, "Installed fixture size changed")
        dynamic = bounded(["/usr/bin/readelf", "--wide", "--dynamic", "/proc/self/fd/" + str(descriptor)],
                          state, "fixture-" + role, 10, {"PATH": PATH, "LC_ALL": "C"},
                          pass_fds=(descriptor,))
        no_links(path)
        require(file_identity(os.fstat(descriptor)) == file_identity(before)
                and file_identity(path.lstat()) == file_identity(before), "Installed fixture changed during capture")
        value = bytes(data)
        identity = elf_identity(value, dynamic, role)
        return value, dict(identity, source_path=str(path), bytes=len(value),
                           sha256=hashlib.sha256(value).hexdigest(), stable_root_owned_file=True)
    finally:
        os.close(descriptor)


def executable_rootfs(state):
    """Deterministic TAR of existing installed bytes, with no package operation."""
    entries, evidence, total = {}, [], 0
    for role, source, target in SYSTEM_ELFS:
        data, record = read_system_elf(source, role, state)
        total += len(data)
        require(total <= ELF_TOTAL_CAP, "Installed fixture closure exceeds bound")
        entries[target] = data
        evidence.append(dict(record, role=role, archive_path=target))
    # libc's direct loader NEEDED name is available at the standard library path too.
    entries["lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"] = entries["lib64/ld-linux-x86-64.so.2"]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        directories = sorted({str(parent) for name in entries for parent in Path(name).parents
                              if str(parent) != "."})
        for name in directories:
            member = tarfile.TarInfo(name)
            member.type, member.mode, member.mtime = tarfile.DIRTYPE, 0o755, 0
            member.uid = member.gid = 0
            archive.addfile(member)
        for name, data in sorted(entries.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), 0o555, 0
            member.uid = member.gid = 0
            archive.addfile(member, io.BytesIO(data))
    payload = output.getvalue()
    require(len(payload) <= 2 * ELF_TOTAL_CAP, "Executable fixture TAR exceeds bound")
    return payload, {"files": evidence, "tar_sha256": hashlib.sha256(payload).hexdigest(),
                     "tar_bytes": len(payload), "origin": "current_authenticated_worker_installed_files",
                     "compiled": False, "downloaded": False, "package_generated": False}


def running_identity(data, name, container, image, stopped=False):
    row = json.loads(data)
    require(row["Id"] == container and row["Image"] == image and row["Name"] == "/" + name
            and row["Config"]["Cmd"] == ["/usr/bin/sleep", "60"]
            and row["Config"]["User"] == "65534:65534"
            and row["HostConfig"]["NetworkMode"] == "none"
            and row["HostConfig"]["ReadonlyRootfs"] is True
            and not row["HostConfig"].get("Privileged")
            and not row["HostConfig"].get("Binds")
            and row["HostConfig"]["CapDrop"] == ["ALL"]
            and row["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]
            and row["HostConfig"]["PidsLimit"] == 8
            and row["HostConfig"]["Memory"] == 32 * 1024**2
            and row["HostConfig"]["NanoCpus"] == 250000000
            and row["Config"].get("Entrypoint") in (None, [])
            and not any(x.startswith("LD_") for x in row["Config"].get("Env", [])), "Running fixture identity or isolation differs")
    state = row["State"]
    require(state.get("OOMKilled") is False and state.get("Dead") is False
            and state.get("Error") == "" and state.get("Paused") is False
            and state.get("Restarting") is False
            and isinstance(state.get("StartedAt"), str)
            and not state["StartedAt"].startswith("0001-"), "Running fixture lifecycle differs")
    if stopped:
        require(state["Status"] == "exited" and state["Running"] is False and state["Pid"] == 0
                and type(state["ExitCode"]) is int and state["ExitCode"] in (143, 137)
                and not state["FinishedAt"].startswith("0001-"),
                "Fixture did not stop within Docker stop policy")
        return state["ExitCode"]
    else:
        require(state["Status"] == "running" and state["Running"] is True
                and type(state["Pid"]) is int and state["Pid"] > 0, "Fixture was not observed running")


def running_container(executable, environment, state):
    require(re.fullmatch(r"vs-runtime-[0-9a-f]{24}", state.name), "Unexpected running-container scope")
    name = "sonic-vs-running-" + state.name.removeprefix("vs-runtime-")
    tag = name + ":fixture"
    container, image, imported, created = name, tag, False, False
    result = {"status": "failed", "running_observed": False, "normal_stop_verified": False,
              "network_mode": "none", "owned_container_removed": None, "owned_image_removed": None}
    def operation(arguments, label, payload=None):
        return docker_operation(executable, arguments, environment, state, label, payload)
    try:
        payload, result["fixture"] = executable_rootfs(state)
        imported = True
        value = operation(["import", "-", tag], "running-import", payload).decode().strip()
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", value), "Unexpected executable image identity")
        image = value
        created = True
        value = operation(["create", "--pull=never", "--name", name, "--network=none",
                               "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                               "--pids-limit=8", "--memory=32m", "--cpus=0.25", "--user=65534:65534",
                               "--env=LC_ALL=C", image, "/usr/bin/sleep", "60"], "running-create").decode().strip()
        require(re.fullmatch(r"[0-9a-f]{64}", value), "Unexpected running-container identity")
        container = value
        require(operation(["start", container], "running-start").decode().strip() == container,
                "Running fixture start response differs")
        running_identity(operation(["inspect", "--format", "{{json .}}", container], "running-inspect"),
                         name, container, image)
        result["running_observed"] = True
        require(operation(["stop", "--timeout", "5", container], "running-stop").decode().strip() == container,
                "Running fixture stop response differs")
        result["exit_code"] = running_identity(operation(["inspect", "--format", "{{json .}}", container], "running-inspect-stopped"),
                         name, container, image, stopped=True)
        result["normal_stop_verified"] = True
        result["stop_policy"] = "Docker stop: TERM then bounded KILL after 5 seconds"
        result["graceful_TERM_claimed"] = result["exit_code"] == 143
        result["status"] = "passed"
    except BaseException as error:
        result["failure"] = safe_failure(error)
    finally:
        with cleanup_window():
            for attempted, arguments, label, key in (
                (created, ["rm", "--force", container], "running-remove-container", "owned_container_removed"),
                (imported, ["image", "rm", "--force", image], "running-remove-image", "owned_image_removed"),
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


def native_stop_source():
    root = ROOT.with_name(ROOT.name + "-native-cleanup")
    no_links(root)
    require(root.is_dir(), "Missing separately pinned native controller")
    checked_helper(root / "tools/bazel/ci/source_image_worker.py", NATIVE_WORKER_SHA256)
    return root


def native_mount_diagnostics(state):
    """Retain only the exact public-safe mount schema emitted by native581."""
    path = state / "native-stop.stderr"
    try:
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= CAP,
                "Unavailable native stop diagnostics")
        records = []
        for line in path.read_text().splitlines():
            if not line.startswith("{"):
                continue
            def unique(pairs):
                row = dict(pairs)
                require(len(row) == len(pairs), "Duplicate diagnostic key")
                return row
            row = json.loads(line, object_pairs_hook=unique)
            if row.get("phase") != "private_docker_mount_teardown":
                continue
            require(set(row) == {"status", "phase", "wait_limit_seconds", "mount_count",
                                 "reported_mounts", "truncated"}
                    and row["status"] == "failed" and type(row["wait_limit_seconds"]) is int
                    and row["wait_limit_seconds"] == 30 and type(row["mount_count"]) is int
                    and row["mount_count"] > 0 and type(row["truncated"]) is bool
                    and row["truncated"] == (row["mount_count"] > 16)
                    and isinstance(row["reported_mounts"], list)
                    and len(row["reported_mounts"]) == min(16, row["mount_count"]),
                    "Unrecognized native mount summary")
            for mount in row["reported_mounts"]:
                require(set(mount) == {"mount_id", "parent_id", "filesystem", "location",
                                       "relative_depth", "relative_path_sha256"}
                        and all(type(mount[key]) is int and mount[key] >= 0
                                for key in ("mount_id", "parent_id", "relative_depth"))
                        and mount["filesystem"] in ("overlay", "ext4", "xfs", "tmpfs", "proc",
                                                     "sysfs", "cgroup2", "other")
                        and mount["location"] in ("root", "overlay2", "containers", "containerd",
                                                 "volumes", "image", "buildkit", "other")
                        and re.fullmatch(r"[0-9a-f]{64}", mount["relative_path_sha256"]),
                        "Unrecognized native mount row")
            records.append(row)
        return records[0] if len(records) == 1 else None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


@contextmanager
def reap_owned_daemon(child):
    """Native stop waits for /proc/PID disappearance; reap our foreground child."""
    previous = signal.getsignal(signal.SIGCHLD)
    def reap(signum=None, frame=None):
        child.poll()
    signal.signal(signal.SIGCHLD, reap)
    try:
        reap()
        yield
    finally:
        reap()
        signal.signal(signal.SIGCHLD, previous)


def native_control(paths, source):
    """Create only the real private-daemon control state before its first capture."""
    state, execution = paths["state"], paths["exec"]
    row = paths["identity"]
    require(paths["docker_data"].parent == state and paths["docker_data"].name ==
            "source-image-docker-" + row["run"] + "-" + row["attempt"], "Native private root scope differs")
    require(not paths["docker_data"].exists() and not paths["docker_data"].is_symlink(),
            "Native capture requires an absent private data root")
    control = state / "native-stop-control"
    control.mkdir(mode=0o700)
    os.chown(control, 1000, 1000)
    private = {"image": "gearbox", "revision": NATIVE_CONTROLLER, "workspace": str(source),
               "pidfile": str(execution / "dockerd.pid"), "docker_root": str(paths["docker_data"]),
               "environment": {"DOCKER_HOST": "unix://" + str(execution / "docker.sock"),
                               "DOCKER_CONFIG": str(state / "client")}}
    with (control / "worker-private.json").open("x") as stream:
        json.dump(private, stream, sort_keys=True)
    os.chown(control / "worker-private.json", 1000, 1000)


def native_action(action, paths, source, seconds):
    require(action in ("capture-docker-mount-before", "capture-docker-mount-ready", "stop"),
            "Unknown native private-daemon action")
    state, row = paths["state"], paths["identity"]
    env = {"PATH": PATH, "HOME": str(state / "client"), "PYTHONNOUSERSITE": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "RUNNER_TEMP": str(state),
           "RUNNER_NAME": row["worker"], "WORKER_POOL": inspection.POOL,
           "GITHUB_RUN_ID": row["run"], "GITHUB_RUN_ATTEMPT": row["attempt"]}
    command = [sys.executable, "-B", str(source / "tools/bazel/ci/source_image_worker.py"),
               action, "--image", "gearbox", "--revision", NATIVE_CONTROLLER,
               "--workspace", str(source), "--artifacts", str(state / "native-stop-control")]
    bounded(command, state, "native-" + action, seconds, env, worker_user=True)


def native_capture(stage, paths, source):
    require(stage in ("before", "ready"), "Unknown native capture stage")
    if stage == "before":
        native_control(paths, source)
    native_action("capture-docker-mount-" + stage, paths, source, NATIVE_CAPTURE_SECONDS)
    if stage == "ready":
        private = json.loads((paths["state"] / "native-stop-control/worker-private.json").read_text())
        require(type(private.get("docker_mount_protocol")) is int and private["docker_mount_protocol"] == 1,
                "Native ready capture did not establish its mount proof")
    return {"status": "passed", "controller": NATIVE_CONTROLLER, "helper_sha256": NATIVE_WORKER_SHA256,
            "phase": stage, "private_control_state_only": True, "native_request_or_receipt_created": False}


def native_stop(child, paths, source):
    """Use the pinned shared captures and published stop CLI for this real daemon."""
    state, execution = paths["state"], paths["exec"]
    private = json.loads((state / "native-stop-control/worker-private.json").read_text())
    require(type(private.get("docker_mount_protocol")) is int and private["docker_mount_protocol"] == 1,
            "Native stop requires the completed prospective mount proof")
    pidfile = execution / "dockerd.pid"
    require(not pidfile.is_symlink() and pidfile.read_text().strip() == str(child.pid),
            "Native stop PID does not identify our daemon")
    pidfile.chmod(0o444)
    with reap_owned_daemon(child):
        native_action("stop", paths, source, NATIVE_STOP_SECONDS)
    require(child.poll() == 0 and not paths["docker_data"].exists() and not pidfile.exists(),
            "Native stop did not close and remove only its private state")
    return {"status": "passed", "controller": NATIVE_CONTROLLER, "helper_sha256": NATIVE_WORKER_SHA256,
            "phase": "stop", "private_control_state_only": True, "native_request_or_receipt_created": False,
            "prospective_mount_proof": True, "daemon_reaped": True,
            "private_data_removed": True, "pidfile_removed": True}



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
    native = native_stop_source()
    for path in (state / "daemon.json", paths["docker_data"], state / "client", state / "dockerd.log",
                 state / "native-stop-control"):
        require(not path.exists() and not path.is_symlink(), "Preserve existing private daemon state")
    result = {"status": "failed", "daemon_started": False, "daemon_closed": True,
              "container_workload_executed": False, "networking_exercised": False,
              "daemon_network_mutation_disabled": True, "data_lifetime": "current_worker_run_attempt"}
    child = None
    def interrupted(signum, frame):
        raise ProbeFailure("Private runtime probe interrupted")
    require(stat.S_ISFIFO(os.fstat(0).st_mode), "Root supervisor lifeline must be a pipe")
    os.set_blocking(0, False)
    deadline = time.monotonic() + ROOT_BODY_SECONDS
    previous = {s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT)}
    previous[signal.SIGALRM] = signal.signal(signal.SIGALRM, lambda signum, frame: root_lifeline_tick(deadline))
    signal.setitimer(signal.ITIMER_REAL, 1, 1)
    try:
        root_lifeline_tick(deadline)
        result["stage"] = "private-cgroup"
        bounded([sys.executable, "-I", "-S", "-B", str(helper)], state, "private-cgroup", 30)
        execution.mkdir(mode=0o700)
        os.chown(execution, 1000, 1000)
        (state / "client").mkdir(mode=0o700)
        os.chown(state / "client", 1000, 1000)
        with (state / "daemon.json").open("x") as stream:
            stream.write("{}\n")
        result["stage"] = "native-capture-before"
        result["native_capture_before"] = native_capture("before", paths, native)
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
            result["stage"] = "native-capture-ready"
            result["native_capture_ready"] = native_capture("ready", paths, native)
            result["stage"] = "stopped-container"
            result["stopped_container"] = stopped_container(docker, client_env, state)
            result["container_workload_executed"] = result["stopped_container"]["ever_started_observed"]
            require(result["stopped_container"]["status"] == "passed", "Stopped-container qualification or cleanup failed")
            result["stage"] = "running-container"
            result["running_container"] = running_container(docker, client_env, state)
            result["container_workload_executed"] = result["running_container"]["running_observed"]
            require(result["running_container"]["status"] == "passed", "Running-container qualification or cleanup failed")
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
                    result["native_stop"] = native_stop(child, paths, native)
                    result["daemon_closed"] = child.returncode is not None
                    result["daemon_exit_code"] = child.returncode
                except BaseException as error:
                    result.update(status="failed", native_stop={"status": "failed", "controller": NATIVE_CONTROLLER,
                                  "helper_sha256": NATIVE_WORKER_SHA256, "failure": safe_failure(error),
                                  "mount_diagnostics": native_mount_diagnostics(state)})
                    try:
                        result["daemon_closed"] = stop_owned_daemon(child)
                        result["daemon_exit_code"] = child.returncode
                    except BaseException as cleanup_error:
                        result.update(cleanup_failure=safe_failure(cleanup_error))
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
                        "--temporary", os.environ["RUNNER_TEMP"]], paths["state"], "private-docker", ROOT_COMMAND_SECONDS, root_supervisor=True)
        value["private_docker"] = json.loads(data)
        value["container_workload_executed"] = value["private_docker"]["container_workload_executed"]
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
