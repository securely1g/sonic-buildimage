#!/usr/bin/env python3
"""Run the existing installed FRR probes against this checkout's validated images."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.ci.frr_image import IMAGES
from tools.bazel.ci.sysmgr_image import source_identity
from tools.bazel.ci.validate_runtime import load_archive, verify_loaded_image


def run_loaded(directory):
    directory = directory.resolve()
    receipt = json.loads((directory / "validation.json").read_text())
    require(receipt["status"] == "passed" and receipt["architecture"] == "amd64",
            "FRR static validation did not pass")
    identity = source_identity()
    require(receipt["revision"] == identity["revision"]
            and receipt["tested_revision_parents"] == identity["tested_revision_parents"],
            "installed runtime evidence belongs to a different checkout")
    for name in IMAGES:
        path = directory / (name + ".gz")
        require(sha(path) == receipt["artifacts"][path.name]["sha256"],
                "FRR image archive changed")
        expected = json.loads((directory / (name + ".validation.json")).read_text())
        tag = "sonic-frr-validation-" + name + ":latest"
        actual, config, _ = load_archive(path, tag, expected["config_digest"])
        verify_loaded_image(actual, config, expected["config_digest"])
        require(actual["Os"] == "linux" and actual["Architecture"] == "amd64", "FRR loaded platform differs")
        require(actual["RootFS"]["Layers"] == expected["rootfs"]["diff_ids"], "FRR loaded layers differ")
    redis = directory / "test-redis.tar"
    require(sha(redis) == receipt["artifacts"][redis.name]["sha256"],
            "FRR Redis fixture changed")
    fixture = directory / "redis-fixture"
    fixture.mkdir(exist_ok=True)
    subprocess.run(["tar", "--no-same-owner", "-xf", str(redis), "-C", str(fixture)], check=True)
    for name in IMAGES:
        command = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--read-only", "--user=300:300", "--group-add=102", "--pids-limit=128", "--memory=4g", "--cpus=2",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m",
                   "--mount", "type=bind,src=" + str(ROOT / "dockers/docker-fpm-frr/bazel/runtime_probe.py") + ",dst=/probe/runtime_probe.py,readonly",
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
    for name in IMAGES:
        report = json.loads((directory / (name + ".runtime.json")).read_text())
        require(report.get("status") == "passed", f"{name} did not pass installed service validation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    run_loaded(args.artifacts)


if __name__ == "__main__":
    main()
