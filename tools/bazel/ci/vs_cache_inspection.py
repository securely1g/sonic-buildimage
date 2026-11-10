#!/usr/bin/env python3
"""Observe existing K3s cache metadata without preparing or using the caches."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import time

POOL = "sonic-slave-container-k3s"
ROOTS = ("/dpkg_cache", "/bazel_cache")
MANAGER = "c650551885f8ad22cbc104082d1f1b6171e67eef"
IMAGE = "sha256:52cc68070154c5d9132995341a1800f1aa80f59d089f951d274a725693bff732"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
OUTPUT_LIMIT = 1024 * 1024


def read(path):
    with Path(path).open("rb") as stream:
        value = stream.read(64 * 1024 + 1)
    if len(value) > 64 * 1024:
        raise ValueError("Oversized prerequisite observation")
    return value.decode().strip()


def marker():
    path = Path("/etc/sonic-slave-github-runner.json")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222:
        raise ValueError("Untrusted runner marker")
    value = json.loads(read(path))
    fixed = {"uid": 1000, "gid": 1000, "runner_root": "/opt/actions-runner", "work_root": "/actions/_work"}
    if any(value.get(key) != expected for key, expected in fixed.items()):
        raise ValueError("Different runner marker profile")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("base_image", "")):
        raise ValueError("Missing immutable runner image")
    manager = value.get("manager_revision")
    if not isinstance(manager, str) or not re.fullmatch(r"[0-9a-f]{40}", manager):
        raise ValueError("Missing immutable runner manager")
    return {"base_image": value["base_image"], "manager_revision": manager}


def identity(worker, environment, actual_marker):
    if (worker != environment.get("RUNNER_NAME") or
            not re.fullmatch(r"ks-[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?", worker)):
        raise ValueError("Assigned runner identity differs")
    expected = {"GITHUB_REPOSITORY": "securely1g/sonic-buildimage",
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "securely1g",
                "GITHUB_TRIGGERING_ACTOR": "securely1g"}
    if any(environment.get(key) != value for key, value in expected.items()):
        raise ValueError("Unexpected manual observation context")
    if not all(re.fullmatch(r"[1-9][0-9]*", environment.get(key, ""))
               for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")):
        raise ValueError("Invalid original run identity")
    if not re.fullmatch(r"[0-9a-f]{40}", environment.get("GITHUB_SHA", "")):
        raise ValueError("Missing immutable controller")
    if os.getuid() != 1000 or os.geteuid() != 1000 or os.getgid() != 1000 or os.getegid() != 1000:
        raise ValueError("Observation requires the unprivileged runner identity")
    if actual_marker != {"base_image": IMAGE, "manager_revision": MANAGER}:
        raise ValueError("Runner image or manager differs")
    return {"worker": worker, "pool": POOL, "run": int(environment["GITHUB_RUN_ID"]),
            "attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
            "controller": environment["GITHUB_SHA"], "marker": actual_marker}


def parse_mounts(text):
    """Keep only safe mount identity fields; never retain source/options strings."""
    result = []
    unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        before, after = left.split(), right.split()
        if not separator or len(before) < 6 or len(after) < 3:
            raise ValueError("Malformed mount metadata")
        if not re.fullmatch(r"[0-9]+:[0-9]+", before[2]):
            raise ValueError("Invalid mount device")
        mountpoint = unescape(before[4])
        if not mountpoint.startswith("/"):
            raise ValueError("Invalid mountpoint")
        result.append({"mountpoint": mountpoint, "device": before[2], "filesystem": after[0],
                       "mount_root_sha256": hashlib.sha256(unescape(before[3]).encode()).hexdigest()})
    return result


def covering_mount(root, mounts):
    matches = [item for item in mounts if root == item["mountpoint"] or
               root.startswith(item["mountpoint"].rstrip("/") + "/")]
    if not matches:
        raise ValueError("No covering mount")
    selected = max(matches, key=lambda item: len(item["mountpoint"]))
    return dict(selected, exact_mountpoint=root == selected["mountpoint"])


class Budget:
    def __init__(self, *, seconds=30, entries=512, directories=32, depth=2):
        self.end = time.monotonic() + seconds
        self.max_entries, self.max_directories, self.depth = entries, directories, depth
        self.entries = self.directories = 0

    def check_time(self):
        if time.monotonic() >= self.end:
            raise TimeoutError("Metadata observation deadline")


def metadata(info):
    kind = ("directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode)
            else "symlink" if stat.S_ISLNK(info.st_mode) else "special")
    return {"kind": kind, "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
            "gid": info.st_gid, "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": str(os.major(info.st_dev)) + ":" + str(os.minor(info.st_dev)),
            "inode": info.st_ino}


def inspect_root(root, mounts, budget):
    """Read no cache file contents; open only directories, without following links."""
    result = {"root": str(root), "entries": [], "status": "not_observed", "truncation": []}
    descriptors = set()
    try:
        budget.check_time()
        initial = os.lstat(root)
        result["metadata"] = metadata(initial)
        if stat.S_ISLNK(initial.st_mode):
            result["status"] = "symlink_rejected"
            return result
        if not stat.S_ISDIR(initial.st_mode):
            result["status"] = "non_directory"
            return result
        fd = os.open(root, DIRECTORY_FLAGS)
        descriptors.add(fd)
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (initial.st_dev, initial.st_ino):
            result["status"] = "changed_during_observation"
            return result
        result["mount"] = covering_mount(str(root), mounts)
        if result["mount"]["device"] != result["metadata"]["device"]:
            result["status"] = "changed_during_observation"
            return result
        storage = os.fstatvfs(fd)
        result["storage"] = {"available_bytes": storage.f_bavail * storage.f_frsize,
                             "total_bytes": storage.f_blocks * storage.f_frsize,
                             "measurement": "current descriptor statvfs; not a quota or reservation"}
        queue = [("", fd, 0)]
        while queue:
            relative, parent_fd, depth = queue.pop(0)
            budget.check_time()
            try:
                with os.scandir(parent_fd) as entries:
                    for entry in entries:
                        budget.check_time()
                        if budget.entries >= budget.max_entries:
                            result["truncation"].append("entry_limit")
                            queue.clear()
                            break
                        budget.entries += 1
                        name = relative + "/" + entry.name if relative else entry.name
                        row = {"relative_name": name}
                        result["entries"].append(row)
                        try:
                            info = os.stat(entry.name, dir_fd=parent_fd, follow_symlinks=False)
                            row.update(metadata(info))
                            if stat.S_ISDIR(info.st_mode):
                                if depth + 1 >= budget.depth:
                                    row["children"] = "depth_limit"
                                elif budget.directories >= budget.max_directories:
                                    row["children"] = "directory_limit"
                                    result["truncation"].append("directory_limit")
                                elif info.st_dev != actual.st_dev:
                                    row["children"] = "different_device_not_descended"
                                    result["truncation"].append("different_device")
                                else:
                                    child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=parent_fd)
                                    descriptors.add(child)
                                    opened = os.fstat(child)
                                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                        row["children"] = "changed_during_observation"
                                        result["truncation"].append("changed_during_observation")
                                        os.close(child); descriptors.remove(child)
                                    else:
                                        budget.directories += 1
                                        queue.append((name, child, depth + 1))
                        except OSError as error:
                            row["metadata_error"] = type(error).__name__
                            result["truncation"].append("entry_" + type(error).__name__)
            except OSError as error:
                result["truncation"].append("directory_" + type(error).__name__)
            finally:
                os.close(parent_fd); descriptors.remove(parent_fd)
        result["status"] = ("observed_truncated" if result["truncation"]
                            else "observed_complete_at_selected_depth")
    except FileNotFoundError:
        result["status"] = "missing"
    except PermissionError:
        result["status"] = "permission_denied"
    except TimeoutError:
        result["status"] = "observed_truncated"
        result["truncation"].append("deadline")
    except OSError as error:
        result["status"] = "symlink_rejected" if error.errno == errno.ELOOP else "metadata_error"
        result["error_type"] = type(error).__name__
    finally:
        for fd in descriptors:
            os.close(fd)
    result["truncation"] = sorted(set(result["truncation"]))
    return result


def collect(identity_record):
    with open("/proc/self/mountinfo", "rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Oversized mount metadata")
    mounts = parse_mounts(raw.decode())
    budget = Budget()
    roots = [inspect_root(root, mounts, budget) for root in ROOTS]
    devices = [root["metadata"]["device"] for root in roots if "storage" in root]
    return {"schema": 1, "status": "CACHE_METADATA_OBSERVATION_NOT_ADMISSION", **identity_record,
            "roots": roots, "entries_observed": budget.entries,
            "shared_backing_device": (devices[0] == devices[1]) if len(devices) == 2 else None,
            "capacity_is_not_additive": True, "cross_pod_reuse": "not_established",
            "scope": "two existing cache roots on this assigned worker; depth2/512 entries/32 directories",
            "cache_contents_read": False, "cache_writes": False, "build_or_package_generation": False}


def deadline(signum, frame):
    raise TimeoutError("Metadata observation deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    temporary = Path(os.environ["RUNNER_TEMP"])
    if (temporary.resolve() != temporary.absolute() or not temporary.is_relative_to("/actions/_work") or
            args.output.parent != temporary or args.output.exists() or args.output.is_symlink()):
        parser.error("Require a fresh receipt outside the inspected cache roots")
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 30)
    code = 0
    try:
        value = collect(identity(args.worker, os.environ, marker()))
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        code = 1
        value = {"schema": 1, "status": "CACHE_METADATA_OBSERVATION_FAILED",
                 "error_type": type(error).__name__, "cache_contents_read": False, "cache_writes": False}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if len(encoded) > OUTPUT_LIMIT:
        raise ValueError("Oversized metadata receipt")
    with args.output.open("xb") as stream:
        stream.write(encoded)
    print(value["status"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
