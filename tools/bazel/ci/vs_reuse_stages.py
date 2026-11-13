#!/usr/bin/env python3
"""Small stage bindings for the existing VS supervisor; no build runner or admission policy."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

CAP = 256 * 1024
BOOTSTRAP_INPUT = "src/sonic-build-hooks/buildinfo/sonic-build-hooks_1.0_all.deb"
POLICIES = ("artifacts/vs/swss-installed-images-policy.json",
            "artifacts/vs/syncd-installed-images-policy.json")


def require(value, reason):
    if not value:
        raise ValueError(reason)


def path(value):
    value = Path(value)
    require(value.is_absolute() and str(value) == os.path.normpath(str(value))
            and not any(c in str(value) for c in "\0\r\n"), "Noncanonical stage path")
    return value


def digest(value):
    return hashlib.sha256(value).hexdigest()


def decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            require(key not in result, "Duplicate preparation key")
            result[key] = item
        return result
    def constant(_):
        raise ValueError("Nonfinite preparation value")
    result = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    require(type(result) is dict, "Preparation record is not an object")
    return result


def stable_record(filename, *, json_record=True):
    filename = path(filename)
    require(not any(p.is_symlink() for p in (filename, *filename.parents)),
            "Symlink in preparation path")
    with os.fdopen(os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 1000
                and before.st_nlink == 1 and before.st_size <= CAP,
                "Preparation ownership, type or bound differs")
        body = stream.read(CAP + 1)
        after = os.fstat(stream.fileno())
        fields = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_uid,
                              row.st_gid, row.st_nlink, row.st_size,
                              row.st_mtime_ns, row.st_ctime_ns)
        require(len(body) == before.st_size and fields(before) == fields(after)
                and fields(filename.lstat()) == fields(before), "Preparation changed during read")
    return body, decode(body) if json_record else None


def verify_assembly_checkout(native, workspace, *, revision, tree, allowed_untracked=(), bootstrap_input=None):
    """Check the actual checkout before frontend Make or assembly source imports.

    The caller supplies the already verified native worker module and runs this
    binding as the worker user. Reuse its source_check implementation. Existing
    artifact data may be untracked; extra Make inputs and Python bytecode may not.
    This is not native acceptance or permission to assemble an image.
    """
    workspace = path(workspace)
    require(not any(p.is_symlink() for p in (workspace, *workspace.parents)),
            "Symlink in assembly checkout")
    require(re.fullmatch(r"[0-9a-f]{40}", revision or "")
            and re.fullmatch(r"[0-9a-f]{40}", tree or ""),
            "Missing assembly commit or tree")
    admitted = set()
    for name in allowed_untracked:
        require(type(name) is str and (name.startswith(("target/", "artifacts/")) or name == BOOTSTRAP_INPUT)
                and not name.endswith(".dep")
                and not any(c in name for c in "\0\r\n")
                and not any(part in {"", ".", ".."} for part in name.split("/")),
                "Only exact artifact data paths may be untracked")
        require(name not in admitted, "Duplicate admitted untracked path")
        admitted.add(name)

    if BOOTSTRAP_INPUT in admitted:
        require(type(bootstrap_input) is dict and set(bootstrap_input) == {"bytes", "sha256"}
                and type(bootstrap_input["bytes"]) is int and 0 < bootstrap_input["bytes"] <= CAP
                and type(bootstrap_input["sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", bootstrap_input["sha256"]),
                "Existing bootstrap input is not hash-bound")
        # The vendored buildinfo directory is normally gitignored. Admit only
        # this original data file, through the existing held regular-file reader.
        body, _ = stable_record(workspace / BOOTSTRAP_INPUT, json_record=False)
        require(len(body) == bootstrap_input["bytes"] and digest(body) == bootstrap_input["sha256"],
                "Existing bootstrap bytes differ from frozen input")
    else:
        require(bootstrap_input is None, "Unadmitted bootstrap declaration")

    native.source_check(workspace, revision)

    def observed(arguments):
        completed = native.run(["git", "-C", str(workspace), *arguments],
                               stdout=subprocess.PIPE)
        require(type(completed.stdout) is bytes and len(completed.stdout) <= CAP,
                "Unbounded or invalid checkout observation")
        return completed.stdout

    require(observed(["rev-parse", "HEAD^{tree}"]) == (tree + "\n").encode(),
            "Assembly checkout tree differs")
    submodules = observed(["submodule", "status", "--recursive"])
    for line in submodules.splitlines():
        require(re.match(rb" [0-9a-f]{40} ", line) is not None,
                "Assembly submodule is absent, changed or conflicted")
    untracked = observed(["ls-files", "--others", "-z"])
    require(not untracked or untracked.endswith(b"\0"), "Incomplete untracked-file observation")
    names = untracked[:-1].split(b"\0") if untracked else []
    require(len(names) == len(set(names)), "Duplicate untracked-file observation")
    for name in names:
        require(name.decode("utf-8", errors="strict") in admitted,
                "Unadmitted untracked assembly input")
    # Reuse the core check again to detect a checkout change during observation.
    native.source_check(workspace, revision)
    return {"status": "CHECKOUT_VERIFIED_ONLY", "revision": revision, "tree": tree,
            "recursive_submodules": "clean", "untracked_inputs": len(names),
            "native_acceptance_or_assembly": False}


def retain_preparation(workspace, destination):
    """Keep original first-phase bytes before the frontend rewrites its report."""
    require(os.getuid() == os.geteuid() == 1000, "Preparation custody requires the worker user")
    workspace, destination = path(workspace), path(destination)
    require(not any(p.is_symlink() for p in (destination, *destination.parents))
            and not os.path.lexists(destination), "Preserve existing preparation custody")
    names = ("slave-preparation.json", "k3s-storage.json")
    records = {name: stable_record(workspace / "artifacts/vs" / name) for name in names}
    preparation, storage = (records[name][1] for name in names)
    require(preparation.get("stage") == "before-daemon" and preparation.get("status") == "prepared",
            "First preparation phase did not pass")
    require(storage.get("status") == "passed", "Fresh storage preparation did not pass")
    destination.mkdir(mode=0o700)
    retained = {}
    with ExitStack() as cleanup:
        directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        cleanup.callback(os.close, directory)
        for name in names:
            body = records[name][0]
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as target:
                target.write(body)
            retained[name] = {"bytes": len(body), "sha256": digest(body)}
    return {"status": "FIRST_PHASE_BYTES_RETAINED_ONLY", "files": retained,
            "environment": {"SONIC_VS_PREPARATION_SHA256": retained[names[0]]["sha256"],
                            "SONIC_VS_STORAGE_SHA256": retained[names[1]]["sha256"]},
            "assembly_or_native_admission": False}


def planner_command(mode, *, controller, workspace, manifest, manifest_sha256, merge, source_head, source_tree,
                    producer=None, registry=None, receipt=None, receipt_sha256=None,
                    make_inputs=None, make_inputs_sha256=None, python=sys.executable):
    """Every canonical planner call gets a fresh isolated Python process."""
    require(mode in {"inputs", "admission", "environment", "metadata", "plan", "verify"},
            "Unsupported planner mode")
    require(re.fullmatch(r"[0-9a-f]{64}", manifest_sha256 or "")
            and all(re.fullmatch(r"[0-9a-f]{40}", item or "")
                    for item in (merge, source_head, source_tree)), "Missing pinned planner identity")
    command = [str(path(python)), "-I", "-S", "-B",
               str(path(controller) / "tools/bazel/ci/vs_reuse_plan.py"), mode,
               "--workspace", str(path(workspace)), "--manifest", str(path(manifest)),
               "--manifest-sha256", manifest_sha256, "--merge", merge,
               "--source-head", source_head, "--source-tree", source_tree]
    if mode in {"metadata", "plan"}:
        require(producer is not None and registry is not None, "Original kernel workspaces are required")
        command += ["--producer-workspace", str(path(producer)), "--registry-workspace", str(path(registry))]
    if mode in {"plan", "verify"}:
        require(receipt is not None, "Original archive capture is required")
        command += ["--receipt", str(path(receipt))]
    if mode == "plan":
        require(make_inputs is not None and re.fullmatch(r"[0-9a-f]{64}", make_inputs_sha256 or ""),
                "Actual configured input capture is required")
        command += ["--make-inputs", str(path(make_inputs)),
                    "--make-inputs-sha256", make_inputs_sha256]
    if mode == "verify":
        require(re.fullmatch(r"[0-9a-f]{64}", receipt_sha256 or ""), "Original capture hash is required")
        command += ["--receipt-sha256", receipt_sha256]
    if mode in {"metadata", "plan", "verify"}:
        command += ["--require-environment"]
    return command


def frontend_phase(controller, phase, retained=None):
    require(phase in {"before-daemon", "after-daemon"}, "Unsupported frontend phase")
    environment = {"SONIC_VS_REUSE_ONLY": "y", "SONIC_VS_SUPERVISED_PHASE": phase}
    if phase == "after-daemon":
        require(type(retained) is dict and retained.get("status") == "FIRST_PHASE_BYTES_RETAINED_ONLY",
                "Retained first-phase bytes are required")
        for key, name in (("SONIC_VS_PREPARATION_SHA256", "slave-preparation.json"),
                          ("SONIC_VS_STORAGE_SHA256", "k3s-storage.json")):
            value = retained["files"][name]["sha256"]
            require(re.fullmatch(r"[0-9a-f]{64}", value or "")
                    and retained["environment"][key] == value, "Retained first-phase hash differs")
            environment[key] = value
    return {"argv": ["/bin/bash", "--noprofile", "--norc",
                     str(path(controller) / "tools/bazel/ci/prepare_vs_slave.sh")],
            "environment": environment, "worker_user": True}


def postbuild_commands(context, *, tested_merge, expected_short_commit, profile_paths):
    """Construct only existing read/inspection interfaces; never the legacy build verifier."""
    require(re.fullmatch(r"[0-9a-f]{40}", tested_merge or "")
            and re.fullmatch(r"[0-9a-f]{7,40}", expected_short_commit or "")
            and tested_merge.startswith(expected_short_commit), "Installer source identity differs")
    require(set(POLICIES) <= set(profile_paths), "Installed-image policies are not bound in the input profile")
    workspace = path(context["workspace"])
    outputs = [workspace / "artifacts/vs/sonic-version.json", workspace / "artifacts/vs/installed-images.json"]
    require(all(not os.path.lexists(p) for p in outputs), "Preserve existing post-build outputs")
    interpreter = str(path(context.get("python", sys.executable)))
    return [
        {"label": "post-archive-verify", "argv": planner_command("verify", **context)},
        {"label": "installer-version", "argv": [interpreter, "-I", "-B",
            str(workspace / "scripts/validate_sonic_version.py"), str(workspace / "target/sonic-vs.bin"),
            "--installer", "--secure-upgrade-mode", "no_sign", "--expected-commit-id", expected_short_commit,
            "--json-output", str(outputs[0])]},
        {"label": "installed-images", "argv": [interpreter, "-I", "-B", str(workspace / "tools/bazel/ci/validate_runtime.py"),
            "--images-policy", str(workspace / POLICIES[0]), "--images-policy", str(workspace / POLICIES[1]),
            "--target", str(workspace / "target"), "--output", str(outputs[1])]},
    ]



def full_vs_workload(runtime, context):
    """Connect the existing supervisor to preparation, real capture and assembly.

    Canonical native admission remains in the frontend, after its source guard.
    Every assembly import therefore happens after the clean checkout check.
    """
    import pwd
    import time
    expected = {"workspace", "manifest", "manifest_sha256", "merge", "producer",
                "registry", "native_controller", "source_head", "source_tree",
                "expected_short_commit", "profile_paths", "manager_revision"}
    require(set(context) == expected, "Incomplete full VS caller context")
    for key in ("workspace", "manifest", "producer", "registry", "native_controller"):
        context[key] = path(context[key])
    require(all(re.fullmatch(r"[0-9a-f]{40}", context[key] or "")
                for key in ("source_head", "source_tree", "merge", "manager_revision")),
            "Missing actual source identity")
    require(re.fullmatch(r"[0-9a-f]{64}", context["manifest_sha256"] or ""),
            "Missing immutable input manifest")
    controller, workspace = runtime.ROOT, context["workspace"]
    common = {name: context[name] for name in ("workspace", "manifest", "manifest_sha256", "merge", "source_head", "source_tree")}
    common["controller"] = controller
    state = {}
    stage_timings = []

    def child(paths, label, argv, environment, seconds=900, *, assembly=False):
        # The frontend used this same soft limit in its existing shell export.
        if assembly:
            argv = ["/bin/bash", "--noprofile", "--norc", "-c",
                    'ulimit -Sn 524288; exec "$@"', "sonic-vs-build", *argv]
        started, outcome = time.monotonic(), "failed"
        try:
            result = runtime.bounded(argv, paths["state"], label, seconds, environment,
                                     worker_user=True, worker_umask=0o022,
                                     private_log_records=assembly,
                                     working_directory=workspace)
            outcome = "passed"
            return result
        finally:
            # Same setup/build/validation categories as the existing dashboard;
            # these are measured child spans, not a partition of job wall time.
            phase = ("build" if assembly else "validation" if label in {
                "full-vs-post-archive-verify", "full-vs-installer-version", "full-vs-installed-images"} else "setup")
            stage_timings.append({"stage": label, "phase": phase, "outcome": outcome,
                                  "duration_seconds": time.monotonic() - started})

    def workload(phase, docker, docker_environment, paths):
        require(phase in {"before-daemon", "after-daemon"}, "Unknown supervised VS phase")
        if phase == "before-daemon":
            require(not state, "Preparation phase already ran")
            row = paths["identity"]
            environment = {
                "PATH": "/usr/local/sbin:/usr/local/bin:" + runtime.PATH,
                "HOME": pwd.getpwuid(1000).pw_dir, "PYTHONDONTWRITEBYTECODE": "1",
                "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(paths["state"].parent),
                "GITHUB_RUN_ID": row["run"], "GITHUB_RUN_ATTEMPT": row["attempt"],
                "RUNNER_NAME": row["worker"], "WORKER_POOL": "sonic-slave-container-k3s",
                "VS_REUSE_INPUTS": str(context["manifest"]),
                "VS_REUSE_INPUTS_SHA256": context["manifest_sha256"],
                "VS_SOURCE_MERGE": context["merge"],
                "VS_SOURCE_HEAD": context["source_head"],
                "VS_SOURCE_TREE": context["source_tree"],
                "EXPECTED_MANAGER_REVISION": context["manager_revision"],
                "SONIC_VS_NATIVE_CONTROLLER_ROOT": str(context["native_controller"]),
                "SONIC_BAZEL_VS_ARCHIVE_PROFILE": "final23",
            }
            command = planner_command("environment", **common) + ["--json-environment"]
            build_environment = decode(child(paths, "full-vs-environment", command, environment))
            require(all(isinstance(k, str) and isinstance(v, str)
                        for k, v in build_environment.items()), "Build identity is not data")
            environment.update(build_environment)
            prepared = frontend_phase(controller, phase)
            child(paths, "full-vs-before-daemon", prepared["argv"],
                  dict(environment, **prepared["environment"]), seconds=1800)
            retained = decode(child(paths, "full-vs-retain-preparation", [
                sys.executable, "-I", "-S", "-B", str(controller / "tools/bazel/ci/vs_reuse_stages.py"),
                "retain-preparation", "--workspace", str(workspace),
                "--destination", str(paths["state"] / "original-preparation")], environment))
            storage = stable_record(workspace / "artifacts/vs/k3s-storage.json")[1]
            checkout, native = storage["assembly_checkout"], storage["native_admission"]
            require(checkout["revision"] == context["merge"]
                    and checkout["tree"] == context["source_tree"]
                    and native["assembly"]["commit"] == context["merge"]
                    and native["assembly"]["tree"] == context["source_tree"]
                    and native["source"]["head"] == context["source_head"]
                    and native["source"]["merge"] == context["merge"]
                    and native["source"]["tree"] == context["source_tree"],
                    "Prepared source differs from actual caller")
            state.update(environment=environment, retained=retained)
            return {"status": "passed", "preparation": retained,
                    "source_checkout": checkout, "native_admission": native}

        require(state and docker_environment is not None, "Missing first phase or private daemon")
        prepared = frontend_phase(controller, phase, state["retained"])
        environment = dict(state["environment"], **docker_environment, **prepared["environment"])
        child(paths, "full-vs-after-daemon", prepared["argv"], environment, seconds=1800)
        environment_path = paths["state"].parent / "sonic-vs-slave.env.json"
        environment_bytes, exported = stable_record(environment_path)
        require(set(exported) == {"schema", "unset", "environment"}
                and exported["schema"] == 1 and exported["unset"] == ["PLATFORM"]
                and type(exported["environment"]) is dict,
                "Prepared environment record differs")
        for key, value in exported["environment"].items():
            require(re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                    and isinstance(value, str) and "\0" not in value
                    and key not in {"PATH", "HOME", "BASH_ENV", "ENV", "GITHUB_WORKSPACE",
                                    "RUNNER_TEMP", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD",
                                    "LD_LIBRARY_PATH"}, "Unexpected prepared environment setting")
        environment.update(exported["environment"])
        environment.pop("PLATFORM", None)
        require(environment.get("BAZEL") == environment.get("BAZEL_REAL") == "/usr/bin/false"
                and environment.get("SONIC_DPKG_CACHE_METHOD") == "none"
                and environment.get("SONIC_DPKG_CACHE_METHOD_OVERRIDE") == "none"
                and environment.get("SONIC_BAZEL_CACHE_POLICY") == "remote-only"
                and environment.get("SONIC_DEB_BUILD_POLICY") == "reuse-only",
                "Prepared no-generation/cache policy differs")
        for key in ("DOCKER_HOST", "DOCKER_CONFIG"):
            require(environment.get(key) == docker_environment.get(key), "Private Docker endpoint differs")
        kernel = {name: context[name] for name in ("producer", "registry")}
        metadata = decode(child(paths, "full-vs-metadata-command",
                                planner_command("metadata", **common, **kernel), environment))
        require(metadata.get("status") == "METADATA_COMMAND_ONLY", "Metadata command did not pass")
        child(paths, "full-vs-metadata", metadata["argv"], environment)
        make_inputs = workspace / "target/sonic-vs.bin.make-inputs.tsv"
        capture_bytes = stable_record(make_inputs, json_record=False)[0]
        receipt = paths["state"] / "original-archives.json"
        planning = dict(common, **kernel, receipt=receipt, make_inputs=make_inputs,
                        make_inputs_sha256=digest(capture_bytes))
        result = decode(child(paths, "full-vs-plan", planner_command("plan", **planning), environment))
        require(result.get("status") == "CONFIGURED_INPUTS_VERIFIED_COMMANDS_ONLY"
                and result["assembly"]["commit"] == context["merge"]
                and result["assembly"]["tree"] == context["source_tree"],
                "Configured input closure did not pass")
        # This is the unchanged Make -> prepare.py verify path, including all
        # sixteen configured values. It is never ignored by the final Make.
        child(paths, "full-vs-kernel-verify", result["kernel_verify_argv"], environment)
        logs = child(paths, "full-vs-assembly", result["build_argv"], environment,
                     seconds=runtime.FULL_VS_BODY_SECONDS, assembly=True)
        post_context = dict(common, receipt=receipt,
                            receipt_sha256=result["original_archive_capture"]["sha256"])
        checks = postbuild_commands(post_context, tested_merge=context["merge"],
                                    expected_short_commit=context["expected_short_commit"],
                                    profile_paths=context["profile_paths"])
        for check in checks:
            child(paths, "full-vs-" + check["label"], check["argv"], environment)
        return {"status": "passed", "container_workload_executed": True,
                "make_inputs_sha256": digest(capture_bytes),
                "environment_sha256": digest(environment_bytes),
                "configured_packages": result["configured_packages"],
                "configured_sources": result["configured_sources"],
                "assembly_logs": logs, "postbuild_checks": [item["label"] for item in checks],
                "native_acceptance": "unchanged canonical original-domain checks",
                "scope": "Full VS assembly and separate post-build checks; daemon closure is owned by the supervisor."}
    workload.stage_timings = stage_timings
    return workload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("retain-preparation",))
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(retain_preparation(args.workspace, args.destination), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
