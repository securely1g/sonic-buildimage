#!/usr/bin/env python3
"""Measure the dedicated source-image worker without building packages."""

import argparse
import grp
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.public_artifacts import safe_json

WORKERS = tuple(f"sonic-slave-lgh114-{number}" for number in range(1, 5))
POOLS = ("sonic-slave-container-dpool", "sonic-slave-container-k3s")
# Public receipt/path syntax only; authoritative job metadata binds the pool.
K3S_WORKER_PATTERN = r"ks-[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?"
MINIMUM_MEMORY_BYTES = 20 * 1024**3


def select_build_profile(memory_bytes, cpus):
    """Choose scheduling from the admitted worker's measured cgroup limits."""
    if (type(memory_bytes) is not int or memory_bytes < MINIMUM_MEMORY_BYTES or
            type(cpus) not in (int, float) or not 2 <= cpus <= 6):
        raise ValueError("Worker does not meet the existing bounded resource gate")
    parallel = cpus >= 4
    return {"name": "two_actions" if parallel else "one_action", "status": "pilot",
            "minimum_memory_bytes": MINIMUM_MEMORY_BYTES, "minimum_cpu": 4 if parallel else 2,
            "measured_memory_limit_bytes": memory_bytes, "measured_cpu_limit": cpus,
            "jvm_heap_gib": 3, "jobs": 2 if parallel else 1, "cpu": 2 if parallel else 1,
            "scheduler_memory_mb": 8000 if parallel else 4000, "loaded_container_memory_gib": 4}


def cpu_limit(value):
    quota, period = value.split()
    if quota == "max" or int(quota) <= 0 or int(period) <= 0:
        raise ValueError("A positive finite CPU quota is required")
    return int(quota) / int(period)


def marker_identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222:
        raise ValueError("Marker must be a root-owned read-only regular file")
    value = json.loads(path.read_text())
    expected = {"uid": 1000, "gid": 1000, "runner_root": "/opt/actions-runner", "work_root": "/actions/_work"}
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("Unexpected runner profile")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("base_image", "")):
        raise ValueError("Missing immutable SONiC slave image")
    return {"base_image": value["base_image"]}


def worker_matches_pool(worker, pool):
    if pool == "sonic-slave-container-dpool":
        return worker in WORKERS
    return pool == "sonic-slave-container-k3s" and re.fullmatch(K3S_WORKER_PATTERN, worker) is not None


def worker_cache_root(pool="sonic-slave-container-dpool"):
    """Keep the existing cache layout on the selected pool's mounted storage."""
    if pool == "sonic-slave-container-dpool":
        return Path("/bazel_cache")
    if pool == "sonic-slave-container-k3s":
        return Path("/actions/_work/.source-image-cache")
    raise ValueError("Unknown source-worker pool")


RESOURCE_TOOLS = ("bazel", "docker", "dockerd", "sudo", "git", "make", "readelf", "mountpoint", "findmnt", "unshare")


def required_checks(pool):
    """Required checks for this pinned resource schema, including the pool cache."""
    if pool not in ("sonic-slave-container-dpool", "sonic-slave-container-k3s"):
        raise ValueError("Unsupported worker pool")
    names = {"worker_affinity", "build_user", "native_amd64", "trixie", "container", "slave_identity",
             "private_cgroup_v2", "delegated_controllers", "bounded_build_resources", "private_docker",
             "docker_group", "sudo", "user_mount_namespace", "workspace_mount", "workspace_storage",
             "cache_mount", "cache_storage"}
    names.update("tool_" + name for name in RESOURCE_TOOLS)
    if pool == "sonic-slave-container-k3s":
        names.add("cache_writable")
    return frozenset(names)


def measure(expected_worker, pool="sonic-slave-container-dpool"):
    checks, facts = {}, {}
    build_profile = None

    def check(name, probe):
        try:
            checks[name] = bool(probe())
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            checks[name] = False

    actual = os.environ.get("RUNNER_NAME", "")
    check("worker_affinity", lambda: actual == expected_worker and worker_matches_pool(actual, pool))
    facts["worker"] = actual if worker_matches_pool(actual, pool) else "unexpected"
    if pool == "sonic-slave-container-k3s":
        facts["pool"] = pool
    facts["uid"], facts["gid"] = os.getuid(), os.getgid()
    check("build_user", lambda: facts["uid"] == facts["gid"] == 1000)
    facts["machine"] = platform.machine()
    check("native_amd64", lambda: facts["machine"] == "x86_64")
    check("trixie", lambda: platform.freedesktop_os_release().get("ID") == "debian"
          and platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie")
    check("container", lambda: Path("/.dockerenv").is_file())

    def marker():
        facts["slave"] = marker_identity(Path("/etc/sonic-slave-github-runner.json"))
        return True

    check("slave_identity", marker)
    cgroups = Path("/sys/fs/cgroup")
    check("private_cgroup_v2", lambda: Path("/proc/self/cgroup").read_text().strip() == "0::/"
          and (cgroups / "cgroup.type").read_text().strip() == "domain")
    check("delegated_controllers", lambda: {"cpu", "memory", "pids"}.issubset(
        (cgroups / "cgroup.controllers").read_text().split()))

    def resources():
        nonlocal build_profile
        facts["memory_limit"] = (cgroups / "memory.max").read_text().strip()
        facts["cpu_max"] = (cgroups / "cpu.max").read_text().strip()
        facts["memory_limit_bytes"] = int(facts["memory_limit"])
        facts["memory_current_bytes"] = int((cgroups / "memory.current").read_text())
        facts["cpu_limit"] = cpu_limit(facts["cpu_max"])
        build_profile = select_build_profile(facts["memory_limit_bytes"], facts["cpu_limit"])
        return True

    check("bounded_build_resources", resources)
    for name in RESOURCE_TOOLS:
        check("tool_" + name, lambda name=name: shutil.which(name) is not None)
    check("private_docker", lambda: not Path("/var/run/docker.sock").exists() and not any(
        os.environ.get(name) for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")))
    check("docker_group", lambda: grp.getgrnam("docker").gr_gid in os.getgroups())
    check("sudo", lambda: subprocess.run(["sudo", "-n", "true"], timeout=10, capture_output=True).returncode == 0)
    check("user_mount_namespace", lambda: subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "true"], timeout=10, capture_output=True).returncode == 0)
    mounts = (("workspace", "/actions"), ("cache", "/bazel_cache"))
    if pool == "sonic-slave-container-k3s":
        mounts = (("workspace", "/actions"),)
    for name, mount in mounts:
        check(name + "_mount", lambda mount=mount: subprocess.run(
            ["mountpoint", "-q", mount], timeout=10, capture_output=True).returncode == 0)

        def storage(name=name, mount=mount):
            filesystem = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", mount],
                                                 text=True, timeout=10).strip()
            free = shutil.disk_usage(mount).free
            facts[name + "_storage"] = {"filesystem": filesystem, "free_bytes": free}
            return filesystem in ("ext4", "xfs") and free >= 50 * 1024**3

        check(name + "_storage", storage)
    if pool == "sonic-slave-container-k3s":
        namespace = worker_cache_root(pool)
        cache = namespace / "source-images" / actual
        work_root = namespace.parent
        directories = (namespace, namespace / "source-images", cache)
        backing = Path("/actions")
        measured_path = work_root

        def private_directory(path):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or path.resolve() != path or
                    info.st_uid != 1000 or info.st_gid != 1000 or
                    stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError("Unsafe source-worker cache directory")
            return info

        def cache_mount():
            nonlocal measured_path
            if not checks["worker_affinity"] or not checks["workspace_mount"]:
                return False
            info = work_root.lstat()
            facts["cache_root"] = {"namespace": "actions_work_source_images",
                                   "worker_directory": "source-images/" + actual,
                                   "backing_mount": "actions", "shared_with_workspace": True,
                                   "lifetime": "current_worker_mount", "cross_pod_reuse": "not_established",
                                   "work_root_uid": info.st_uid, "work_root_gid": info.st_gid,
                                   "work_root_mode": oct(stat.S_IMODE(info.st_mode))}
            if not stat.S_ISDIR(info.st_mode) or work_root.resolve() != work_root:
                return False
            for path in directories:
                if path.exists() or path.is_symlink():
                    private_directory(path)
                    measured_path = path
            target = subprocess.check_output(["findmnt", "-n", "-o", "TARGET", "-T", str(measured_path)],
                                             text=True, timeout=10).strip()
            return target == str(backing) and measured_path.stat().st_dev == backing.stat().st_dev

        def cache_storage():
            if not checks["cache_mount"]:
                return False
            filesystem = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", str(measured_path)],
                                                 text=True, timeout=10).strip()
            free = shutil.disk_usage(measured_path).free
            facts["cache_storage"] = {"filesystem": filesystem, "free_bytes": free,
                                      "backing_mount": "actions", "shared_with_workspace": True}
            return filesystem in ("ext4", "xfs") and free >= 50 * 1024**3

        def cache_writable():
            if not all(checks.values()):
                return False
            for path in directories:
                path.mkdir(mode=0o700, exist_ok=True)
                if private_directory(path).st_dev != backing.stat().st_dev:
                    return False
            target = subprocess.check_output(["findmnt", "-n", "-o", "TARGET", "-T", str(cache)],
                                             text=True, timeout=10).strip()
            if target != str(backing):
                return False
            # Only this new anonymous probe file is removed on close; cache data stays.
            with tempfile.TemporaryFile(mode="w+b", dir=cache) as probe:
                probe.write(b"source-worker-cache-probe\n")
                probe.flush()
                os.fsync(probe.fileno())
                probe.seek(0)
                if probe.read() != b"source-worker-cache-probe\n":
                    return False
            facts["cache_root"].update(uid=1000, gid=1000, mode="0o700", writable=True,
                                       write_probe_closed=True)
            return True

        check("cache_mount", cache_mount)
        check("cache_storage", cache_storage)
        check("cache_writable", cache_writable)
    return {"schema": 1, "status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "facts": facts, "build_profile": build_profile, "scope": "resource_and_namespace_preflight_only",
            "image_build_executed": False, "deb_generation_executed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=POOLS, default="sonic-slave-container-dpool")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not worker_matches_pool(args.worker, args.pool):
        parser.error("Worker name does not match the selected preflight pool")
    receipt = measure(args.worker, args.pool)
    receipt["dispatcher_revision"] = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    safe_json(receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
