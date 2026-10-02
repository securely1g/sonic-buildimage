#!/usr/bin/env python3
"""Produce native VS predecessors from this invocation's source checkout.

Make runs in an owned privileged container with its own Docker daemon. Only
the dedicated build area is mounted; the host Docker socket is never mounted.
The existing native recipes supply the host and non-orchagent services. Bazel
builds orchagent and the final installer after this helper returns.
"""

import argparse
import datetime
import errno
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time


LABEL = "sonic.bazel.native.invocation"
USER = "sonicnative"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(argv, cwd=None, **kwargs):
    return subprocess.run([str(value) for value in argv], cwd=cwd,
                          check=True, text=True, **kwargs)


def capture(argv):
    return command(argv, stdout=subprocess.PIPE).stdout.strip()


def validate(args):
    require(re.fullmatch(r"[0-9a-f]{16}", args.invocation), "invalid native invocation ID")
    require(re.fullmatch(r"[0-9a-f]{40}", args.source_commit), "invalid source commit")
    workspace = args.workspace.resolve(strict=True)
    state, artifacts, output = args.state.resolve(), args.artifacts.resolve(), args.output.resolve()
    require((workspace / ".git").is_dir() and not (workspace / ".git").is_symlink(),
            "native CI requires a standalone source checkout")
    root = workspace.parent
    require(root != Path("/") and state.is_relative_to(root) and state != root
            and not state.is_relative_to(workspace) and not workspace.is_relative_to(state),
            "native state must be a dedicated sibling inside the build area")
    require(artifacts.is_relative_to(workspace) or artifacts.is_relative_to(state),
            "native evidence must stay inside the build area")
    require(output.is_relative_to(workspace) or output.is_relative_to(state),
            "native receipt must stay inside the build area")
    require(not output.exists(), "native receipt already exists")
    require(not (workspace / "target").exists() and not (workspace / "target").is_symlink(),
            "native build requires an empty target directory")
    require(capture(["git", "-c", "safe.directory=" + str(workspace), "-C", workspace,
                     "rev-parse", "HEAD"]) == args.source_commit, "native source revision changed")
    spec = json.loads(args.worker_spec.read_text())
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", spec.get("worker_image", "")),
            "native worker must have an immutable image identity")
    require(spec.get("platform") == "linux/amd64" and spec.get("distribution") == "trixie",
            "native CI requires the AMD64 Trixie worker")
    return workspace, state, artifacts, output, root, spec


def assert_owned(container, invocation, worker_image, mount_root):
    info = json.loads(capture(["docker", "inspect", container]))[0]
    require(info["Id"] == container and info["Config"].get("Labels", {}).get(LABEL) == invocation,
            "refusing to act on an unrelated native worker")
    require(info["Config"]["Image"] == worker_image and info["HostConfig"]["Privileged"],
            "native worker image or isolation settings changed")
    require(info["HostConfig"].get("CgroupnsMode") == "private",
            "native worker requires its own cgroup namespace")
    mounts = info["Mounts"]
    require(len(mounts) == 1 and mounts[0]["Type"] == "bind"
            and mounts[0]["Source"] == str(mount_root)
            and mounts[0]["Destination"] == str(mount_root) and mounts[0]["RW"],
            "native worker mounts differ from its dedicated build area")
    return info


def native_environment(socket):
    env = {key: value for key, value in os.environ.items() if key not in {
        "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG",
        "DOCKER_API_VERSION", "DOCKER_DEFAULT_PLATFORM",
        "SONIC_BUILD_SLAVE_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE", "PIP_CERT", "WGETRC",
    }}
    env.update(DOCKER_HOST="unix://" + str(socket), USER=USER, LOGNAME=USER,
               HOME="/home/" + USER, LC_ALL="C.UTF-8")
    return env


def execution_trust():
    """Trust is part of the declared worker image, never inherited from its host."""
    root = Path("/usr/local/share/sonic-build-trust")
    bundle, record = root / "ca-bundle.pem", root / "receipt.json"
    if not record.exists():
        require(not bundle.exists(), "execution trust bundle has no receipt")
        return {"enabled": False}, None
    metadata = json.loads(record.read_text())
    require(type(metadata.get("enabled")) is bool, "invalid execution trust receipt")
    if not metadata["enabled"]:
        require(not bundle.exists(), "disabled execution trust contains a bundle")
        return {"enabled": False}, None
    for key in ("sha256", "installed_bundle_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", metadata.get(key, "")), "invalid execution trust digest")
    count = metadata.get("certificate_count")
    require(type(count) is int and count > 0, "invalid execution trust certificate count")
    require(bundle.is_file() and hashlib.sha256(bundle.read_bytes()).hexdigest() ==
            metadata["installed_bundle_sha256"], "execution trust bundle differs from worker receipt")
    return {key: metadata[key] for key in
            ("enabled", "sha256", "installed_bundle_sha256", "certificate_count")}, bundle


def prepare_cgroups():
    """Delegate only this worker's private cgroup v2 subtree to nested Docker.

    As in Moby's hack/dind, keep worker processes in a leaf before enabling
    domain controllers. Otherwise runc creates a threaded subtree which cannot
    enforce the native slave's memory limit. The host checks CgroupnsMode before
    starting us; these checks additionally reject a host-root or shared view.
    """
    root = Path("/sys/fs/cgroup")
    if not (root / "cgroup.controllers").exists():
        return {"version": 1}
    require(Path("/proc/self/cgroup").read_text().strip() == "0::/"
            and (root / "cgroup.type").read_text().strip() == "domain",
            "native cgroup setup requires a fresh private domain")
    controllers = (root / "cgroup.controllers").read_text().split()
    require({"cpu", "memory", "pids"}.issubset(controllers),
            "native worker lacks delegated CPU, memory, or PID controllers")
    leaf = root / "sonic-native-init"
    leaf.mkdir()
    for attempt in range(5):
        for pid in (root / "cgroup.procs").read_text().split():
            require(pid.isdecimal() and int(pid) > 0, "invalid worker cgroup process")
            try:
                (leaf / "cgroup.procs").write_text(pid)
            except ProcessLookupError:
                pass  # A process can exit between reading and moving it.
        try:
            (root / "cgroup.subtree_control").write_text(
                " ".join("+" + controller for controller in controllers))
            return {"version": 2, "controllers": controllers,
                    "process_leaf": leaf.name}
        except OSError as error:
            if error.errno != errno.EBUSY or attempt == 4:
                raise
            time.sleep(0.1)
    raise AssertionError("unreachable cgroup preparation state")


def daemon_preflight(args, env):
    """Exercise the actual nonroot BuildKit and resource-limited slave boundary.

    The tiny image contains only busybox and its libraries from the immutable
    execution worker. It needs no registry, network, or prepared SONiC artifact.
    """
    context = args.state / "docker-preflight"
    rootfs, scratch = context / "rootfs", context / "scratch"
    rootfs.mkdir(parents=True)
    scratch.mkdir()
    os.chown(scratch, 1000, 1000)
    dependencies = command(["ldd", "/bin/busybox"], stdout=subprocess.PIPE).stdout
    for name in sorted({"/bin/busybox", *re.findall(r"(/\S+)", dependencies)}):
        source = Path(name)
        require(source.is_file(), "missing worker busybox runtime: " + name)
        destination = rootfs / source.relative_to("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=True)
    (context / "Dockerfile").write_text(
        'FROM scratch\nCOPY rootfs/ /\nRUN ["/bin/busybox", "true"]\n')
    tag = "sonic-native-preflight:" + args.invocation
    prefix = ["runuser", "--preserve-environment", "--user", USER, "--"]
    # Every call is bounded and uses the same nonroot client/private socket as
    # Make. Running the nested container also proves writable bind propagation.
    command([*prefix, "docker", "buildx", "version"], env=env, timeout=30)
    command([*prefix, "docker", "buildx", "build", "--load", "--network=none",
             "--progress=plain", "--tag", tag, context], env=env, timeout=180)
    check = (
        'if [ -f /sys/fs/cgroup/cgroup.controllers ]; then '
        'test "$(/bin/busybox cat /sys/fs/cgroup/memory.max)" = 15032385536; '
        'test "$(/bin/busybox cat /sys/fs/cgroup/memory.swap.max)" = 0; '
        'else test "$(/bin/busybox cat /sys/fs/cgroup/memory/memory.limit_in_bytes)" = 15032385536; '
        'test "$(/bin/busybox cat /sys/fs/cgroup/memory/memory.memsw.limit_in_bytes)" = 15032385536; fi; '
        'test "$(ulimit -n)" = 524288; printf "native-preflight-ok\\n" > /probe/result'
    )
    command([*prefix, "docker", "run", "--rm", "--privileged", "--init",
             "--memory=14g", "--memory-swap=14g", "--ulimit", "nofile=524288:524288",
             "--mount", "type=bind,src=" + str(scratch) + ",dst=/probe",
             tag, "/bin/busybox", "sh", "-ec", check], env=env, timeout=60)
    require((scratch / "result").read_text() == "native-preflight-ok\n",
            "native Docker preflight did not write through its bind mount")
    command([*prefix, "docker", "image", "rm", tag], env=env, timeout=30)
    (args.artifacts / "docker-preflight.json").write_text(json.dumps({
        "status": "passed", "image_source": "immutable worker busybox and runtime libraries",
        "buildkit_run": True, "nonroot_client": USER, "memory_bytes": 14 * 1024 ** 3,
        "swap_bytes": 0, "nofile": 524288, "writable_bind": True,
    }, indent=2) + "\n")


def inside(args):
    require(os.geteuid() == 0 and Path("/.dockerenv").exists()
            and os.environ.get("SONIC_NATIVE_CI_INVOCATION") == args.invocation,
            "native bootstrap must run inside its owned container")
    workspace, state = args.workspace.resolve(strict=True), args.state.resolve(strict=True)
    try:
        account = pwd.getpwuid(1000)
        require(account.pw_name == USER, "unexpected worker UID 1000 account")
    except KeyError:
        try:
            group = grp.getgrgid(1000)
            require(group.gr_name == USER, "unexpected worker GID 1000 group")
        except KeyError:
            command(["groupadd", "--gid", "1000", USER])
        command(["useradd", "--uid", "1000", "--gid", "1000", "--create-home",
                 "--shell", "/bin/bash", USER])
    sudoers = Path("/etc/sudoers.d/sonic-native-ci")
    sudoers.write_text(USER + " ALL=(ALL) NOPASSWD:ALL\n")
    sudoers.chmod(0o440)
    (args.artifacts / "cgroup-delegation.json").write_text(
        json.dumps(prepare_cgroups(), indent=2) + "\n")
    socket = Path("/run/sonic-native-ci/docker.sock")
    socket.parent.mkdir(parents=True)
    docker_log = (args.artifacts / "dockerd.log").open("w")
    daemon = subprocess.Popen([
        "dockerd", "--host=unix://" + str(socket), "--group=" + USER,
        "--data-root=" + str(state / "docker-data"),
        "--exec-root=/run/sonic-native-ci/exec", "--pidfile=/run/sonic-native-ci/docker.pid",
        "--storage-driver=overlay2", "--exec-opt=native.cgroupdriver=cgroupfs"],
        stdout=docker_log, stderr=subprocess.STDOUT)
    env = native_environment(socket)
    try:
        trust_metadata, ca_bundle = execution_trust()
        (args.artifacts / "execution-trust.json").write_text(json.dumps(trust_metadata, indent=2) + "\n")
        for _ in range(90):
            if daemon.poll() is not None:
                raise RuntimeError("private native Docker daemon exited during startup")
            check = subprocess.run(["docker", "info"], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5)
            if check.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("private native Docker daemon did not become ready")
        daemon_preflight(args, env)
        # slave.mk reads this file inside the native Docker boundary. Export
        # source selection for package submakes as well as setting concurrency.
        # Generate it only in this invocation's fresh checkout.
        config = workspace / "rules/config.user"
        with config.open("x") as stream:
            stream.write("SONIC_CONFIG_MAKE_JOBS = 2\n"
                         "export MONIT_SOURCE_METHOD = debian\n"
                         "export RASDAEMON_SOURCE_METHOD = debian\n"
                         "export P4LANG_PI_SOURCE_METHOD = github\n")
        os.chown(config, 1000, 1000)
        epoch = capture(["git", "-c", "safe.directory=" + str(workspace), "-C", workspace,
                         "show", "-s", "--format=%ct", args.source_commit])
        timestamp = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime("%Y%m%d.%H%M%S")
        options = ["BLDENV=trixie", "PLATFORM_ARCH=amd64", "USERNAME=admin",
                   "BAZEL_MIN_READINESS=bazel_disabled", "ENABLE_DOCKER_BASE_PULL=n",
                   # Use the upstream public registry; native version control
                   # still applies the recorded Debian image digest.
                   "DEFAULT_CONTAINER_REGISTRY=docker.io",
                   "MIRROR_URLS=http://deb.debian.org/debian/",
                   "MIRROR_SECURITY_URLS=http://deb.debian.org/debian-security/",
                   "SONIC_DPKG_CACHE_SOURCE=" + str(state / "native-cache"),
                   "SONIC_DPKG_CACHE_METHOD=none", "SONIC_DPKG_CACHE_METHOD_OVERRIDE=none",
                   "SONIC_CONFIG_USE_DOCKER_CACHE=n", "SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD=n",
                   "SONIC_BUILD_JOBS=1", "SONIC_BUILD_MEMORY=14g", "SONIC_BUILD_MEMORY_SWAP=14g",
                   "KERNEL_PROCURE_METHOD=build", "ENABLE_SBOM=n", "ENABLE_IMAGE_SIGNATURE=n",
                   "BUILD_NUMBER=0", "BUILD_TIMESTAMP=" + timestamp, "SOURCE_DATE_EPOCH=" + epoch]
        options.append("SONIC_BUILD_SLAVE_CA_BUNDLE=" + (str(ca_bundle) if ca_bundle else ""))
        for stage in ("init", "configure", "bazel-vs-native-inputs"):
            argv = ["runuser", "--preserve-environment", "--user", USER, "--",
                    "make", "-f", "Makefile.work", *options,
                    "PLATFORM=vs" if stage == "configure" else "PLATFORM=", stage]
            print(json.dumps({"native_stage": stage, "argv": argv}), flush=True)
            command(argv, cwd=workspace, env=env)
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=30)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
        docker_log.close()


def build(args):
    workspace, state, artifacts, output, root, spec = validate(args)
    state.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.chown(state, 1000, 1000)
    started, container = time.monotonic(), None
    receipt = {"schema": 1, "status": "running", "invocation": args.invocation,
               "source_commit": args.source_commit, "native_provenance": "target/bazel-native/provenance.json",
               "worker_image": spec["worker_image"], "scope": "Native Make source prerequisites; "
               "orchagent archive and final installer are built later by Bazel.",
               "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        argv = ["docker", "create", "--name", "sonic-native-ci-" + args.invocation,
                "--label", LABEL + "=" + args.invocation, "--privileged", "--init",
                "--cgroupns=private",
                "--cpus=4", "--memory=16g", "--memory-swap=16g",
                "--mount", "type=bind,src=" + str(root) + ",dst=" + str(root),
                "--env", "SONIC_NATIVE_CI_INVOCATION=" + args.invocation,
                "--workdir", str(workspace), spec["worker_image"],
                "python3", str(workspace / "tools/bazel/ci/native_build.py"), "--inside-worker",
                "--workspace", str(workspace), "--state", str(state), "--artifacts", str(artifacts),
                "--worker-spec", str(args.worker_spec.resolve()), "--source-commit", args.source_commit,
                "--invocation", args.invocation, "--output", str(output)]
        container = capture(argv)
        require(re.fullmatch(r"[0-9a-f]{64}", container), "invalid created native worker ID")
        receipt["container"] = container
        assert_owned(container, args.invocation, spec["worker_image"], root)
        command(["docker", "start", "--attach", container])
        trust_receipt = artifacts / "execution-trust.json"
        if trust_receipt.is_file():
            receipt["execution_trust"] = json.loads(trust_receipt.read_text())
        info = assert_owned(container, args.invocation, spec["worker_image"], root)
        require(not info["State"]["Running"] and info["State"]["ExitCode"] == 0,
                "native worker did not complete successfully")
        provenance = workspace / receipt["native_provenance"]
        require(provenance.is_file(), "native build omitted its source provenance")
        require(json.loads(provenance.read_text())["source_commit"] == args.source_commit,
                "native producer recorded a different source commit")
        receipt["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        receipt.update(status="failed", error=str(error), error_type=type(error).__name__)
    finally:
        if container:
            try:
                info = assert_owned(container, args.invocation, spec["worker_image"], root)
                if info["State"]["Running"]:
                    command(["docker", "stop", "--time", "45", container], timeout=60)
                command(["docker", "rm", container], timeout=30)
                receipt["worker_removed"] = True
            except (Exception, KeyboardInterrupt) as error:
                receipt.update(status="failed", worker_cleanup_error=str(error))
        receipt["wall_seconds"] = time.monotonic() - started
        output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": receipt["status"], "receipt": str(output)}), flush=True)
    return 0 if receipt["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "state", "artifacts", "worker-spec", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("source-commit", "invocation"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--inside-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    def interrupted(signum, _frame):
        raise InterruptedError("received signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.inside_worker:
            inside(args)
            return 0
        return build(args)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
