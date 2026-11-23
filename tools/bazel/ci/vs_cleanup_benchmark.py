"""Validate explicit, isolated VS cleanup benchmark modes and cache inputs."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


PINS = ("VS_BENCHMARK_HOST_BASE_ARTIFACT", "VS_BENCHMARK_KERNEL_ARTIFACT")
WORKFLOW = ".github/workflows/bazel-oci.yml"
WORK_ROOT = Path("/mnt/build/actions-runner-vs-canary-work")


def configuration(env):
    arm = env.get("VS_CLEANUP_BENCHMARK_ARM") or "none"
    if arm == "none":
        if any(env.get(key) for key in PINS):
            raise ValueError("Artifact pins require an explicit benchmark arm")
        return arm
    if (arm not in ("A", "B", "C")
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("VS_BENCHMARK_CANARY") != "true"
            or env.get("VS_BENCHMARK_SKIP_VS") != "false"
            or env.get("VS_BENCHMARK_SOURCE_IMAGE") != "none"):
        raise ValueError("Benchmark requires a manual native VS canary dispatch")
    if any(not re.fullmatch(r"0|[1-9][0-9]*", env.get(key, "")) for key in PINS):
        raise ValueError("Pin both cache artifact IDs; use 0 explicitly for no artifact")
    return arm


def pinned_artifact(api, env, name, key):
    """Select an exact previous master artifact, never a latest/fallback input."""
    if configuration(env) == "none":
        raise ValueError("Pinned lookup requires an explicit benchmark arm")
    pin = env[key]
    if pin == "0":
        return None
    artifact = api(f"artifacts/{pin}")
    producer = (artifact.get("workflow_run") or {}).get("id")
    if (type(artifact.get("id")) is not int or artifact["id"] != int(pin)
            or artifact.get("name") != name
            or artifact.get("expired") is not False
            or type(producer) is not int or producer <= 0
            or datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00"))
            <= datetime.now(timezone.utc)):
        raise ValueError("Pinned cache artifact is unavailable or has the wrong identity")
    workflow = api("workflows/bazel-oci.yml")
    run = api(f"runs/{producer}")
    if (workflow.get("path") != WORKFLOW or type(workflow.get("id")) is not int
            or workflow["id"] <= 0 or run.get("workflow_id") != workflow["id"]
            or run.get("path") != WORKFLOW or run.get("event") != "push"
            or run.get("head_branch") != "master"
            or (run.get("repository") or {}).get("full_name") != env["GITHUB_REPOSITORY"]
            or (run.get("head_repository") or {}).get("full_name") != env["GITHUB_REPOSITORY"]
            or type(run.get("run_number")) is not int
            or not 0 < run["run_number"] < int(env["GITHUB_RUN_NUMBER"])):
        raise ValueError("Pinned cache must come from this workflow's previous trusted master push")
    return artifact


def validate_lifecycle(env, status, settings_path=None, work_root=None):
    arm = configuration(env)
    if arm == "none":
        return
    expected_status = "active" if arm == "C" else "inactive"
    enrolled = env.get("VS_BENCHMARK_ENROLLED") == "true"
    if status != expected_status or enrolled != (arm == "C"):
        raise ValueError("Benchmark lifecycle/deferral differs from the requested arm")
    package = "vs-canary" if arm == "C" else "vs-benchmark"
    settings_path = settings_path or Path(f"/opt/actions-runner-{package}/.runner")
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    folder = Path(settings["workFolder"])
    if not folder.is_absolute():
        folder = settings_path.parent / folder
    folder = folder.resolve(strict=True)
    if (settings.get("ephemeral", False) is not (arm == "C")
            or settings.get("agentName") != env["RUNNER_NAME"]
            or folder != (work_root or WORK_ROOT).resolve(strict=True)
            or folder
            != Path(env["RUNNER_WORKSPACE"]).parent.resolve(strict=True)):
        raise ValueError("Benchmark registration is not the requested isolated runner profile")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("inputs", "lifecycle"))
    args = parser.parse_args()
    arm = configuration(os.environ)
    if arm == "none":
        return
    if args.phase == "lifecycle":
        result = subprocess.run(
            ["systemctl", "is-active", "sonic-vs-cleanup.service"],
            capture_output=True, text=True, timeout=15)
        validate_lifecycle(os.environ, result.stdout.strip())
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            print("builder_cleanup=" + ("n" if arm == "A" else "y"), file=output)
    print(f"Validated VS cleanup benchmark arm {arm}")


if __name__ == "__main__":
    main()
