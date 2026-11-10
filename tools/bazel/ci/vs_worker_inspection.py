#!/usr/bin/env python3
"""Observe VS prerequisites without preparing a slave or building anything."""
import argparse
import grp
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import source_worker_preflight as resources
from tools.bazel.ci.image_worker_receipt import verify_k3s_preflight

POOL = "sonic-slave-container-k3s"
TOOLS = ("bazel", "docker", "dockerd", "make", "git", "python3", "dpkg-deb", "mount",
         "findmnt", "ip", "timeout", "unshare", "sudo", "jq", "j2", "tar", "gzip")


def read(path):
    with Path(path).open("rb") as stream:
        value = stream.read(64 * 1024 + 1)
    if len(value) > 64 * 1024:
        raise ValueError("Oversized prerequisite observation")
    return value.decode().strip()


def command(argv):
    return subprocess.check_output(argv, text=True, timeout=10)


def observation(probe):
    try:
        return {"observed": True, "value": probe()}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Do not publish arbitrary exception strings, environment or host paths.
        return {"observed": False, "error_type": type(error).__name__}


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


def routes_clear():
    reserved = [ipaddress.ip_network("192.168.240.0/24"), ipaddress.ip_network("192.168.248.0/21")]
    for route in json.loads(command(["ip", "-j", "-4", "route", "show"])):
        if route.get("dst", "default") != "default":
            actual = ipaddress.ip_network(route["dst"], strict=False)
            if any(actual.overlaps(network) for network in reserved):
                return False
    return True


def collect(worker):
    resource = resources.measure(worker, POOL)
    resources.safe_json(resource)
    facts = {
        "marker": observation(marker),
        "tools": {name: shutil.which(name) is not None for name in TOOLS},
        "docker_group": observation(lambda: grp.getgrnam("docker").gr_gid in os.getgroups()),
        "cgroup_type": observation(lambda: read("/sys/fs/cgroup/cgroup.type")),
        "cgroup_controllers": observation(lambda: sorted(read("/sys/fs/cgroup/cgroup.controllers").split())),
        "cgroup_delegation_writable": observation(lambda: all(subprocess.run(
             ["sudo", "-n", "test", "-w", "/sys/fs/cgroup/" + name], stdin=subprocess.DEVNULL,
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
             for name in ("cgroup.procs", "cgroup.subtree_control"))),
        "docker_endpoint_absent": not any(os.environ.get(key) for key in
             ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"))
             and not Path("/var/run/docker.sock").exists() and not Path("/var/run/docker.sock").is_symlink(),
        "private_docker_state_absent": not any(Path(path).exists() or Path(path).is_symlink()
             for path in ("/actions/sonic-native-docker", "/run/sonic-native-docker")),
        "sonic_directory_empty": observation(lambda: Path("/sonic").is_dir()
             and not Path("/sonic").is_symlink() and next(Path("/sonic").iterdir(), None) is None),
        "private_docker_routes_clear": observation(routes_clear),
        "sudo_available": observation(lambda: subprocess.run(["sudo", "-n", "true"], check=False,
             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0),
    }
    return {"schema": 1, "status": "OBSERVATION_NOT_QUALIFICATION", "worker": worker, "pool": POOL,
            "resources": resource, "vs_observations": facts,
            "unperformed": ["pinned kernel sandbox execution", "private Docker startup/overlay validation",
                            "reviewed manager requirement comparison", "source/submodule and complete input admission"],
            "build_or_package_generation": False, "host_kernel_configuration_changed": False,
            "cache_lifetime": "cross_pod_reuse_not_established"}


def check_reviewed_requirements(value, expected_manager):
    """Check measured prerequisites; this does not qualify the private daemon."""
    if not isinstance(expected_manager, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_manager):
        raise ValueError("A reviewed immutable manager requirement is necessary")
    worker = value.get("worker")
    if (type(value.get("schema")) is not int or value["schema"] != 1 or
            value.get("status") != "OBSERVATION_NOT_QUALIFICATION" or value.get("pool") != POOL or
            not isinstance(worker, str) or not resources.worker_matches_pool(worker, POOL) or
            value.get("build_or_package_generation") is not False or
            value.get("host_kernel_configuration_changed") is not False):
        raise ValueError("Not a VS prerequisite observation")
    r, f = value["resources"], value["vs_observations"]
    if (type(r.get("schema")) is not int or r["schema"] != 1 or
            r.get("scope") != "resource_and_namespace_preflight_only" or
            r.get("status") != "passed" or set(r.get("checks", {})) != resources.required_checks(POOL) or
            not all(x is True for x in r["checks"].values()) or
            r.get("image_build_executed") is not False or r.get("deb_generation_executed") is not False):
        raise ValueError("Resource/namespace prerequisites failed")
    verify_k3s_preflight(r, {"runner_name": worker})
    actual = r["facts"]
    if actual["memory_limit_bytes"] != 20 * 1024**3 or actual["cpu_limit"] != 6:
        raise ValueError("VS allocation differs")
    if actual["workspace_storage"]["free_bytes"] < 90 * 1024**3:
        raise ValueError("VS requires90GiB free after input staging")
    if not all(f["tools"].get(name) is True for name in TOOLS):
        raise ValueError("Missing VS tools")
    expected = {"docker_group": True, "cgroup_type": "domain", "cgroup_delegation_writable": True,
                "sonic_directory_empty": True, "private_docker_routes_clear": True, "sudo_available": True}
    if any(f[key].get("observed") is not True or f[key].get("value") != item for key, item in expected.items()):
        raise ValueError("VS isolation/delegation prerequisites differ")
    if not f["docker_endpoint_absent"] or not f["private_docker_state_absent"]:
        raise ValueError("Existing Docker state must not be adopted")
    if f["marker"].get("observed") is not True or f["marker"]["value"]["manager_revision"] != expected_manager:
        raise ValueError("Manager differs from the reviewed requirement")
    controllers = f["cgroup_controllers"]
    if controllers.get("observed") is not True or not {"cpu", "memory", "pids"} <= set(controllers["value"]):
        raise ValueError("Missing delegated controllers")
    return {"status": "MEASURED_PREREQUISITES_MATCH", "private_daemon_qualified": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worker", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.worker != os.environ.get("RUNNER_NAME") or not resources.worker_matches_pool(a.worker, POOL):
        p.error("Assigned runner identity differs")
    if a.output.exists() or a.output.is_symlink():
        p.error("Preserve the previous observation")
    value = collect(a.worker)
    value["run"] = int(os.environ["GITHUB_RUN_ID"])
    value["attempt"] = int(os.environ["GITHUB_RUN_ATTEMPT"])
    value["controller"] = os.environ["GITHUB_SHA"]
    with a.output.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(value["status"])


if __name__ == "__main__":
    main()
