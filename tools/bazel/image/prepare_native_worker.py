#!/usr/bin/env python3
"""Build the declared native assembly worker and freeze its verified inputs."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


def native_tag(output):
    names = re.findall(r"^sonic-slave-trixie:[0-9a-f]{11}$", output, re.M)
    if len(names) != 1:
        raise ValueError("Expected exactly one native Trixie sonic-slave image tag")
    return names[0]


def worker_spec(base, worker, worker_id):
    if (not re.fullmatch(r"sha256:[0-9a-f]{64}", worker_id)
            or worker.get("Id") != worker_id
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", base.get("Id", ""))):
        raise ValueError("Expected immutable native and assembly worker image identities")
    layers = base["RootFS"]["Layers"]
    if not layers or worker["RootFS"]["Layers"][:len(layers)] != layers:
        raise ValueError("Assembly worker does not retain the native sonic-slave layers")
    return {
        "schema": 1, "platform": "linux/amd64", "distribution": "trixie",
        "worker_image": worker_id, "sonic_slave_image": base["Id"],
        "docker_version": "28.5.2", "containerd_version": "1.7.28",
        "storage_driver": "overlay2",
    }


def prepare(workspace, archives_dir, evidence_dir):
    workspace = workspace.resolve(strict=True)
    archives_dir = (workspace / archives_dir).resolve(strict=True)
    evidence_dir = workspace / evidence_dir
    target = workspace / "target"
    target.mkdir(exist_ok=True)

    def command(argv, **kwargs):
        subprocess.run(argv, cwd=workspace, check=True, **kwargs)

    def inspect(image):
        records = json.loads(subprocess.check_output(
            ["docker", "image", "inspect", image], cwd=workspace, text=True))
        if len(records) != 1:
            raise ValueError("Expected exactly one inspected Docker image")
        return records[0]

    command(["bash", "tools/bazel/image/worker/prepare-worker-inputs.sh"])
    tag_log = target / "sonic-slave-tag.log"
    with tag_log.open("w") as stream:
        command(["make", "-s", "-f", "Makefile.work", "BLDENV=trixie", "showtag"],
                stdout=stream, text=True)
    tag = native_tag(tag_log.read_text())
    base = inspect(tag)
    command(["docker", "build", "--build-arg", "BASE_IMAGE=" + tag,
             "--iidfile", "target/bazel-image-worker.id", "tools/bazel/image/worker"])
    worker_id = (target / "bazel-image-worker.id").read_text().strip()
    spec = worker_spec(base, inspect(worker_id), worker_id)
    (target / "bazel-image-worker.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")

    command([sys.executable, "tools/bazel/image/prepare_inputs.py",
             "--native-directory", "target/bazel-native", "--installer-source", str(workspace),
             "--execution-environment", "target/bazel-image-worker.json",
             "--output", "target/bazel-image-inputs"])
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with (evidence_dir / "assembly-source-download-receipt.json").open("w") as stream:
        command([sys.executable, "-B", "tools/bazel/gzip/source_archive.py",
                 "--archives-dir", str(archives_dir),
                 "--output", "target/bazel-image-inputs/source-downloads"],
                stdout=stream, text=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, default=Path("artifacts/unprivileged"))
    args = parser.parse_args(argv)
    prepare(Path.cwd(), args.archives_dir, args.evidence_dir)


if __name__ == "__main__":
    main()
