"""Validate an image consumer's inputs from the VS build without invoking Bazel or Make."""

from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import re
import subprocess
import uuid

from tools.bazel.ci import validate_runtime
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci.image_validation import docker_archive


def read_oci(directory, variant):
    """Read the file inventory and config taken from the producer's OCI layout."""
    with gzip.open(directory / (variant + ".oci.json.gz"), "rt", encoding="utf-8") as stream:
        return json.load(stream)


def bundle_file(directory, name):
    """Resolve a regular file published at the root of this family's artifact."""
    require(isinstance(name, str) and name and Path(name).name == name and name not in (".", ".."),
            "Invalid VS artifact filename: " + repr(name))
    path = directory / name
    require(path.is_file() and not path.is_symlink(), "Missing VS artifact file: " + name)
    return path


def validate_artifacts(directory, image, revision, debug_enabled, build_version):
    """Bind every selected archive and probe input to the checkout and producer's layout."""
    bundle = json.loads(bundle_file(directory, "bundle.json").read_bytes())
    require(bundle["image"] == image and bundle["revision"] == revision,
            "VS artifact does not match the selected container and checkout")
    require(bundle["architecture"] == "amd64", "VS artifact has the wrong architecture")
    require(bundle["debug_enabled"] is debug_enabled and isinstance(bundle["supports_debug"], bool),
            "VS artifact does not match the requested debug setting")
    require(isinstance(build_version, str) and build_version and bundle["build_version"] == build_version,
            "VS artifact has the wrong VS release")
    variants = ["runtime"]
    if debug_enabled and bundle["supports_debug"]:
        variants.append("debug")
    archives = bundle["archives"]
    require(set(archives) == set(variants), "VS artifact does not contain exactly the selected variants")

    fixtures = bundle["fixtures"]
    for name, digest in fixtures.items():
        require(sha(bundle_file(directory, name)) == digest, "VS fixture checksum differs: " + name)

    for variant in variants:
        spec = archives[variant]
        name = image if variant == "runtime" else image + "-dbg"
        require(spec["file"] == name + ".gz", "VS archive has the wrong filename for " + name)
        require(variant + ".oci.json.gz" in fixtures, "Missing producer OCI metadata for " + name)
        oci = read_oci(directory, variant)
        config = oci["config"]
        require((config["os"], config["architecture"]) == ("linux", bundle["architecture"]),
                "OCI metadata has the wrong platform for " + name)
        require(config["config"].get("Labels", {}).get("Tag") == bundle["build_version"],
                "OCI image has the wrong VS release for " + name)
        require(oci["config_digest"] == spec["config_digest"] and oci["layers"] == spec["layers"]
                and config["rootfs"] == spec["rootfs"], "Producer OCI descriptors disagree for " + name)
        rootfs = spec["rootfs"]
        layers = spec["layers"]
        require(layers and rootfs["type"] == "layers" and len(rootfs["diff_ids"]) == len(layers),
                "Invalid VS filesystem layer count for " + name)
        for layer in layers:
            require(re.fullmatch(r"sha256:[a-f0-9]{64}", layer["digest"]) and layer["size"] >= 0,
                    "Invalid VS layer descriptor for " + name)
        archive = bundle_file(directory, spec["file"])
        result = docker_archive(archive, name + ":latest", spec["config_digest"], layers)
        require(result["sha256"] == spec["sha256"], "VS archive checksum differs: " + name)
    if "debug" in archives:
        runtime = archives["runtime"]
        debug = archives["debug"]
        count = len(runtime["layers"])
        require(debug["layers"][:count] == runtime["layers"]
                and debug["rootfs"]["diff_ids"][:count] == runtime["rootfs"]["diff_ids"],
                "The debug image does not extend the selected VS runtime")
    return bundle


@contextmanager
def loaded_image(directory, spec, bundle):
    """Load this verified export under a job-private tag, run checks, then release that tag."""
    tag = "sonic-vs-probe:" + uuid.uuid4().hex
    try:
        image, config, _ = validate_runtime.load_archive(directory / spec["file"], tag, spec["config_digest"])
        validate_runtime.verify_loaded_image(image, config, spec["config_digest"])
        require((config["os"], config["architecture"]) == ("linux", bundle["architecture"])
                and config["rootfs"] == spec["rootfs"], "Docker loaded different VS filesystem layers")
        require(config["config"].get("Labels", {}).get("Tag") == bundle["build_version"],
                "Docker loaded a different VS release")
        yield tag
    finally:
        subprocess.run(["docker", "image", "rm", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60, check=False)
