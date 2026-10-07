#!/usr/bin/env python3
"""Restore pinned, previously built P4 DEBs without invoking a package producer."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

WORKSPACE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKSPACE))
from tools.bazel.build_helpers import cache_options, export_archive

LOCK = WORKSPACE / "tools/bazel/p4/packages.lock.json"
TARGET = "//tools/bazel/p4:debs"
PACKAGES = {"p4lang-pi", "p4lang-bmv2", "p4lang-p4c", "libsai", "libsai-dev"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(path=LOCK, workspace=WORKSPACE):
    lock = json.loads(path.read_text())
    if (lock["schema"], lock["architecture"], lock["distribution"]) != (1, "amd64", "trixie"):
        raise ValueError("P4 imports support only the locked AMD64 Trixie packages")
    packages = lock["packages"]
    if len(packages) != len(PACKAGES) or {p["name"] for p in packages} != PACKAGES:
        raise ValueError("P4 lock must contain all five runtime/development packages")
    for package in packages:
        expected = f'{package["name"]}_{package["version"]}_amd64.deb'
        if package["filename"] != expected or Path(expected).name != expected:
            raise ValueError("invalid P4 package filename")
    if not lock["source_inputs"]:
        raise ValueError("P4 lock must identify the native source and packaging inputs")
    for relative, expected in lock["source_inputs"].items():
        path = workspace / relative
        if not path.resolve().is_relative_to(workspace.resolve()) or not path.is_file() or sha256(path) != expected:
            raise ValueError(f"P4 producer input changed: {relative}; rebuild outside Bazel and refresh the pins")
    return lock


def validate_package(path, package):
    if not path.is_file() or path.stat().st_size != package["size"] or sha256(path) != package["sha256"]:
        raise ValueError("P4 package checksum/size mismatch: " + package["filename"])
    control = subprocess.check_output(
        ["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"], text=True
    )
    fields = dict(line.split(": ", 1) for line in control.splitlines())
    if fields != {"Package": package["name"], "Version": package["version"], "Architecture": "amd64"}:
        raise ValueError("P4 package metadata mismatch: " + package["filename"])


def stage(args):
    lock = load_lock()
    if args.dash_sai_commit is not None and args.dash_sai_commit != lock["source_revisions"]["dash_sai"]:
        raise ValueError("DASH source revision changed; rebuild outside Bazel and refresh the pins")
    if args.expected_package and set(args.expected_package) != {p["filename"] for p in lock["packages"]}:
        raise ValueError("Make selected different P4 package versions; refresh the pins")
    options = cache_options(args.cache_directory) + args.bazel_arg
    command = [args.bazel]
    subprocess.run(command + ["build", *options, TARGET], cwd=WORKSPACE, check=True)
    outputs = subprocess.check_output(
        command + ["cquery", *options, "--output=files", TARGET], cwd=WORKSPACE, text=True
    ).splitlines()
    # A configured info execution_root query misresolves root platform aliases
    # with Bazel 8.5.1. Source files from http_file live under output_base instead.
    output_base = Path(subprocess.check_output(
        command + ["info", "output_base"], cwd=WORKSPACE, text=True
    ).strip())
    sources = {}
    for value in outputs:
        path = Path(value)
        if not path.is_absolute():
            path = output_base / path if value.startswith("external/") else WORKSPACE / path
        if path.name in sources:
            raise ValueError("duplicate P4 artifact in Bazel outputs: " + path.name)
        sources[path.name] = path
    if set(sources) != {p["filename"] for p in lock["packages"]}:
        raise ValueError("Bazel did not return exactly the five locked P4 DEBs")
    # Verify the complete tuple before replacing any retained package.
    for package in lock["packages"]:
        validate_package(sources[package["filename"]], package)
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for package in lock["packages"]:
        # Mark ownership before replacing bytes, including interrupted imports.
        # Native Make must not silently accept these files for another profile.
        marker = args.output_directory / (package["filename"] + ".bazel-imported")
        marker.write_text("Imported from tools/bazel/p4/packages.lock.json\n")
        export_archive(sources[package["filename"]], args.output_directory / package["filename"])
    return {"target": TARGET, "architecture": lock["architecture"], "distribution": lock["distribution"],
            "packages": [{key: p[key] for key in ("filename", "sha256", "size")} for p in lock["packages"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--cache-directory")
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--dash-sai-commit")
    parser.add_argument("--expected-package", action="append", default=[])
    args = parser.parse_args()
    try:
        print(json.dumps(stage(args), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"P4 package restoration failed: {error}\n")


if __name__ == "__main__":
    main()
