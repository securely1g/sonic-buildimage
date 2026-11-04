"""Bind DASH engine archives to source owners, package state and base layers."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile

from tools.bazel.ci import artifact_validation as check, base_images, build
from tools.bazel.ci.dash_engine_image import ROOT, IMAGES, TARGETS, clean_source, source_identity
from tools.bazel.oci import image_validation, oci_layout, source_base_validation


def require_item(files, path, item):
    # The shared OCI inventory records Linux's installed symlink mode (0777),
    # even when an owner's TAR header has mode 0000. Keep the raw TAR-to-TAR
    # comparison below exact; only the installed link's mode has this meaning.
    expected = {**item, "mode": 0o777} if item.get("kind") == "symlink" else item
    check.require(all(files.get(path, {}).get(key) == value for key, value in expected.items()),
                  "installed payload differs: " + path)


def verify_config(parent, actual, manifest):
    expected = deepcopy(parent)
    expected["Entrypoint"] = ["/usr/bin/start.sh"]
    # rules_oci resets a base Cmd when an entrypoint is supplied.
    expected["Cmd"] = []
    environment = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in expected.get("Env", [])}
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    entries = actual.get("Env", [])
    check.require(len({entry.split("=", 1)[0] for entry in entries}) == len(entries), "duplicate OCI environment name")
    check.require(dict(entry.split("=", 1) for entry in entries) == environment, "OCI environment differs")
    expected["Env"] = entries
    expected.setdefault("Labels", {})["com.azure.sonic.manifest"] = actual.get("Labels", {}).get("com.azure.sonic.manifest", "")
    check.require(json.loads(expected["Labels"]["com.azure.sonic.manifest"]) == manifest, "Make manifest label differs")
    # OCI permits omitted empty Cmd; every other field must remain exact.
    normalized = deepcopy(actual)
    normalized.setdefault("Cmd", [])
    check.require(normalized == expected, "OCI configuration changed beyond declared entrypoint/environment/manifest")


def verify_groups(before, after, base_item, item, debug):
    names = [row.split(":", 1)[0] for row in after]
    check.require(len(names) == len(set(names)), "duplicate group name")
    expected = list(before)
    if debug and "_ssh:x:104:" not in expected:
        expected.append("_ssh:x:104:")
    check.require(after == expected, "inherited group rows changed")
    check.require(all(base_item[key] == item[key] for key in ("kind", "mode", "uid", "gid")),
                  "inherited group metadata changed")


def read_groups(layout):
    selected, origins = source_base_validation.inventory(layout)
    with tempfile.TemporaryDirectory(prefix="engine-groups-") as temporary:
        root = Path(temporary)
        source_base_validation.extract_selected(layout, selected, origins, {"etc/group"}, root)
        return (root / "etc/group").read_text().splitlines()


def verify_package_binding(report, selection, parent, apt_path):
    check.require(selection["base_manifest_digest"] == parent.descriptor["digest"] and
                  selection["dependency_check"]["status"] == "satisfied", "APT base/dependency binding differs")
    check.require(report["base_manifest_digest"] == parent.descriptor["digest"] and
                  report["apt_selection_sha256"] == check.sha(apt_path) and
                  report["dependency_check"]["status"] == "satisfied", "installed package binding differs")
    check.require(all(item["package"] != "nnpy" for item in report["source_packages"]),
                  "nnpy must retain Python metadata, not an invented Debian package")


def validate(directory):
    clean_source(directory)
    identity = source_identity()
    receipt = json.loads((directory / "build-receipt.json").read_text())
    check.require(receipt["status"] == "built" and identity["revision"] == receipt["revision"], "unverified source build")
    check.require(set(receipt["artifacts"]) == set(TARGETS), "incomplete artifact inventory")
    for name, item in receipt["artifacts"].items():
        path = directory / name
        check.require(path.stat().st_size == item["bytes"] and check.sha(path) == item["sha256"], "artifact changed: " + name)
    root = build.execution_root(directory / "build-events.jsonl")
    parent = oci_layout.validate_layout(root / receipt["base_layout"], "linux/amd64")
    layouts = {name: oci_layout.validate_layout(root / receipt["layouts"][name], "linux/amd64") for name in IMAGES}
    inherited = base_images.installed_inventory(parent)
    parent_groups = read_groups(parent)
    configuration = check.payload(directory / "engine.files.tar")
    files_only = {path: item for path, item in configuration.items() if item["kind"] != "directory"}
    check.require(set(files_only) == {"usr/bin/start.sh"}, "startup file inventory differs")
    start = files_only["usr/bin/start.sh"]
    check.require(start["sha256"] == check.sha(ROOT / "platform/vs/docker-dash-engine/start.sh") and
                  (start["kind"], start["mode"], start["uid"], start["gid"]) == ("file", 0o755, 0, 0), "original startup script changed")
    source = check.payload(directory / "engine.source.tar")
    original = {}
    for name in ("pi.source.tar", "bmv2.source.tar", "nnpy.source.tar"):
        for path, item in check.payload(directory / name).items():
            if item["kind"] == "directory":
                continue
            normalized = "usr/" + path if path.startswith(("lib/", "bin/", "sbin/")) else path
            check.require(normalized not in original or original[normalized] == item, "source owners collide: " + normalized)
            original[normalized] = item
    check.require(original == {path: item for path, item in source.items() if item["kind"] != "directory"},
                  "flattened source packages changed payloads")
    nnpy = check.payload(directory / "nnpy.source.tar")
    check.require("usr/lib/python3/dist-packages/_nnpy.so" in nnpy and
                  "usr/lib/python3/dist-packages/nnpy-1.4.2.egg-info/PKG-INFO" in nnpy, "nnpy extension/distribution metadata absent")
    debug_tools = check.payload(directory / "engine.debug-tools.tar", require_root=False)
    manifest = json.loads((ROOT / "target/bazel-manifests/docker-dash-engine/manifest.json").read_text())
    inventories, reports = {}, [json.loads((directory / "base.packages.json").read_text())]
    for name in IMAGES:
        debug = name.endswith("-dbg")
        layout = layouts[name]
        base = layouts[IMAGES[0]] if debug else parent
        image_validation.assert_ancestry(base, layout, "engine stage ancestry")
        image_validation.verify_unique_layer_paths(layout)
        files = base_images.installed_inventory(layout)
        image_validation.verify_directory_aliases(files)
        for path, item in (source | configuration).items():
            if item["kind"] != "directory":
                require_item(files, path, item)
        stage_parent = inventories[IMAGES[0]] if debug else inherited
        for path, item in stage_parent.items():
            if item["kind"] == "directory" or path in ("var/lib/dpkg/status", "etc/group"):
                continue
            expected = (debug_tools if debug else configuration).get(path, item)
            require_item(files, path, expected)
        if debug:
            for path, item in debug_tools.items():
                if item["kind"] != "directory":
                    require_item(files, path, item)
        verify_groups(parent_groups, read_groups(layout), inherited["etc/group"], files["etc/group"], debug)
        prefix = "engine-debug" if debug else "engine"
        report = json.loads((directory / (prefix + ".packages.json")).read_text())
        apt_path = directory / ("debug-apt.json" if debug else "runtime-apt.json")
        verify_package_binding(report, json.loads(apt_path.read_text()), base, apt_path)
        reports.append(report)
        for path, artifact in (("var/lib/dpkg/status", prefix + ".status"),
                               ("usr/share/sonic/bazel/dash-engine" + ("-debug" if debug else "") + "-packages.json", prefix + ".packages.json")):
            check.require(files.get(path, {}).get("sha256") == check.sha(directory / artifact), "installed package state differs: " + path)
        check.require(all(item.get("elf_machine", 62) == 62 for item in files.values()), "foreign ELF architecture")
        verify_config(parent.config.get("config", {}), layout.config.get("config", {}), manifest)
        archive = image_validation.docker_archive(directory / (name + ".gz"), name + ":latest",
            layout.manifest["config"]["digest"], layout.manifest["layers"])
        inventories[name] = files
        (directory / (name + ".validation.json")).write_text(json.dumps({"status": "passed",
            "manifest_digest": layout.descriptor["digest"], "rootfs": layout.config["rootfs"],
            "config_digest": layout.manifest["config"]["digest"], "archive": archive}, indent=2) + "\n")
    runtime, debug = (inventories[name] for name in IMAGES)
    for path, item in runtime.items():
        if "elf_machine" in item:
            check.require(debug.get(path) == item, "debug image changed runtime ELF: " + path)
    native = base_images.verify_source_files(debug, reports, 62)
    runtimes = [directory / name for name in ("base.source.tar", "engine.source.tar", "openssh.source.tar")]
    for archive in runtimes:
        for path, item in check.payload(archive, require_root=False).items():
            if "elf_machine" not in item:
                continue
            require_item(debug, path, item)
            if path in native:
                check.require(native[path]["sha256"] == item["sha256"], "source owners disagree: " + path)
            else:
                native[path] = {**item, "source_package": archive.name}
    pairs = base_images.verify_symbols(native, runtimes, directory / "engine.debug.tar", directory)
    check.require((debug["usr/bin/ssh-agent"]["gid"], debug["usr/bin/ssh-agent"]["mode"]) == (104, 0o2755), "SSH client privilege differs")
    selected, origins = source_base_validation.inventory(layouts[IMAGES[1]])
    elfs = []
    with tempfile.TemporaryDirectory(prefix="engine-native-") as temporary:
        root = Path(temporary)
        source_base_validation.extract_selected(layouts[IMAGES[1]], selected, origins, set(native), root)
        for path, item in sorted(native.items()):
            info = check.elf_info(root / path, dynamic=True)
            elfs.append({"path": path, "needed": info["needed"], "sha256": item["sha256"]})
    clean_source(directory)
    check.require(source_identity() == identity, "validation source changed")
    report = {"status": "passed", "architecture": "amd64", "revision": receipt["revision"],
              "image_version": receipt["image_version"], "native": native, "source_debug_pairs": pairs,
              "runtime_native": {path: item for path, item in native.items() if path in runtime},
              "elfs": elfs, "artifacts": receipt["artifacts"]}
    (directory / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "passed", "source_elf_debug_pairs": len(pairs)}))
