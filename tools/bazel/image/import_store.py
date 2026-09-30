#!/usr/bin/env python3
"""Import declared service archives in a disposable Docker 28 overlay2 store.

Run inside the dedicated privileged image-build worker, in a private PID/mount
namespace. No daemon socket or writable Docker directory is shared with the
worker, host, other actions, or a running testbed.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def run(args):
    if os.geteuid() != 0 or os.getpid() != 1:
        raise ValueError("run in a privileged worker through unshare --pid --fork --mount-proc")
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    expected = json.loads(args.execution_environment.read_text())
    actual = json.loads(Path("/run/sonic-image-worker.json").read_text())
    if actual != expected:
        raise ValueError("image worker does not match the declared execution environment")
    version = subprocess.check_output(["dockerd", "--version"], text=True)
    if expected["docker_version"] not in version:
        raise ValueError("wrong Docker version: " + version)
    output = args.output.resolve()
    archives = [path.resolve() for path in args.archive]
    if output.exists():
        # Bazel precreates TreeArtifact directories. Only an empty directory
        # belongs to this new action; never discard a preexisting store.
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Docker's overlay upperdir must be on the bind-mounted build filesystem,
    # rather than the outer container's overlay filesystem.
    root = Path(tempfile.mkdtemp(prefix=".sonic-store-", dir=output.parent))
    runtime = Path(tempfile.mkdtemp(prefix="sonic-docker-"))
    (runtime / "daemon.json").write_text("{}")
    started = time.monotonic()
    phases = {}
    process = None
    try:
        socket = runtime / "docker.sock"
        env = dict(os.environ, DOCKER_HOST="unix://" + str(socket))
        env.pop("DOCKER_CONTEXT", None)
        with (root / "dockerd.log").open("w") as log:
            process = subprocess.Popen([
                "dockerd", "--config-file=" + str(runtime / "daemon.json"), "--host=" + env["DOCKER_HOST"],
                "--data-root=" + str(root / "data"), "--exec-root=" + str(runtime / "exec"),
                "--pidfile=" + str(runtime / "docker.pid"), "--storage-driver=overlay2",
                "--bridge=none", "--iptables=false", "--ip6tables=false",
                "--ip-forward=false", "--ip-masq=false", "--log-level=error",
            ], stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            while subprocess.run(["docker", "info"], env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).returncode:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("private Docker did not start: " + (root / "dockerd.log").read_text())
                time.sleep(0.1)
            phases["daemon_start"] = time.monotonic() - started
            begin = time.monotonic()
            for archive in archives:
                subprocess.run(["docker", "load", "-i", str(archive)], env=env, check=True)
            for tag in args.tag:
                source, destination = tag.split("=", 1)
                subprocess.run(["docker", "tag", source, destination], env=env, check=True)
            phases["load_and_tag"] = time.monotonic() - begin
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=60)
            if process.returncode:
                raise RuntimeError("private Docker exited unsuccessfully")
            process = None
        begin = time.monotonic()
        command = [sys.executable, str(args.collector.resolve()), "collect",
                   "--store-root", str(root / "data"), "--output", str(output),
                   "--pigz", "/usr/bin/pigz", "--jobs", str(args.jobs)]
        # Keep every loaded tag plus the native SONiC version alias. The daemon
        # was empty before these declared archives were loaded.
        subprocess.run(command, check=True)
        phases["collect_compressed_layers"] = time.monotonic() - begin
        print(json.dumps({"action": "SonicDockerImport", "phases_seconds": phases}), flush=True)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        shutil.rmtree(root)
        shutil.rmtree(runtime)
        # Bazel must also be able to remove a partial failed TreeArtifact.
        for directory, _, files in os.walk(output):
            os.chown(directory, args.uid, args.gid)
            for name in files:
                os.chown(Path(directory) / name, args.uid, args.gid, follow_symlinks=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, action="append", required=True)
    parser.add_argument("--tag", action="append", default=[], help="loaded-reference=installed-reference")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--execution-environment", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    run(parser.parse_args())
