#!/usr/bin/env python3
"""Transfer verified native inputs between jobs and check unprivileged assembly.

Only the native job calls prepare-host. The assembly job imports a checksummed
handoff and runs the public Make command inside the restricted worker. No native
Make command, host finalization, or Docker socket is available in that worker.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
from build_events import bep_outputs, output_identity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gzip"))
from source_archive import SOURCES

TARGET = "//tools/bazel/image/vs:sonic-vs.bin"
PROFILE = ["BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y",
           "ENABLE_SBOM=n", "SONIC_ENABLE_IMAGE_SIGNATURE=n", "SECURE_UPGRADE_MODE=no_sign",
           "ENABLE_ASAN=n", "SONIC_INSTALL_DEBUG_TOOLS=n", "INSTALL_DEBUG_TOOLS=n"]
# This CI worker reads a checkout owned by the runner account. Resolution
# evidence is retained by the hosted producer; final assembly writes only its
# declared output directories, without updating a lockfile or workspace links.
READ_ONLY_WORKSPACE_ARGS = ["--lockfile_mode=off", "--symlink_prefix=/"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(argv, **kwargs):
    subprocess.run([str(arg) for arg in argv], check=True, **kwargs)


def capture(argv, **kwargs):
    return subprocess.check_output([str(arg) for arg in argv], text=True, **kwargs)


def file_info(path):
    require(path.is_file() and not path.is_symlink(), "expected regular file: " + str(path))
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return {"sha256": value.hexdigest(), "bytes": path.stat().st_size}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def revision(workspace):
    return capture(["git", "rev-parse", "HEAD"], cwd=workspace).strip()


def paths(workspace):
    return workspace / "target/bazel-image-inputs", workspace / "artifacts/unprivileged"


def distdir_args(workspace):
    """Recheck the carried public source bytes before a fresh worker uses them."""
    root = paths(workspace)[0] / "source-downloads"
    arguments = []
    for name, (version, integrity) in SOURCES.items():
        directory = root / (name + "-" + version)
        archive = directory / "src.tar.gz"
        require(archive.is_file() and not archive.is_symlink(),
                "missing carried source archive: " + str(archive))
        expected = base64.b64decode(integrity.split("-", 1)[1]).hex()
        require(hashlib.sha512(archive.read_bytes()).hexdigest() == expected,
                name + " carried source archive does not match its BCR SHA512")
        arguments.append("--distdir=" + str(directory))
    return arguments


def prepare_worker_paths(workspace, directories, files=()):
    """Give the worker access to job-owned paths without requiring host sudo."""
    inputs, _ = paths(workspace)
    image = json.loads((inputs / "execution-environment.json").read_text())["worker_image"]
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "invalid worker image ID")
    arguments = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                 "--user=0:0", "--cap-drop=ALL", "--cap-add=CHOWN", "--cap-add=FOWNER",
                 "--security-opt=no-new-privileges", "--entrypoint=/usr/bin/python3"]
    changes = []
    for path, directory in [(path, True) for path in directories] + [(path, False) for path in files]:
        require(path.resolve() == path and path.is_relative_to(workspace) and path != workspace,
                "ownership setup must stay within explicit job paths")
        require(path.is_dir() if directory else path.is_file(), "missing ownership setup path")
        require("," not in str(path) and "\n" not in str(path), "unsupported ownership setup path")
        destination = "/ownership/" + str(len(changes))
        arguments += ["--mount", "type=bind,source=" + str(path) + ",target=" + destination]
        changes.append([destination, 0o775 if directory else 0o664])
    require(changes, "ownership setup needs at least one path")
    script = "import json,os,sys\nfor path,mode in json.loads(sys.argv[2]):\n os.chown(path,1000,int(sys.argv[1]))\n os.chmod(path,mode)"
    command([*arguments, image, "-c", script, str(os.getgid()), json.dumps(changes)])


def launcher(workspace, output, worker=None, unprivileged=False):
    inputs, _ = paths(workspace)
    result = [sys.executable, str(workspace / "tools/bazel/image/run.py"),
              "--workspace", str(workspace), "--mount-root", str(workspace),
              "--worker-spec", str(inputs / "execution-environment.json"),
              "--output-user-root", str(output)]
    if worker:
        result += ["--persistent-worker", worker]
    if unprivileged:
        result += ["--unprivileged"]
    return result


def output_by_suffix(outputs, target, suffix):
    matches = [path for path in outputs[target] if path.name.endswith(suffix)]
    require(len(matches) == 1, "missing/ambiguous output for " + target + ": " + suffix)
    return matches[0]


def prepare_host(workspace):
    downloads = distdir_args(workspace)
    inputs, evidence = paths(workspace)
    evidence.mkdir(parents=True, exist_ok=True)
    output = workspace / "target/bazel-host-state"
    output.mkdir(exist_ok=True)
    backend.select_backend(workspace, build_with_bazel_when_available="y",
                           use_container_image_store_as_backend="y")
    events = evidence / "host-events.jsonl"
    # The host worker writes this file but does not need to own other artifacts.
    events.touch(exist_ok=False)
    prepare_worker_paths(workspace, [output], [events])
    worker = "sonic-host-" + os.environ.get("GITHUB_RUN_ID", str(os.getpid())) + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    launch = launcher(workspace, output, worker)
    try:
        command([*launch, "--", "build", TARGET + "_host", *READ_ONLY_WORKSPACE_ARGS, *downloads,
                 "--build_event_json_file=" + str(events)])
    finally:
        command([*launch, "--worker-action", "stop"])
    outputs = bep_outputs(events, [TARGET + "_host"], output)
    argv = [sys.executable, str(workspace / "tools/bazel/image/prepared_host.py"), "export"]
    for flag, suffix in (("fs", ".squashfs"), ("boot", ".boot.tar"),
                         ("platform", ".platform.tar.gz"), ("receipt", ".receipt.json")):
        path = output_by_suffix(outputs, TARGET + "_host", suffix)
        require(file_info(path) == output_identity(events, path), "host output differs from this build's BEP")
        argv += ["--" + flag, str(path)]
    command([*argv, "--output", inputs / "prepared-host"])
    backend.select_backend(workspace, build_with_bazel_when_available="y",
                           use_container_image_store_as_backend="y")


def pack(workspace, destination):
    require(not destination.exists(), "handoff output must be fresh")
    destination.mkdir(parents=True)
    inputs, _ = paths(workspace)
    prepared = inputs / "prepared-host"
    require(all((prepared / name).is_file() for name in
                ("fs.squashfs", "boot.tar", "platform.tar.gz", "receipt.json")),
            "prepared host is missing")
    worker = json.loads((inputs / "execution-environment.json").read_text())["worker_image"]
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", worker), "invalid worker image ID")
    generated = workspace / "tools/bazel/image/vs/BUILD.bazel"
    require(generated.read_bytes() == generated.with_name("BUILD.bazel.in").read_bytes(),
            "generated assembly graph differs from the checked-in template")
    archive = destination / "inputs.tar"
    with tarfile.open(archive, "w") as stream:
        selected = sorted(inputs.rglob("*")) + [generated]
        for path in selected:
            require(not path.is_symlink() and (path.is_file() or path.is_dir()),
                    "handoff input must be a regular file or directory")
            stream.add(path, arcname=path.relative_to(workspace), recursive=False)
    command(["docker", "save", "--output", destination / "worker.tar", worker])
    producer = workspace / "target/bazel-native/provenance.json"
    shutil.copyfile(producer, destination / "native-provenance.json")
    write_json(destination / "handoff.json", {
        "schema": 1, "source_commit": revision(workspace), "worker_image": worker,
        "scope": "Native source prerequisites and privileged host preparation; no installer assembled",
        "files": {name: file_info(destination / name) for name in
                  ("inputs.tar", "worker.tar", "native-provenance.json")},
    })


def validate_handoff(workspace, source):
    receipt = json.loads((source / "handoff.json").read_text())
    require(receipt.get("schema") == 1 and receipt.get("source_commit") == revision(workspace),
            "handoff source revision differs from this assembly checkout")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", receipt.get("worker_image", "")),
            "invalid handoff worker identity")
    require(set(receipt.get("files", {})) == {"inputs.tar", "worker.tar", "native-provenance.json"},
            "unexpected handoff file inventory")
    for name, expected in receipt["files"].items():
        require(file_info(source / name) == expected, "handoff checksum mismatch: " + name)
    return receipt


def extract_inputs(archive, workspace):
    generated = "tools/bazel/image/vs/BUILD.bazel"
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            require(str(path) == member.name and not path.is_absolute() and ".." not in path.parts,
                    "invalid handoff member path")
            require(member.name not in names, "duplicate handoff member")
            names.add(member.name)
            require(member.name.startswith("target/bazel-image-inputs/") or member.name == generated,
                    "unexpected handoff member: " + member.name)
            require(member.isfile() or member.isdir(), "linked/special handoff member")
            destination = workspace / member.name
            require(destination.resolve() == destination and not destination.exists(),
                    "refusing to replace an existing or linked handoff member")
        for member in members:
            destination = workspace / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as incoming, destination.open("xb") as output:
                    shutil.copyfileobj(incoming, output)
                destination.chmod(member.mode & 0o777 & ~0o022)


def restore(workspace, source):
    receipt = validate_handoff(workspace, source)
    inputs, evidence = paths(workspace)
    require(not inputs.exists(), "assembly inputs must be fresh")
    extract_inputs(source / "inputs.tar", workspace)
    actual = json.loads((inputs / "execution-environment.json").read_text())["worker_image"]
    require(actual == receipt["worker_image"], "handoff worker identity differs from prepared inputs")
    command(["docker", "load", "--input", source / "worker.tar"])
    loaded = capture(["docker", "image", "inspect", "--format", "{{.Id}}", actual]).strip()
    require(loaded == actual, "loaded worker differs from declared immutable image")
    evidence.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "handoff.json", evidence / "handoff.json")
    shutil.copyfile(source / "native-provenance.json", evidence / "native-provenance.json")


RUNTIME_PROBE = '''import json, os
from pathlib import Path
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
value = {"uid": os.getuid(), "gid": os.getgid(), "status": {key: status[key].strip() for key in ("CapEff", "CapPrm", "CapBnd", "NoNewPrivs", "Seccomp")},
         "docker_sockets": [str(path) for path in (Path("/var/run/docker.sock"), Path("/run/docker.sock")) if path.exists()],
         "docker_host": os.environ.get("DOCKER_HOST", "")}
print(json.dumps(value))
'''


def validate_restrictions(inspect, runtime):
    host, config = inspect["HostConfig"], inspect["Config"]
    require(host.get("Privileged") is False, "assembly container is privileged")
    require(config.get("User") == "1000:1000", "assembly container does not select UID/GID1000")
    require(set(host.get("CapDrop") or []) == {"ALL"} and not host.get("CapAdd"),
            "assembly container capabilities are not completely dropped")
    security = host.get("SecurityOpt") or []
    require(any(value in {"no-new-privileges", "no-new-privileges:true"} for value in security),
            "assembly container permits new privileges")
    require(not any("unconfined" in value for value in security), "assembly container disables security confinement")
    require(not host.get("Devices") and host.get("PidMode") != "host" and host.get("NetworkMode") != "host",
            "assembly container exposes host devices or namespaces")
    require(not any("docker.sock" in mount.get("Destination", "") or "docker.sock" in mount.get("Source", "")
                    for mount in inspect.get("Mounts", [])), "assembly container mounts a Docker socket")
    require(runtime["uid"] == runtime["gid"] == 1000 and not runtime["docker_sockets"]
            and not runtime["docker_host"], "assembly process is root or can access Docker")
    require(all(int(runtime["status"][key], 16) == 0 for key in ("CapEff", "CapPrm", "CapBnd")),
            "assembly process retains capabilities")
    require(runtime["status"]["NoNewPrivs"] == "1" and runtime["status"]["Seccomp"] == "2",
            "assembly process lacks no-new-privileges or seccomp filtering")
    return {"schema": 1, "status": "passed", "container": {
        "image": inspect["Image"], "privileged": False, "user": config["User"],
        "cap_drop": host["CapDrop"], "security_opt": security}, "process": runtime}


def assemble(workspace, worker):
    downloads = distdir_args(workspace)
    inputs, evidence = paths(workspace)
    evidence.mkdir(parents=True, exist_ok=True)
    output = workspace / "target/bazel-unprivileged-state"
    require(not output.exists(), "CI final assembly requires a fresh Bazel output root")
    output.mkdir()
    # Ownership applies only to this job's fresh build/output directories.
    shared = [output, evidence, workspace / "target", inputs]
    prepare_worker_paths(workspace, shared)
    launch = launcher(workspace, output, worker, unprivileged=True)
    command([*launch, "--worker-action", "start"])
    execute = ["docker", "exec", "--workdir", str(workspace),
               "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory",
               "--env", "GIT_CONFIG_VALUE_0=" + str(workspace),
               "--env", "BAZEL_IMAGE_ARGS=" + shlex.join(["--remote_cache=", "--disk_cache=",
                                                          *READ_ONLY_WORKSPACE_ARGS, *downloads]), worker]
    try:
        inspection = json.loads(capture(["docker", "inspect", worker]))[0]
        runtime = json.loads(capture([*execute, "python3", "-c", RUNTIME_PROBE]))
        write_json(evidence / "privileges.json", validate_restrictions(inspection, runtime))
        # The public target runs inside the restricted container, with no Docker
        # socket; build.py selects its direct Bazel execution path there.
        command([*execute, "make", *PROFILE, "BAZEL_IMAGE_OUTPUT_USER_ROOT=" + str(output), "target/sonic-vs.bin"])
        targets = [TARGET, TARGET + "_host", TARGET + "_dockerfs", TARGET + "_fs"]
        events = evidence / "assembly-events.jsonl"
        command([*execute, "/usr/local/bin/bazel", "--output_user_root=" + str(output), "build",
                 *targets, "--jobs=4", "--local_resources=cpu=4", "--spawn_strategy=processwrapper-sandbox",
                 "--remote_cache=", "--disk_cache=", *READ_ONLY_WORKSPACE_ARGS, *downloads,
                 "--build_event_json_file=" + str(events)])
        built = bep_outputs(events, targets, output)
        selected = {"installer": output_by_suffix(built, TARGET, ".bin"),
                    "payload": output_by_suffix(built, TARGET + "_fs", ".zip"),
                    "dockerfs": output_by_suffix(built, TARGET + "_dockerfs", ".tar.gz"),
                    "squashfs": output_by_suffix(built, TARGET + "_host", ".squashfs"),
                    "boot": output_by_suffix(built, TARGET + "_host", ".boot.tar"),
                    "platform": output_by_suffix(built, TARGET + "_host", ".platform.tar.gz")}
        for path in selected.values():
            require(file_info(path) == output_identity(events, path), "assembly output differs from its BEP identity")
        published = workspace / "target/sonic-vs.bin"
        require(file_info(published) == file_info(selected["installer"]), "published installer differs from validated Bazel output")
        verify = [*execute, "python3", "tools/bazel/ci/verify_image.py"]
        for name, path in selected.items():
            verify += ["--" + name, str(path)]
        command([*verify, "--output", evidence / "byte-chain.json"])
        write_json(evidence / "result.json", {"schema": 1, "status": "passed", "source_commit": revision(workspace),
            "scope": "Actual source-produced VS installer assembled in a nonroot, capability-free container; no guest boot claim",
            "installer": file_info(published), "worker_image": inspection["Image"]})
    finally:
        command([*launch, "--worker-action", "stop"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare-host", "pack", "restore", "assemble"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--worker")
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    if args.action == "prepare-host":
        prepare_host(workspace)
    elif args.action == "pack":
        require(args.handoff is not None, "pack requires --handoff")
        pack(workspace, args.handoff.resolve())
    elif args.action == "restore":
        require(args.handoff is not None, "restore requires --handoff")
        restore(workspace, args.handoff.resolve(strict=True))
    else:
        require(args.worker is not None, "assemble requires --worker")
        assemble(workspace, args.worker)


if __name__ == "__main__":
    main()
