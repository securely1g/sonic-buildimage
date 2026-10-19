#!/usr/bin/env python3
"""Run the existing image controller through a runner's local Docker access."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid

import image


LABEL = "org.sonic.image-ci.controller"
STOP_SECONDS = 180
SOCKET = Path("/var/run/docker.sock")


def relocate_checkout(source, destination):
    """Keep each checkout unique while caches survive ephemeral runner attempts."""
    source = source.resolve(strict=True)
    destination = destination.resolve()
    image.require((source / ".git").is_dir() and not (source / ".git").is_symlink(),
                  "staged checkout must be a standalone clone")
    image.require(not destination.exists() and not destination.is_symlink(),
                  "refusing to replace a retained checkout")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Worker UID1000 must traverse this bind root even when the runner uses
    # another UID and its private host-side cache ancestors have mode0700.
    destination.parent.chmod(0o755)
    shutil.move(str(source), str(destination))
    digest = hashlib.sha256()
    for name in ("MODULE.bazel", "tools/bazel/common.bazelrc"):
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256((destination / name).read_bytes()).digest())
    return digest.hexdigest()


def restore_caches(state, owner):
    """Restore runner access without visiting retained native Docker trees."""
    os.chown(state, *owner, follow_symlinks=False)
    for name in ("package-cache", "repository-cache"):
        path = state / name
        image.require(not path.is_symlink(), "shared cache must not be a symlink: " + str(path))
        if path.exists():
            image.chown_tree(path, *owner)


def inside(args):
    image.require(os.geteuid() == 0 and Path("/.dockerenv").exists(),
                  "controller must run as root inside its Docker container")
    image.require(args.runner_uid is not None and args.runner_gid is not None,
                  "controller requires the runner UID and GID")
    # Validate before restoration can touch any state, including on failure.
    _, state, _, _ = image.validate_paths(args.workspace, args.state, args.artifacts)
    state.mkdir(parents=True, exist_ok=True)
    try:
        return image.build(args)
    finally:
        restore_caches(state, (args.runner_uid, args.runner_gid))


def inspect_owned(container, invocation, missing_ok=False):
    result = subprocess.run(["docker", "inspect", container], capture_output=True, text=True,
                            timeout=30, start_new_session=True)
    if missing_ok and result.returncode and (
            "no such object:" in result.stderr.lower() or "no such container:" in result.stderr.lower()):
        return None
    result.check_returncode()
    details = json.loads(result.stdout)
    image.require(len(details) == 1 and
                  container in (details[0]["Id"], details[0].get("Name", "").lstrip("/")) and
                  details[0]["Config"].get("Labels", {}).get(LABEL) == invocation,
                  "refusing to touch a controller not owned by this invocation")
    return details[0]


def stop_owned(container, invocation, missing_ok=False):
    details = inspect_owned(container, invocation, missing_ok=missing_ok)
    if details is None:
        return
    container = details["Id"]
    if details["State"]["Running"]:
        subprocess.run(["docker", "stop", "--time", str(STOP_SECONDS), container],
                       check=True, timeout=STOP_SECONDS + 15, start_new_session=True)
    details = inspect_owned(container, invocation, missing_ok=True)
    if details is None:
        return  # --rm also finishes cleanup if the host launcher was killed.
    image.require(not details["State"]["Running"], "controller did not stop")
    result = subprocess.run(["docker", "rm", container], capture_output=True, text=True,
                            timeout=30, start_new_session=True)
    if result.returncode and inspect_owned(container, invocation, missing_ok=True) is not None:
        result.check_returncode()


def create_command(args, mount_root, worker_image, invocation, ca_bundle):
    image.require(re.fullmatch(r"sha256:[0-9a-f]{64}", worker_image),
                  "controller requires an immutable worker image ID")
    command = ["docker", "create", "--name", "sonic-vs-controller-" + invocation,
               "--label", LABEL + "=" + invocation, "--rm", "--init", "--user", "0:0",
               "--stop-timeout", str(STOP_SECONDS),
               "--mount", "type=bind,src=" + str(mount_root) + ",dst=" + str(mount_root),
               "--mount", "type=bind,src=" + str(SOCKET) + ",dst=" + str(SOCKET),
               "--workdir", str(args.workspace)]
    if args.git_config:
        command += ["--env", "GIT_CONFIG_NOSYSTEM=0",
                    "--env", "GIT_CONFIG_SYSTEM=" + str(args.git_config)]
    command += ["--entrypoint", "python3", worker_image, "-B",
                str(args.workspace / "tools/bazel/ci/image_docker.py"), "--inside-controller",
                "--workspace", str(args.workspace), "--state", str(args.state),
                "--artifacts", str(args.artifacts),
                "--runner-uid", str(os.getuid()), "--runner-gid", str(os.getgid())]
    if args.git_config:
        command += ["--git-config", str(args.git_config)]
    if ca_bundle:
        command += ["--ca-bundle", str(ca_bundle)]
    return command


def run(args):
    args.workspace, args.state, args.artifacts, mount_root = image.validate_paths(
        args.workspace, args.state, args.artifacts)
    image.require(SOCKET.is_socket(), "image CI requires the runner's local Docker socket")
    if args.git_config:
        args.git_config = args.git_config.resolve(strict=True)
        image.require(args.git_config.is_file() and args.git_config.is_relative_to(args.workspace),
                      "Git transport config must be a source file inside the checkout")
    # The same local daemon must interpret all absolute bind paths, including
    # image.build_worker's calls and the controller's child workers.
    os.environ.pop("DOCKER_CONTEXT", None)
    os.environ["DOCKER_HOST"] = "unix://" + str(SOCKET)
    args.state.mkdir(parents=True, exist_ok=True)
    invocation = uuid.uuid4().hex
    # Bootstrap logs must not become untracked files in the fresh source tree
    # before image.build captures its provenance. Publish them only afterward.
    artifacts = args.state / ("controller-" + invocation)
    artifacts.mkdir(exist_ok=False)
    published = args.artifacts.with_name("controller")
    image.require(not published.exists(), "controller evidence already exists")
    receipt = {"schema": 1, "status": "running", "commands": [], "invocation": invocation}
    container, process = None, None
    started = time.monotonic()
    try:
        # Use the public recipe and existing verification, never a saved image
        # supplied by the runner. Its context stays outside the source checkout.
        spec = image.build_worker(args.workspace, args.state, artifacts, receipt,
                                  "controller-" + invocation, args.ca_bundle)
        worker_image = json.loads(spec.read_text())["worker_image"]
        receipt["worker_image"] = worker_image
        ca_bundle = spec.parent / "build-ca-bundle.pem" if args.ca_bundle else None
        command = create_command(args, mount_root, worker_image, invocation, ca_bundle)
        container = subprocess.check_output(command, text=True, timeout=60).strip()
        image.require(re.fullmatch(r"[0-9a-f]{64}", container), "invalid controller container ID")
        receipt["container"] = container
        inspect_owned(container, invocation)
        # Separate the attach client from the runner's signal group. Our signal
        # handler stops the owned container with time for nested-worker cleanup.
        process = subprocess.Popen(["docker", "start", "--attach", container], start_new_session=True)
        attached = process.wait()
        details = inspect_owned(container, invocation, missing_ok=True)
        if details is not None:
            image.require(not details["State"]["Running"], "controller attach ended before its process")
            image.require(attached == details["State"]["ExitCode"],
                          "Docker attach and controller exit status differ")
        result = attached
        receipt.update(status="passed" if result == 0 else "failed", returncode=result)
        return result
    except (Exception, KeyboardInterrupt) as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        # A second cancellation signal must not interrupt bounded cleanup.
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            # The unique label also covers cancellation between Docker create
            # and receiving its ID. Never remove a similarly named foreign job.
            stop_owned(container or "sonic-vs-controller-" + invocation, invocation, missing_ok=True)
            if process:
                process.wait(timeout=30)
        except Exception as error:
            receipt.update(status="failed", cleanup_error=str(error))
            raise
        finally:
            try:
                receipt["wall_seconds"] = time.monotonic() - started
                (artifacts / "controller-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
                shutil.copytree(artifacts, published)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "state", "artifacts"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("ca-bundle", "git-config"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--relocate-from", type=Path,
                        help="move only this staged checkout into the stable build area")
    parser.add_argument("--inside-controller", action="store_true", help=argparse.SUPPRESS)
    for name in ("runner-uid", "runner-gid"):
        parser.add_argument("--" + name, type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    def interrupted(signum, _frame):
        raise InterruptedError("received signal " + str(signum))

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        if args.relocate_from:
            digest = relocate_checkout(args.relocate_from, args.workspace)
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write("source-cache-key=" + digest + "\n")
            return 0
        return inside(args) if args.inside_controller else run(args)
    except (Exception, KeyboardInterrupt) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
