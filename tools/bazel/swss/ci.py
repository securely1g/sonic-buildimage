#!/usr/bin/env python3
"""Build and inspect the SWSS source layers on a native AMD64 Trixie runner."""

import argparse
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

import resolution
import package_contract


ROOT = Path(__file__).resolve().parents[3]
# These targets create tar archives, never Debian packages. Make supplies the
# config-engine base and Scapy wheel when it assembles the complete OCI image.
TARGETS = {
    "swss.tar": "@sonic_swss//dist:swss_pkg",
    "protobuf.tar": "@sonic_dash_api//:protobuf_runtime_pkg",
    "rdeps.tar": "//dockers/docker-orchagent:rdeps",
    "config.tar": "//dockers/docker-orchagent/config:files",
    "debug-symbols.tar": "//tools/bazel/swss:debug_symbols",
}
OPTIONS = [
    "--jobs=4", "--local_resources=cpu=4", "--local_resources=memory=10000",
    "--lockfile_mode=update", "--noshow_progress", "--color=no", "--curses=no",
]
TESTS = [
    "@sonic_swss//crates/countersyncd:common_rust_test",
    "//tools/bazel/dpkg:test_dpkg_patterns_up_to_date",
    "//tools/bazel/oci:container_config_test",
    "//dockers/docker-orchagent/config:render_test",
]


def contract_module():
    return package_contract

def execute(command, directory, receipt, name):
    """Retain diagnostics separately so they cannot be mistaken for output paths."""
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    (directory / (name + ".log")).write_text(result.stdout + result.stderr)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", flush=True)
    receipt["commands"].append({"argv": command, "returncode": result.returncode,
                                "elapsed_seconds": time.monotonic() - started,
                                "log": name + ".log"})
    result.check_returncode()
    return result.stdout


def verify_packages(paths, source):
    contract = contract_module()
    runtime = contract.payload(paths["swss.tar"])
    programs = contract.swss_contract(source, runtime)
    dependencies = contract.payload(paths["rdeps.tar"])
    for name, metadata in runtime.items():
        if metadata["kind"] != "directory":
            contract.require(dependencies.get(name) == metadata, "runtime layer changed SWSS payload: " + name)
    protobuf = contract.payload(paths["protobuf.tar"], require_root=False)
    contract.source_protobuf_contract(dependencies, protobuf)
    symbols = contract.payload(paths["debug-symbols.tar"])
    contract.require(symbols and all(
        metadata["kind"] == "directory" or
        re.fullmatch(r"usr/lib/debug/\.build-id/[0-9a-f]{2}/[0-9a-f]+\.debug", name)
        for name, metadata in symbols.items()), "unexpected debug-symbol payload")
    combined = dict(dependencies)
    for name, metadata in symbols.items():
        contract.require(name not in combined or combined[name] == metadata,
                         "debug symbols change runtime payload: " + name)
        combined[name] = metadata
    elfs = [name for name, metadata in combined.items() if "elf_machine" in metadata]
    contract.require(elfs and all(combined[name]["elf_machine"] == 62 for name in elfs),
                     "expected only AMD64 ELF files")
    with tempfile.TemporaryDirectory(prefix="sonic-ci-symbols-") as temporary:
        extracted = Path(temporary)
        for name in ("rdeps.tar", "debug-symbols.tar"):
            with tarfile.open(paths[name]) as archive:
                archive.extractall(extracted, filter="data")
        pairs, gaps = contract.elf_debug(extracted, combined, set())
    required_debug = set(programs) | {"usr/lib/libdashapi.so", "usr/lib/python3/dist-packages/dash_api/_utils.so", contract.PROTOBUF_RUNTIME}
    contract.require(required_debug <= {pair["path"] for pair in pairs}, "incomplete SWSS/DASH/protobuf debug coverage")
    configuration = contract.payload(paths["config.tar"])
    contract.require(configuration.get("usr/bin/docker-init.sh", {}).get("mode") == 0o755,
                     "missing executable rendered SWSS entrypoint")
    return {"programs": programs, "elf_count": len(elfs), "debug_pairs": pairs,
            "source_contract": {name: contract.sha(source / name)
                                for name in contract.SWSS_CONTRACT_INPUTS},
            "prebuilt_debug_gaps": gaps,
            "scope": "Package payload, architecture, build IDs, DWARF and debuglink CRC; no container or boot execution."}


def source_directory(bazel, options, directory, receipt):
    # Unlike execution_root, output_base needs no configured target. Bazel 8.5.1
    # can misresolve repo-name aliases in --platforms during `info execution_root`.
    output_base = Path(execute([bazel, "info", "output_base"],
                               directory, receipt, "output-base").strip())
    source_root = execute([bazel, "cquery", *options, "--output=starlark",
                           "--starlark:expr=target.label.workspace_root",
                           "@sonic_swss//dist:swss_pkg"],
                          directory, receipt, "source-root").strip()
    source = output_base / source_root
    if not (source / "dist/BUILD.bazel").is_file():
        raise ValueError("Resolved SWSS source lacks its install declarations: " + str(source))
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--bazel", default="bazel")
    args = parser.parse_args()
    directory = args.artifacts.resolve()
    if directory.exists() and any(directory.iterdir()):
        parser.error("artifact directory must be empty: " + str(directory))
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {"status": "running", "commands": [],
               "scope": "SWSS source layers and matching debug symbols; no complete OCI image or VS installer."}
    started = time.monotonic()
    try:
        if platform.machine() != "x86_64" or platform.freedesktop_os_release().get("VERSION_CODENAME") != "trixie":
            raise ValueError("SWSS source-layer CI requires native AMD64 Debian Trixie")
        receipt["platform"] = platform.platform()
        receipt["revision"] = execute(["git", "rev-parse", "HEAD"], directory, receipt, "revision").strip()
        version = execute([args.bazel, "--version"], directory, receipt, "version").strip()
        if version != "bazel " + (ROOT / ".bazelversion").read_text().strip():
            raise ValueError("Bazel version does not match .bazelversion: " + version)
        receipt["bazel_version"] = version
        if (ROOT / "MODULE.bazel.lock").exists():
            raise ValueError("CI must start without a preexisting MODULE.bazel.lock")
        options = OPTIONS
        execute([args.bazel, "test", *options, "--nocache_test_results",
                 "--build_event_json_file=" + str(directory / "test-events.jsonl"),
                 *TESTS], directory, receipt, "bazel-tests")
        receipt["tests"] = {target: "passed" for target in TESTS}
        execute([args.bazel, "build", *options,
                 "--build_event_json_file=" + str(directory / "build-events.jsonl"),
                 *TARGETS.values()], directory, receipt, "build")
        paths = {}
        receipt["artifacts"] = {}
        contract = contract_module()
        for name, target in TARGETS.items():
            files = execute([args.bazel, "cquery", *options, "--output=files", target],
                            directory, receipt, name + ".query").splitlines()
            if len(files) != 1 or not (ROOT / files[0]).is_file() or not (ROOT / files[0]).stat().st_size:
                raise ValueError("Expected exactly one nonempty tar output for " + target)
            destination = directory / name
            shutil.copyfile(ROOT / files[0], destination)
            paths[name] = destination
            receipt["artifacts"][name] = {"target": target, "bytes": destination.stat().st_size,
                                           "sha256": contract.sha(destination)}
        source = source_directory(args.bazel, options, directory, receipt)
        receipt["validation"] = verify_packages(paths, source)
        receipt["resolution"] = resolution.collect(ROOT, directory, bazel=[args.bazel])
        receipt["architecture"] = "amd64"
        receipt["status"] = "passed"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
