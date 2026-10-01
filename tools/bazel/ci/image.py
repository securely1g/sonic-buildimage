#!/usr/bin/env python3
"""Build and verify a VS installer in a fresh, dedicated CI checkout.

Only the package action cache and repository downloads are reusable between
jobs. Full image outputs stay in this invocation's Bazel output root; the same
worker/server reuses compiled SWSS actions within the job.
"""

import argparse
import datetime
import errno
import json
import os
import re
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid

import image_inputs
import source_workspace
import trust


IMAGE = "//tools/bazel/image/vs:sonic-vs.bin"
RUNTIME = "//dockers/docker-orchagent:docker-orchagent.gz"
TARGETS = [IMAGE, IMAGE + "_host", IMAGE + "_fs", IMAGE + "_dockerfs", RUNTIME]
COMPONENTS = ["src/sonic-build-infra", "src/sonic-dash-api", "src/sonic-sairedis",
              "src/sonic-swss", "src/sonic-swss-common"]
OPTIONS = ["--jobs=4", "--local_resources=cpu=4", "--local_resources=memory=10000",
           "--lockfile_mode=off", "--noshow_progress", "--color=no", "--curses=no"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_paths(workspace, state, artifacts):
    workspace = workspace.resolve(strict=True)
    state, artifacts = state.resolve(), artifacts.resolve()
    require((workspace / ".git").is_dir() and not (workspace / ".git").is_symlink(),
            "image CI requires a fresh standalone clone with a .git directory")
    require((workspace / "MODULE.bazel").is_file(), "workspace is not a Bazel checkout")
    mount_root = workspace.parent
    require(mount_root != Path("/"), "workspace must have a dedicated build-area parent")
    require(state != mount_root and state.is_relative_to(mount_root)
            and not state.is_relative_to(workspace) and not workspace.is_relative_to(state),
            "state must be a dedicated sibling of the checkout inside its build area")
    require(artifacts not in (workspace, state) and
            (artifacts.is_relative_to(workspace) or artifacts.is_relative_to(state)),
            "artifacts must be a dedicated directory inside workspace or state")
    require(not artifacts.exists() or not any(artifacts.iterdir()),
            "artifacts must be empty to prevent publishing stale results")
    return workspace, state, artifacts, mount_root


def chown_tree(root, uid, gid):
    """Change only this explicit tree; never follow a source/cache symlink."""
    os.chown(root, uid, gid, follow_symlinks=False)
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)


def source_provenance(workspace):
    def git(directory, *arguments):
        return subprocess.check_output([
            "git", "-c", "safe.directory=" + str(directory), "-C", str(directory), *arguments],
            text=True).strip()

    result = {"source_commit": git(workspace, "rev-parse", "HEAD"), "components": {}}

    def visit(directory, prefix=""):
        git(directory, "diff", "--quiet", "HEAD", "--ignore-submodules=none", "--")
        require(not git(directory, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"),
                "checkout contains modified or untracked source files: " + (prefix or "."))
        # Walk the recorded Git tree, rather than only the five Bazel modules:
        # native prerequisite builds consume the complete recursive checkout.
        for entry in git(directory, "ls-tree", "-r", "-z", "HEAD").split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode, kind, recorded = metadata.split()
            if mode != "160000":
                continue
            require(kind == "commit", "invalid submodule gitlink")
            component = prefix + name
            child = directory / name
            require((child / ".git").exists(), "component submodule is not initialized: " + component)
            actual = git(child, "rev-parse", "HEAD")
            require(actual == recorded, "component HEAD differs from recorded gitlink: " + component)
            result["components"][component] = {"commit": actual, "gitlink": recorded}
            visit(child, component + "/")

    visit(workspace)
    require(set(COMPONENTS) <= result["components"].keys(), "required SONiC component submodules are missing")
    result["source_clean"] = True
    return result


def capture(command, workspace, artifacts, receipt, name):
    record = {"argv": [str(value) for value in command], "log": name + ".log"}
    receipt["commands"].append(record)
    started = time.monotonic()
    try:
        process = subprocess.run(record["argv"], cwd=workspace, capture_output=True, text=True)
        record["returncode"] = process.returncode
        (artifacts / record["log"]).write_text(process.stdout + process.stderr)
        require(process.returncode == 0, name + " failed; see " + record["log"])
        return process.stdout
    finally:
        record["wall_seconds"] = time.monotonic() - started


def build_worker(workspace, state, artifacts, receipt, invocation, ca_bundle=None):
    """Build the public execution recipe locally; never load a saved worker."""
    context = state / ("worker-" + invocation)
    context.mkdir()
    recipe = workspace / "tools/bazel/image/worker"
    sources = ("Dockerfile", ".dockerignore", "prepare-worker-inputs.sh")
    for name in sources:
        shutil.copyfile(recipe / name, context / name)
    receipt["worker_recipe"] = {name: image_inputs.sha256(recipe / name) for name in sources}
    installer = workspace / "tools/bazel/ci/trust.py"
    shutil.copyfile(installer, context / "install-trust.py")
    receipt["worker_recipe"]["install-trust.py"] = image_inputs.sha256(installer)
    receipt["execution_trust"] = trust.stage_bundle(ca_bundle, context / "build-ca-bundle.pem")
    execute(["bash", str(context / "prepare-worker-inputs.sh")], workspace, artifacts, receipt, "worker-inputs")
    execute(["docker", "build", "--platform", "linux/amd64", "--iidfile", str(context / "image.id"),
             str(context)], workspace, artifacts, receipt, "worker-build")
    worker_image = (context / "image.id").read_text().strip()
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", worker_image), "worker build did not return an immutable image ID")
    details = json.loads(capture(["docker", "image", "inspect", worker_image],
                                workspace, artifacts, receipt, "worker-inspect"))
    require(len(details) == 1 and re.fullmatch(r"sha256:[0-9a-f]{64}", details[0].get("Id", "")) and
            details[0]["Os"] == "linux" and details[0]["Architecture"] == "amd64",
            "built worker identity/platform mismatch")
    # Classic and containerd stores may expose config vs manifest IDs. Resolve
    # the build's immutable ID, then use the exact ID returned by this daemon.
    receipt["worker_build_id"] = worker_image
    worker_image = details[0]["Id"]
    version = capture(["docker", "run", "--rm", "--network", "none", "--read-only",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--entrypoint", "/usr/local/bin/bazel", worker_image, "--version"],
                      workspace, artifacts, receipt, "worker-bazel-version").strip()
    expected = (workspace / ".bazelversion").read_text().strip()
    require(version == "bazel " + expected, "built worker Bazel version differs from checkout .bazelversion")
    receipt["bazel_version"] = expected
    spec = context / "execution-environment.json"
    spec.write_text(json.dumps(dict(image_inputs.ENVIRONMENT, worker_image=worker_image), indent=2) + "\n")
    return spec


def execute(command, workspace, artifacts, receipt, name):
    log = artifacts / (name + ".log")
    record = {"argv": [str(value) for value in command], "log": log.name}
    receipt["commands"].append(record)
    started = time.monotonic()
    process = None
    try:
        with log.open("w") as output:
            process = subprocess.Popen(record["argv"], cwd=workspace, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in process.stdout:
                print(line, end="", flush=True)
                output.write(line)
            record["returncode"] = process.wait()
        if record["returncode"]:
            raise RuntimeError(f"{name} failed with exit {record['returncode']}; see {log}")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                # The native helper owns a separate Docker daemon/container.
                # Allow its bounded stop (60s) and removal (30s) to finish before
                # killing the helper; otherwise cancellation can orphan it.
                process.wait(timeout=105 if name == "native-build" else 30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            record["returncode"] = process.returncode
        record["wall_seconds"] = time.monotonic() - started


def bep_outputs(path, targets, output_root):
    """Resolve successful target default outputs from this build's BEP only."""
    named, completed, finished, completion_seen = {}, {}, False, False
    with path.open() as stream:
        for line in stream:
            event = json.loads(line)
            identity = event.get("id", {})
            if "namedSet" in identity:
                named[identity["namedSet"]["id"]] = event["namedSetOfFiles"]
            if "targetCompleted" in identity:
                target = identity["targetCompleted"]
                if target.get("label") in targets and not target.get("aspect"):
                    require(target["label"] not in completed, "ambiguous target completion in BEP")
                    completed[target["label"]] = event["completed"]
            if "buildFinished" in identity:
                require(not completion_seen, "ambiguous build completion in BEP")
                completion_seen = True
                completion = event.get("finished")
                require(isinstance(completion, dict) and isinstance(completion.get("exitCode"), dict),
                        "BEP build completion lacks an explicit exitCode message")
                exit_code = completion["exitCode"]
                # Protobuf JSON omits scalar defaults, including successful code
                # zero. Require the enclosing message and overall success first
                # so a missing/failed completion cannot be mistaken for success.
                code = exit_code.get("code", 0)
                finished = (completion.get("overallSuccess") is True and type(code) is int and code == 0
                            and exit_code.get("name", "SUCCESS") == "SUCCESS")
    require(finished, "BEP does not report a successful completed build")

    def files(identifier, ancestors):
        require(identifier not in ancestors, "cyclic BEP named file sets")
        require(identifier in named, "missing BEP named file set")
        node = named[identifier]
        for file in node.get("files", []):
            uri = urllib.parse.urlparse(file.get("uri", ""))
            require(uri.scheme == "file" and not uri.netloc and not uri.query and not uri.fragment,
                    "BEP output must be a local file URI")
            output = Path(urllib.parse.unquote(uri.path)).resolve(strict=True)
            require(output.is_relative_to(output_root) and output.is_file(),
                    "BEP output escapes this invocation's Bazel output root")
            require(output.stat().st_size > 0, "empty Bazel output: " + str(output))
            yield output
        for child in node.get("fileSets", []):
            yield from files(child["id"], ancestors | {identifier})

    result = {}
    for target in targets:
        require(target in completed and completed[target].get("success"),
                "BEP target did not succeed: " + target)
        groups = [group for group in completed[target].get("outputGroup", [])
                  if group["name"] == "default"]
        require(len(groups) == 1 and not groups[0].get("incomplete"),
                "BEP lacks a complete default output group: " + target)
        outputs = {file for group in groups for node in group.get("fileSets", [])
                   for file in files(node["id"], set())}
        require(outputs, "BEP target has no output files: " + target)
        result[target] = outputs
    return result


def named_output(outputs, target, name):
    matches = [path for path in outputs[target] if path.name == name]
    require(len(matches) == 1, "missing or ambiguous Bazel output: " + name)
    return matches[0]


def publish(source, artifacts, name):
    output = artifacts / name
    method = "hardlink"
    try:
        os.link(source, output, follow_symlinks=False)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # Different filesystems cannot share an inode. Exclusive creation keeps
        # this fallback from overwriting a previous artifact or symlink.
        with source.open("rb") as incoming, output.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        method = "copy"
    info = {"file": name, "bytes": output.stat().st_size, "sha256": image_inputs.sha256(output),
            "publication_method": method}
    require(info["bytes"] > 0 and info["sha256"] == image_inputs.sha256(source),
            "published artifact differs from Bazel output: " + name)
    return info


def build(args):
    workspace, state, artifacts, mount_root = validate_paths(args.workspace, args.state, args.artifacts)
    require(os.geteuid() in (0, 1000), "run image CI as root or worker UID 1000")
    original_owner = (workspace.stat().st_uid, workspace.stat().st_gid)
    artifacts.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    receipt_path = artifacts / "image-receipt.json"
    started = time.monotonic()
    invocation = uuid.uuid4().hex[:16]
    output_root = state / ("output-" + invocation)
    bazel_workspace = state / ("bazel-source-" + invocation)
    worker_name = "sonic-vs-ci-" + invocation
    launcher, worker_attempted, ownership_changed = None, False, False
    output_identity = None
    receipt = {"schema": 1, "status": "running", "commands": [], "outputs": {},
               "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "worker": worker_name, "output_user_root": str(output_root),
               "output_cleanup": {"path": str(output_root), "status": "not_created"},
               "cache_policy": "Package disk cache and repository cache persist; full image disk cache disabled.",
               "scope": "Native prerequisites built from this checkout, then Bazel SWSS and VS image assembly "
                        "from a separate pristine checkout of the same recorded revisions; "
                        "installer byte-chain verification. No boot or forwarding test."}
    try:
        require(not artifacts.is_relative_to(output_root), "artifacts cannot be inside the invocation output root")
        require(not output_root.is_symlink(), "invocation output root must not be a symlink")
        require(not (workspace / "target").exists() and not (workspace / "target").is_symlink(),
                "source image CI requires a fresh checkout without retained target outputs")
        source = source_provenance(workspace)
        receipt.update(source)
        # Claim scratch before doing any work; never adopt another invocation.
        output_root.mkdir()
        created = output_root.lstat()
        output_identity = (created.st_dev, created.st_ino)
        receipt["output_cleanup"]["status"] = "retained"
        for path in (state / "repository-cache", state / "package-cache"):
            path.mkdir(parents=True, exist_ok=True)
        # Native package recipes legitimately create or rewrite source files.
        # Freeze independent Git checkouts before Make starts; never pass its
        # working directories or ignored build products to Bazel.
        clone_started = time.monotonic()
        source_record = source_workspace.clone(workspace, bazel_workspace, source)
        source_record["wall_seconds"] = time.monotonic() - clone_started
        (artifacts / "bazel-source-receipt.json").write_text(
            json.dumps(source_record, indent=2, sort_keys=True) + "\n")
        receipt["bazel_source"] = {"workspace": str(bazel_workspace),
                                  "receipt": "bazel-source-receipt.json",
                                  "clone_seconds": source_record["wall_seconds"]}
        if os.geteuid() == 0:
            ownership_changed = True
            for path in (workspace, state):
                chown_tree(path, 1000, 1000)
        spec = build_worker(workspace, state, artifacts, receipt, invocation,
                            getattr(args, "ca_bundle", None))
        native_receipt = artifacts / "native-receipt.json"
        native_passed = False
        try:
            execute([sys.executable, str(workspace / "tools/bazel/ci/native_build.py"),
                     "--workspace", str(workspace), "--state", str(state / ("native-" + invocation)),
                     "--artifacts", str(artifacts / "native"), "--worker-spec", str(spec),
                     "--source-commit", source["source_commit"], "--invocation", invocation,
                     "--output", str(native_receipt)], workspace, artifacts, receipt, "native-build")
            native_passed = True
        finally:
            # Keep evidence of native mutations without resetting or cleaning
            # the native checkout, including when its build fails.
            try:
                audit = source_workspace.audit(workspace, source)
                (artifacts / "native-source-audit.json").write_text(
                    json.dumps(audit, indent=2, sort_keys=True) + "\n")
                receipt["native_source_audit"] = "native-source-audit.json"
            except Exception as error:
                receipt["native_source_audit_error"] = str(error)
                if native_passed:
                    raise
        receipt["bazel_source"]["verification_after_native"] = source_workspace.verify(bazel_workspace, source)
        prepare_started = time.monotonic()
        image_inputs.prepare(native_receipt, workspace, bazel_workspace, state / ("prepare-" + invocation),
                             artifacts / "input-receipt.json", spec, source, invocation)
        receipt["input_preparation_seconds"] = time.monotonic() - prepare_started
        spec = bazel_workspace / "target/bazel-image-inputs/execution-environment.json"
        receipt["execution_environment"] = json.loads(spec.read_text())
        host_config = json.loads((bazel_workspace / "target/bazel-image-inputs/host-config.json").read_text())
        receipt["native_host_identity"] = host_config["identity"]
        # Host-side preparation creates new inputs after the initial ownership
        # change. Give only this generated bundle to the Bazel worker user.
        if os.geteuid() == 0:
            chown_tree(bazel_workspace / "target", 1000, 1000)
        launcher = [sys.executable, str(bazel_workspace / "tools/bazel/image/run.py"),
                    "--workspace", str(bazel_workspace), "--mount-root", str(mount_root),
                    "--worker-spec", str(spec), "--bazel", "/usr/local/bin/bazel",
                    "--output-user-root", str(output_root), "--repository-cache", str(state / "repository-cache"),
                    "--worker-cpus", "4", "--worker-memory-gib", "12",
                    "--persistent-worker", worker_name]

        def bazel(name, targets, cache):
            return launcher + ["--", "build", *OPTIONS, "--disk_cache=" + cache,
                "--build_event_json_file=" + str(artifacts / (name + ".bep.jsonl")),
                "--profile=" + str(artifacts / (name + ".profile.json.gz")), *targets]

        worker_attempted = True
        execute(bazel("package", ["@sonic_swss//dist:swss_pkg"], str(state / "package-cache")),
                bazel_workspace, artifacts, receipt, "package")
        execute(bazel("image", TARGETS, ""), bazel_workspace, artifacts, receipt, "image")
        outputs = bep_outputs(artifacts / "image.bep.jsonl", TARGETS, output_root)
        receipt["bazel_outputs"] = {target: sorted(str(path) for path in paths)
                                    for target, paths in outputs.items()}
        installer = named_output(outputs, IMAGE, "sonic-vs.bin")
        runtime = named_output(outputs, RUNTIME, "docker-orchagent.gz")
        verify = [sys.executable, str(bazel_workspace / "tools/bazel/ci/verify_image.py"),
                  "--installer", str(installer),
                  "--payload", str(named_output(outputs, IMAGE + "_fs", "sonic-vs.bin_fs.zip")),
                  "--dockerfs", str(named_output(outputs, IMAGE + "_dockerfs", "sonic-vs.bin_dockerfs.tar.gz")),
                  "--squashfs", str(named_output(outputs, IMAGE + "_host", "sonic-vs.bin_host.squashfs")),
                  "--boot", str(named_output(outputs, IMAGE + "_host", "sonic-vs.bin_host.boot.tar")),
                  "--platform", str(named_output(outputs, IMAGE + "_host", "sonic-vs.bin_host.platform.tar.gz")),
                  "--output", str(artifacts / "image-verification.json")]
        execute(verify, bazel_workspace, artifacts, receipt, "verify-image")
        verification = json.loads((artifacts / "image-verification.json").read_text())
        require(verification["status"] == "passed", "image verifier did not pass")
        receipt["outputs"]["installer"] = publish(installer, artifacts, "sonic-vs.bin")
        receipt["outputs"]["runtime"] = publish(runtime, artifacts, "docker-orchagent.gz")
        require(receipt["outputs"]["installer"]["sha256"] == verification["installer"]["sha256"],
                "published installer differs from verified installer")
        shutil.copyfile(named_output(outputs, IMAGE + "_host", "sonic-vs.bin_host.receipt.json"),
                        artifacts / "host-receipt.json")
        (artifacts / "SHA256SUMS").write_text("".join(
            f"{item['sha256']}  {item['file']}\n" for item in receipt["outputs"].values()))
        receipt["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        receipt.update(status="failed", error=str(error), error_type=type(error).__name__)
        print("VS image CI failed: " + str(error), file=sys.stderr)
    finally:
        worker_stopped = False
        if worker_attempted:
            try:
                execute(launcher + ["--worker-action", "stop"], bazel_workspace, artifacts, receipt, "worker-stop")
                worker_stopped = True
            except (Exception, KeyboardInterrupt) as error:
                receipt.update(status="failed", worker_cleanup_error=str(error))
        if receipt["status"] == "passed" and worker_stopped:
            cleanup_started = time.monotonic()
            try:
                require(output_identity is not None and output_root.parent == state
                        and not output_root.is_symlink() and output_root.resolve(strict=True) == output_root
                        and not artifacts.resolve().is_relative_to(output_root),
                        "refusing cleanup of an unexpected invocation output root")
                current = output_root.lstat()
                require(stat.S_ISDIR(current.st_mode)
                        and (current.st_dev, current.st_ino) == output_identity,
                        "refusing cleanup of a replaced invocation output root")
                shutil.rmtree(output_root)
                receipt["output_cleanup"]["status"] = "removed"
            except (Exception, KeyboardInterrupt) as error:
                receipt["status"] = "failed"
                receipt["output_cleanup"].update(status="failed", error=str(error))
            finally:
                receipt["output_cleanup"]["wall_seconds"] = time.monotonic() - cleanup_started
        if ownership_changed:
            try:
                chown_tree(workspace, *original_owner)
            except OSError as error:
                receipt.update(status="failed", ownership_restore_error=str(error))
        receipt["wall_seconds"] = time.monotonic() - started
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        # The final receipt was written after restoring a root-invoked checkout.
        if os.geteuid() == 0 and receipt_path.is_relative_to(workspace):
            os.chown(receipt_path, *original_owner)
    print(json.dumps({"status": receipt["status"], "receipt": str(receipt_path),
                      "wall_seconds": receipt["wall_seconds"]}))
    return 0 if receipt["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "state", "artifacts"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--ca-bundle", type=Path,
                        help="optional PEM certificate bundle for execution workers only")
    args = parser.parse_args(argv)

    def interrupted(signum, _frame):
        raise InterruptedError("received signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return build(args)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
