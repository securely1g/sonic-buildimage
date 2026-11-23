"""Fingerprint the actual VS inputs of validation jobs across successful master runs."""

import hashlib
import json

from tools.bazel.ci import vs_python_packages as wheels
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.ci.vs_container_validation import bundle_file


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def image_config(config, version):
    # The producer stamps each run with a new Tag/IMAGE_VERSION. Check their
    # binding before replacing them; preserve every other OCI setting and rootfs.
    runtime = config["config"]
    labels = dict(runtime.get("Labels", {}))
    if labels.get("Tag") != version:
        raise ValueError("OCI Tag disagrees with the selected VS version")
    labels["Tag"] = "<VS release>"
    normalized = {**runtime, "Labels": labels}
    if "Env" in runtime:
        normalized["Env"] = ["IMAGE_VERSION=<VS release>" if item == f"IMAGE_VERSION={version}" else item
                             for item in runtime["Env"]]
    return digest({**config, "config": normalized})


def read(path):
    return json.loads(path.read_bytes())


def image_inputs(directory, selection, image):
    manifest = read(bundle_file(directory, "bundle.json"))
    version = selection["build_version"]
    require(manifest["image"] == image and all(manifest[key] == selection[key]
            for key in ("architecture", "revision", "build_version")), "Unexpected VS bundle identity: " + image)
    debug = selection["debug_enabled"]
    supports_debug = image + "-dbg.gz" in selection["registered_debug"] + selection["validation_only_debug"]
    require(isinstance(debug, bool) and manifest["debug_enabled"] is debug
            and manifest["supports_debug"] is supports_debug, "Unexpected VS debug configuration: " + image)
    expected = {"runtime": image + ".gz"}
    if debug and supports_debug:
        expected["debug"] = image + "-dbg.gz"
    require(manifest["archives"].keys() == expected.keys(), "Unexpected VS archive variants: " + image)
    fixtures = dict(manifest["fixtures"])
    for name, checksum in fixtures.items():
        require(sha(bundle_file(directory, name)) == checksum, "Unexpected VS fixture checksum: " + name)
    variants = {}
    for name, filename in expected.items():
        item = manifest["archives"][name]
        require(item["file"] == filename, "Unexpected VS archive name: " + filename)
        # stage_archive already hashed this exact output. Check its recorded size
        # here without rereading all the compressed images in the installer job.
        size = bundle_file(directory, filename).stat().st_size
        require(size == item["size"] and size > 0, "Unexpected VS archive size: " + filename)
        variants[name] = {"config": item["validation_config"], "layers": item["layers"], "file": filename}
        # The inventory derives from these same layers and config. Its raw
        # manifest digest includes the run-specific Tag even for identical jobs.
        fixtures.pop(name + ".oci.json.gz")
        env_file = name + ".make.image.env"
        if env_file in fixtures:
            lines = (directory / env_file).read_bytes().splitlines(keepends=True)
            old = ("IMAGE_VERSION=" + version + "\n").encode()
            fixtures[env_file] = digest([line.hex() if line != old else "<VS release>" for line in lines])
    # Inherited inventories are represented by the parent's own fingerprint;
    # changed parents select their consumers even when only a test fixture changed.
    fixtures.pop("parent.oci.json.gz", None)
    settings = {k: v for k, v in manifest.items() if k not in {"revision", "build_version", "archives", "fixtures"}}
    return digest([settings, variants, fixtures])


def create(directory):
    selection = read(directory / "selection.json")
    revision = selection["revision"]
    version = selection["build_version"]
    names = {name.removesuffix(".gz").removesuffix("-dbg") for name in selection["installer_archives"]}
    names.update(selection["auxiliary_parents"])
    images = {name: image_inputs(directory / name, selection, name) for name in sorted(names)}
    components = {}
    for name in selection["components"]:
        owner = read(directory / "components" / name / "manifest.json")
        if owner["revision"] != revision or owner["component"] != name:
            raise ValueError("Component profile differs from this VS build: " + name)
        settings = dict(owner["build_settings"])
        if settings.pop("//tools/bazel/oci:image_version") != version:
            raise ValueError("Unexpected VS component version: " + name)
        components[name] = digest([owner["files"], settings, owner["architecture"], owner["images"], owner["debug"]])
    packaging = read(directory.parent / "python-packages" / "manifest.json")
    kernel = read(directory.parent / "kernel-amd64" / "vs-kernel-handoff.json")
    if packaging["revision"] != revision or kernel["vs_revision"] != revision:
        raise ValueError("VS package inputs have a different revision")
    packages = packaging["packages"]

    def selected(names):
        return {name: packages[name] for name in names}

    groups = {
        "package_amd64": digest([selected(wheels.WHEELS | wheels.DHCP), packages[wheels.YANG_TREE], packages[wheels.SCAPY_WHEEL],
                                 packaging["pddf_enabled"], packages.get(wheels.PDDF_WHEEL)]),
        "archive": digest([selected(wheels.WHEELS), packages[wheels.YANG_TREE]]),
        "frr_python_amd64": digest(selected(wheels.FRR)),
        "yang_models": digest(selected(wheels.YANG)),
        "kernel_packages_amd64": digest(kernel["summary"]["artifacts"]),
    }
    return {"schema": 1, "revision": revision, "architecture": selection["architecture"],
            "debug": selection["debug_enabled"], "images": images, "components": components, "groups": groups}
