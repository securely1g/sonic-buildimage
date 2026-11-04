#!/usr/bin/env python3
"""Read the dedicated worker's base-config and provisioner identity only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.public_artifacts import load_json, safe_json
from tools.bazel.ci.source_worker_preflight import WORKERS, marker_identity

MARKER = Path("/etc/sonic-slave-github-runner.json")
HELPERS = tuple(Path("/opt/sonic-runner") / name for name in
                ("entrypoint.py", "cache_credentials.py", "cache_probe.py"))


def require(value, message):
    if not value:
        raise ValueError(message)


def admission(worker, environment):
    require(environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and
            environment.get("GITHUB_REPOSITORY") == "securely1g/sonic-buildimage" and
            environment.get("GITHUB_ACTOR") == "securely1g",
            "Worker provenance requires an explicit owned dispatch")
    require(worker in WORKERS and environment.get("RUNNER_NAME") == worker,
            "Worker affinity differs")
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        require(re.fullmatch(r"[1-9][0-9]*", environment.get(name, "")), "Missing run identity")


def read_root_file(path, maximum):
    # No symlink following or execution, including for the credential helper.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and
                not before.st_mode & 0o222, "Expected a root-owned read-only regular file")
        require(before.st_size <= maximum, "Provenance file is too large")
        content = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require(len(content) == before.st_size and
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode, before.st_uid) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode, after.st_uid),
            "Provenance file changed while reading")
    return content, {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                     "uid": before.st_uid, "gid": before.st_gid, "mode": stat.S_IMODE(before.st_mode)}


def marker_fields(content):
    value = load_json(content)
    expected = {"uid": 1000, "gid": 1000, "runner_root": "/opt/actions-runner", "work_root": "/actions/_work"}
    require(isinstance(value, dict) and all(value.get(k) == v for k, v in expected.items()), "Unexpected worker marker profile")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("base_image", "")), "Invalid base config digest")
    require(re.fullmatch(r"[0-9a-f]{40}", value.get("manager_revision", "")), "Invalid manager revision")
    require(re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", value.get("runner_version", "")), "Invalid runner version")
    reference = value.get("base_image_ref")
    require(isinstance(reference, str) and len(reference) <= 512 and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", reference) and "://" not in reference,
            "Invalid provisioner base reference")
    # Do not serialize unknown marker fields.
    return {k: value[k] for k in (*expected, "base_image", "base_image_ref", "manager_revision", "runner_version")}


def collect(worker, environment, progress=None):
    progress = {} if progress is None else progress
    progress["stage"] = "dispatch_identity"
    admission(worker, environment)
    progress["stage"] = "runtime_identity"
    require(os.getuid() == os.getgid() == 1000, "Diagnostic must remain UID/GID1000")
    require(platform.machine() == "x86_64", "Expected native AMD64")
    release = platform.freedesktop_os_release()
    require(release.get("ID") == "debian" and release.get("VERSION_CODENAME") == "trixie", "Expected Debian Trixie")
    require(Path("/.dockerenv").is_file(), "Expected the actual disposable slave container")
    progress["stage"] = "marker_identity"
    original = marker_identity(MARKER)
    progress["stage"] = "marker_file"
    content, record = read_root_file(MARKER, 65536)
    progress["stage"] = "marker_profile"
    fields = marker_fields(content)
    require(fields["base_image"] == original["base_image"], "Marker changed between checks")
    progress["stage"] = "provisioner_helpers"
    helpers = {}
    for path in HELPERS:
        _, item = read_root_file(path, 1024 * 1024)
        require(item["mode"] == 0o555, "Unexpected provisioner helper mode")
        helpers[path.name] = item
    return {"schema": 1, "status": "passed", "scope": "base_config_and_provisioner_only",
            "identity_kind": "local_base_config_and_provisioner", "worker": worker,
            "run": int(environment["GITHUB_RUN_ID"]), "attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
            "uid": os.getuid(), "gid": os.getgid(), "architecture": "amd64", "distribution": "debian-trixie",
            "marker": fields, "marker_file": record, "installed_provisioner_helpers": helpers,
            "derived_image_digest_verified": False, "registry_manifest_digest_verified": False,
            "build_executed": False, "packages_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, choices=WORKERS)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    admission(args.worker, os.environ)
    require(args.output.resolve().is_relative_to((ROOT / "artifacts/worker-provenance").resolve()),
            "Diagnostic output must stay in its evidence directory")
    progress = {"stage": "runtime_identity"}
    try:
        result = collect(args.worker, os.environ, progress)
        progress["stage"] = "control_revision"
        result["dispatcher_revision"] = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        require(re.fullmatch(r"[0-9a-f]{40}", result["dispatcher_revision"]), "Invalid control revision")
        progress["stage"] = "public_receipt"
        safe_json(result)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result = {"schema": 1, "status": "failed", "scope": "base_config_and_provisioner_only",
                  "worker": args.worker, "failed_stage": progress["stage"], "error_class": type(error).__name__,
                  "build_executed": False, "packages_changed": False}
    safe_json(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
