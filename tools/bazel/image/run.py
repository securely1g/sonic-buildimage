#!/usr/bin/env python3
"""Run Bazel in the declared, isolated SONiC image assembly worker.

Example:
  python3 tools/bazel/image/run.py --workspace "$PWD" \
    --mount-root /path/to/sonic-workspace \
    --worker-spec target/bazel-image-inputs/execution-environment.json \
    --output-user-root /path/to/sonic-workspace/bazel-state \
    -- build //tools/bazel/image/vs:sonic-vs.bin

The explicit mount root must contain every locally overridden source worktree.
The worker receives no host Docker socket; privileged image actions create their
own isolated daemon and mount/PID/network namespaces. No host users or Docker
configuration are modified. The worker image must already be available locally.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess


BOOTSTRAP = r'''
set -eu
groupadd -f -g 1000 sonic-builder
if ! getent passwd 1000 >/dev/null; then
    useradd -u 1000 -g 1000 -d /var/sonic-builder -m -s /bin/bash sonic-builder
fi
sonic_builder_user=$(getent passwd 1000 | cut -d: -f1)
usermod -g 1000 -a -G sudo,docker "$sonic_builder_user"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$sonic_builder_user" >/etc/sudoers.d/sonic-image-worker
chmod 0440 /etc/sudoers.d/sonic-image-worker
exec runuser -u "$sonic_builder_user" -- "$@"
'''


def contained(path, root, description, must_exist=False):
    result = Path(path).resolve(strict=must_exist)
    if not result.is_relative_to(root):
        raise ValueError(description + " must be inside --mount-root")
    # Docker's comma-separated --mount grammar cannot express these host paths.
    if "," in str(result) or "\n" in str(result):
        raise ValueError("unsupported bind mount path: " + description)
    return result


def build_command(args):
    root = Path(args.mount_root).resolve(strict=True)
    if not root.is_dir() or root == Path("/") or "," in str(root) or "\n" in str(root):
        raise ValueError("--mount-root must name an explicit directory other than /")
    workspace = contained(args.workspace, root, "workspace", must_exist=True)
    if not workspace.is_dir() or not (workspace / "MODULE.bazel").is_file():
        raise ValueError("workspace must contain MODULE.bazel")
    worker_spec = contained(args.worker_spec, root, "worker specification", must_exist=True)
    output_root = contained(args.output_user_root, root, "output user root")
    if output_root == root or output_root == workspace:
        raise ValueError("output user root must be a dedicated directory")
    spec = json.loads(worker_spec.read_text())
    image = spec.get("worker_image", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("worker_image must be an exact local sha256 image ID")
    if spec.get("schema") != 1 or spec.get("platform") != "linux/amd64":
        raise ValueError("unsupported worker schema or platform")
    bazel = Path(args.bazel)
    if not bazel.is_absolute():
        raise ValueError("--bazel must be an absolute path in the worker")
    if args.bazel != "/usr/local/bin/bazel" and bazel.exists():
        contained(bazel, root, "host Bazel executable", must_exist=True)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("a Bazel command is required after --")
    docker = [
        "docker", "run", "--rm", "--init", "--pull=never",
        "--platform", "linux/amd64",
        "--privileged", "--cpus=8", "--memory=24g", "--memory-swap=24g",
        "--user", "0:0", "--ulimit", "nofile=524288:524288",
        "--mount", "type=bind,source=" + str(root) + ",target=" + str(root),
        "--mount", "type=bind,source=" + str(worker_spec) + ",target=/run/sonic-image-worker.json,readonly",
        "--mount", "type=bind,source=/etc/ssl/certs,target=/etc/ssl/certs,readonly",
    ]
    cache_arg = []
    if args.repository_cache:
        cache = Path(args.repository_cache).resolve(strict=True)
        if not cache.is_dir() or cache == Path("/") or "," in str(cache) or "\n" in str(cache):
            raise ValueError("repository cache must name an explicit directory other than /")
        docker += ["--mount", "type=bind,source=" + str(cache) + ",target=/repository-cache"]
        cache_arg = ["--repository_cache=/repository-cache"]
    # Cache and resource flags apply to build/test, not query. Keep caller's
    # startup flags before the command and command-specific flags after it.
    verbs = {
        "build", "test", "query", "cquery", "aquery", "run", "coverage",
        "info", "fetch", "sync", "version", "help", "clean", "shutdown",
        "canonicalize-flags", "analyze-profile", "dump", "mod", "license",
    }
    command_index = next((i for i, value in enumerate(command) if value in verbs), None)
    if command_index is None:
        raise ValueError("Bazel command is missing")
    verb = command[command_index]
    defaults = cache_arg
    if verb in {"build", "test", "run", "coverage"}:
        defaults += ["--jobs=8", "--local_resources=cpu=8"]
    command[command_index + 1:command_index + 1] = defaults
    docker += [
        "--workdir", str(workspace), "--entrypoint", "/bin/bash", image,
        "-ec", BOOTSTRAP, "sonic-image-bootstrap", str(bazel), "--batch",
        "--output_user_root=" + str(output_root), *command,
    ]
    return docker


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--mount-root", required=True)
    parser.add_argument("--worker-spec", required=True)
    parser.add_argument("--bazel", default="/usr/local/bin/bazel")
    parser.add_argument("--output-user-root", required=True)
    parser.add_argument("--repository-cache")
    parser.add_argument("--dry-run", action="store_true", help="print the container command without running it")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = build_command(args)
    if args.dry_run:
        print(json.dumps(command, indent=2))
        return 0
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
