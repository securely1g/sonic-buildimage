#!/usr/bin/env python3
"""Check inherited Common runtime and symbols in the final SWSS images."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import artifact_validation as validation

COMMON = "usr/lib/x86_64-linux-gnu/libswsscommon.so.0.0.0"
DATABASE = "run/redis/sonic-db/database_config.json"
TAGS = ("docker-config-engine-trixie", "docker-orchagent", "docker-orchagent-dbg")
PROBE = r'''
import hashlib, json, os, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
def file(name):
    path = root / name
    value = path.lstat()
    assert stat.S_ISREG(value.st_mode), name
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(value.st_mode), "uid": value.st_uid, "gid": value.st_gid}
print(json.dumps({"var_run": os.readlink(root / "var/run"),
                  "common": file(sys.argv[2]), "database": file(sys.argv[3])}))
'''


def command(arguments):
    return validation.command(arguments)


def verify_inheritance(images, reports):
    """Bind both SWSS variants to the actual source-base image and payload."""
    for image in images:
        validation.require(image.get("Os") == "linux" and image.get("Architecture") == "amd64",
                           "expected native Linux AMD64 images")
    for parent, child in zip(images, images[1:]):
        layers = parent["RootFS"]["Layers"]
        validation.require(layers and child["RootFS"]["Layers"][:len(layers)] == layers,
                           "image does not retain its parent's exact layers")
    for report in reports:
        validation.require(report["var_run"] == "/run", "image changed /var/run -> /run")
        validation.require(report["database"]["mode"] == 0o644 and
                           report["database"]["uid"] == report["database"]["gid"] == 0,
                           "database config must remain root-owned 0644")
        validation.require(report["common"] == reports[0]["common"],
                           "SWSS changed inherited Common bytes or metadata")
        validation.require(report["database"] == reports[0]["database"],
                           "SWSS changed inherited database configuration")


def verify(archives):
    """Use a private Docker daemon; create only uniquely named inspection containers."""
    images, reports, containers = [], [], []
    try:
        for tag, archive in zip(TAGS, archives):
            command(["docker", "load", "--input", str(archive)])
            image = json.loads(command(["docker", "image", "inspect", tag + ":latest"]))[0]
            images.append(image)
            container = command([
                "docker", "create", "--name", "swss-common-check-" + uuid.uuid4().hex,
                "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=32", "--memory=256m",
                "--entrypoint=/usr/bin/python3", image["Id"], "-B", "-c", PROBE,
                "/", COMMON, DATABASE,
            ]).strip()
            containers.append(container)
            report = command(["docker", "start", "--attach", container])
            state = json.loads(command(["docker", "inspect", container]))[0]["State"]
            validation.require(state["ExitCode"] == 0, "installed-image probe failed")
            reports.append(json.loads(report))
        verify_inheritance(images, reports)
        with tempfile.TemporaryDirectory(prefix="swss-common-debug-") as temporary:
            extracted = Path(temporary)
            binary = extracted / COMMON
            binary.parent.mkdir(parents=True)
            command(["docker", "cp", containers[1] + ":/" + COMMON, str(binary)])
            validation.require(validation.sha(binary) == reports[1]["common"]["sha256"],
                               "copied Common differs from the runtime image")
            match = re.search(r"Build ID: ([0-9a-f]+)", command(["readelf", "-n", str(binary)]))
            validation.require(match is not None, "inherited Common lacks a build ID")
            identifier = match[1]
            debug_name = "usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug"
            symbols = extracted / debug_name
            symbols.parent.mkdir(parents=True)
            command(["docker", "cp", containers[2] + ":/" + debug_name, str(symbols)])
            with binary.open("rb") as source:
                metadata = validation.file_metadata(source, COMMON)
            validation.require(metadata.get("elf_machine") == 62, "Common is not AMD64 ELF")
            pairs, gaps = validation.elf_debug(extracted, {COMMON: metadata, debug_name: {}}, set())
        return {"status": "passed", "scope": "installed files and symbols; no service boot",
                "images": {tag: image["Id"] for tag, image in zip(TAGS, images)},
                "archives": {path.name: validation.sha(path) for path in archives},
                "common": reports[0]["common"], "database": reports[0]["database"],
                "var_run": "/run", "debug_pairs": pairs}
    finally:
        for container in containers:
            subprocess.run(["docker", "rm", "--force", container], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path("target"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify([args.target / (tag + ".gz") for tag in TAGS])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
