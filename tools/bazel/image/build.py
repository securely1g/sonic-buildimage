#!/usr/bin/env python3
"""Publish the selected VS image producer through the ordinary Make target."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
from build_events import bep_outputs, named_output, output_identity
import verify_inputs

TARGET = "//tools/bazel/image/vs:sonic-vs.bin"
PROVENANCE_TARGET = "//tools/bazel/image/vs:sonic-vs.bin_input_provenance"
RECEIPT = "target/.bazel-vs-image.json"
BACKUP = "target/.sonic-vs.bin.bazel-previous"
TRANSITION = "target/.sonic-vs.bin.native-in-progress"


def check_transition(workspace):
    backup = workspace / BACKUP
    transition = workspace / TRANSITION
    retained_backup = backup.exists() or backup.is_symlink()
    retained_transition = transition.exists() or transition.is_symlink()
    if retained_backup or retained_transition:
        message = "unfinished native transition; stop remaining native workers"
        if retained_backup:
            message += "; restore " + str(backup) + " to target/sonic-vs.bin and verify the producer receipt"
        elif retained_transition:
            message += "; inspect and recover target/sonic-vs.bin and its producer receipt"
        if retained_transition:
            message += "; remove " + str(transition) + " only after recovery is complete"
        raise ValueError(message)


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


def publish(source, destination, receipt, expected, input_provenance):
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
            record = {"schema": 1, "producer": "bazel", **identity,
                      "input_provenance": verify_inputs.identity(input_provenance, "verified preparation receipt")}
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


def native_transition(workspace, execute):
    """Invalidate a retained Bazel installer without losing it on build failure."""
    output = workspace / "target/sonic-vs.bin"
    receipt = workspace / RECEIPT
    backup = workspace / BACKUP
    transition = workspace / TRANSITION
    check_transition(workspace)
    previous_receipt = receipt.read_bytes() if receipt.exists() else None
    with transition.open("xb") as stream:
        stream.write(b"native VS image transition in progress\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if previous_receipt is not None and output.exists():
            output.replace(backup)
        execute()
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError("native assembly did not produce a nonempty target/sonic-vs.bin")
    except (KeyboardInterrupt, SystemExit):
        # A cancelled native client may leave a native worker alive in Docker.
        # Never restore an old image into a path that worker could still write.
        if previous_receipt is not None:
            replace_bytes(receipt, previous_receipt)
        raise RuntimeError("native build interrupted; stop remaining native workers before retrying"
                           + ("; previous installer preserved at " + str(backup) if backup.exists() else "")
                           + "; retain " + str(transition) + " until recovery is complete")
    except Exception:
        if backup.exists():
            backup.replace(output)
        if previous_receipt is not None:
            replace_bytes(receipt, previous_receipt)
        transition.unlink()
        raise
    receipt.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)
    transition.unlink()


def native_build(workspace, execute=subprocess.run):
    native_transition(workspace, lambda: execute(
        ["make", "SONIC_BAZEL_NATIVE_PUBLISH=y", "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n", "target/sonic-vs.bin"],
        cwd=workspace, check=True))


def native_assembly(workspace, execute=subprocess.run):
    environment = dict(os.environ, SONIC_BAZEL_NATIVE_PUBLISH="y")
    native_transition(workspace, lambda: execute(
        ["bash", "build_image.sh"], cwd=workspace, env=environment, check=True))


def verified_worker_spec(workspace, source, metadata):
    """Retain a verified stable mount source for persistent worker reuse."""
    expected = verify_inputs.identity(metadata, "execution-environment.json")
    directory = workspace / "target/bazel-image-worker-specs"
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir() or directory.resolve(strict=True) != directory:
        raise ValueError("verified worker specification directory must be unlinked")
    destination = directory / (expected["sha256"] + ".json")
    if not destination.exists() and not destination.is_symlink():
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=directory) as temporary:
            pending = Path(temporary) / "execution-environment.json"
            verify_inputs.copy_verified_file(source, pending, expected)
            try:
                os.link(pending, destination)
            except FileExistsError:
                pass
    if (not destination.is_file() or destination.is_symlink() or
            destination.resolve(strict=True) != destination or destination.stat().st_nlink != 1 or
            verify_inputs.file_identity(destination) != expected):
        raise ValueError("retained verified worker specification is linked or differs from its receipt: " + str(destination))
    return destination


def bazel_build(args):
    workspace = args.workspace
    inputs = workspace / "target/bazel-image-inputs"
    receipt_bytes, receipt = verify_inputs.verify_bundle(workspace)
    receipt_identity = verify_inputs.bytes_identity(receipt_bytes)
    graph = verify_inputs.verified_graph_snapshot(workspace, receipt_bytes)
    worker_spec = verified_worker_spec(workspace, inputs / "execution-environment.json",
                                       receipt["files"]["execution-environment.json"])
    # This directory is visible in the worker's declared workspace mount. Its
    # fresh BEP cannot accidentally identify an earlier output-base symlink.
    with tempfile.TemporaryDirectory(prefix=".vs-publish-", dir=workspace / "target") as directory:
        directory = Path(directory)
        installer_config = directory / "installer-config.json"
        verify_inputs.copy_verified_file(inputs / "installer/config.json", installer_config,
                                         receipt["files"]["installer/config.json"])
        from native import producer
        producer.check_installer_request(installer_config, os.environ, receipt["native_config_flags"])
        launcher = [sys.executable, str(workspace / "tools/bazel/image/run.py"),
                    "--workspace", str(workspace), "--mount-root", str(args.mount_root),
                    "--worker-spec", str(worker_spec),
                    "--output-user-root", str(args.output_user_root)]
        for option in ("repository_cache", "persistent_worker", "git_config"):
            value = getattr(args, option)
            if value:
                launcher += ["--" + option.replace("_", "-"), str(value)]
        events = directory / "events.jsonl"
        subprocess.run([*launcher, "--", "build", "--package_path=" + str(graph) + ":%workspace%",
                        TARGET, PROVENANCE_TARGET,
                        "--build_event_json_file=" + str(events)], cwd=workspace, check=True)
        outputs = bep_outputs(events, [TARGET, PROVENANCE_TARGET], args.output_user_root.resolve())
        source = named_output(outputs, TARGET, "sonic-vs.bin")
        verified_receipt = named_output(outputs, PROVENANCE_TARGET, "provenance.json")
        if output_identity(events, verified_receipt) != receipt_identity:
            raise ValueError("Bazel verified a different image input receipt; refusing publication")
        current_receipt, _ = verify_inputs.verify_bundle(workspace)
        if current_receipt != receipt_bytes:
            raise ValueError("image input provenance changed during the build; refusing publication")
        publish(source, workspace / "target/sonic-vs.bin", workspace / RECEIPT,
                output_identity(events, source), receipt_identity)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build-with-bazel-when-available", choices=("y", "n"))
    mode.add_argument("--native-assembly", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mount-root", type=Path)
    parser.add_argument("--output-user-root", type=Path)
    parser.add_argument("--repository-cache")
    parser.add_argument("--persistent-worker")
    parser.add_argument("--git-config")
    args = parser.parse_args(argv)
    args.workspace = args.workspace.resolve(strict=True)
    args.mount_root = args.mount_root or args.workspace
    args.output_user_root = args.output_user_root or args.workspace / "target/bazel-image-state"
    try:
        (args.workspace / "target").mkdir(exist_ok=True)
        with (args.workspace / "target/.bazel-vs-image.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            check_transition(args.workspace)
            if args.native_assembly:
                native_assembly(args.workspace)
            elif args.build_with_bazel_when_available == "y":
                bazel_build(args)
            else:
                native_build(args.workspace)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
