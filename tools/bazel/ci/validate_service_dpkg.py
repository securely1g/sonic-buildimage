"""Verify APT transactions against the exact Syncd and Orchagent image archives."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.bazel_commands import inspect_actions
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.ci.validate_dpkg_state import validate as validate_dpkg_state
from tools.bazel.oci.oci_layout import validate_layout

OWNER = "//platform/vs/docker-syncd-vs"
ORCHAGENT = "//dockers/docker-orchagent"
TARGETS = {
    "runtime": OWNER + ":docker-syncd-vs",
    "debug": OWNER + ":docker-syncd-vs-dbg",
    "runtime_archive": OWNER + ":docker-syncd-vs.gz",
    "debug_archive": OWNER + ":docker-syncd-vs-dbg.gz",
    "orchagent_runtime": ORCHAGENT + ":docker-orchagent",
    "orchagent_debug": ORCHAGENT + ":docker-orchagent-dbg",
    "orchagent_runtime_archive": ORCHAGENT + ":docker-orchagent.gz",
    "orchagent_debug_archive": ORCHAGENT + ":docker-orchagent-dbg.gz",
}

def save(path, value):
    value = dict(value, _generated={
        "notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
        "generator": "tools/bazel/ci/validate_service_dpkg.py",
    })
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def build_inputs(bazel, options, output, selected_targets=None, workspace=None):
    """Audit the actual selected graph, build it, and query its declared outputs."""
    selected_targets = TARGETS if selected_targets is None else selected_targets
    workspace = Path.cwd() if workspace is None else workspace
    targets = sorted(set(selected_targets.values()))
    output.mkdir(parents=True, exist_ok=True)
    expression = "set(" + " ".join(targets) + ")"
    actions = output / "actions.json"
    with actions.open("w") as result, (output / "aquery.log").open("w") as log:
        subprocess.run(bazel + ["aquery", *options, "--include_file_write_contents", "--output=jsonproto", "deps(" + expression + ")"],
                       cwd=workspace, stdout=result, stderr=log, check=True)
    audit = inspect_actions(actions)
    audit["targets"] = targets
    audit["options_sha256"] = hashlib.sha256(json.dumps(options, separators=(",", ":")).encode()).hexdigest()
    audit["deb_generation_authorized"] = False
    save(output / "action-audit.json", audit)
    require(audit["action_count"] > 0, "Image build action graph is empty")
    require(not (audit["deb_outputs"] or audit["deb_packaging_actions"] or audit["packaging_wrappers"]),
            "DEB or packaging wrapper found in image build action graph")
    with (output / "build.log").open("w") as log:
        subprocess.run(bazel + ["build", *options, *targets], cwd=workspace, stdout=log, stderr=subprocess.STDOUT, check=True)
    formatter = "str(target.label) + '\\t' + repr([f.path for f in target.files.to_list()])"
    with (output / "cquery.log").open("w") as log:
        result = subprocess.run(bazel + ["cquery", *options, expression, "--output=starlark",
                                       "--starlark:expr=" + formatter],
                                cwd=workspace, stdout=subprocess.PIPE, stderr=log, text=True, check=True)
    paths = {}
    for line in result.stdout.splitlines():
        label, files = line.split("\t", 1)
        label = label[label.index("//"):]
        files = ast.literal_eval(files)
        if label not in targets or len(files) != 1:
            raise ValueError("Expected one output for each requested image input: " + label)
        path = Path(files[0])
        path = path if path.is_absolute() else workspace / path
        if not path.exists():
            raise ValueError("Bazel output is missing: " + str(path))
        paths[label] = path
    if set(paths) != set(targets):
        raise ValueError("Bazel did not return every requested image input")
    inputs = {key: paths[label] for key, label in selected_targets.items()}
    return inputs


def query_owner_arguments(workspace):
    """Read configured owner arguments without selecting a build target."""
    query = """$(info VS_APT:BLDENV=$(strip $(BLDENV)))
$(info VS_APT:PLATFORM=$(strip $(CONFIGURED_PLATFORM)))
$(info VS_APT:ARCH=$(strip $(CONFIGURED_ARCH)))
$(info VS_APT:BAZEL=$(strip $(BUILD_WITH_BAZEL_WHEN_AVAILABLE)))
$(info VS_APT:FIPS=$(strip $(INCLUDE_FIPS)))
$(info VS_APT:DASH=$(strip $(INCLUDE_VS_DASH_SAI)))
$(info VS_APT:ASAN=$(strip $(ENABLE_ASAN)))
$(info VS_APT:SYNCD_RPC=$(strip $(ENABLE_SYNCD_RPC)))
$(info VS_APT:CROSS=$(strip $(CROSS_BUILD_ENVIRON)))
$(info VS_APT:QEMU=$(strip $(MULTIARCH_QEMU_ENVIRON)))
$(info VS_APT:RUNTIME_IMAGES=$(strip $(SONIC_BAZEL_DOCKER_IMAGES)))
$(info VS_APT:DEBUG_IMAGES=$(strip $(SONIC_BAZEL_DBG_DOCKER_IMAGES)))
$(info VS_APT:ORCHAGENT_RUNTIME=$(strip $(docker-orchagent.gz_BAZEL_ARGS)))
$(info VS_APT:ORCHAGENT_DEBUG=$(strip $(docker-orchagent-dbg.gz_BAZEL_ARGS)))
$(info VS_APT:SYNCD_RUNTIME=$(strip $(docker-syncd-vs.gz_BAZEL_ARGS)))
$(info VS_APT:SYNCD_DEBUG=$(strip $(docker-syncd-vs-dbg.gz_BAZEL_ARGS)))
.PHONY: .vs-apt-options
.vs-apt-options: ; @:
"""
    command = ["make", "--no-print-directory", "-s", "-j1", "-f", "slave.mk", "-f", "-", ".vs-apt-options",
               "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "INCLUDE_VS_DASH_SAI=y", "INCLUDE_FIPS=y",
               "ENABLE_ASAN=n", "ENABLE_SYNCD_RPC=n", "INSHOOKS="]
    result = subprocess.run(command, cwd=workspace, input=query, text=True, capture_output=True, check=True)
    values = {}
    for line in result.stdout.splitlines():
        if line.startswith("VS_APT:"):
            key, value = line.removeprefix("VS_APT:").split("=", 1)
            require(key not in values, "Duplicate Make APT validation setting")
            values[key] = value
    expected = {"BLDENV": "trixie", "PLATFORM": "vs", "ARCH": "amd64", "BAZEL": "y",
                "FIPS": "y", "DASH": "y", "ASAN": "n", "SYNCD_RPC": "n", "CROSS": "n", "QEMU": "n"}
    require(set(values) == set(expected) | {"RUNTIME_IMAGES", "DEBUG_IMAGES", "ORCHAGENT_RUNTIME",
            "ORCHAGENT_DEBUG", "SYNCD_RUNTIME", "SYNCD_DEBUG"}, "Incomplete Make APT validation settings")
    require(all(values.get(key) == value for key, value in expected.items()),
            "APT validation requires the configured native AMD64 Trixie VS/FIPS/DASH profile")
    require({"docker-orchagent.gz", "docker-syncd-vs.gz"} <= set(values.get("RUNTIME_IMAGES", "").split()) and
            {"docker-orchagent-dbg.gz", "docker-syncd-vs-dbg.gz"} <= set(values.get("DEBUG_IMAGES", "").split()),
            "APT validation requires both Bazel runtime/debug image pairs")
    owners = {}
    for owner in ("ORCHAGENT", "SYNCD"):
        runtime = values.get(owner + "_RUNTIME")
        # The source config-engine default needs no extra Orchagent flag.
        require(runtime == values.get(owner + "_DEBUG"),
                "APT validation requires matching runtime/debug owner arguments: " + owner)
        owners[owner.lower()] = runtime
    return owners


def resolve_bazel_options(workspace, owner_arguments):
    """Use the same common/owner parsing and final platform order as docker.mk."""
    common = os.environ.get("BAZEL_CONTAINER_ARGS", os.environ.get("BAZEL_SWSS_ARGS", ""))
    resolved = {}
    for owner, arguments in owner_arguments.items():
        result = subprocess.run(
            [sys.executable, "tools/bazel/build_helpers.py", "options", "--arguments=" + common + " " + arguments],
            cwd=workspace, text=True, capture_output=True)
        require(result.returncode == 0, "Could not resolve Make Bazel options for " + owner)
        options = result.stdout.splitlines()
        require(all(options) and all("\0" not in value for value in options), "Invalid Make Bazel options")
        options += ["--platforms=@sonic_build_infra//platforms:x86_64_trixie",
                    "--host_platform=@sonic_build_infra//platforms:x86_64_trixie"]
        resolved[owner] = options
    return resolved


def build_service_inputs(bazel, options, output, workspace):
    """Audit each owner's actual configured graph independently."""
    paths = {}
    for owner in ("syncd", "orchagent"):
        selected = {key: label for key, label in TARGETS.items()
                    if key.startswith("orchagent_") == (owner == "orchagent")}
        paths.update(build_inputs(bazel, options[owner], output / (owner + "-graph"), selected, workspace))
    return paths


def captured_archives(workspace, receipt_path, output):
    """Select only the Make target archives named by the captured pair receipt."""
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)
    images = {"syncd": "docker-syncd-vs", "orchagent": "docker-orchagent"}
    require(isinstance(receipt, dict) and receipt.get("schema") == 1 and
            isinstance(receipt.get("pairs"), dict) and set(receipt["pairs"]) == set(images.values()),
            "Unsupported captured service-pair receipt")
    installer = receipt.get("installer", {})
    require(isinstance(installer, dict) and installer.get("image") in {"sonic-vs.bin", "sonic-vs.img.gz"} and
            isinstance(installer.get("debug"), bool) and isinstance(installer.get("sha256"), str) and
            re.fullmatch(r"[0-9a-f]{64}", installer["sha256"]),
            "Captured pair receipt has no supported installer selection")
    suffix = "-dbg.gz" if installer["debug"] else ".gz"
    require(installer.get("paired_images") == ["target/" + image + suffix for image in ("docker-orchagent", "docker-syncd-vs")],
            "Captured pair receipt selects unexpected service archives")
    captured = {}
    for owner, image in images.items():
        require(isinstance(receipt["pairs"][image], dict), "Captured service pair is incomplete")
        for variant in ("runtime", "debug"):
            snapshot = receipt["pairs"][image].get(variant, {})
            require(isinstance(snapshot, dict) and isinstance(snapshot.get("sha256"), str) and
                    isinstance(snapshot.get("config_digest"), str) and
                    re.fullmatch(r"[0-9a-f]{64}", snapshot["sha256"]) and
                    re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot["config_digest"]) and
                    type(snapshot.get("bytes")) is int and snapshot["bytes"] > 0,
                    "Captured service archive identity is incomplete")
            relative = "target/" + image + ("-dbg.gz" if variant == "debug" else ".gz")
            path = workspace / relative
            require(path.is_file() and path.stat().st_size == snapshot["bytes"],
                    "Captured Make service archive size differs: " + relative)
            captured[owner + "-" + variant] = {"path": path, "relative": relative,
                "sha256": snapshot["sha256"], "bytes": snapshot["bytes"], "config_digest": snapshot["config_digest"]}
    save(output / "captured-inputs.json", {"pair_receipt_sha256": hashlib.sha256(raw).hexdigest(),
         "installer": installer, "images": {name: {key: value for key, value in row.items() if key != "path"}
         for name, row in captured.items()}})
    return captured


def validate_dpkg_images(paths, captured, output):
    """Exercise exact source-built service images; this profile remains AMD64-only."""
    cases = (
        ("syncd", "runtime", ""),
        ("syncd", "debug", ""),
        ("orchagent", "runtime", "orchagent_"),
        ("orchagent", "debug", "orchagent_"),
    )
    results = {}
    for image, variant, prefix in cases:
        name = image + "-" + variant
        try:
            layout = validate_layout(paths[prefix + variant], "linux/amd64")
            queried_archive = paths[prefix + variant + "_archive"]
            selected = captured[name]
            require(queried_archive.stat().st_size == selected["bytes"] and sha(queried_archive) == selected["sha256"],
                    "Queried Bazel archive differs from the captured Make archive: " + name)
            require(layout.manifest["config"]["digest"] == selected["config_digest"],
                    "Queried Bazel config differs from the captured Make archive: " + name)
            archive = selected["path"]
            # Each image installs the same additional command and dependencies
            # while preserving its existing Debian packages and source layers.
            receipt = image + "-packages.json"
            result = validate_dpkg_state(
                architecture="amd64", output=output / ("dpkg-" + name), archive=archive,
                archive_sha256=selected["sha256"], config_digest=selected["config_digest"],
                installed_receipts=["/usr/share/sonic/bazel/" + receipt],
                installed_source_receipts=[
                    "/usr/share/sonic/bazel/base-packages.json",
                    "/usr/share/sonic/bazel/config-engine-packages.json",
                ])
            results[name] = {"status": result["status"], "archive": selected["relative"],
                             "archive_sha256": selected["sha256"], "config_digest": selected["config_digest"],
                             "image_manifest_digest": layout.descriptor["digest"],
                             "receipt_sha256": sha(output / ("dpkg-" + name) / "receipt.json")}
        except Exception as error:
            results[name] = {"status": "failed", "error": str(error)}
        finally:
            save(output / "dpkg-images.json", {"architecture": "amd64", "images": results})
    require(all(result["status"] == "passed" for result in results.values()),
            "Installed package transactions failed; see dpkg-images.json and the per-image logs")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", default=os.environ.get("BAZEL", "bazel"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pair-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = args.output if args.output.is_absolute() else workspace / args.output
    receipt = args.pair_receipt if args.pair_receipt.is_absolute() else workspace / args.pair_receipt
    output.mkdir(parents=True, exist_ok=True)
    captured = captured_archives(workspace, receipt, output)
    options = resolve_bazel_options(workspace, query_owner_arguments(workspace))
    paths = build_service_inputs(shlex.split(args.bazel), options, output, workspace)
    validate_dpkg_images(paths, captured, output)


if __name__ == "__main__":
    main()
