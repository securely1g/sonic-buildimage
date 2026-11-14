#!/usr/bin/env python3
"""Run the existing installed FRR probes against this checkout's validated images."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.artifact_validation import require
from tools.bazel.ci.validate_runtime import load_archive, verify_loaded_image


def run_loaded(directory, source=ROOT):
    directory = directory.resolve()
    receipt = json.loads((directory / "validation.json").read_text())
    require(receipt["status"] == "passed" and receipt["architecture"] == "amd64",
            "FRR static validation did not pass")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    require(receipt["revision"] == revision, "FRR runtime checkout differs from built source")
    if source != ROOT:
        headers = subprocess.check_output(["git", "cat-file", "-p", "HEAD"], cwd=source, text=True)
        parents = [line.split()[1] for line in headers.split("\n\n", 1)[0].splitlines() if line.startswith("parent ")]
        require(receipt["tested_revision_parents"] == parents, "FRR runtime checkout parents differ")
    images = ("docker-fpm-frr", "docker-fpm-frr-dbg")
    for name in images:
        path = directory / (name + ".gz")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == receipt["artifacts"][path.name]["sha256"],
                "FRR image archive changed")
        expected = json.loads((directory / (name + ".validation.json")).read_text())
        tag = "sonic-frr-validation-" + name + ":latest"
        actual, config, _ = load_archive(path, tag, expected["config_digest"])
        verify_loaded_image(actual, config, expected["config_digest"])
        require(actual["Os"] == "linux" and actual["Architecture"] == "amd64", "FRR loaded platform differs")
        require(actual["RootFS"]["Layers"] == expected["rootfs"]["diff_ids"], "FRR loaded layers differ")
    redis = directory / "test-redis.tar"
    require(hashlib.sha256(redis.read_bytes()).hexdigest() == receipt["artifacts"][redis.name]["sha256"],
            "FRR Redis fixture changed")
    fixture = directory / "redis-fixture"
    fixture.mkdir(exist_ok=True)
    subprocess.run(["tar", "--no-same-owner", "-xf", str(redis), "-C", str(fixture)], check=True)
    for name in images:
        command = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--read-only", "--user=300:300", "--group-add=102", "--pids-limit=128", "--memory=4g", "--cpus=2",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m",
                   "--mount", "type=bind,src=" + str(source / "dockers/docker-fpm-frr/bazel/runtime_probe.py") + ",dst=/probe/runtime_probe.py,readonly",
                   "--mount", "type=bind,src=" + str(fixture) + ",dst=/probe/redis,readonly",
                   "--mount", "type=bind,src=" + str(directory / "validation.json") + ",dst=/probe/validation.json,readonly",
                   "--entrypoint", "/usr/bin/python3", "sonic-frr-validation-" + name + ":latest",
                   "-B", "/probe/runtime_probe.py", "--validation", "/probe/validation.json"]
        if name.endswith("-dbg"):
            command.append("--debug")
        report = directory / (name + ".runtime.json")
        with report.open("w") as stream:
            try:
                subprocess.run(command, stdout=stream, check=True)
            finally:
                stream.flush()
                print(report.read_text(), end="")
        require(json.loads(report.read_text()).get("status") == "passed", "Installed FRR probe did not pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=ROOT)
    args = parser.parse_args()
    run_loaded(args.artifacts, args.source.resolve())


if __name__ == "__main__":
    main()
