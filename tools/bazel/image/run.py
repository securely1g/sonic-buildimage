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

Add --persistent-worker NAME to retain this isolated worker and its Bazel server.
The run action creates or reuses it; --worker-action start initializes it without
running Bazel, status inspects it, and stop shuts down Bazel and removes only that
verified worker. Use identical worker arguments for every lifecycle operation.
An output-root lock serializes operations, including builds. Startup option
changes use Bazel's normal server restart; output-root overrides and batch mode
are rejected in persistent mode.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


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


PERSISTENT_BOOTSTRAP = BOOTSTRAP.replace(
    'exec runuser -u "$sonic_builder_user" -- "$@"',
    "touch /run/sonic-image-worker-ready\nexec /bin/sleep infinity",
)
EXEC_USER = r'''
set -eu
sonic_builder_user=$(getent passwd 1000 | cut -d: -f1)
exec runuser -u "$sonic_builder_user" -- "$@"
'''
LABEL = "org.sonic-net.bazel-image-worker"
MEMORY = 24 * 1024 ** 3


def build_plan(args):
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
    persistent = getattr(args, "persistent_worker", None)
    action = getattr(args, "worker_action", "run")
    if action != "run" and not persistent:
        raise ValueError("--worker-action requires --persistent-worker")
    if persistent and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", persistent):
        raise ValueError("invalid persistent worker name")
    if action == "run" and not command:
        raise ValueError("a Bazel command is required after --")
    if action != "run" and command:
        raise ValueError("lifecycle operations do not take a Bazel command")
    user = getattr(args, "worker_user", None)
    home = getattr(args, "worker_home", None)
    if bool(user) != bool(home) or (user and not persistent):
        raise ValueError("--worker-user and --worker-home must be supplied together in persistent mode")
    if user and (not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user)
                 or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", home)
                 or ".." in Path(home).parts or home == "/"):
        raise ValueError("invalid worker user or home directory")
    bootstrap = PERSISTENT_BOOTSTRAP
    if user:
        bootstrap = bootstrap.replace("/var/sonic-builder", home).replace("sonic-builder", user)
        expected_account = user + ":" + home
        bootstrap = bootstrap.replace(
            'usermod -g 1000',
            '[ "$(getent passwd 1000 | cut -d: -f1,6)" = "' + expected_account +
            '" ] || { echo "worker UID 1000 account does not match" >&2; exit 1; }\nusermod -g 1000',
        )
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
    cache = None
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
    if action == "run":
        if command_index is None:
            raise ValueError("Bazel command is missing")
        verb = command[command_index]
        defaults = list(cache_arg)
        if verb in {"build", "test", "run", "coverage"}:
            defaults += ["--jobs=8", "--local_resources=cpu=8"]
            if persistent:
                defaults += ["--cache_computed_file_digests=200000"]
        command[command_index + 1:command_index + 1] = defaults
    if persistent:
        for value in command[:command_index]:
            name = value.split("=", 1)[0]
            if name in {"--batch", "--nobatch", "--output_base", "--output_user_root"}:
                raise ValueError("persistent worker controls Bazel startup option " + name)
    identity = {
        "schema": 1,
        "name": persistent,
        "image": image,
        "spec_path": str(worker_spec),
        "spec_sha256": hashlib.sha256(worker_spec.read_bytes()).hexdigest(),
        "root": str(root),
        "workspace": str(workspace),
        "output_root": str(output_root),
        "bazel": str(bazel),
        "repository_cache": str(cache) if cache else None,
        "bootstrap_sha256": hashlib.sha256(bootstrap.encode()).hexdigest(),
        "worker_user": user,
        "worker_home": home,
        "cpu_count": 8,
        "memory_bytes": MEMORY,
    }
    if bazel.exists() and bazel.is_relative_to(root):
        identity["bazel_sha256"] = hashlib.sha256(bazel.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    mounts = [(str(root), str(root), True),
              (str(worker_spec), "/run/sonic-image-worker.json", False),
              ("/etc/ssl/certs", "/etc/ssl/certs", False)]
    if cache:
        mounts.append((str(cache), "/repository-cache", True))
    common = docker[3:]
    create = ["docker", "create", "--name", persistent or "unused",
              "--label", LABEL + "=" + digest,
              "--network=bridge", "--ipc=private", *common,
              "--workdir", str(workspace), "--entrypoint", "/bin/bash", image,
              "-ec", bootstrap, "sonic-image-worker"]
    docker += [
        "--workdir", str(workspace), "--entrypoint", "/bin/bash", image,
        "-ec", BOOTSTRAP, "sonic-image-bootstrap", str(bazel), "--batch",
        "--output_user_root=" + str(output_root), *command,
    ]
    return {
        "disposable": docker, "create": create, "identity": identity, "bootstrap": bootstrap,
        "digest": digest, "mounts": mounts,
        "bazel_command": [str(bazel), "--output_user_root=" + str(output_root), *command],
    }


def build_command(args):
    """Retain the original disposable command API for existing callers."""
    if getattr(args, "persistent_worker", None):
        raise ValueError("persistent mode requires the launcher lifecycle, not build_command")
    return build_plan(args)["disposable"]


def capture(command):
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("command failed: " + " ".join(command[:3]) + "\n" + result.stderr.strip())
    return result.stdout


def inspect_worker(name):
    result = subprocess.run(["docker", "inspect", "--type", "container", name],
                            text=True, capture_output=True)
    if result.returncode:
        if "No such container" in result.stderr or "No such object" in result.stderr:
            return None
        raise RuntimeError("cannot inspect worker: " + result.stderr.strip())
    records = json.loads(result.stdout)
    if len(records) != 1:
        raise ValueError("unexpected Docker inspect response")
    return records[0]


def validate_worker(worker, plan):
    """Check live Docker configuration as well as the immutable ownership label."""
    identity = plan["identity"]
    config = worker.get("Config", {})
    host = worker.get("HostConfig", {})
    expected = {
        "name": (worker.get("Name"), "/" + identity["name"]),
        "image ID": (worker.get("Image"), identity["image"]),
        "configured image": (config.get("Image"), identity["image"]),
        "ownership identity": ((config.get("Labels") or {}).get(LABEL), plan["digest"]),
        "entrypoint": (config.get("Entrypoint"), ["/bin/bash"]),
        "command": (config.get("Cmd"), ["-ec", plan["bootstrap"], "sonic-image-worker"]),
        "working directory": (config.get("WorkingDir"), identity["workspace"]),
        "user": (config.get("User"), "0:0"),
        "init": (host.get("Init"), True),
        "privileged": (host.get("Privileged"), True),
        "CPU limit": (host.get("NanoCpus"), 8_000_000_000),
        "memory limit": (host.get("Memory"), MEMORY),
        "swap limit": (host.get("MemorySwap"), MEMORY),
        "network namespace": (host.get("NetworkMode"), "bridge"),
        "PID namespace": (host.get("PidMode", ""), ""),
        "IPC namespace": (host.get("IpcMode"), "private"),
        "automatic removal": (host.get("AutoRemove", False), False),
        "automatic port publication": (host.get("PublishAllPorts", False), False),
        "port bindings": (host.get("PortBindings") or {}, {}),
        "restart policy": ((host.get("RestartPolicy") or {}).get("Name", "no"), "no"),
    }
    for label, (actual, wanted) in expected.items():
        if actual != wanted:
            raise ValueError("refusing mismatched persistent worker " + identity["name"] + ": " + label)
    mounts = worker.get("Mounts", [])
    observed = sorted((item.get("Source"), item.get("Destination"), item.get("RW"))
                      for item in mounts if item.get("Type") == "bind"
                      and item.get("Propagation") == "rprivate")
    if len(observed) != len(mounts) or observed != sorted(plan["mounts"]):
        raise ValueError("refusing mismatched persistent worker: bind mounts")
    container_id = worker.get("Id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("invalid worker container ID")
    return container_id


def exec_command(container_id, plan, bazel_command=None):
    return ["docker", "exec", "--user", "0:0", "--workdir", plan["identity"]["workspace"],
            container_id, "/bin/bash", "-ec", EXEC_USER, "sonic-image-command",
            *(bazel_command or plan["bazel_command"])]


@contextlib.contextmanager
def output_lock(output_root):
    """Serialize worker lifecycle and Bazel clients without stale PID lock files."""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / ".sonic-image-worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root / ".sonic-image-worker-owner.json"
    finally:
        os.close(fd)


def check_owner(owner_file, plan):
    if owner_file.is_symlink():
        raise ValueError("worker ownership receipt must not be a symlink")
    if owner_file.exists():
        owner = json.loads(owner_file.read_text())
        if owner["name"] != plan["identity"]["name"]:
            if inspect_worker(owner["container_id"]) is not None:
                raise ValueError("output root belongs to persistent worker " + owner["name"] + "; stop it first")
            owner_file.unlink()


def save_owner(owner_file, plan, container_id):
    temporary = owner_file.with_suffix(".tmp")
    # Exclusive creation refuses leftovers or symlinks rather than truncating them.
    with temporary.open("x") as output:
        json.dump({"name": plan["identity"]["name"], "container_id": container_id,
                   "identity": plan["digest"]}, output)
        output.write("\n")
    os.replace(temporary, owner_file)


def ready_worker(container_id, plan):
    started = time.monotonic()
    while True:
        result = subprocess.run(["docker", "exec", container_id, "test", "-f",
                                 "/run/sonic-image-worker-ready"], capture_output=True)
        if result.returncode == 0:
            break
        if time.monotonic() - started >= 30:
            raise RuntimeError("worker did not finish initialization; inspect its Docker logs")
        state = inspect_worker(container_id)
        if not state or not state.get("State", {}).get("Running"):
            raise RuntimeError("worker exited during initialization; inspect its Docker logs")
        time.sleep(0.1)
    actual = capture(["docker", "exec", container_id, "sha256sum", "/run/sonic-image-worker.json"])
    if actual.split()[0] != plan["identity"]["spec_sha256"]:
        raise ValueError("worker specification changed after worker creation")


def persistent_action(args, plan, owner_file):
    check_owner(owner_file, plan)
    worker = inspect_worker(args.persistent_worker)
    action = args.worker_action
    if owner_file.exists():
        owner = json.loads(owner_file.read_text())
        if worker is None or worker.get("Id") != owner["container_id"]:
            if inspect_worker(owner["container_id"]) is not None:
                raise ValueError("output root's previous worker still exists under another name; refusing replacement")
        elif owner.get("identity") != plan["digest"]:
            raise ValueError("output root's worker ownership identity does not match")
    if worker is None and action in {"status", "stop"}:
        if action == "stop" and owner_file.exists():
            owner_file.unlink()
        print(json.dumps({"worker": args.persistent_worker, "state": "absent"}))
        return 0
    if worker is None:
        # Docker creates names atomically. If another launcher won this name,
        # inspect and validate it; never remove a colliding container.
        result = subprocess.run(plan["create"], text=True, capture_output=True)
        worker = inspect_worker(args.persistent_worker)
        if worker is None:
            raise RuntimeError("cannot create worker: " + result.stderr.strip())
    container_id = validate_worker(worker, plan)
    running = worker.get("State", {}).get("Running", False)
    if action == "status":
        print(json.dumps({"worker": args.persistent_worker, "container_id": container_id,
                          "identity": plan["digest"], "state": worker["State"]["Status"]}))
        return 0
    save_owner(owner_file, plan, container_id)
    if action == "stop":
        if running:
            ready_worker(container_id, plan)
            shutdown = [plan["identity"]["bazel"],
                        "--output_user_root=" + plan["identity"]["output_root"], "shutdown"]
            result = subprocess.run(exec_command(container_id, plan, shutdown))
            if result.returncode:
                return result.returncode
            capture(["docker", "stop", "--time", "30", container_id])
        capture(["docker", "rm", container_id])
        owner_file.unlink()
        print(json.dumps({"worker": args.persistent_worker, "container_id": container_id, "state": "removed"}))
        return 0
    if not running:
        capture(["docker", "start", container_id])
    ready_worker(container_id, plan)
    if action == "start":
        print(json.dumps({"worker": args.persistent_worker, "container_id": container_id,
                          "identity": plan["digest"], "state": "running"}))
        return 0
    return subprocess.run(exec_command(container_id, plan)).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--mount-root", required=True)
    parser.add_argument("--worker-spec", required=True)
    parser.add_argument("--bazel", default="/usr/local/bin/bazel")
    parser.add_argument("--output-user-root", required=True)
    parser.add_argument("--repository-cache")
    parser.add_argument("--persistent-worker", help="retain and reuse this named worker and its Bazel server")
    parser.add_argument("--worker-user", help="explicit UID 1000 account for a persistent worker")
    parser.add_argument("--worker-home", help="home for --worker-user; both options are required together")
    parser.add_argument("--worker-action", choices=("run", "start", "status", "stop"), default="run")
    parser.add_argument("--dry-run", action="store_true", help="print the command/worker contract without running it")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        plan = build_plan(args)
        if args.dry_run:
            print(json.dumps(plan if args.persistent_worker else plan["disposable"], indent=2))
            return 0
        with output_lock(plan["identity"]["output_root"]) as owner_file:
            if args.persistent_worker:
                return persistent_action(args, plan, owner_file)
            check_owner(owner_file, plan)
            return subprocess.run(plan["disposable"]).returncode
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
