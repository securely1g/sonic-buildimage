#!/usr/bin/env python3
"""Exact-source receipts for the disposable November 2 master backfill branch."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import urllib.request

REPOSITORY = "securely1g/sonic-buildimage"
INPUTS = {
    "8bf48af011622657a73570a54d1a660b821bb30a": (
        40634292391, 12792399274,
        "sha256:54e87182feb71d8ec3d9b844e9099e72bbcff4e28593b1b119a4b2e1d12a42ec"),
    "13d750dee1750ee1c325e0fea9b7b4eaaf10cb14": (
        40634543155, 12792534883,
        "sha256:b357dc6d701790413385c66411e9736f5f4b275588c71128d6441160a0d83cd9"),
    "2dc503aaecfb0d3c317608aaeabc68a108e32b97": (
        40634671547, 12793195886,
        "sha256:43570784d046c6ba2058e2f8a569c7dd35b90edf8f58b6fdbbd9362bc7ee54b1"),
    "c20b1d43e26c6eb2928191f041b78a60251ef9a0": (
        41423968669, 13130803809,
        "sha256:2c226f2764aec2cc960b24966bfa503118bafa205108fc2c32056d140211ae5c"),
}
P4_INPUTS = {
    "c20b1d43e26c6eb2928191f041b78a60251ef9a0": (
        13130893268, "p4-source-inputs-41423968669-1",
        "sha256:9102a9108ce7f4c9ef7620d45940d5acd3eb729232f9871357d18dc58d2bf068"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_inputs(source, run, artifact, p4_artifact=None):
    run_id, artifact_id, digest = INPUTS[source]
    require(run["id"] == run_id and run["head_sha"] == source,
            "Original run does not match the pinned source")
    require(run["repository"]["full_name"] == REPOSITORY and
            run["event"] == "push" and run["head_branch"] == "master",
            "Original run is not a master push in the expected repository")
    require(artifact["id"] == artifact_id and
            artifact["name"] == "bazel-repository-inputs" and
            artifact["digest"] == digest and not artifact["expired"],
            "Original input artifact identity, digest or availability changed")
    require(artifact["workflow_run"]["id"] == run_id and
            artifact["workflow_run"]["head_sha"] == source,
            "Input artifact belongs to another source run")
    receipt = {"source_sha": source, "original_run_id": run_id,
               "input_artifact_id": artifact_id, "input_artifact_digest": digest,
               "input_artifact_expires_at": artifact["expires_at"],
               "prefer_debian_repo": "y" if source in P4_INPUTS else "n",
               "native_docker_apt_retries": "0" if source in P4_INPUTS else ""}
    if source in P4_INPUTS:
        p4_id, p4_name, p4_digest = P4_INPUTS[source]
        require(isinstance(p4_artifact, dict), "Pinned P4 source artifact is required")
        require(p4_artifact["id"] == p4_id and p4_artifact["name"] == p4_name and
                p4_artifact["digest"] == p4_digest and not p4_artifact["expired"],
                "P4 source artifact identity, digest or availability changed")
        require(p4_artifact["workflow_run"]["id"] == run_id and
                p4_artifact["workflow_run"]["head_sha"] == source,
                "P4 source artifact belongs to another source run")
        receipt.update(p4_artifact_id=p4_id, p4_artifact_digest=p4_digest,
                       p4_artifact_expires_at=p4_artifact["expires_at"])
    else:
        require(p4_artifact is None, "Historical source has no P4 artifact transport")
    return receipt


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def git(workspace, *arguments):
    return subprocess.check_output(["git", "-C", str(workspace), *arguments], text=True).strip()


def identity(source, workspace, after_build):
    require(git(workspace, "rev-parse", "HEAD") == source, "Checkout HEAD changed")
    # Make legitimately patches files inside pinned submodule worktrees. Those
    # transformations must not be confused with switching a submodule revision.
    subprocess.run(["git", "-C", str(workspace), "diff", "--exit-code", "HEAD",
                    "--ignore-submodules=dirty" if after_build else "--ignore-submodules=none"],
                   check=True)
    submodules = git(workspace, "submodule", "status", "--recursive").splitlines()
    require(bool(submodules) and all(line and line[0] not in "+-U" for line in submodules),
            "A submodule is missing, conflicted, or moved from its pinned revision")
    return {"source_sha": source, "source_tree": git(workspace, "rev-parse", "HEAD^{tree}"),
            "submodules": submodules, "stage": "after-build" if after_build else "before-build",
            "tracked_superproject_unchanged": True,
            "submodule_commits_unchanged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inputs", "identity"))
    parser.add_argument("--source-sha", required=True, choices=INPUTS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--after-build", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.command == "inputs":
        run_id, artifact_id, _ = INPUTS[args.source_sha]
        p4_artifact = (api(f"actions/artifacts/{P4_INPUTS[args.source_sha][0]}")
                       if args.source_sha in P4_INPUTS else None)
        receipt = check_inputs(args.source_sha, api(f"actions/runs/{run_id}"),
                               api(f"actions/artifacts/{artifact_id}"), p4_artifact)
        args.output.mkdir(parents=True, exist_ok=True)
        path = args.output / "inputs.json"
        if args.github_output:
            with args.github_output.open("a") as output:
                output.write(f"run_id={run_id}\nartifact_id={artifact_id}\n")
                output.write(f"p4_artifact_id={receipt.get('p4_artifact_id', '')}\n")
                output.write(f"prefer_debian_repo={receipt['prefer_debian_repo']}\n")
                output.write(f"native_docker_apt_retries={receipt['native_docker_apt_retries']}\n")
    else:
        require(args.workspace is not None, "identity requires --workspace")
        receipt = identity(args.source_sha, args.workspace, args.after_build)
        path = args.output
    receipt.update(driver_sha=os.environ.get("GITHUB_SHA"),
                   repository=REPOSITORY, run_id=os.environ.get("GITHUB_RUN_ID"),
                   run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
                   runner_name=os.environ.get("RUNNER_NAME"),
                   target="target/sonic-vs.bin", architecture="amd64",
                   execution="native_sonic_slave", package_cache="worker_private",
                   runner_environment={key: os.environ.get(key) for key in (
                       "DEFAULT_CONTAINER_REGISTRY", "MIRROR_URLS", "MIRROR_SECURITY_URLS",
                       "BAZEL", "BAZEL_CONTAINER_ARGS", "SONIC_BAZEL_CACHE_SOURCE",
                       "SONIC_DPKG_CACHE_SOURCE", "SONIC_DPKG_CACHE_METHOD",
                       "SONIC_CONFIG_USE_DOCKER_CACHE", "SONIC_BUILD_MEMORY",
                       "SONIC_BUILD_MEMORY_SWAP", "SECURE_UPGRADE_MODE",
                       "ENABLE_MULTIDB", "ENABLE_VRF_STRICT", "PREFER_DEBIAN_REPO",
                       "SONIC_DOCKER_APT_RETRIES")},
                   build_flags={"BUILD_WITH_BAZEL_WHEN_AVAILABLE": "y",
                                "INCLUDE_VS_DASH_SAI": "y", "INCLUDE_FIPS": "y",
                                "ENABLE_ASAN": "n", "ENABLE_SYNCD_RPC": "n",
                                "SONIC_CONFIG_BUILD_JOBS": "1", "SONIC_CONFIG_MAKE_JOBS": "4"},
                   scope=("ONIE installer and original c20 source-package/archive-pair checks; no KVM boot"
                          if args.source_sha in P4_INPUTS else
                          "ONIE installer build; no KVM boot or newer image-runtime checks"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
