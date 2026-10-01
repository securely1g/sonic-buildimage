#!/usr/bin/env python3
"""Run the fresh-checkout Bazel checks and verify source-built SWSS packages."""

import argparse
import importlib.util
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parents[3]
TEST_TARGETS = [
    "//tools/bazel/image:metadata_test",
    "//tools/bazel/image:host_test",
    "//tools/bazel/image:store_test",
    "//tools/bazel/image:installer_test",
    "//tools/bazel/image:run_test",
    "//tools/bazel/oci:docker_archive_to_oci_layout_test",
    "//dockers/docker-orchagent/config:render_test",
    "//tools/bazel/tests:make_bridge_test",
    "//tools/bazel/registry:registry_lib_test",
]
BUILD_TARGETS = {
    "swss.tar": "@sonic_swss//dist:swss_pkg",
    "rdeps.tar": "//dockers/docker-orchagent:rdeps",
    "config.tar": "//dockers/docker-orchagent/config:files",
    "debug-symbols.tar": "//tools/bazel/ci:swss_debug_symbols",
}
OPTIONS = [
    "--jobs=4", "--local_resources=cpu=4", "--local_resources=memory=10000",
    "--lockfile_mode=off", "--noshow_progress", "--color=no", "--curses=no",
]


def contract_module():
    spec = importlib.util.spec_from_file_location(
        "swss_contract", ROOT / "tools/bazel/tests/swss_container_test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(command, directory, receipt, name):
    """Preserve the exact invocation and its output, including failed commands."""
    log = directory / (name + ".log")
    record = {"argv": command, "log": log.name}
    receipt["commands"].append(record)
    started = time.monotonic()
    output = []
    with log.open("w") as stream:
        process = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        for line in process.stdout:
            print(line, end="", flush=True)
            stream.write(line)
            output.append(line)
        record["returncode"] = process.wait()
    record["elapsed_seconds"] = time.monotonic() - started
    if record["returncode"]:
        raise RuntimeError(f"{name} failed with exit {record['returncode']}; see {log}")
    return "".join(output)


def verify_tests(path):
    summaries = {}
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if "testSummary" in event.get("id", {}):
            summaries[event["id"]["testSummary"]["label"]] = event["testSummary"]["overallStatus"]
    if set(summaries) != set(TEST_TARGETS) or any(value != "PASSED" for value in summaries.values()):
        raise ValueError("Expected a passing Bazel test summary for every explicit target: " + repr(summaries))
    return summaries


def verify_packages(paths):
    contract = contract_module()
    runtime = contract.payload(paths["swss.tar"])
    programs = contract.swss_contract(ROOT / "src/sonic-swss", runtime)
    dependencies = contract.payload(paths["rdeps.tar"])
    for name, metadata in runtime.items():
        if metadata["kind"] != "directory":
            contract.require(dependencies.get(name) == metadata, "runtime layer changed SWSS payload: " + name)
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
        pairs, gaps = contract.elf_debug(extracted, combined, {"usr/lib/libdashapi.so"})
    contract.require(set(programs) <= {pair["path"] for pair in pairs}, "incomplete SWSS debug coverage")
    configuration = contract.payload(paths["config.tar"])
    contract.require(configuration.get("usr/bin/docker-init.sh", {}).get("mode") == 0o755,
                     "missing executable rendered SWSS entrypoint")
    return {"programs": programs, "elf_count": len(elfs), "debug_pairs": pairs,
            "prebuilt_debug_gaps": gaps,
            "scope": "Package payload, architecture, build IDs, DWARF and debuglink CRC; no container or boot execution."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("test", "build"))
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--bazel", default="bazel")
    args = parser.parse_args()
    directory = args.artifacts.resolve()
    if directory.exists() and any(directory.iterdir()):
        parser.error("artifact directory must be empty: " + str(directory))
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {"status": "running", "mode": args.command, "commands": [],
               "platform": platform.platform(), "coverage": "Fresh-checkout source/package checks; native image predecessors are not supplied."}
    started = time.monotonic()
    try:
        receipt["revision"] = execute(["git", "rev-parse", "HEAD"], directory, receipt, "revision").strip()
        expected = "bazel " + (ROOT / ".bazelversion").read_text().strip()
        version = execute([args.bazel, "--version"], directory, receipt, "bazel-version").strip()
        if version != expected:
            raise ValueError(f"Expected {expected}, got {version}")
        receipt["bazel_version"] = version
        if args.command == "build":
            release = platform.freedesktop_os_release()
            if platform.machine() != "x86_64" or release.get("VERSION_CODENAME") != "trixie":
                raise ValueError("SWSS package CI requires a native AMD64 Debian Trixie environment")
        targets = TEST_TARGETS if args.command == "test" else list(BUILD_TARGETS.values())
        if args.command == "test":
            # These configuration checks deliberately discover the source-tree
            # modules. Their existing Bazel targets do not declare that tree as
            # runfiles, so check the actual initialized checkout directly.
            for name in ("root_config_test", "submodule_config_test"):
                execute(["python3", "-E", str(ROOT / "tools/bazel/registry" / (name + ".py"))],
                        directory, receipt, name)
        options = OPTIONS + ["--build_event_json_file=" + str(directory / "bep.json"),
                             "--profile=" + str(directory / "profile.json.gz")]
        if args.command == "test":
            options += ["--nocache_test_results", "--test_output=errors"]
        execute([args.bazel, args.command, *options, *targets], directory, receipt, args.command)
        if args.command == "test":
            receipt["tests"] = verify_tests(directory / "bep.json")
        else:
            contract = contract_module()
            paths = {}
            receipt["artifacts"] = {}
            for name, target in BUILD_TARGETS.items():
                # Use stdout alone: Bazel diagnostics are not artifact paths.
                command = [args.bazel, "cquery", *OPTIONS, "--output=files", target]
                query = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
                (directory / (name + ".query.log")).write_text(query.stdout + query.stderr)
                receipt["commands"].append({"argv": command, "returncode": query.returncode,
                                            "log": name + ".query.log"})
                query.check_returncode()
                files = query.stdout.splitlines()
                if len(files) != 1 or not (ROOT / files[0]).is_file() or not (ROOT / files[0]).stat().st_size:
                    raise ValueError("Expected exactly one nonempty package output for " + target)
                destination = directory / name
                shutil.copyfile(ROOT / files[0], destination)
                paths[name] = destination
                receipt["artifacts"][name] = {"target": target, "bytes": destination.stat().st_size,
                                               "sha256": contract.sha(destination)}
            receipt["validation"] = verify_packages(paths)
        receipt["status"] = "passed"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
