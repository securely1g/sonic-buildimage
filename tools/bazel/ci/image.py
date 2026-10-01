#!/usr/bin/env python3
"""Build and verify a VS installer in a fresh, dedicated CI checkout.

Only the package action cache and repository downloads are reusable between
jobs. Full image outputs stay in this invocation's Bazel output root; the same
worker/server reuses compiled SWSS actions within the job.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid

import image_inputs


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
    git(workspace, "diff", "--quiet", "HEAD", "--ignore-submodules=none", "--")
    require(not git(workspace, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"),
            "checkout contains modified or untracked source files")
    for component in COMPONENTS:
        directory = workspace / component
        require((directory / ".git").exists(), "component submodule is not initialized: " + component)
        entry = git(workspace, "ls-tree", "HEAD", "--", component).split()
        require(len(entry) == 4 and entry[:2] == ["160000", "commit"] and entry[3] == component,
                "component is not a recorded Git submodule: " + component)
        actual = git(directory, "rev-parse", "HEAD")
        require(actual == entry[2], "component HEAD differs from recorded gitlink: " + component)
        git(directory, "diff", "--quiet", "HEAD", "--ignore-submodules=none", "--")
        require(not git(directory, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"),
                "component contains modified or untracked source files: " + component)
        result["components"][component] = {"commit": actual, "gitlink": entry[2]}
    result["source_clean"] = True
    return result


def check_bazel_version(manifest, workspace):
    expected = (workspace / ".bazelversion").read_text().strip()
    require(manifest["worker"].get("bazel_version") == expected,
            "pinned worker Bazel version differs from checkout .bazelversion")
    return expected


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
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            record["returncode"] = process.returncode
        record["wall_seconds"] = time.monotonic() - started


def bep_outputs(path, targets, output_root):
    """Resolve successful target default outputs from this build's BEP only."""
    named, completed, finished = {}, {}, False
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
                finished = event["finished"]["exitCode"]["code"] == 0
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
    shutil.copyfile(source, output)
    info = {"file": name, "bytes": output.stat().st_size, "sha256": image_inputs.sha256(output)}
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
    worker_name = "sonic-vs-ci-" + invocation
    launcher, worker_attempted, ownership_changed = None, False, False
    receipt = {"schema": 1, "status": "running", "commands": [], "outputs": {},
               "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "worker": worker_name, "output_user_root": str(output_root),
               "cache_policy": "Package disk cache and repository cache persist; full image disk cache disabled.",
               "scope": "Bazel SWSS compile and VS image assembly from pinned native predecessors; "
                        "installer byte-chain verification. No boot or forwarding test."}
    try:
        manifest = args.manifest.resolve(strict=True)
        receipt["manifest_sha256"] = image_inputs.sha256(manifest)
        receipt["bazel_version"] = check_bazel_version(image_inputs.load_manifest(manifest), workspace)
        receipt.update(source_provenance(workspace))
        prepare_started = time.monotonic()
        image_inputs.prepare(manifest, workspace, state / "input-downloads",
                             artifacts / "input-receipt.json", args.local_assets)
        receipt["input_preparation_seconds"] = time.monotonic() - prepare_started
        spec = workspace / "target/bazel-image-inputs/execution-environment.json"
        receipt["execution_environment"] = json.loads(spec.read_text())
        host_config = json.loads((workspace / "target/bazel-image-inputs/host-config.json").read_text())
        receipt["native_host_identity"] = host_config.get("identity")
        for path in (output_root, state / "repository-cache", state / "package-cache"):
            path.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            ownership_changed = True
            for path in (workspace, state):
                chown_tree(path, 1000, 1000)
        launcher = [sys.executable, str(workspace / "tools/bazel/image/run.py"),
                    "--workspace", str(workspace), "--mount-root", str(mount_root),
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
                workspace, artifacts, receipt, "package")
        execute(bazel("image", TARGETS, ""), workspace, artifacts, receipt, "image")
        outputs = bep_outputs(artifacts / "image.bep.jsonl", TARGETS, output_root)
        receipt["bazel_outputs"] = {target: sorted(str(path) for path in paths)
                                    for target, paths in outputs.items()}
        installer = named_output(outputs, IMAGE, "sonic-vs.bin")
        runtime = named_output(outputs, RUNTIME, "docker-orchagent.gz")
        verify = [sys.executable, str(workspace / "tools/bazel/ci/verify_image.py"),
                  "--installer", str(installer),
                  "--payload", str(named_output(outputs, IMAGE + "_fs", "sonic-vs.bin_fs.zip")),
                  "--dockerfs", str(named_output(outputs, IMAGE + "_dockerfs", "sonic-vs.bin_dockerfs.tar.gz")),
                  "--squashfs", str(named_output(outputs, IMAGE + "_host", "sonic-vs.bin_host.squashfs")),
                  "--boot", str(named_output(outputs, IMAGE + "_host", "sonic-vs.bin_host.boot.tar")),
                  "--platform", str(named_output(outputs, IMAGE + "_host", "sonic-vs.bin_host.platform.tar.gz")),
                  "--output", str(artifacts / "image-verification.json")]
        execute(verify, workspace, artifacts, receipt, "verify-image")
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
        if worker_attempted:
            try:
                execute(launcher + ["--worker-action", "stop"], workspace, artifacts, receipt, "worker-stop")
            except (Exception, KeyboardInterrupt) as error:
                receipt.update(status="failed", worker_cleanup_error=str(error))
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
    for name in ("workspace", "state", "artifacts", "manifest"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--local-assets", type=Path)
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
