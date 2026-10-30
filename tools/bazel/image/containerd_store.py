#!/usr/bin/env python3
"""Build a stopped containerd image store from declared OCI archive parts.

Run as PID1 in the image worker's private mount/PID/network namespace. A cached
base is always extracted into a new writable copy; no running daemon or mutable
store is shared between actions. Images retain complete compressed content and
metadata; Docker unpacks their overlayfs snapshots on the target before first
use. The installer restores the resulting TAR with ownership, xattrs and ACLs.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import containerd_import as ci
import oci_bundle


def environment(expected, actual, machine=None):
    ci.require(expected == actual, "image worker does not match the declared execution environment")
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine or platform.machine())
    ci.require(expected.get("schema") == 1 and expected.get("platform") == "linux/" + str(architecture),
               "worker architecture does not match the target")
    ci.require(isinstance(expected.get("worker_image"), str) and ci.DIGEST.fullmatch(expected["worker_image"]),
               "worker must use an immutable image ID")
    ci.require(expected.get("docker_version") == "28.5.2", "unsupported target Docker version")
    ci.require(expected.get("containerd_version", ci.VERSION) == ci.VERSION, "unsupported containerd version")
    return architecture


def containerd_version(version):
    ci.require(re.search(r"(?:^|\s)v?" + re.escape(ci.VERSION) + r"(?:\s|$)", version),
               "wrong containerd version: " + version)


def parts_inventory(parts, architecture):
    records, sources = [], {}
    for part in sorted(map(Path, parts)):
        manifest = ci.read_manifest(part / "manifest.json", architecture)
        for record in manifest["images"]:
            source = part / Path(record["archive"]).name
            ci.require(stat.S_ISREG(source.lstat().st_mode), "OCI archive must be a regular file")
            ci.require(ci.sha256(source) == record["sha256"], "OCI archive checksum mismatch")
            digest, _config = oci_bundle.inspect_oci(source, architecture, record["reference"])
            ci.require(digest == record["digest"], "OCI descriptor does not match the import record")
            records.append(record)
            sources[record["archive"]] = source.resolve()
    ci.validate_records(records)
    return records, sources


def base_inventory(archive, receipt_path, expected, architecture):
    receipt = json.loads(receipt_path.read_text())
    ci.require(type(receipt.get("schema")) is int and receipt["schema"] == 1
               and receipt.get("kind") == "sonic-containerd-store", "invalid base store receipt")
    ci.require(receipt.get("execution_environment") == expected, "base store belongs to another worker")
    ci.require(receipt.get("architecture") == architecture and receipt.get("namespace") == ci.NAMESPACE
               and receipt.get("snapshotter") == ci.SNAPSHOTTER and receipt.get("containerd_version") == ci.VERSION,
               "base store runtime is incompatible")
    ci.require(receipt.get("root") == "containerd-store", "invalid base store root")
    ci.require(receipt.get("unpacked") is False, "base store must defer unpacking to the target")
    tags = ci.validate_records(receipt.get("images"))
    ci.require(tags == receipt.get("tags"), "base receipt inventory differs from its image records")
    ci.require(archive.stat().st_size == receipt.get("bytes") and ci.sha256(archive) == receipt.get("sha256"),
               "base store checksum mismatch")
    return receipt


def stop(process):
    process.terminate()
    try:
        status = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise RuntimeError("private containerd did not stop cleanly")
    ci.require(status == 0, "private containerd shutdown failed")


def pack(root, output, jobs):
    with output.open("wb") as stream:
        compressor = subprocess.Popen(["zstd", "-6", "--quiet", "--threads=" + str(jobs), "--stdout"],
                                      stdin=subprocess.PIPE, stdout=stream)
        try:
            subprocess.run(["tar", "--create", "--file=-", "--directory", str(root),
                            "--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner", "--sparse",
                            "--sort=name", "--pax-option=delete=atime,delete=ctime", "containerd-store"],
                           stdout=compressor.stdin, check=True)
            compressor.stdin.close()
            ci.require(compressor.wait(timeout=1800) == 0, "store compression failed")
        finally:
            if not compressor.stdin.closed:
                compressor.stdin.close()
            if compressor.poll() is None:
                compressor.kill()
                compressor.wait()


def restore(archive, root):
    # GNU tar rejects parent traversal and does not follow archive-created
    # directory symlinks. Preserve absolute symlink *targets* inside image
    # snapshots; they are legitimate container filesystem content.
    # GNU tar detects compression from the file header, including cached gzip
    # archives. The public dockerfs.tar.gz name is SONiC's legacy interface.
    subprocess.run(["tar", "--extract", "--file", str(archive), "--directory", str(root),
                    "--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner", "--same-owner",
                    "--same-permissions"], check=True)
    ci.require(sorted(path.name for path in root.iterdir()) == ["containerd-store"], "unexpected base store root")
    store = root / "containerd-store"
    ci.require(store.is_dir() and not store.is_symlink(), "base store root must be a directory")
    for name in ("io.containerd.metadata.v1.bolt", "io.containerd.content.v1.content",
                 "io.containerd.snapshotter.v1.overlayfs"):
        ci.require((store / name).is_dir() and not (store / name).is_symlink(), "invalid containerd data directory")


def run(args):
    ci.require(os.geteuid() == 0 and os.getpid() == 1, "requires the private privileged unshare PID1 worker")
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    expected = json.loads(args.execution_environment.read_text())
    actual = json.loads(Path("/run/sonic-image-worker.json").read_text())
    architecture = environment(expected, actual)
    version = subprocess.check_output(["containerd", "--version"], text=True).strip()
    containerd_version(version)
    ci.require(bool(args.base) == bool(args.base_receipt), "base store and receipt must be supplied together")
    ci.require(args.jobs > 0 and args.uid >= 0 and args.gid >= 0, "invalid ownership or compression settings")
    output, receipt_path = args.output.resolve(), args.receipt.resolve()
    ci.require(output != receipt_path and not output.exists() and not receipt_path.exists(), "store outputs must be new files")
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    records, sources = parts_inventory(args.part, architecture)
    base = base_inventory(args.base, args.base_receipt, expected, architecture) if args.base else None
    all_records = sorted((base["images"] if base else []) + records, key=lambda item: item["archive"])
    tags = ci.validate_records(all_records)
    phases = {"verify_inputs": time.monotonic() - started}
    # Overlay upperdirs need the worker's bind-mounted build filesystem.
    root = Path(tempfile.mkdtemp(prefix=".sonic-containerd-", dir=output.parent))
    runtime = Path(tempfile.mkdtemp(prefix="sonic-containerd-"))
    process = None
    previous_handlers = {}

    def interrupted(signum, _frame):
        raise InterruptedError("containerd store action interrupted by signal " + str(signum))

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, interrupted)
    try:
        data = root / "data"
        data.mkdir()
        if base:
            begin = time.monotonic()
            restore(args.base.resolve(), data)
            phases["restore_base"] = time.monotonic() - begin
        (runtime / "containerd.toml").write_text('version = 2\ndisabled_plugins = ["io.containerd.grpc.v1.cri"]\n')
        socket = runtime / "containerd.sock"
        ctr = ci.Ctr(address=str(socket))
        with (runtime / "containerd.log").open("w") as log:
            begin = time.monotonic()
            process = subprocess.Popen(["containerd", "--config", str(runtime / "containerd.toml"),
                "--root", str(data / "containerd-store"), "--state", str(runtime / "state"),
                "--address", str(socket)], stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            while ctr.execute("version", check=False).returncode:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("private containerd did not start:\n" + (runtime / "containerd.log").read_text()[-8000:])
                time.sleep(.1)
            phases["daemon_start"] = time.monotonic() - begin
            ctr.inventory(base["tags"] if base else {}, unpacked=False)
            begin = time.monotonic()
            for record in records:
                ctr.command("images", "import", "--no-unpack", "--platform", "linux/" + architecture,
                            "--snapshotter", ci.SNAPSHOTTER, str(sources[record["archive"]]))
                for tag in record["tags"]:
                    if tag != record["reference"]:
                        ctr.command("images", "tag", record["reference"], tag)
            ctr.inventory(tags, unpacked=False)
            phases["import_and_verify"] = time.monotonic() - begin
            stop(process)
            process = None
        begin = time.monotonic()
        temporary_output = root / "store.tar.zst"
        pack(data, temporary_output, args.jobs)
        phases["archive_store"] = time.monotonic() - begin
        receipt = {"schema": 1, "kind": "sonic-containerd-store", "architecture": architecture,
            "namespace": ci.NAMESPACE, "snapshotter": ci.SNAPSHOTTER, "containerd_version": ci.VERSION,
            "containerd_build": version, "execution_environment": expected, "root": "containerd-store",
            "sha256": ci.sha256(temporary_output), "bytes": temporary_output.stat().st_size,
            "compression": "zstd", "compression_level": 6, "unpacked": False,
            "images": all_records, "tags": tags, "imported_images": len(records),
            "base_sha256": base["sha256"] if base else None, "phases_seconds": phases}
        temporary_receipt = root / "receipt.json"
        temporary_receipt.write_bytes(oci_bundle.canonical(receipt))
        for source, destination in ((temporary_output, output), (temporary_receipt, receipt_path)):
            os.chown(source, args.uid, args.gid)
            os.chmod(source, 0o555)
            os.replace(source, destination)
        print(json.dumps({"action": "SonicContainerdStore", "images": len(all_records),
                          "imported": len(records), "phases_seconds": phases}), flush=True)
    finally:
        if process is not None and process.poll() is None:
            try:
                stop(process)
            except Exception:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        shutil.rmtree(root)
        shutil.rmtree(runtime)
        # PID1 also reaps descendants orphaned during containerd shutdown.
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", type=Path, action="append", required=True)
    for flag in ("execution-environment", "output", "receipt"):
        parser.add_argument("--" + flag, type=Path, required=True)
    for flag in ("base", "base-receipt"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    run(parser.parse_args())
