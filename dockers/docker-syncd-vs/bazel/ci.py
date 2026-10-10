#!/usr/bin/env python3
"""Run the explicit syncd OCI contract tests without production image builds."""

import argparse
from functools import partial
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse

OWNER = Path(__file__).absolute().parent
ROOT = OWNER.parents[2]
sys.path.insert(0, str(OWNER))
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.bazel_commands import inspect_actions
from tools.bazel.ci import command_log, resolution
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.gzip.source_archive import check_versions

execute = partial(command_log.execute, cwd=ROOT)

TARGET_NAMES = [
    "manifest_labels_test",
    "package_state_layer_test",
    "select_apt_payloads_test",
    "validate_image_test",
    "validate_payloads_test",
]
TARGETS = ["//dockers/docker-syncd-vs/bazel:" + name for name in TARGET_NAMES] + [
    "//tools/bazel/tests:apt_selection_test",
]


def source_hashes():
    return {str(path.relative_to(ROOT)): sha(path) for path in (
        ROOT / "MODULE.bazel", OWNER.parent / "BUILD.bazel", OWNER / "BUILD.bazel",
        ROOT / "tools/bazel/oci/BUILD.bazel", ROOT / "tools/bazel/oci/apt_selection.py",
        ROOT / "tools/bazel/oci/apt_layer.bzl",
        OWNER / "apt.lock.json", OWNER / "apt_inputs.MODULE.bazel", OWNER / "prepare_packages.py",
        OWNER / "runtime_package_state.json")}


def collect_test_outputs(events, artifacts):
    """Use the tested configuration's paths, including Python transitions."""
    outputs = {}
    for line in events.read_text().splitlines():
        event = json.loads(line)
        target = event.get("id", {}).get("testResult", {}).get("label")
        if target not in TARGETS:
            continue
        for output in event.get("testResult", {}).get("testActionOutput", []):
            filename = output.get("name")
            if filename not in ("test.log", "test.xml"):
                continue
            uri = urlparse(output.get("uri", ""))
            require(uri.scheme == "file" and not uri.netloc,
                    "test evidence is not a local file: " + target + "/" + filename)
            source = Path(unquote(uri.path))
            key = (target, filename)
            require(key not in outputs or outputs[key] == source,
                    "ambiguous configured test evidence: " + target + "/" + filename)
            outputs[key] = source
    collected = []
    for target in TARGETS:
        for filename in ("test.log", "test.xml"):
            source = outputs.get((target, filename))
            require(source is not None and source.is_file() and source.stat().st_size > 0,
                    "missing required test evidence: " + target + "/" + filename)
            destination = artifacts / "tests" / target.rsplit(":", 1)[1] / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            collected.append(str(destination.relative_to(artifacts)))
    return collected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.absolute()
    artifacts.mkdir(parents=True, exist_ok=False)
    receipt = {"status": "running", "commands": []}
    try:
        for variant in ("docker-syncd-vs", "docker-syncd-vs-dbg"):
            require((ROOT / "target/bazel-manifests" / variant / "manifest.json").is_file(),
                    "prepare the syncd Make manifests before running contract CI")
        before = source_hashes()
        version = execute([args.bazel, "--version"], artifacts, receipt, "bazel-version").strip()
        require(version == "bazel " + (ROOT / ".bazelversion").read_text().strip(), "unexpected Bazel version: " + version)
        actions = artifacts / "actions.raw.json"
        actions.touch(mode=0o600, exist_ok=False)
        try:
            execute([args.bazel, "aquery"] + args.bazel_arg + ["deps(set(" + " ".join(TARGETS) + "))", "--output=jsonproto"],
                    artifacts, receipt, "actions", output_path=actions)
            audit = inspect_actions(actions)
        finally:
            # Publish the selected audit fields; action environments stay temporary.
            actions.unlink(missing_ok=True)
        audit["targets"] = TARGETS
        (artifacts / "execution-gate-audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
        require(not audit["deb_outputs"] and not audit["packaging_wrappers"], "contract tests contain a DEB or packaging wrapper action")
        test_events = artifacts / "tests.raw.json"
        test_events.touch(mode=0o600, exist_ok=False)
        try:
            execute([args.bazel, "test"] + args.bazel_arg + ["--nocache_test_results", "--test_output=errors",
                    "--build_event_json_file=" + str(test_events)] + TARGETS, artifacts, receipt, "tests")
            test_outputs = collect_test_outputs(test_events, artifacts)
        finally:
            # Retain the selected logs/XML, not raw event or command metadata.
            test_events.unlink(missing_ok=True)
        resolution.collect(ROOT, artifacts, bazel=[args.bazel])
        check_versions(artifacts / "module-graph.json")
        module_lock = ROOT / "MODULE.bazel.lock"
        require(source_hashes() == before, "contract CI changed a checked source or package lock")
        report = {"schema": 1, "bazel_version": version, "targets": TARGETS, "test_outputs": test_outputs,
                  "source_hashes": before, "module_lock_sha256": sha(module_lock),
                  "execution_gate_audit": "execution-gate-audit.json",
                  "scope": "Safe contract and manifest tests; no production OCI image, native predecessor, or Bazel DEB-producing target executed."}
        (artifacts / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        receipt["status"] = "passed"
        print(json.dumps(report, indent=2, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        receipt.update(status="failed", error=str(error))
        parser.exit(1, "syncd contract CI failed: " + str(error) + "\n")
    finally:
        (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
