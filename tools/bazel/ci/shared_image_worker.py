#!/usr/bin/env python3
"""Run the existing shared-image interfaces in a prepared private AMD64 worker.

The source drivers retain their combined build/inspection API. Installed commands
come from bazel-base-images.yml and bazel-oci.yml; no image build logic lives here.
"""

import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.artifact_validation import require

IMAGES = ("docker-base-trixie", "docker-config-engine-trixie")
PLATFORM = ["--platforms=@sonic_build_infra//platforms:x86_64_trixie",
            "--host_platform=@sonic_build_infra//platforms:x86_64_trixie"]
SWSS_TESTS = ["@libteam//:consumer_test", "@sonic_swss//crates/countersyncd:common_rust_test",
              "@sonic_swss//bazel:protobuf_runtime_test", "//tools/bazel/tests:swss_package_contract_test"]
SWSS_PARENT_ARCHIVE = "//dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz"

# These are the original workflow checks, with only paths passed as argv and the
# private receipt used before public export. Imports resolve in the source cwd.
BASE_LOAD_CHECK = '''import hashlib, json, subprocess, sys
from pathlib import Path
directory = Path(sys.argv[1])
receipt = json.loads((directory / 'receipt.json').read_text())
assert receipt['status'] == 'passed' and receipt['architecture'] == 'amd64'
assert receipt['revision'] == subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert hashlib.sha256((directory / 'installed-images-policy.json').read_bytes()).hexdigest() == receipt['installed_images_policy_sha256']
for name in ('docker-base-trixie.gz', 'docker-config-engine-trixie.gz'):
    with (directory / name).open('rb') as archive:
        actual = hashlib.file_digest(archive, 'sha256').hexdigest()
    assert actual == receipt['artifacts'][name]['sha256'], name
'''
SWSS_LOAD_CHECK = '''import hashlib, json, subprocess, sys
from pathlib import Path
from tools.bazel.ci.validate_runtime import load_archive, verify_loaded_image
root = Path(sys.argv[1])
receipt = json.loads((root / 'receipt.json').read_text())
assert receipt['status'] == 'passed' and receipt['architecture'] == 'amd64'
assert receipt['revision'] == subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
name = 'docker-swss-layer-trixie.gz'
with (root / name).open('rb') as archive:
    assert hashlib.file_digest(archive, 'sha256').hexdigest() == receipt['artifacts'][name]['sha256']
report = json.loads((root / 'validation.json').read_text())
actual, config, _ = load_archive(root / name, report['archive']['tag'], report['config_digest'])
verify_loaded_image(actual, config, report['config_digest'])
assert actual['Architecture'] == report['architecture'] == 'amd64'
assert actual['RootFS']['Layers'] == report['rootfs']['diff_ids']
'''


def required_source_files(image):
    common = ("tools/bazel/ci/libteam_draft_bazel.sh", "tools/bazel/ci/review_bazel.sh",
              "tools/bazel/ci/validate_runtime.py", "tools/bazel/oci/runtime_smoke.py")
    specific = {
        "base-config": ("tools/bazel/ci/base_images.py", "tools/bazel/ci/config_engine_rebuild.py",
                        "tools/bazel/oci/base_runtime_smoke.py"),
        "swss-layer": ("tools/bazel/ci/swss_layer.py", "tools/bazel/ci/swss_image_rebuild.py",
                       "tools/bazel/ci/bazel_commands.py", "tools/bazel/oci/swss_layer_runtime.py",
                       "tools/bazel/tests/swss_layer_payload_test.py"),
    }
    require(image in specific, "Unknown shared-image profile")
    return common + specific[image]


def commands(image, phase, workspace, artifacts):
    """Return fixed argv vectors; never infer a service driver's phase API."""
    require(image in ("base-config", "swss-layer"), "Unknown shared-image profile")
    require(phase in ("build-inspect", "incremental", "loaded"), "Unknown shared-image phase")
    ci, oci = workspace / "tools/bazel/ci", workspace / "tools/bazel/oci"
    py = [sys.executable, "-B"]
    bazel = str(ci / "libteam_draft_bazel.sh")
    if phase == "build-inspect":
        if image == "base-config":
            return [py + [str(ci / "base_images.py"), "--bazel", bazel, "--architecture", "amd64",
                          "--artifacts", str(artifacts), "--config-engine-acceptance"]]
        return [
            py + [str(workspace / "tools/bazel/tests/swss_layer_payload_test.py")],
            py + [str(ci / "swss_layer.py"), "--bazel", bazel, "--artifacts", str(artifacts)],
            # The driver audits its 10 outputs plus two layouts. Audit the four
            # existing consumer tests and incremental parent archive as well.
            py + [str(ci / "bazel_commands.py"), "--bazel", bazel,
                  *["--bazel-arg=" + flag for flag in PLATFORM],
                  "--output", str(artifacts / "consumer-action-audit.json"), *SWSS_TESTS, SWSS_PARENT_ARCHIVE],
            [bazel, "test", *PLATFORM, "--jobs=2", "--local_resources=cpu=2",
             "--local_resources=memory=8000", "--nocache_test_results", "--test_output=all",
             "--build_event_json_file=" + str(artifacts / "consumer-events.jsonl"), *SWSS_TESTS],
        ]
    if phase == "incremental":
        if image == "base-config":
            return [py + [str(ci / "config_engine_rebuild.py"), "--bazel", bazel,
                          "--output", str(artifacts / "config-engine-rebuild.json")]]
        return [py + [str(ci / "swss_image_rebuild.py"), "--bazel", bazel,
                      "--runtime-target", "//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie.gz",
                      "--symbols-target", "//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie.debug_symbols",
                      "--additional-runtime-target", "//dockers/docker-swss-layer-trixie:source_runtime",
                      "--unchanged-target", SWSS_PARENT_ARCHIVE, "--output", str(artifacts / "rebuild")]]
    docker = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL",
              "--security-opt=no-new-privileges", "--read-only", "--pids-limit=128", "--memory=4g", "--cpus=2",
              "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=128m"]
    if image == "base-config":
        return [
            py + ["-c", BASE_LOAD_CHECK, str(artifacts)],
            py + [str(ci / "validate_runtime.py"), "--images-policy", str(artifacts / "installed-images-policy.json"),
                  "--target", str(artifacts), "--output", str(artifacts / "installed-images.json")],
            docker + ["--mount", "type=bind,src=" + str(oci / "base_runtime_smoke.py") + ",dst=/probe/smoke.py,readonly",
                      "--mount", "type=bind,src=" + str(artifacts / "docker-config-engine-trixie.validation.json") + ",dst=/probe/validation.json,readonly",
                      "--entrypoint", "/usr/bin/python3", "docker-config-engine-trixie:latest",
                      "-B", "/probe/smoke.py", "/probe/validation.json"],
        ]
    return [
        py + ["-c", SWSS_LOAD_CHECK, str(artifacts)],
        docker + ["--mount", "type=bind,src=" + str(oci / "swss_layer_runtime.py") + ",dst=/probe/swss_layer_runtime.py,readonly",
                  "--mount", "type=bind,src=" + str(oci / "runtime_smoke.py") + ",dst=/probe/runtime_smoke.py,readonly",
                  "--mount", "type=bind,src=" + str(artifacts / "validation.json") + ",dst=/probe/validation.json,readonly",
                  "--entrypoint", "/usr/bin/python3", "docker-swss-layer-trixie:latest",
                  "-B", "/probe/swss_layer_runtime.py", "/probe/validation.json"],
    ]


def public_view(name, value, original_sha256):
    """Explicit base-only projection; full originals remain native-check inputs."""
    names = {image + ".validation.json" for image in IMAGES}
    if name not in names | {"installed-images-policy.json"}:
        return None
    require(re.fullmatch(r"[0-9a-f]{64}", original_sha256), "Missing original receipt digest")
    projected = copy.deepcopy(value)
    omitted = []
    if name in names:
        config = projected["config"]
        require(isinstance(config, dict), "Invalid image configuration")
        if "Env" in config:
            del config["Env"]
            omitted.append("/config/Env")
    else:
        require([v["name"] for v in projected["images"]] == list(IMAGES), "Unexpected installed-image policy scope")
        for index, image in enumerate(projected["images"]):
            config = image["expected_config"]
            require(isinstance(config, dict), "Invalid installed-image configuration")
            if "Env" in config:
                del config["Env"]
                omitted.append("/images/" + str(index) + "/expected_config/Env")
    projected["public_projection"] = {"original_file_sha256": original_sha256,
                                      "omitted_fields": omitted,
                                      "scope": "Public receipt view only; native and loaded checks use the unchanged original file."}
    return projected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build-inspect", "incremental", "loaded"))
    parser.add_argument("--image", choices=("base-config", "swss-layer"), required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    workspace, artifacts = args.workspace.resolve(), args.artifacts.resolve()
    selected = commands(args.image, args.phase, workspace, artifacts)
    for command in selected:
        if args.phase == "loaded" and command[0] == "docker":
            result = subprocess.run(command, cwd=workspace, check=True, capture_output=True, text=True)
            # Preserve the original runtime probe JSON, not Docker/environment diagnostics.
            record = json.loads(result.stdout)
            (artifacts / "runtime-smoke.json").write_text(json.dumps(record, indent=2) + "\n")
        else:
            subprocess.run(command, cwd=workspace, check=True)


if __name__ == "__main__":
    main()
