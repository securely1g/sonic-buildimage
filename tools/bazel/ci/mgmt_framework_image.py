#!/usr/bin/env python3
"""Build Framework images, then validate completed source and OCI artifacts."""

import argparse
from functools import partial
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci import base_images, bazel_commands, build, command_log, resolution
from tools.bazel.oci import image_validation, oci_layout

OWNER = "//dockers/docker-sonic-mgmt-framework:"
IMAGES = ("docker-sonic-mgmt-framework", "docker-sonic-mgmt-framework-dbg")
PARENT = "//dockers/docker-config-engine-trixie:docker-config-engine-trixie"
TARGETS = {
    **{name + ".gz": OWNER + name + ".gz" for name in IMAGES},
    "source-runtime.tar": OWNER + "source_runtime",
    "framework-runtime.tar": "@sonic_mgmt_framework//dist:runtime",
    "framework-debug.tar": "@sonic_mgmt_framework//dist:runtime.debug_symbols",
    "common-runtime.tar": "@sonic_mgmt_common//dist:runtime",
    "all-debug.tar": OWNER + "all_debug_symbols",
    "debug-tools.tar": OWNER + "debug_tools",
    "test-redis.tar": OWNER + "test_redis",
    "python-runtime.tar": OWNER + "python_runtime",
    "base-source.tar": "//dockers/docker-base-trixie:source_runtime",
    "base-packages.json": "//dockers/docker-base-trixie:runtime_package_metadata",
    "config-engine-source.tar": "//dockers/docker-config-engine-trixie:source_runtime",
    "config-engine-packages.json": "//dockers/docker-config-engine-trixie:runtime_package_metadata",
    "openssh-source.tar": OWNER + "debug_client",
    "packages.json": OWNER + "runtime_package_metadata",
    "installed.status": OWNER + "installed_packages",
    "debug-packages.json": OWNER + "debug_package_metadata",
    "debug.status": OWNER + "debug_installed_packages",
}
OPTIONS = ["--jobs=2", "--local_resources=cpu=2", "--local_resources=memory=10000"]


def build_images(args, directory):
    check.require(platform.machine() == "x86_64" and
                  platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie",
                  "Framework image build requires native AMD64 Debian Trixie")
    receipt = {"status": "building", "architecture": "amd64", "commands": []}
    execute = partial(command_log.execute, cwd=ROOT)
    try:
        receipt["revision"] = execute(["git", "rev-parse", "HEAD"], directory, receipt, "revision").strip()
        receipt["commit_headers"] = subprocess.check_output(
            ["git", "cat-file", "-p", "HEAD"], cwd=ROOT, text=True).split("\n\n", 1)[0]
        execute(["make", "-f", "tools/bazel/prepare_manifests.mk",
                 "MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-sonic-mgmt-framework.mk",
                 "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "BLDENV=trixie", "CONFIGURED_PLATFORM=vs",
                 "CONFIGURED_ARCH=amd64", "INCLUDE_FIPS=y", "ENABLE_ASAN=n", "DBG_IMAGE_MARK=dbg",
                 "DOCKERS_PATH=dockers"], directory, receipt, "manifests")
        layouts = [OWNER + name for name in IMAGES] + [PARENT]
        receipt["action_audit"] = bazel_commands.audit_targets(
            args.bazel, OPTIONS, [*TARGETS.values(), *layouts], workspace=ROOT,
            output=directory / "action-audit.json")
        build.collect_archives(ROOT, directory, receipt, TARGETS, bazel=args.bazel, options=OPTIONS)
        execute([args.bazel, "build", *OPTIONS, *layouts], directory, receipt, "layouts")
        receipt["layouts"] = {target: execute(
            [args.bazel, "cquery", *OPTIONS, target, "--output=files"], directory, receipt,
            "layout-" + str(index)).strip() for index, target in enumerate(layouts)}
        receipt["resolution"] = resolution.collect(ROOT, directory, bazel=[args.bazel])
        receipt["status"] = "built"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        (directory / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def validate_images(directory):
    receipt = json.loads((directory / "build-receipt.json").read_text())
    check.require(receipt["status"] == "built", "image build did not complete")
    for name, item in receipt["artifacts"].items():
        check.require(check.sha(directory / name) == item["sha256"], "artifact changed: " + name)
    parent = oci_layout.validate_layout(ROOT / receipt["layouts"][PARENT], "linux/amd64")
    inherited = base_images.installed_inventory(parent)
    debug_tools = check.payload(directory / "debug-tools.tar", require_root=False)
    reports, inventories = {}, {}
    configs = {"start.sh": "usr/bin/start.sh", "rest-server.sh": "usr/bin/rest-server.sh",
               "mgmt_vars.j2": "usr/share/sonic/templates/mgmt_vars.j2",
               "supervisord.conf": "etc/supervisor/conf.d/supervisord.conf"}
    for name in IMAGES:
        layout = oci_layout.validate_layout(ROOT / receipt["layouts"][OWNER + name], "linux/amd64")
        base_images.verify_unique_layer_paths(layout)
        files = base_images.installed_inventory(layout)
        base_images.verify_directory_aliases(files)
        check.require(all(item.get("elf_machine", 62) == 62 for item in files.values()), "non-AMD64 ELF")
        # The service owns these startup files and its new package inventory.
        # Debug tools also add their explicitly declared _ssh group.
        changed = set(configs.values()) | {"var/lib/dpkg/status"}
        if name.endswith("-dbg"):
            changed |= {"etc/group", "etc/gshadow"}
        for path, item in inherited.items():
            if item["kind"] == "directory" or path in changed:
                continue
            # Full Vim deliberately replaces the parent's vim-tiny alternatives.
            # Require the declared debug-tool payload for those exact paths.
            expected_item = debug_tools.get(path, item) if name.endswith("-dbg") else item
            check.require(all(files.get(path, {}).get(key) == value for key, value in expected_item.items()),
                          "image replaced an inherited parent file: " + path)
        if name.endswith("-dbg"):
            for path, item in debug_tools.items():
                if item["kind"] != "directory":
                    check.require(all(files.get(path, {}).get(key) == value for key, value in item.items()),
                                  "debug tool payload changed: " + path)
        checked = {}
        for archive in ("framework-runtime.tar", "common-runtime.tar", "python-runtime.tar"):
            checked[archive] = len(base_images.verify_installed_layer(files, directory / archive))
        for source, destination in configs.items():
            item = files.get(destination, {})
            expected_mode = 0o755 if source.endswith(".sh") else 0o644
            check.require(item.get("sha256") == check.sha(ROOT / "dockers/docker-sonic-mgmt-framework" / source)
                          and (item.get("uid"), item.get("gid"), item.get("mode")) == (0, 0, expected_mode),
                          "startup bytes or metadata changed: " + destination)
        debug = name.endswith("-dbg")
        status, metadata = (("debug.status", "debug-packages.json") if debug else ("installed.status", "packages.json"))
        installed_metadata = "usr/share/sonic/bazel/mgmt-framework" + ("-debug" if debug else "") + "-packages.json"
        check.require(files["var/lib/dpkg/status"]["sha256"] == check.sha(directory / status)
                      and files[installed_metadata]["sha256"] == check.sha(directory / metadata),
                      "installed package inventory differs")
        config = layout.config.get("config", {})
        check.require(config.get("Entrypoint") == ["/usr/local/bin/supervisord"], "entrypoint changed")
        check.require("DEBIAN_FRONTEND=noninteractive" in config.get("Env", []), "environment changed")
        expected = json.loads((ROOT / "target/bazel-manifests" / name / "manifest.json").read_text())
        check.require(json.loads(config["Labels"]["com.azure.sonic.manifest"]) == expected, "Make labels differ")
        archive = image_validation.docker_archive(directory / (name + ".gz"), name + ":latest",
                                                  layout.manifest["config"]["digest"], layout.manifest["layers"])
        # Retain only public verification values, never a serialized environment.
        reports[name] = {"manifest_digest": layout.descriptor["digest"],
                         "config_digest": layout.manifest["config"]["digest"],
                         "rootfs": layout.config["rootfs"], "archive": archive,
                         "owner_payloads": checked, "parent_payload_preserved": True}
        inventories[name] = files
    runtime, debug = (inventories[name] for name in IMAGES)
    check.require(all(debug.get(path) == item for path, item in runtime.items() if "elf_machine" in item),
                  "debug variant replaces a runtime ELF")
    framework = check.debug_archives(directory / "framework-runtime.tar", directory / "framework-debug.tar",
                                     expected_machine=62)
    check.require(len(framework["debug_pairs"]) == 8, "Framework debug coverage differs")
    native = base_images.verify_source_files(debug, [json.loads((directory / name).read_text()) for name in
        ("base-packages.json", "config-engine-packages.json", "debug-packages.json")], 62)
    for path, item in check.payload(directory / "framework-runtime.tar").items():
        if "elf_machine" in item:
            check.require(path not in native, "duplicate native owner: " + path)
            native[path] = {**item, "source_package": "sonic-mgmt-framework"}
    pairs = base_images.verify_symbols(native, [directory / name for name in
        ("base-source.tar", "config-engine-source.tar", "openssh-source.tar", "framework-runtime.tar")],
        directory / "all-debug.tar", directory)
    for path, item in check.payload(directory / "all-debug.tar").items():
        if item["kind"] != "directory":
            check.require(all(debug.get(path, {}).get(key) == value for key, value in item.items())
                          and path not in runtime, "debug layer differs: " + path)
    check.require(debug["usr/bin/ssh-agent"]["gid"] == 104 and debug["usr/bin/ssh-agent"]["mode"] == 0o2755,
                  "source OpenSSH permissions differ")
    for name in IMAGES:
        reports[name]["native_paths"] = sorted(set(native).intersection(inventories[name]))
    report = {"status": "passed", "revision": receipt["revision"], "architecture": "amd64",
              "images": reports, "source_debug_pairs": pairs, "native": native, "artifacts": receipt["artifacts"]}
    (directory / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "passed", "source_elf_debug_pairs": len(pairs)}))


def incremental(args, directory):
    receipt = json.loads((directory / "build-receipt.json").read_text())
    check.require(receipt["status"] == "built", "missing accepted build")
    labels = [OWNER + name + suffix for name in IMAGES for suffix in ("", ".gz")]
    labels += [PARENT, TARGETS["source-runtime.tar"], TARGETS["all-debug.tar"]]
    command = [args.bazel, "build", *OPTIONS, *labels]
    paths = {}
    for label in labels:
        paths[label] = ROOT / subprocess.check_output(
            [args.bazel, "cquery", *OPTIONS, label, "--output=files"], cwd=ROOT, text=True).strip()

    def snapshot(stage):
        started = time.monotonic()
        with (directory / ("incremental-" + stage + ".log")).open("w") as log:
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        return {"seconds": time.monotonic() - started, "sha256": {
            label: (oci_layout.validate_layout(path, "linux/amd64").descriptor["digest"] if path.is_dir()
                    else check.sha(path)) for label, path in paths.items()}}

    results = {"baseline": snapshot("baseline"), "noop": snapshot("noop")}
    original = results["baseline"]["sha256"]
    check.require(results["noop"]["sha256"] == original, "no-op changed image outputs")
    for name in IMAGES:
        check.require(original[OWNER + name + ".gz"] == receipt["artifacts"][name + ".gz"]["sha256"],
                      "incremental baseline differs from accepted archives")
    source = ROOT / "dockers/docker-sonic-mgmt-framework/mgmt_vars.j2"
    content = source.read_bytes()
    try:
        source.write_bytes(content + b"\n{# Framework image incremental probe #}\n")
        results["edit"] = snapshot("edit")
        changed = {label for label, digest in results["edit"]["sha256"].items() if original[label] != digest}
        check.require(changed == {OWNER + name + suffix for name in IMAGES for suffix in ("", ".gz")},
                      "template edit changed unrelated source outputs: " + repr(changed))
    finally:
        source.write_bytes(content)
    results["restore"] = snapshot("restore")
    check.require(results["restore"]["sha256"] == original, "restored inputs changed image/archive digests")
    (directory / "incremental.json").write_text(json.dumps(results, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "validate", "incremental"))
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    directory = args.artifacts.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.phase == "validate":
        validate_images(directory)
    elif args.phase == "incremental":
        incremental(args, directory)
    else:
        build_images(args, directory)


if __name__ == "__main__":
    main()
