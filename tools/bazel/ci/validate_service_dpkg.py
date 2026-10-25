"""Verify APT transactions against the exact Syncd and Orchagent image archives."""
import argparse
import ast
import json
import os
from pathlib import Path
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


def build_inputs(bazel, options, output):
    """Audit the actual selected graph, build it, and query its declared outputs."""
    targets = sorted(set(TARGETS.values()))
    expression = "set(" + " ".join(targets) + ")"
    actions = output / "actions.json"
    with actions.open("w") as result, (output / "aquery.log").open("w") as log:
        subprocess.run(bazel + ["aquery", *options, "--include_file_write_contents", "--output=jsonproto", "deps(" + expression + ")"],
                       stdout=result, stderr=log, check=True)
    audit = inspect_actions(actions)
    audit["targets"] = targets
    audit["deb_generation_authorized"] = False
    save(output / "action-audit.json", audit)
    require(audit["action_count"] > 0, "Image build action graph is empty")
    require(not (audit["deb_outputs"] or audit["deb_packaging_actions"] or audit["packaging_wrappers"]),
            "DEB or packaging wrapper found in image build action graph")
    with (output / "build.log").open("w") as log:
        subprocess.run(bazel + ["build", *options, *targets], stdout=log, stderr=subprocess.STDOUT, check=True)
    formatter = "str(target.label) + '\\t' + repr([f.path for f in target.files.to_list()])"
    with (output / "cquery.log").open("w") as log:
        result = subprocess.run(bazel + ["cquery", *options, expression, "--output=starlark",
                                       "--starlark:expr=" + formatter],
                                stdout=subprocess.PIPE, stderr=log, text=True, check=True)
    paths = {}
    for line in result.stdout.splitlines():
        label, files = line.split("\t", 1)
        label = label[label.index("//"):]
        files = ast.literal_eval(files)
        if label not in targets or len(files) != 1:
            raise ValueError("Expected one output for each requested image input: " + label)
        path = Path(files[0]).absolute()
        if not path.exists():
            raise ValueError("Bazel output is missing: " + str(path))
        paths[label] = path
    if set(paths) != set(targets):
        raise ValueError("Bazel did not return every requested image input")
    inputs = {key: paths[label] for key, label in TARGETS.items()}
    return inputs


def validate_dpkg_images(paths, output):
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
            archive = paths[prefix + variant + "_archive"]
            # Each image installs the same additional command and dependencies
            # while preserving its existing Debian packages and source layers.
            receipt = image + "-packages.json"
            result = validate_dpkg_state(
                architecture="amd64", output=output / ("dpkg-" + name), archive=archive,
                archive_sha256=sha(archive), config_digest=layout.manifest["config"]["digest"],
                installed_receipts=["/usr/share/sonic/bazel/" + receipt],
                installed_source_receipts=[
                    "/usr/share/sonic/bazel/base-packages.json",
                    "/usr/share/sonic/bazel/config-engine-packages.json",
                ])
            results[name] = {"status": result["status"], "archive_sha256": sha(archive),
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    options = ["--config=syncd-dash", "--//dockers/docker-orchagent:source_config_engine=true",
               *shlex.split(os.environ.get("BAZEL_CONTAINER_ARGS", ""))]
    paths = build_inputs(shlex.split(args.bazel), options, args.output)
    validate_dpkg_images(paths, args.output)


if __name__ == "__main__":
    main()
