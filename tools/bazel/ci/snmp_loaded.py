#!/usr/bin/env python3
"""Run the existing installed SNMP probes against this checkout's validated images."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.artifact_validation import require


def run_loaded(directory):
    directory = directory.resolve()
    receipt = json.loads((directory / "validation.json").read_text())
    require(receipt["status"] == "passed" and receipt["architecture"] == "amd64",
            "SNMP static validation did not pass")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(receipt["revision"] == revision, "SNMP runtime checkout differs from built source")
    images = ("docker-snmp", "docker-snmp-dbg")
    for name in images:
        path = directory / (name + ".gz")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == receipt["artifacts"][path.name]["sha256"],
                "SNMP image archive changed")
        subprocess.run(["docker", "load", "--input", str(path)], check=True)
        actual = json.loads(subprocess.check_output(["docker", "image", "inspect", name + ":latest"]))[0]
        expected = json.loads((directory / (name + ".validation.json")).read_text())
        require(actual["Os"] == "linux" and actual["Architecture"] == "amd64", "SNMP loaded platform differs")
        require(actual["RootFS"]["Layers"] == expected["rootfs"]["diff_ids"], "SNMP loaded layers differ")
        meaningful = lambda values: {key: value for key, value in values.items() if value not in (None, "", [], {}, False)}
        require(meaningful(actual["Config"]) == meaningful(expected["config"]), "SNMP loaded configuration differs")
    redis = directory / "test-redis.tar"
    require(hashlib.sha256(redis.read_bytes()).hexdigest() == receipt["artifacts"][redis.name]["sha256"],
            "SNMP Redis fixture changed")
    fixture = directory / "redis-fixture"
    fixture.mkdir(exist_ok=True)
    subprocess.run(["tar", "--no-same-owner", "-xf", str(redis), "-C", str(fixture)], check=True)
    for name in images:
        command = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--read-only", "--user=101:102", "--pids-limit=128", "--memory=4g", "--cpus=2",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m",
                   "--mount", "type=bind,src=" + str(ROOT / "dockers/docker-snmp/bazel/runtime_probe.py") + ",dst=/probe/runtime_probe.py,readonly",
                   "--mount", "type=bind,src=" + str(fixture) + ",dst=/probe/redis,readonly",
                   "--entrypoint", "/usr/bin/python3", name + ":latest", "-B", "/probe/runtime_probe.py"]
        report = directory / (name + ".runtime.json")
        with report.open("w") as stream:
            try:
                subprocess.run(command, stdout=stream, check=True)
            finally:
                stream.flush()
                print(report.read_text(), end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    run_loaded(parser.parse_args().artifacts)


if __name__ == "__main__":
    main()
