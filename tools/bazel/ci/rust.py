#!/usr/bin/env python3
"""Record and verify the tracked Cargo inputs read directly by rules_rs."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


COMPONENTS = ("sonic-swss-common", "sonic-swss")
INPUTS = ("MODULE.bazel", "Cargo.toml", "Cargo.lock")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tracked_inputs(component):
    """Reject local edits before recording the selected source revision."""
    def git(*args):
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(component),
                                        "-C", str(component), *args])

    revision = git("rev-parse", "HEAD").decode().strip()
    contents = {}
    for name in INPUTS:
        path = component / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing regular tracked Rust input: " + str(path))
        contents[name] = path.read_bytes()
        if contents[name] != git("show", "HEAD:" + name):
            raise ValueError("modified tracked Rust input: " + str(path))
    return revision, contents


def record(workspace, artifacts):
    workspace = workspace.resolve(strict=True)
    artifacts = artifacts.resolve()
    if artifacts.exists() and any(artifacts.iterdir()):
        raise ValueError("Rust input artifacts must be empty")
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": 2, "status": "running", "components": {},
               "policy": "rules_rs reads tracked Cargo inputs directly; no Cargo.Bazel.lock generation."}
    try:
        for name in COMPONENTS:
            revision, contents = tracked_inputs(workspace / "src" / name)
            directory = artifacts / name
            directory.mkdir()
            for filename, data in contents.items():
                (directory / filename).write_bytes(data)
            receipt["components"][name] = {
                "revision": revision,
                "inputs": {filename: {"sha256": sha256(data), "bytes": len(data)}
                           for filename, data in contents.items()},
            }
        receipt["status"] = "passed"
        return receipt
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def verify(workspace, artifacts):
    """Check both the retained evidence and source inputs after Bazel execution."""
    receipt = json.loads((artifacts / "receipt.json").read_text())
    if receipt.get("schema") != 2 or receipt.get("status") != "passed":
        raise ValueError("Rust inputs do not have a successful capture receipt")
    if set(receipt["components"]) != set(COMPONENTS):
        raise ValueError("Rust input receipt has incomplete components")
    for name in COMPONENTS:
        entry = receipt["components"][name]
        revision, contents = tracked_inputs(workspace / "src" / name)
        if revision != entry["revision"] or set(entry["inputs"]) != set(INPUTS):
            raise ValueError("Rust source revision or input set changed: " + name)
        for filename, data in contents.items():
            expected = entry["inputs"][filename]
            retained = artifacts / name / filename
            if (expected != {"sha256": sha256(data), "bytes": len(data)} or
                    retained.is_symlink() or retained.read_bytes() != data):
                raise ValueError("Rust input or retained evidence changed: " + name + "/" + filename)
    return {"status": "passed", "tracked_inputs_unchanged": True,
            "components": receipt["components"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = verify(args.workspace, args.artifacts) if args.verify else record(args.workspace, args.artifacts)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
