#!/usr/bin/env python3
"""Admit original artifact metadata and copy reviewed files into a fresh VS checkout.

The existing pinned download-artifact action performs transport. This helper
does not download, produce a package, change a producer receipt, or authorize VS.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import vs_reuse_plan as policy
from tools.bazel.ci.image_worker_receipt import api

require = policy.require
MAX_FILES_BYTES = 128 * 1024**3
MAX_ARTIFACTS = 64
ASSEMBLY_RESERVE_BYTES = 90 * 1024**3


def positive(value):
    return type(value) is int and value > 0


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "Artifact timestamp has no timezone")
    return parsed


def transfers(value):
    """Describe copies only; a frozen inventory is not replacement provenance."""
    rows = value["transfer"]
    require(isinstance(rows, list) and 0 < len(rows) <= MAX_ARTIFACTS, "Invalid artifact transfer set")
    ids, names, destinations = set(), set(), set()
    for row in rows:
        require(set(row) == {"artifact_id", "name", "digest", "zip_bytes", "expanded_bytes", "run_id", "attempt",
                            "job_id", "job_name", "upload_step", "head", "event", "workflow", "members"},
                "Incomplete original artifact identity")
        require(all(positive(row[k]) for k in ("artifact_id", "run_id", "attempt", "job_id", "zip_bytes", "expanded_bytes")),
                "Invalid original artifact/run/job identity")
        require(row["artifact_id"] not in ids and row["name"] not in names,
                "Duplicate artifact identity or extraction directory")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", row["name"]) and
                re.fullmatch(r"sha256:[0-9a-f]{64}", row["digest"]) and
                re.fullmatch(r"[0-9a-f]{40}", row["head"]), "Unsafe artifact name or identity")
        require(row["event"] in ("pull_request", "push", "workflow_dispatch") and
                re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", row["workflow"]),
                "Unreviewed producer workflow")
        require(all(isinstance(row[k], str) and 0 < len(row[k]) <= 256 and
                    not any(c in row[k] for c in "\0\r\n") for k in ("job_name", "upload_step")),
                "Invalid upload-job binding")
        members = row["members"]
        require(isinstance(members, dict) and 0 < len(members) <= 20000, "Invalid artifact member selection")
        for member, target in members.items():
            policy.relative(member)
            policy.relative(target)
            require(target in value["files"] and target not in destinations and
                    target not in value["groups"]["source"], "Duplicate or source-overwriting transfer")
            destinations.add(target)
        require(row["expanded_bytes"] >= sum(value["files"][target]["bytes"] for target in members.values()),
                "Original expanded size is smaller than its selected members")
        ids.add(row["artifact_id"])
        names.add(row["name"])
    require(destinations == set(value["files"]) - set(value["groups"]["source"]),
            "Every non-source input must retain an original artifact binding")
    require(sum(row["bytes"] for row in value["files"].values()) <= MAX_FILES_BYTES and
            sum(row["zip_bytes"] for row in rows) <= MAX_FILES_BYTES and
            sum(row["expanded_bytes"] for row in rows) <= MAX_FILES_BYTES, "Input transfer exceeds its bound")
    return rows


def bazel_builds(value):
    """Bind reviewed Bazel producer steps to the exact imported archive members.

    The independently reviewed inventory identifies the actual build step in
    each immutable producer workflow. This is build-method provenance only;
    owner, native, image-content and source checks remain separate.
    """
    proofs = value.get("bazel_archive_builds")
    require(isinstance(proofs, dict) and len(proofs) <= 42,
            "Reviewed Bazel archive build evidence is required")
    owners = {target: row for row in transfers(value) for target in row["members"].values()}
    for target, proof in proofs.items():
        require(isinstance(target, str) and re.fullmatch(r"target/docker-[a-z0-9-]+\.gz", target)
                and target in owners, "Bazel evidence does not name an imported archive")
        row = owners[target]
        require(isinstance(proof, dict) and set(proof) == {"artifact_id", "sha256", "build_step"}
                and positive(proof["artifact_id"]) and proof["artifact_id"] == row["artifact_id"]
                and proof["sha256"] == value["files"][target]["sha256"],
                "Bazel build evidence differs from the imported archive identity")
        step = proof["build_step"]
        require(isinstance(step, str) and 0 < len(step) <= 256
                and not any(c in step for c in "\0\r\n") and step != row["upload_step"],
                "An independently reviewed Bazel build step is required")
    return proofs


def storage_budget(value):
    """Use original member sizes; the pinned upstream action streams ZIP extraction."""
    rows = transfers(value)
    terms = {"artifact_expanded_bytes": sum(row["expanded_bytes"] for row in rows),
             "destination_copy_bytes": sum(value["files"][target]["bytes"]
                for row in rows for target in row["members"].values()),
             "assembly_reserve_bytes": ASSEMBLY_RESERVE_BYTES}
    return {**terms, "required_free_bytes": sum(terms.values())}


def download_groups(value):
    """Group original IDs for the upstream action's run-scoped lookup."""
    grouped = {}
    for row in transfers(value):
        grouped.setdefault(row["run_id"], []).append(row)
    return [{"run_id": run, "artifact_ids": [row["artifact_id"] for row in rows],
             # Upstream flattens a single artifact. Preserve its exact member root.
             "directory": rows[0]["name"] if len(rows) == 1 else ""}
            for run, rows in sorted(grouped.items())]


def check_producer(row, artifact, run, job):
    require(artifact["id"] == row["artifact_id"] and artifact["name"] == row["name"] and
            artifact["digest"] == row["digest"] and artifact["size_in_bytes"] == row["zip_bytes"] and
            artifact["expired"] is False and artifact["workflow_run"]["id"] == row["run_id"] and
            artifact["workflow_run"]["head_sha"] == row["head"], "Original artifact metadata differs")
    require(run["id"] == row["run_id"] and run["run_attempt"] == row["attempt"] and
            run["head_sha"] == row["head"] and run["event"] == row["event"] and
            run["path"] == row["workflow"] and run["status"] == "completed" and
            run["repository"]["full_name"] == "securely1g/sonic-buildimage" and
            run["actor"]["login"] == run["triggering_actor"]["login"] == "securely1g",
            "Original producer run differs")
    require(job["id"] == row["job_id"] and job["run_id"] == row["run_id"] and
            job["run_attempt"] == row["attempt"] and job["head_sha"] == row["head"] and
            job["name"] == row["job_name"] and job["status"] == "completed",
            "Original upload job differs")
    steps = [step for step in job["steps"] if step["name"] == row["upload_step"]]
    require(len(steps) == 1 and steps[0]["status"] == "completed" and steps[0]["conclusion"] == "success",
            "Original artifact upload did not succeed")
    require(timestamp(job["started_at"]) <= timestamp(artifact["created_at"]) <= timestamp(job["completed_at"]),
            "Artifact was not created during the bound upload job")
    # A later aggregate failure does not erase a successful immutable upload.
    # Canonical native/kernel/package content acceptance still runs separately.
    return {"artifact": artifact, "run": run, "job": job,
            "metadata_digest_matched": True, "raw_zip_digest_recomputed_here": False}


def admit(value, api_read=api):
    result, runs, jobs = {}, {}, {}
    builds = bazel_builds(value)
    for row in transfers(value):
        artifact = api_read("/actions/artifacts/" + str(row["artifact_id"]))
        key = (row["run_id"], row["attempt"])
        if key not in runs:
            runs[key] = api_read("/actions/runs/%s/attempts/%s" % key)
        if row["job_id"] not in jobs:
            jobs[row["job_id"]] = api_read("/actions/jobs/" + str(row["job_id"]))
        accepted = check_producer(row, artifact, runs[key], jobs[row["job_id"]])
        accepted["bazel_archive_builds"] = {}
        for target, proof in builds.items():
            if proof["artifact_id"] != row["artifact_id"]:
                continue
            steps = [step for step in jobs[row["job_id"]]["steps"]
                     if step["name"] == proof["build_step"]]
            require(len(steps) == 1 and steps[0]["status"] == "completed"
                    and steps[0]["conclusion"] == "success",
                    "Original Bazel build step did not succeed: " + target)
            upload = next(step for step in jobs[row["job_id"]]["steps"]
                          if step["name"] == row["upload_step"])
            require(jobs[row["job_id"]]["steps"].index(steps[0])
                    < jobs[row["job_id"]]["steps"].index(upload),
                    "Original Bazel build step follows its artifact upload: " + target)
            accepted["bazel_archive_builds"][target] = proof
        result[str(row["artifact_id"])] = accepted
    return result


def stage(workspace, inbox, value):
    rows = transfers(value)
    require(workspace.resolve() == workspace.absolute() and inbox.resolve() == inbox.absolute(),
            "Staging roots must not traverse a symlink")
    # Keep source files immutable; transfers may only create absent input paths.
    for name in value["groups"]["source"]:
        require(policy.file_digest(policy.safe(workspace, name)) == value["files"][name],
                "Checked-in source differs: " + name)
    sources = []
    for row in rows:
        for member, name in row["members"].items():
            source = policy.safe(inbox, row["name"] + "/" + member)
            require(policy.file_digest(source) == value["files"][name], "Downloaded original differs: " + name)
            target = workspace
            for part in Path(name).parts:
                target = target / part
                require(not target.is_symlink(), "Symlink in destination")
            require(not target.exists(), "Preserve existing destination: " + name)
            sources.append((source, target, value["files"][name]))
    for source, target, expected in sources:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode), "Original is not regular")
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "rb", closefd=False) as src, target.open("xb") as dst:
                for block in iter(lambda: src.read(1024**2), b""):
                    size += len(block)
                    require(size <= expected["bytes"], "Original grew during staging")
                    digest.update(block)
                    dst.write(block)
            after = os.fstat(fd)
            identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
            require(identity(before) == identity(after) and
                    {"bytes": size, "sha256": digest.hexdigest()} == expected, "Original changed during staging")
        finally:
            os.close(fd)
    return {"status": "ORIGINAL_FILE_BYTES_STAGED_ONLY", "files": len(sources),
            "kernel_native_package_acceptance": False, "VS_qualified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("admit", "stage"))
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--merge", required=True)
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--inbox", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.require_environment = False
    root, value = policy.inventory(args)
    require(not args.output.exists() and not args.output.is_symlink(), "Preserve prior staging evidence")
    if args.mode == "admit":
        require(args.inbox is not None and args.inbox.is_dir()
                and args.inbox.resolve() == args.inbox.absolute(),
                "A fresh original-download directory is required before admission")
        temporary = Path(os.environ["RUNNER_TEMP"])
        require(root.stat().st_dev == args.inbox.stat().st_dev == temporary.stat().st_dev,
                "Download, assembly and temporary paths must share the checked backing storage")
        budget = storage_budget(value)
        budget["observed_free_bytes"] = shutil.disk_usage(root).free
        require(budget["observed_free_bytes"] >= budget["required_free_bytes"],
                "Insufficient staging space: %d free bytes, %d required for original members, destination copies and90GiB assembly reserve"
                % (budget["observed_free_bytes"], budget["required_free_bytes"]))
        result = {"status": "ORIGINAL_ARTIFACT_METADATA_ADMITTED_ONLY", "producers": admit(value),
                  "download_groups": download_groups(value), "storage": budget}
    else:
        require(args.inbox is not None, "Original download directory required")
        result = stage(root, args.inbox, value)
        policy.inputs(args)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
