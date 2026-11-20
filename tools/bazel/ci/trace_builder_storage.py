"""Observe the selected VS builder's writable layer during docker run --rm teardown."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import platform
import re
import stat
import time


# The VS experiment runs on Linux/AMD64. Keep only the syscall name and whether
# its directory descriptor belongs to this specific builder. Never save args.
DIRECTORY_SYSCALLS_X86_64 = {
    3: "close", 74: "fsync", 75: "fdatasync", 217: "getdents64", 257: "openat",
    262: "newfstatat", 263: "unlinkat", 264: "renameat", 267: "readlinkat",
    306: "syncfs", 316: "renameat2", 332: "statx", 437: "openat2",
}
PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")


def read_small(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def link(path):
    try:
        return os.readlink(path)
    except OSError:
        return ""


def selected_mount(pid):
    for line in read_small(PROC / str(pid) / "mountinfo").splitlines():
        left, _, right = line.partition(" - ")
        fields, fs = left.split(), right.split()
        if len(fields) >= 5 and fields[4] == "/" and len(fs) >= 3 and fs[0] == "overlay":
            for option in fs[2].split(","):
                if option.startswith("upperdir=/"):
                    return Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), option[9:]))
    return None


class TrackedDirectory:
    """Track an underlying storage directory without opening the builder's merged mount."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        self.original = str(path)

    def path(self):
        return link(PROC / "self/fd" / str(self.fd)).removesuffix(" (deleted)")

    def state(self):
        if os.fstat(self.fd).st_nlink == 0:
            return "unlinked"
        return "original" if self.path() == self.original else "renamed"

    def close(self):
        os.close(self.fd)


def category(relative_path):
    parts = relative_path.split("/")
    if ".cache" in parts and ("bazel" in parts or any(p.startswith("_bazel_") for p in parts)):
        for name in ("execroot", "external", "sandbox", "action_cache", "install", "cache"):
            if name in parts:
                return "bazel_" + name
        return "bazel_other"
    return parts[0] if parts[0] in {"usr", "opt", "home", "root", "var", "tmp", "etc", "sonic"} else "other"


def classify(path, upper, layer, container_id):
    path = path.removesuffix(" (deleted)")
    if upper:
        if path == upper:
            return "writable_root"
        if path.startswith(upper + "/"):
            return category(path[len(upper) + 1:])
    if layer and (path == layer or path.startswith(layer + "/")):
        return "layer_metadata"
    if container_id in path.split("/"):
        return "container_metadata"
    return None


def host_daemons():
    namespace = link(PROC / "1/ns/pid")
    return {p: read_small(p / "comm") for p in PROC.iterdir()
            if p.name.isdecimal() and link(p / "ns/pid") == namespace
            and read_small(p / "comm") in {"dockerd", "containerd"}}


def daemon_sample(process, upper, layer, container_id, syscalls):
    categories = {}
    try:
        for fd in (process / "fd").iterdir():
            kind = classify(link(fd), upper, layer, container_id)
            if kind:
                categories[int(fd.name)] = kind
    except FileNotFoundError:
        return {"gone": True}
    except PermissionError:
        return {"accessible": False}

    activity = Counter()
    wait_channels = Counter()
    if categories and syscalls:
        for task in (process / "task").iterdir():
            call = read_small(task / "syscall").split()
            if len(call) < 2 or not call[0].isdigit():
                continue
            name = DIRECTORY_SYSCALLS_X86_64.get(int(call[0]))
            if not name:
                continue
            fd = int(call[1], 0)
            if fd in categories:
                activity[name + "/" + categories[fd]] += 1
                wait_channel = read_small(task / "wchan")
                if re.fullmatch(r"[A-Za-z0-9_]+", wait_channel):
                    wait_channels[wait_channel] += 1
    io = {}
    for row in read_small(process / "io").splitlines():
        key, _, value = row.partition(": ")
        if key in {"read_bytes", "write_bytes", "syscr", "syscw"} and value.isdigit():
            io[key] = int(value)
    fields = read_small(process / "stat").rpartition(") ")[2].split()
    return {"target_fds": dict(Counter(categories.values())), "target_wait_channels": dict(wait_channels),
            "target_syscalls": dict(activity), "process_io_cumulative": io,
            "process_cpu_ticks": sum(map(int, fields[11:13])) if len(fields) > 12 else None}


def pressure():
    result = {}
    for line in read_small(PROC / "pressure/io").splitlines():
        name, _, values = line.partition(" ")
        for value in values.split():
            if value.startswith("total="):
                result[name] = int(value[6:])
    return result


def cgroup_path(pid, container_id):
    for line in read_small(PROC / str(pid) / "cgroup").splitlines():
        if line.startswith("0::/") and container_id in line and ".." not in Path(line[4:]).parts:
            return CGROUP / line[4:].lstrip("/")
    return None


def selected_processes(cg):
    if cg is None:
        return None
    ids = read_small(cg / "cgroup.procs").split()
    known = {"docker-init", "tini", "su", "bash", "sh", "dockerd", "containerd", "java", "bazel"}
    count = Counter()
    for pid in ids[:512]:
        comm = read_small(PROC / pid / "comm")
        count[comm if comm in known else "other"] += 1
    return {"total": len(ids), "sampled": dict(count)}


def free_space(upper):
    if upper is None:
        return None
    try:
        fs = os.fstatvfs(upper.fd)
        return {"inodes": fs.f_ffree, "bytes": fs.f_bfree * fs.f_frsize}
    except OSError:
        return None


def block_stats(stat_path):
    if stat_path is None:
        return None
    values = read_small(stat_path).split()
    if len(values) < 11:
        return None
    # Linux kernel block stat fields: completed IO counts, 512-byte sectors, IO time.
    return dict(zip(("reads", "read_sectors", "writes", "write_sectors", "io_ms"),
                    [int(values[n]) for n in (0, 2, 4, 6, 9)]))


def output_stream(path):
    # Even when sampling as root, the file must be opened with the runner's
    # credentials. Never truncate an arbitrary root-owned or symlinked file.
    uid = os.geteuid()
    user = int(os.environ.get("SUDO_UID", uid))
    try:
        os.seteuid(user)
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != user or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("expected a regular receipt owned by the runner")
        os.ftruncate(fd, 0)
        return os.fdopen(fd, "w")
    finally:
        os.seteuid(uid)


def observe(args):
    # Container ID and PID are taken from Docker inspect by the unprivileged
    # parent. Check membership before examining the host process or its layer.
    if args.container_id not in read_small(PROC / str(args.pid) / "cgroup"):
        return {"available": False, "reason": "could not match init PID to container cgroup"}
    mounted = selected_mount(args.pid)
    upper, layer, block = None, None, None
    cg = cgroup_path(args.pid, args.container_id)
    if mounted and mounted.name in {"diff", "fs"}:
        try:
            upper = TrackedDirectory(mounted)
            layer = TrackedDirectory(mounted.parent)
            dev = os.fstat(upper.fd).st_dev
            block = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}/stat")
        except OSError:
            pass
    daemons = host_daemons()
    report = {"available": True, "layout": mounted.name if upper else None,
              "interval_seconds": 1, "clock": "Unix realtime nanoseconds", "clock_ticks": os.sysconf("SC_CLK_TCK"),
              "daemon_counters_are_host_wide": True, "samples": []}
    deadline = time.monotonic() + 6 * 3600
    try:
        while time.monotonic() < deadline and not args.stop.exists():
            if read_small(args.marker).isdigit():
                break
            time.sleep(0.5)
        tail_deadline = min(deadline, time.monotonic() + 1800)
        while True:
            sample = {"at_ns": time.time_ns(), "upper": upper.state() if upper else None,
                      "layer": layer.state() if layer else None,
                      "cgroup_events": read_small(cg / "cgroup.events") if cg else None,
                      "io_pressure_us": pressure(), "device": block_stats(block),
                      "cgroup_processes": selected_processes(cg), "filesystem_free": free_space(upper),
                      "daemons": {}}
            upper_path = upper.path() if upper else ""
            layer_path = layer.path() if layer else ""
            for process, name in daemons.items():
                try:
                    observed = daemon_sample(process, upper_path, layer_path, args.container_id,
                                             platform.machine() == "x86_64")
                except OSError as error:
                    observed = {"errno": error.errno}
                sample["daemons"][name + "-" + process.name] = observed
            report["samples"].append(sample)
            if args.stop.exists() or time.monotonic() >= tail_deadline:
                break
            time.sleep(1)
    finally:
        for directory in (upper, layer):
            if directory:
                directory.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--container-id", required=True)
    for option in ("--marker", "--output", "--stop"):
        parser.add_argument(option, type=Path, required=True)
    args = parser.parse_args()
    if args.pid <= 1 or not re.fullmatch(r"[a-f0-9]{64}", args.container_id):
        parser.error("invalid container identity")
    with output_stream(args.output) as out:
        try:
            report = observe(args)
        except (OSError, ValueError) as exc:
            report = {"available": False, "reason": type(exc).__name__, "errno": getattr(exc, "errno", None)}
        json.dump(report, out, separators=(",", ":"))
        out.write("\n")


if __name__ == "__main__":
    main()
