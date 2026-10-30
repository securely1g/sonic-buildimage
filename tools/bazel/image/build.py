#!/usr/bin/env python3
"""Publish the selected VS image producer through the ordinary Make target."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend
import privileges

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
from build_events import bep_outputs, named_output, output_identity

TARGET = "//tools/bazel/image/vs:sonic-vs.bin"
RECEIPT = "target/.bazel-vs-image.json"
BACKUP = "target/.sonic-vs.bin.bazel-previous"
WORKER_RECORD = Path("/run/sonic-image-worker.json")


def check_transition(workspace):
    backup = workspace / BACKUP
    if backup.exists() or backup.is_symlink():
        raise ValueError("unfinished native transition; stop remaining native workers before restoring "
                         + str(backup) + " to target/sonic-vs.bin")


def replace_bytes(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def publish(source, destination, receipt, expected, *, image_store_backend="docker"):
    """Keep the previous installer until a complete replacement is available."""
    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            with source.open("rb") as incoming:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    stream.write(block)
                    digest.update(block)
                    size += len(block)
                    if size > expected["bytes"]:
                        raise ValueError("installer bytes differ from this build's BEP")
            identity = {"sha256": digest.hexdigest(), "bytes": size}
            if identity != expected:
                raise ValueError("installer bytes differ from this build's BEP")
            stream.flush()
            os.fsync(stream.fileno())
            pending.chmod(0o755)
            # Write the producer marker first: an interruption can cause a
            # redundant native rebuild, but cannot reuse a Bazel image as native.
            previous = receipt.read_bytes() if receipt.exists() else None
            record = {"schema": 1, "producer": "bazel", "backend": image_store_backend, **identity}
            replace_bytes(receipt, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
            try:
                pending.replace(destination)
            except BaseException:
                if previous is None:
                    receipt.unlink(missing_ok=True)
                else:
                    replace_bytes(receipt, previous)
                raise
        finally:
            pending.unlink(missing_ok=True)


def native_build(workspace, execute=subprocess.run):
    """Invalidate a retained Bazel installer without losing it on build failure."""
    output = workspace / "target/sonic-vs.bin"
    receipt = workspace / RECEIPT
    backup = workspace / BACKUP
    check_transition(workspace)
    previous_receipt = receipt.read_bytes() if receipt.exists() else None
    if previous_receipt is not None and output.exists():
        output.replace(backup)
    try:
        execute(["make", "SONIC_BAZEL_NATIVE_PUBLISH=y", "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n",
                 "USE_CONTAINER_IMAGE_STORE_AS_BACKEND=n", "target/sonic-vs.bin"],
                cwd=workspace, check=True)
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError("native Make did not produce a nonempty target/sonic-vs.bin")
    except (KeyboardInterrupt, SystemExit):
        # A cancelled Make client may leave a native worker alive in Docker.
        # Never restore an old image into a path that worker could still write.
        if previous_receipt is not None:
            replace_bytes(receipt, previous_receipt)
        raise RuntimeError("native build interrupted; stop remaining native workers before retrying"
                           + ("; previous installer preserved at " + str(backup) if backup.exists() else ""))
    except Exception:
        if backup.exists():
            backup.replace(output)
        if previous_receipt is not None:
            replace_bytes(receipt, previous_receipt)
        raise
    receipt.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)


def bazel_build(args):
    workspace = args.workspace
    inputs = workspace / "target/bazel-image-inputs"
    if not (inputs / "execution-environment.json").is_file():
        raise ValueError("prepare the native VS inputs and execution worker first; "
                         "see tools/bazel/image/README.md (Declared inputs and execution)")
    backend.select_backend(workspace, build_with_bazel_when_available="y",
                           use_container_image_store_as_backend=args.backend)
    if args.backend == "y" and not (inputs / "prepared-host/receipt.json").is_file():
        raise ValueError("containerd final assembly requires a prepared host from the separate native job; see tools/bazel/image/README.md")
    launcher = [sys.executable, str(workspace / "tools/bazel/image/run.py"),
                "--workspace", str(workspace), "--mount-root", str(args.mount_root),
                "--worker-spec", str(inputs / "execution-environment.json"),
                "--output-user-root", str(args.output_user_root)]
    if args.backend == "y":
        launcher += ["--unprivileged"]
    for option in ("repository_cache", "persistent_worker", "git_config"):
        value = getattr(args, option)
        if value:
            launcher += ["--" + option.replace("_", "-"), str(value)]
    worker_record = WORKER_RECORD
    inside_worker = worker_record.is_file()
    if inside_worker:
        if args.backend != "y":
            raise ValueError("in-container final assembly requires the containerd backend")
        privileges.check()
        if json.loads(worker_record.read_text()) != json.loads((inputs / "execution-environment.json").read_text()):
            raise ValueError("assembly runner differs from the prepared execution environment")
    # This directory is visible in the worker's declared workspace mount. Its
    # fresh BEP cannot accidentally identify an earlier output-base symlink.
    with tempfile.TemporaryDirectory(prefix=".vs-publish-", dir=workspace / "target") as directory:
        events = Path(directory) / "events.jsonl"
        extra_args = shlex.split(os.environ.get("BAZEL_IMAGE_ARGS", ""))
        command = [*launcher, "--", "build", TARGET, *extra_args, "--build_event_json_file=" + str(events)]
        if inside_worker:
            command = ["/usr/local/bin/bazel", "--output_user_root=" + str(args.output_user_root),
                       "build", "--jobs=4", "--local_resources=cpu=4",
                       "--spawn_strategy=processwrapper-sandbox", TARGET, *extra_args,
                       "--build_event_json_file=" + str(events)]
            if args.repository_cache:
                command += ["--repository_cache=" + str(args.repository_cache)]
        subprocess.run(command, cwd=workspace, check=True)
        outputs = bep_outputs(events, [TARGET], args.output_user_root.resolve())
        source = named_output(outputs, TARGET, "sonic-vs.bin")
        publish(source, workspace / "target/sonic-vs.bin", workspace / RECEIPT,
                output_identity(events, source),
                image_store_backend="containerd" if args.backend == "y" else "docker")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--build-with-bazel-when-available", choices=("y", "n"), required=True)
    parser.add_argument("--backend", choices=("y", "n"), default="n")
    parser.add_argument("--mount-root", type=Path)
    parser.add_argument("--output-user-root", type=Path)
    parser.add_argument("--repository-cache")
    parser.add_argument("--persistent-worker")
    parser.add_argument("--git-config")
    args = parser.parse_args()
    args.workspace = args.workspace.resolve(strict=True)
    args.mount_root = args.mount_root or args.workspace
    args.output_user_root = args.output_user_root or args.workspace / "target/bazel-image-state"
    try:
        backend.selection(args.build_with_bazel_when_available, args.backend)
        (args.workspace / "target").mkdir(exist_ok=True)
        with (args.workspace / "target/.bazel-vs-image.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            check_transition(args.workspace)
            if args.build_with_bazel_when_available == "y":
                bazel_build(args)
            else:
                native_build(args.workspace)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
