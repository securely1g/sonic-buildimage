#!/usr/bin/env python3
"""Prepare component Rust metadata before loading the buildimage Bazel graph."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


# Common's generated repository is consumed by SWSS's standalone module.
COMPONENTS = ("sonic-swss-common", "sonic-swss")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(workspace, artifacts, bazel="bazel", startup=(), options=()):
    workspace = workspace.resolve(strict=True)
    artifacts = artifacts.resolve()
    if artifacts.exists() and any(artifacts.iterdir()):
        raise ValueError("Rust preparation artifacts must be empty")
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": 1, "status": "running", "components": {}, "commands": [],
               "policy": "Regenerate Cargo.Bazel.lock from committed Cargo.lock before root module evaluation."}
    started = time.monotonic()
    locks = {name: workspace / "src" / name / "Cargo.lock" for name in COMPONENTS}
    try:
        before = {name: sha256(path) for name, path in locks.items()}
        for name in COMPONENTS:
            component = workspace / "src" / name
            evidence = artifacts / name
            evidence.mkdir()
            launcher = component / "tools/bazel/prepare_rust.py"
            # Bazel rejects overrides for modules absent from this component's
            # graph. Common has no dependency on DASH or Sairedis.
            dependencies = ("sonic-build-infra",)
            if name == "sonic-swss":
                dependencies += ("sonic-dash-api", "sonic-sairedis")
            component_options = ["--override_module=" + dependency + "=" +
                                 str(workspace / "src" / dependency)
                                 for dependency in dependencies] + list(options)
            command = [sys.executable, str(launcher), "--bazel", bazel,
                       "--receipt", str(evidence / "preparation.json"),
                       *["--bazel-startup-arg=" + value for value in startup],
                       *["--bazel-arg=" + value for value in component_options]]
            if name == "sonic-swss":
                command += ["--prepared-common", str(workspace / "src/sonic-swss-common")]
            record = {"component": name, "argv": command, "log": name + ".log"}
            receipt["commands"].append(record)
            with (artifacts / record["log"]).open("w") as output:
                process = subprocess.run(command, cwd=component, stdout=output, stderr=subprocess.STDOUT)
            record["returncode"] = process.returncode
            if process.returncode:
                raise RuntimeError(name + " Rust preparation failed; see " + record["log"])
            for source, path in locks.items():
                if sha256(path) != before[source]:
                    raise ValueError("Rust preparation changed committed Cargo.lock: " + source)
            generated = component / "Cargo.Bazel.lock"
            if generated.is_symlink() or not generated.is_file() or not generated.stat().st_size:
                raise ValueError("missing generated Rust metadata: " + name)
            metadata = json.loads(generated.read_text())
            if not isinstance(metadata, dict) or not metadata.get("crates"):
                raise ValueError("invalid generated Rust metadata: " + name)
            if not (evidence / "preparation.json").is_file():
                raise ValueError("Rust helper did not retain preparation evidence: " + name)
            for source in (locks[name], generated):
                shutil.copyfile(source, evidence / source.name)
            receipt["components"][name] = {
                "cargo_lock_sha256": before[name], "cargo_lock_unchanged": True,
                "bazel_lock_sha256": sha256(generated), "preparation": name + "/preparation.json",
            }
        receipt["status"] = "passed"
        return receipt
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt["wall_seconds"] = time.monotonic() - started
        (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--output-user-root", type=Path)
    parser.add_argument("--bazel-arg", action="append", default=[])
    args = parser.parse_args()
    # Batch preparation exits each component JVM before building the consumer.
    # It must not leave two additional toolchain-resolution servers resident.
    startup = ["--batch"]
    if args.output_user_root:
        startup.append("--output_user_root=" + str(args.output_user_root.resolve()))
    options = list(args.bazel_arg)
    if os.environ.get("GIT_CONFIG_SYSTEM"):
        for name in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "CARGO_NET_GIT_FETCH_WITH_CLI"):
            option = "--repo_env=" + name
            if not any(value == option or value.startswith(option + "=") for value in options):
                options.append(option)
    prepare(args.workspace, args.artifacts, args.bazel, startup, options)


if __name__ == "__main__":
    main()
