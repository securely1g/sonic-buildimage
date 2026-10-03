#!/usr/bin/env python3
"""Run the fresh-checkout Bazel checks and verify source-built SWSS packages."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import resolution


ROOT = Path(__file__).resolve().parents[3]
TEST_TARGETS = [
    "//tools/bazel/image:metadata_test",
    "//tools/bazel/image:host_test",
    "//tools/bazel/image:store_test",
    "//tools/bazel/image:import_store_test",
    "//tools/bazel/image:installer_test",
    "//tools/bazel/image:run_test",
    "//tools/bazel/oci:docker_archive_to_oci_layout_test",
    "//dockers/docker-orchagent/config:render_test",
    "//tools/bazel/tests:make_bridge_test",
    "//tools/bazel/tests:swss_contract_test",
    "//tools/bazel/registry:registry_lib_test",
    "//tools/bazel/equivalence_checker:deployment_tar_test",
    "//tools/bazel/ci:run_test",
    "//tools/bazel/ci:rust_test",
    "//tools/bazel/ci:image_inputs_test",
    "//tools/bazel/ci:native_build_test",
    "//tools/bazel/ci:kernel_test",
    "//tools/bazel/ci:p4lang_pi_source_test",
    "//tools/bazel/ci:trust_test",
    "//tools/bazel/image/native:producer_test",
    "//tools/bazel/image/native:handoff_test",
    "//tools/bazel/image/native:source_identity_test",
    "//tools/bazel/image/native:ca_hook_test",
    "//tools/bazel/ci:image_test",
    "//tools/bazel/ci:source_workspace_test",
    "//tools/bazel/ci:verify_image_test",
]
BUILD_TARGETS = {
    "swss.tar": "@sonic_swss//dist:swss_pkg",
    "protobuf.tar": "@sonic_dash_api//:protobuf_runtime_pkg",
    "rdeps.tar": "//dockers/docker-orchagent:rdeps",
    "config.tar": "//dockers/docker-orchagent/config:files",
    "debug-symbols.tar": "//tools/bazel/ci:swss_debug_symbols",
}
HEADER_TARGETS = [
    "@rules_distroless//registry_ci:protobuf_headers_test",
    "@rules_distroless//registry_ci:architecture_amd64_test",
    "@rules_distroless//registry_ci:architecture_arm64_test",
]
GIT_OPTIONS = (["--repo_env=GIT_CONFIG_SYSTEM", "--repo_env=GIT_CONFIG_NOSYSTEM",
                "--repo_env=CARGO_NET_GIT_FETCH_WITH_CLI"]
               if os.environ.get("GIT_CONFIG_SYSTEM") else [])
OPTIONS = [
    "--jobs=4", "--local_resources=cpu=4", "--local_resources=memory=10000",
    "--lockfile_mode=update", "--noshow_progress", "--color=no", "--curses=no",
] + GIT_OPTIONS


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


def capture(command, directory, receipt, name):
    """Return machine-readable stdout while retaining diagnostic stderr."""
    log = directory / (name + ".log")
    record = {"argv": command, "log": log.name}
    receipt["commands"].append(record)
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    record.update(returncode=result.returncode, elapsed_seconds=time.monotonic() - started)
    log.write_text(result.stdout + result.stderr)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        raise RuntimeError(f"{name} failed with exit {result.returncode}; see {log}")
    return result.stdout


def check_bazel_version(bazel, expected, directory, receipt):
    # Bazelisk's first invocation can download Bazel and report progress on
    # stderr. That diagnostic output is not part of Bazel's version string.
    version = capture([bazel, "--version"], directory, receipt, "bazel-version").strip()
    if version != expected:
        raise ValueError(f"Expected {expected}, got {version}")
    return version


def verify_tests(path, targets=TEST_TARGETS):
    summaries = {}
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if "testSummary" in event.get("id", {}):
            label = event["id"]["testSummary"]["label"]
            if label.startswith("@@"):
                repository, target = label[2:].split("//", 1)
                label = "@" + repository.split("+", 1)[0] + "//" + target
            summaries[label] = event["testSummary"]["overallStatus"]
    if set(summaries) != set(targets) or any(value != "PASSED" for value in summaries.values()):
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
            "source_contract": {name: contract.sha(ROOT / "src/sonic-swss" / name)
                                for name in contract.SWSS_CONTRACT_INPUTS},
            "prebuilt_debug_gaps": gaps,
            "scope": "Package payload, architecture, build IDs, DWARF and debuglink CRC; no container or boot execution."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("test", "build", "headers"))
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
        version = check_bazel_version(args.bazel, expected, directory, receipt)
        receipt["bazel_version"] = version
        execute([sys.executable, str(ROOT / "tools/bazel/ci/rust.py"),
                 "--workspace", str(ROOT), "--artifacts", str(directory / "rust"),
                 "--bazel", args.bazel,
                 *["--bazel-arg=" + option for option in GIT_OPTIONS]],
                directory, receipt, "rust-preparation")
        receipt["rust_preparation"] = "rust/receipt.json"
        if args.command == "headers":
            machine = platform.machine()
            if machine not in ("x86_64", "aarch64") or platform.freedesktop_os_release().get("VERSION_CODENAME") != "trixie":
                raise ValueError("Header CI requires native AMD64 or ARM64 Debian Trixie")
            options = [option for option in OPTIONS if not option.startswith("--lockfile_mode=")]
            options += ["--lockfile_mode=update"]
            if machine == "aarch64":
                options += ["--config=aarch64"]
            receipt["architecture"] = "arm64" if machine == "aarch64" else "amd64"
            receipt["coverage"] = "Native protobuf header import through the buildimage module graph; no ARM64 image build."
            graph_options = ["--config=aarch64"] if machine == "aarch64" else []
            graph = capture([args.bazel, "mod", "graph", "--extension_info=hidden", "--lockfile_mode=update",
                             *graph_options, *GIT_OPTIONS], directory, receipt, "module-graph")
            (directory / "module-graph.txt").write_text(graph)
            execute([args.bazel, "test", *options, "--nocache_test_results",
                     "--build_event_json_file=" + str(directory / "bep.json"), *HEADER_TARGETS],
                    directory, receipt, "headers")
            receipt["tests"] = verify_tests(directory / "bep.json", HEADER_TARGETS)
            output_base = Path(capture([args.bazel, "info", "output_base", *GIT_OPTIONS],
                                       directory, receipt, "output-base").strip())
            fetched = output_base / "external/rules_distroless+/MODULE.bazel"
            text = fetched.read_text()
            if not re.search(r'\bversion\s*=\s*"0\.9\.4-sonic\.1"', text):
                raise ValueError("Expected the native Distroless header-fix module")
            shutil.copyfile(fetched, directory / "rules_distroless.MODULE.bazel")
            receipt["resolution"] = resolution.retain(ROOT, directory, graph)
            receipt["distroless_version"] = "0.9.4-sonic.1"
            receipt["status"] = "passed"
            return
        if args.command == "build":
            release = platform.freedesktop_os_release()
            if platform.machine() != "x86_64" or release.get("VERSION_CODENAME") != "trixie":
                raise ValueError("SWSS package CI requires a native AMD64 Debian Trixie environment")
        targets = TEST_TARGETS if args.command == "test" else list(BUILD_TARGETS.values())
        if args.command == "test":
            execute([args.bazel, "run", "--lockfile_mode=update", *GIT_OPTIONS,
                     "//tools/bazel/buildifier:buildifier.check"], directory, receipt, "format")
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
                files = capture(command, directory, receipt, name + ".query").splitlines()
                if len(files) != 1 or not (ROOT / files[0]).is_file() or not (ROOT / files[0]).stat().st_size:
                    raise ValueError("Expected exactly one nonempty package output for " + target)
                destination = directory / name
                shutil.copyfile(ROOT / files[0], destination)
                paths[name] = destination
                receipt["artifacts"][name] = {"target": target, "bytes": destination.stat().st_size,
                                               "sha256": contract.sha(destination)}
            receipt["validation"] = verify_packages(paths)
        graph = capture([args.bazel, "mod", "graph", "--extension_info=hidden", "--lockfile_mode=update", *GIT_OPTIONS],
                        directory, receipt, "module-graph")
        receipt["resolution"] = resolution.retain(ROOT, directory, graph)
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
