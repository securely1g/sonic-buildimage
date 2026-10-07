#!/usr/bin/env python3
"""Run the explicit syncd OCI contract tests without production image builds."""

import argparse
import json
from pathlib import Path
import shutil
import sys

OWNER = Path(__file__).absolute().parent
ROOT = OWNER.parents[2]
sys.path.insert(0, str(OWNER))
sys.path.insert(0, str(ROOT))
import apt_lock
from refresh_apt_lock import capture, inspect_actions, run
from tools.bazel.ci import resolution
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.gzip.source_archive import check_versions

TARGET_NAMES = [
    "apt_lock_check",
    "manifest_labels_test",
    "package_state_layer_test",
    "refresh_apt_lock_test",
    "select_apt_payloads_test",
    "validate_image_test",
    "validate_payloads_test",
]
TARGETS = ["//dockers/docker-syncd-vs:" + name for name in TARGET_NAMES]


def source_hashes():
    return {str(path.relative_to(ROOT)): sha(path) for path in (
        ROOT / "MODULE.bazel", OWNER / "apt.lock.json", OWNER / "apt_packages.bzl", OWNER / "runtime_package_state.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    try:
        artifacts = args.artifacts.absolute()
        artifacts.mkdir(parents=True, exist_ok=False)
        for variant in ("docker-syncd-vs", "docker-syncd-vs-dbg"):
            require((ROOT / "target/bazel-manifests" / variant / "manifest.json").is_file(),
                    "prepare the syncd Make manifests before running contract CI")
        before = source_hashes()
        require((OWNER / "apt_packages.bzl").read_text() == apt_lock.render(json.loads((OWNER / "apt.lock.json").read_bytes())),
                "syncd APT label export differs from its checked content lock")
        version = capture([args.bazel, "--version"], artifacts / "bazel-version.log")
        require(version == "bazel " + apt_lock.BAZEL_VERSION, "unexpected Bazel version: " + version)
        actions = artifacts / "actions.raw.json"
        actions.touch(mode=0o600, exist_ok=False)
        try:
            run([args.bazel, "aquery"] + args.bazel_arg + ["deps(set(" + " ".join(TARGETS) + "))", "--output=jsonproto"],
                artifacts / "actions.log", output_path=actions)
            audit = inspect_actions(actions)
        finally:
            # Publish the selected audit fields; action environments stay temporary.
            actions.unlink(missing_ok=True)
        audit["targets"] = TARGETS
        (artifacts / "execution-gate-audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
        require(not audit["deb_outputs"] and not audit["packaging_wrappers"], "contract tests contain a DEB or packaging wrapper action")
        run([args.bazel, "test"] + args.bazel_arg + ["--nocache_test_results", "--test_output=errors"] + TARGETS,
            artifacts / "tests.log")
        test_outputs = []
        for name in TARGET_NAMES:
            for filename in ("test.log", "test.xml"):
                source = ROOT / "bazel-testlogs/dockers/docker-syncd-vs" / name / filename
                require(source.is_file() and source.stat().st_size > 0, "missing required test evidence: " + str(source))
                destination = artifacts / "tests" / name / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                test_outputs.append(str(destination.relative_to(artifacts)))
        resolution.collect(ROOT, artifacts, bazel=[args.bazel])
        check_versions(artifacts / "module-graph.json")
        module_lock = ROOT / "MODULE.bazel.lock"
        require(source_hashes() == before, "contract CI changed a checked source or package lock")
        report = {"schema": 1, "bazel_version": version, "targets": TARGETS, "test_outputs": test_outputs,
                  "source_hashes": before, "module_lock_sha256": sha(module_lock),
                  "execution_gate_audit": "execution-gate-audit.json",
                  "scope": "Safe contract and manifest tests; no production OCI image, native predecessor, or Bazel DEB-producing target executed."}
        (artifacts / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(1, "syncd contract CI failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
