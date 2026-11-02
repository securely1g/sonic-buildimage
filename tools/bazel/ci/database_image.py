#!/usr/bin/env python3
"""Build Database archives, then check completed contents and incremental reuse."""

import argparse
from functools import partial
import json
import hashlib
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci import base_images, bazel_commands, build, command_log, resolution
from tools.bazel.oci import image_validation, oci_layout

OWNER = "//dockers/docker-database:"
IMAGES = ("docker-database", "docker-database-dbg")
PARENT = "//dockers/docker-config-engine-trixie:docker-config-engine-trixie"
TARGETS = {
    **{name + ".gz": OWNER + name + ".gz" for name in IMAGES},
    "source-runtime.tar": OWNER + "source_runtime",
    "source-debug.tar": OWNER + "source_debug_symbols",
    "config-files.tar": "//dockers/docker-database/config:files",
    "redis.original.conf": "//dockers/docker-database/config:redis_original",
    "runtime-packages.json": OWNER + "runtime_package_metadata",
    "runtime.status": OWNER + "installed_packages",
    "all-debug.tar": OWNER + "all_debug_symbols",
    "debug-tools.tar": OWNER + "debug_tools",
    "base-source.tar": "//dockers/docker-base-trixie:source_runtime",
    "base-packages.json": "//dockers/docker-base-trixie:runtime_package_metadata",
    "config-engine-source.tar": "//dockers/docker-config-engine-trixie:source_runtime",
    "config-engine-packages.json": "//dockers/docker-config-engine-trixie:runtime_package_metadata",
    "openssh-source.tar": OWNER + "debug_client",
    "debug-packages.json": OWNER + "debug_package_metadata",
    "debug.status": OWNER + "debug_installed_packages",
}
OPTIONS = ["--jobs=2", "--local_resources=cpu=2", "--local_resources=memory=10000"]
CONFIGS = {
    "dockers/docker-database/docker-database-init.sh": ("usr/local/bin/docker-database-init.sh", 0o755),
    "dockers/docker-database/flush_unused_database": ("usr/local/bin/flush_unused_database", 0o755),
    "files/scripts/update_chassisdb_config": ("usr/local/bin/update_chassisdb_config", 0o755),
    "files/image_config/sysctl/90-sonic.conf": ("usr/lib/sysctl.d/90-sonic.conf", 0o644),
    **{"dockers/docker-database/" + name: ("usr/share/sonic/templates/" + name, 0o644)
       for name in ("database_config.json.j2", "database_global.json.j2", "multi_database_config.json.j2", "supervisord.conf.j2", "critical_processes.j2")},
}


def build_images(args, directory):
    check.require(platform.machine() == "x86_64" and
                  platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie",
                  "Database image build requires native AMD64 Debian Trixie")
    receipt = {"status": "building", "architecture": "amd64", "commands": [], "include_system_eventd": args.include_system_eventd}
    execute = partial(command_log.execute, cwd=ROOT)
    try:
        receipt["revision"] = execute(["git", "rev-parse", "HEAD"], directory, receipt, "revision").strip()
        receipt["commit_headers"] = subprocess.check_output(
            ["git", "cat-file", "-p", "HEAD"], cwd=ROOT, text=True).split("\n\n", 1)[0]
        execute(["make", "-f", "tools/bazel/prepare_manifests.mk", "manifests",
                 "target/bazel-manifests/docker-database/image.env",
                 "MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-database.mk",
                 "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "BLDENV=trixie", "CONFIGURED_PLATFORM=vs",
                 "CONFIGURED_ARCH=amd64", "INCLUDE_FIPS=y", "ENABLE_ASAN=n", "DBG_IMAGE_MARK=dbg",
                 "DOCKERS_PATH=dockers", "INCLUDE_SYSTEM_EVENTD=" + args.include_system_eventd], directory, receipt, "manifests")
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
    inherited = base_images.installed_inventory(oci_layout.validate_layout(ROOT / receipt["layouts"][PARENT], "linux/amd64"))
    tools = check.payload(directory / "debug-tools.tar", require_root=False)
    configs = check.payload(directory / "config-files.tar", require_root=False)
    source = check.payload(directory / "source-runtime.tar")
    own = check.debug_archives(directory / "source-runtime.tar", directory / "source-debug.tar", expected_machine=62)
    check.require({item["path"] for item in own["debug_pairs"]} == {name for name, item in source.items() if "elf_machine" in item},
                  "DASH API/Protobuf source symbols are incomplete")
    # Execute the original Dockerfile's exact sed expression on the exact
    # locked package file; compare bytes with the upstream expand_template rule.
    recipe = (ROOT / "dockers/docker-database/Dockerfile.common.j2").read_text()
    match = re.search(r"RUN sed -ri '(.*?)' /etc/redis/redis.conf", recipe, re.S)
    check.require(match is not None, "original Redis configuration recipe changed")
    expression = match.group(1).replace("\\\n", "")
    configured = subprocess.run(["sed", "-r", expression], input=(directory / "redis.original.conf").read_bytes(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    check.require(configs["etc/redis/redis.conf"]["sha256"] == hashlib.sha256(configured).hexdigest(),
                  "Bazel Redis configuration differs from the native Dockerfile recipe")
    reports, inventories = {}, {}
    for name in IMAGES:
        debug = name.endswith("-dbg")
        layout = oci_layout.validate_layout(ROOT / receipt["layouts"][OWNER + name], "linux/amd64")
        base_images.verify_unique_layer_paths(layout)
        files = base_images.installed_inventory(layout)
        base_images.verify_directory_aliases(files)
        check.require(all(item.get("elf_machine", 62) == 62 for item in files.values()), "non-AMD64 ELF")
        replaced = {"var/lib/dpkg/status"} | ({"etc/group", "etc/gshadow"} if debug else set())
        # No existing dpkg override may be discarded by the declared Redis
        # postinst record. The selected source base has no such override file.
        check.require("var/lib/dpkg/statoverride" not in inherited, "base has existing statoverride records to preserve")
        for path, item in inherited.items():
            if item["kind"] != "directory" and path not in replaced:
                expected = tools.get(path, item) if debug else item
                check.require(all(files.get(path, {}).get(key) == value for key, value in expected.items()),
                              "image replaced an inherited file: " + path)
        base_images.verify_installed_layer(files, directory / "source-runtime.tar")
        for path, item in configs.items():
            check.require(all(files.get(path, {}).get(key) == value for key, value in item.items()),
                          "database configuration payload differs: " + path)
        if debug:
            base_images.verify_installed_layer(files, directory / "debug-tools.tar")
        for source_file, (destination, mode) in CONFIGS.items():
            item = files.get(destination, {})
            check.require(item.get("sha256") == check.sha(ROOT / source_file) and
                          (item.get("uid"), item.get("gid"), item.get("mode")) == (0, 0, mode),
                          "original database file differs: " + destination)
        for path, uid, gid, mode in (("etc/redis", 100, 101, 0o2770), ("etc/redis/redis.conf", 100, 101, 0o640),
                                    ("var/lib/redis", 100, 101, 0o750), ("var/log/redis", 100, 4, 0o2750)):
            check.require(tuple(files.get(path, {}).get(k) for k in ("uid", "gid", "mode")) == (uid, gid, mode),
                          "Redis installation permissions differ: " + path)
        check.require(files["usr/share/sonic/bazel/database-packages.json"]["sha256"] == check.sha(directory / "runtime-packages.json"),
                      "runtime imported package records differ")
        status_name = "debug.status" if debug else "runtime.status"
        check.require(files["var/lib/dpkg/status"]["sha256"] == check.sha(directory / status_name), "installed package status differs")
        config = layout.config.get("config", {})
        check.require(config.get("Entrypoint") == ["/usr/local/bin/docker-database-init.sh"], "entrypoint changed")
        check.require("DEBIAN_FRONTEND=noninteractive" in config.get("Env", []) and
                      "INCLUDE_SYSTEM_EVENTD=" + receipt["include_system_eventd"] in config.get("Env", []), "image environment changed")
        expected = json.loads((ROOT / "target/bazel-manifests" / name / "manifest.json").read_text())
        check.require(json.loads(config["Labels"]["com.azure.sonic.manifest"]) == expected, "Make labels differ")
        archive = image_validation.docker_archive(directory / (name + ".gz"), name + ":latest",
                                                  layout.manifest["config"]["digest"], layout.manifest["layers"])
        reports[name] = {"manifest_digest": layout.descriptor["digest"], "config_digest": layout.manifest["config"]["digest"],
                         "rootfs": layout.config["rootfs"], "archive": archive, "parent_payload_preserved": True}
        inventories[name] = files
    runtime, debug = (inventories[name] for name in IMAGES)
    check.require(debug["usr/share/sonic/bazel/database-debug-packages.json"]["sha256"] == check.sha(directory / "debug-packages.json"),
                  "debug imported/source package metadata differs")
    for path, item in runtime.items():
        if "elf_machine" in item:
            expected = tools.get(path, item)
            check.require(all(debug.get(path, {}).get(key) == value for key, value in expected.items()),
                          "debug variant replaced an undeclared runtime ELF: " + path)
    native = base_images.verify_source_files(debug, [json.loads((directory / name).read_text()) for name in
        ("base-packages.json", "config-engine-packages.json", "debug-packages.json")], 62)
    for name, item in source.items():
        if "elf_machine" in item:
            check.require(name not in native, "DASH owner replaces another source owner's ELF: " + name)
            # This owner exposes TARs without Debian package identities. Keep
            # that distinction in the symbol verifier's source provenance.
            native[name] = {**item, "source_package": None, "source_target": TARGETS["source-runtime.tar"]}
    pairs = base_images.verify_symbols(native, [directory / name for name in
        ("base-source.tar", "config-engine-source.tar", "source-runtime.tar", "openssh-source.tar")], directory / "all-debug.tar", directory)
    for path, item in check.payload(directory / "all-debug.tar").items():
        if item["kind"] != "directory":
            check.require(all(debug.get(path, {}).get(key) == value for key, value in item.items()) and path not in runtime,
                          "debug symbols differ: " + path)
    check.require(debug["usr/bin/ssh-agent"]["gid"] == 104 and debug["usr/bin/ssh-agent"]["mode"] == 0o2755,
                  "source OpenSSH permissions differ")
    for name in IMAGES:
        reports[name]["native_paths"] = sorted(set(native).intersection(inventories[name]))
    report = {"status": "passed", "revision": receipt["revision"], "architecture": "amd64",
              "include_system_eventd": receipt["include_system_eventd"], "images": reports,
              "source_debug_pairs": pairs, "own_debug_pairs": own["debug_pairs"], "native": native,
              "redis_config_matches_legacy": True, "artifacts": receipt["artifacts"]}
    (directory / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "passed", "source_elf_debug_pairs": len(pairs), "legacy_redis_config_matches": True}))


def incremental(args, directory):
    receipt = json.loads((directory / "build-receipt.json").read_text())
    check.require(receipt["status"] == "built", "missing accepted build")
    labels = [OWNER + name + suffix for name in IMAGES for suffix in ("", ".gz")]
    labels += [PARENT, TARGETS["source-runtime.tar"], TARGETS["all-debug.tar"]]
    paths = {label: ROOT / subprocess.check_output(
        [args.bazel, "cquery", *OPTIONS, label, "--output=files"], cwd=ROOT, text=True).strip() for label in labels}

    def snapshot(stage):
        started = time.monotonic()
        with (directory / ("incremental-" + stage + ".log")).open("w") as log:
            subprocess.run([args.bazel, "build", *OPTIONS, *labels], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        return {"seconds": time.monotonic() - started, "sha256": {
            label: (oci_layout.validate_layout(path, "linux/amd64").descriptor["digest"] if path.is_dir()
                    else check.sha(path)) for label, path in paths.items()}}

    results = {"baseline": snapshot("baseline"), "noop": snapshot("noop")}
    original = results["baseline"]["sha256"]
    check.require(results["noop"]["sha256"] == original, "no-op changed outputs")
    for name in IMAGES:
        check.require(original[OWNER + name + ".gz"] == receipt["artifacts"][name + ".gz"]["sha256"], "incremental baseline changed")
    source = ROOT / "dockers/docker-database/supervisord.conf.j2"
    content = source.read_bytes()
    try:
        source.write_bytes(b"; database image incremental probe\n" + content)
        results["edit"] = snapshot("edit")
        changed = {label for label, digest in results["edit"]["sha256"].items() if original[label] != digest}
        check.require(changed == {OWNER + name + suffix for name in IMAGES for suffix in ("", ".gz")},
                      "supervisor template edit changed unrelated source outputs: " + repr(changed))
    finally:
        source.write_bytes(content)
    results["restore"] = snapshot("restore")
    check.require(results["restore"]["sha256"] == original, "restored inputs changed output digests")
    (directory / "incremental.json").write_text(json.dumps(results, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "validate", "incremental"))
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--include-system-eventd", choices=("y", "n"), default="y")
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
