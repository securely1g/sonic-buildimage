#!/usr/bin/env python3
"""Build a content-only store without privileges, then check it on a runtime.

The build mode must run as a nonroot user with no capabilities and
no-new-privileges. The separate check-runtime mode needs a disposable privileged
container to run the resulting images; it never rebuilds the store being tested.
Neither mode accesses a host Docker socket or downloads an image.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import containerd_import as ci
import containerd_store as store
import oci_bundle
from containerd_acceptance_test import Lab, runtime_inputs


def confinement():
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    ci.require(os.geteuid() != 0, "assembly acceptance must run as a nonroot user")
    for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        ci.require(int(status[name].strip(), 16) == 0, "assembly must drop all capabilities: " + name)
    ci.require(status["NoNewPrivs"].strip() == "1", "assembly requires no-new-privileges")
    ci.require(status["Seccomp"].strip() == "2", "assembly requires the container seccomp filter")
    for socket in ("/var/run/docker.sock", ci.ADDRESS):
        ci.require(not Path(socket).exists(), "assembly must not receive a host runtime socket")
    return {"uid": os.getuid(), "gid": os.getgid(), "capabilities": "none",
            "no_new_privileges": True, "seccomp_filter": True, "host_runtime_socket": False}


def check_tar_owners(archive):
    process = subprocess.Popen(["zstd", "--decompress", "--quiet", "--stdout", str(archive)], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
            count = 0
            for member in stream:
                ci.require(member.uid == member.gid == 0, "containerd store must be installed as root")
                ci.require(member.isfile() or member.isdir(), "content-only store contains a special file")
                count += 1
        ci.require(process.wait(timeout=60) == 0 and count > 0, "store archive inspection failed")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    return count


def image_sources(root):
    return ((root / "input.oci.tar", "oci-archive", "docker-oci-probe"),
            (root / "input.docker.tar.gz", "docker-archive", "docker-archive-probe"))


def part_identity(part):
    return {path.relative_to(part).as_posix(): ci.sha256(path)
            for path in sorted(part.rglob("*")) if path.is_file()}


def build(root):
    restricted = confinement()
    root.mkdir(parents=True, exist_ok=False)
    expected = json.loads(Path("/run/sonic-image-worker.json").read_text())
    architecture = store.environment(expected, expected)
    layout, docker_archive, configurations = runtime_inputs(root, architecture)
    # Model the archive emitted by an OCI image action, including root-owned
    # headers. Conversion must not attempt chown while reading those headers.
    subprocess.run(["tar", "--create", "--file", str(root / "input.oci.tar"),
                    "--directory", str(layout), "--owner=0", "--group=0", "."], check=True)
    parts, by_tag = [], {}
    for source, transport, name in image_sources(root):
        part = root / name
        record = oci_bundle.convert(source, transport, name, architecture, "sonic-version", part)
        by_tag.update({tag: configurations["oci" if transport == "oci-archive" else transport]
                       for tag in record["tags"]})
        # Match the TreeArtifact layout seen by the real processwrapper action:
        # regular directories whose declared file leaves are symlinks.
        sandbox_part = root / "sandbox-parts" / name
        sandbox_part.mkdir(parents=True)
        for source_file in part.iterdir():
            (sandbox_part / source_file.name).symlink_to(source_file)
        parts.append(sandbox_part)
    (root / "expected-runtime.json").write_bytes(oci_bundle.canonical(by_tag))
    producer = Path(__file__).with_name("containerd_store.py")
    specification = root / "execution-environment.json"
    specification.write_bytes(oci_bundle.canonical(expected))

    def produce(label, part, base=None):
        archive, receipt = root / (label + ".tar.gz"), root / (label + ".json")
        command = [sys.executable, str(producer), "--execution-environment", str(specification),
                   "--part", str(part), "--output", str(archive), "--receipt", str(receipt), "--jobs", "2"]
        if base:
            command += ["--base", str(base[0]), "--base-receipt", str(base[1])]
        subprocess.run(command, check=True)
        check_tar_owners(archive)
        return archive, receipt

    base = produce("base-store", parts[0])
    before = [ci.sha256(path) for path in base]
    final = produce("dockerfs", parts[1], base)
    ci.require(before == [ci.sha256(path) for path in base], "cached base changed during private restore")
    receipt = json.loads(final[1].read_text())
    ci.require(receipt["producer_uid"] == restricted["uid"] and receipt["imported_images"] == 1,
               "incremental store must be imported by the unprivileged producer")
    ci.require(len(receipt["images"]) == 2 and len(receipt["tags"]) == 5 and receipt["unpacked"] is False,
               "unexpected completed store inventory")
    report = {"schema": 1, "passed": True, "scope": "unprivileged base and incremental store assembly",
              "confinement": restricted, "execution_environment": expected,
              "base_unchanged": True, "images": 2, "tags": 5,
              "sandbox_input_links": True,
              "store_sha256": ci.sha256(final[0]), "archive_owner": "0:0",
              "producer_sha256": ci.sha256(producer)}
    (root / "rootless-report.json").write_bytes(oci_bundle.canonical(report))
    print(json.dumps(report, indent=2))


def check_runtime(root):
    ci.require(os.geteuid() == 0 and not Path(ci.ADDRESS).exists(),
               "runtime acceptance needs a fresh privileged container")
    proof = json.loads((root / "rootless-report.json").read_text())
    ci.require(proof.get("passed") is True and proof["confinement"]["uid"] != 0,
               "a successful unprivileged producer receipt is required")
    archive = root / "dockerfs.tar.gz"
    receipt_path = root / "dockerfs.json"
    expected = json.loads((root / "execution-environment.json").read_text())
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    receipt = store.base_inventory(archive, receipt_path, expected, architecture)
    ci.require(proof["store_sha256"] == receipt["sha256"], "runtime must test the exact rootless store")
    configurations = json.loads((root / "expected-runtime.json").read_text())
    runtime = root / "runtime-check"
    runtime.mkdir(exist_ok=False)
    # Prepared-host handoff hashes these complete parts. Re-conversion under a
    # different UID and timestamp must reproduce every byte, including TARs.
    time.sleep(1.1)
    for source, transport, name in image_sources(root):
        reproduced = runtime / name
        oci_bundle.convert(source, transport, name, architecture, "sonic-version", reproduced)
        ci.require(part_identity(root / name) == part_identity(reproduced),
                   "root and nonroot OCI conversion must be byte-identical")
    restored = runtime / "restored"
    restored.mkdir()
    subprocess.run(["tar", "-xf", str(archive), "-C", str(restored), "--same-owner", "--same-permissions",
                    "--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner"], check=True)
    ci.require(all(path.stat().st_uid == path.stat().st_gid == 0 for path in restored.rglob("*")),
               "restored daemon files must be owned by root")
    lab = Lab(runtime)
    report = {"schema": 1, "passed": False, "scope": "runtime consumption of rootless-built store; no SONiC guest boot",
              "store_sha256": receipt["sha256"], "checked_tags": [], "daemon_restart": False,
              "conversion_bytes_equal_across_users": True}
    try:
        lab.containerd("restored", data_root=restored / "containerd-store")
        lab.start_docker("restored")
        lab.ctr.inventory(receipt["tags"], unpacked=False)
        for tag in sorted(receipt["tags"]):
            image = json.loads(lab.docker("image", "inspect", tag))[0]
            ci.require(image["Id"] == receipt["tags"][tag], "runtime manifest digest changed")
            ci.require(image["RootFS"]["Layers"] == configurations[tag]["rootfs"]["diff_ids"],
                       "runtime layer diffIDs changed")
            for key, value in configurations[tag]["config"].items():
                ci.require(image["Config"].get(key) == value, "runtime image configuration changed: " + key)
            ci.require(lab.docker("run", "--rm", "--network=none", "--pull=never", tag) == "SONIC_CONTAINERD_OK\n",
                       "image ownership, hardlink, whiteout or execution check failed")
            report["checked_tags"].append(tag)
        lab.ctr.inventory(receipt["tags"], unpacked=True)
        lab.stop()
        lab.containerd("restored", data_root=restored / "containerd-store")
        lab.start_docker("restored")
        lab.ctr.inventory(receipt["tags"], unpacked=True)
        report.update(passed=True, daemon_restart=True)
    finally:
        lab.stop()
        (root / "runtime-report.json").write_bytes(oci_bundle.canonical(report))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("build", "check-runtime"))
    parser.add_argument("--work-directory", type=Path, required=True)
    args = parser.parse_args()
    (build if args.mode == "build" else check_runtime)(args.work_directory.resolve())
