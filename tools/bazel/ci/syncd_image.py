#!/usr/bin/env python3
"""Build Syncd archives and validate their original package and image contract."""

import argparse
from functools import partial
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci import bazel_commands, build, command_log, resolution
from tools.bazel.oci import image_validation, native_packages, oci_layout, source_base_validation
from tools.bazel.tests.integration.syncd_p4_images_test import validate as validate_syncd

OWNER = "//platform/vs/docker-syncd-vs:"
SHARED = "//platform/vs/docker-syncd-vs:"
BASE = "//dockers/docker-config-engine-trixie:docker-config-engine-trixie"
IMAGES = ("docker-syncd-vs", "docker-syncd-vs-dbg")
OPTIONS = ["--jobs=2", "--local_resources=cpu=2", "--local_resources=memory=8000", "--config=syncd-dash"]
TARGETS = {
    **{name + ".gz": OWNER + name + ".gz" for name in IMAGES},
    "source-runtime.tar": OWNER + "source_runtime_payloads",
    "source-debug.tar": SHARED + "source_debug_payloads",
    "source-contract.json": OWNER + "source_contract",
    "source-receipt.json": OWNER + "source_package_receipt",
    "policy.json": OWNER + "package_policy",
    "config-files.tar": "//platform/vs/docker-syncd-vs/config:files",
    "runtime-apt.tar": OWNER + "apt_deps",
    "runtime-apt.json": OWNER + "runtime_apt_selection",
    "debug-tools.tar": OWNER + "debug_tools",
    "debug-apt.json": OWNER + "debug_apt_selection",
    "installation-links.tar": SHARED + "installation_links",
    "runtime-packages.json": OWNER + "runtime_package_metadata",
    "runtime.status": OWNER + "installed_packages",
    "debug-packages.json": OWNER + "debug_package_metadata",
    "debug.status": OWNER + "debug_installed_packages",
    "base-debug.tar": BASE + ".debug_symbols",
    "base-packages.json": "//dockers/docker-base-trixie:runtime_package_metadata",
    "config-engine-packages.json": "//dockers/docker-config-engine-trixie:runtime_package_metadata",
    "config-engine-source.tar": "//dockers/docker-config-engine-trixie:source_runtime",
    "test-redis.tar": "//platform/vs/docker-gbsyncd-vs:test_redis",
}


def clean_source(directory):
    check.require(not ROOT.is_relative_to(directory), "artifact directory contains checkout")
    if directory.is_relative_to(ROOT):
        relative = directory.relative_to(ROOT)
        check.require(len(relative.parts) > 1 and relative.parts[0] == "artifacts",
                      "in-checkout evidence must use artifacts/<name>")
    subprocess.run(["git", "diff", "--exit-code", "--ignore-submodules=none", "HEAD"], cwd=ROOT, check=True)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    unexpected = [name for name in untracked if name and not (ROOT / name).is_relative_to(directory) and
                  not (len(Path(name).parts) > 1 and Path(name).parts[0] == "artifacts")]
    check.require(not unexpected, "commit all source inputs before acceptance: " + repr(unexpected))


def identity():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    headers = subprocess.check_output(["git", "cat-file", "-p", "HEAD"], cwd=ROOT, text=True).split("\n\n", 1)[0]
    parents = [line.split()[1] for line in headers.splitlines() if line.startswith("parent ")]
    if os.environ.get("CI_HEAD_SHA"):
        check.require(os.environ["CI_HEAD_SHA"] in [revision, *parents], "CI source differs from requested PR head")
    if os.environ.get("CI_BASE_SHA"):
        check.require(os.environ["CI_BASE_SHA"] in parents, "CI merge differs from requested PR base")
    return {"revision": revision, "tested_revision_parents": parents}


def verify_inherited_accounts(inherited, installed, inherited_groups, groups, *, add_ssh):
    for rows in (inherited_groups, groups):
        names = [row.split(":", 1)[0] for row in rows]
        check.require(len(names) == len(set(names)), "duplicate group name")
    expected = inherited_groups + (["_ssh:x:104:"] if add_ssh and "_ssh:x:104:" not in inherited_groups else [])
    check.require(groups == expected, "account merge changed inherited group rows")
    check.require("etc/group" in inherited and "etc/group" in installed,
                  "inherited group file is missing")
    check.require(all(installed["etc/group"][key] == inherited["etc/group"][key]
                      for key in ("kind", "uid", "gid", "mode")), "group file metadata changed")
    for path in ("etc/passwd", "etc/gshadow"):
        check.require(installed.get(path) == inherited.get(path), "changed inherited account state: " + path)


def validate_groups(base_layout, runtime_layout, debug_layout):
    rows, inventories = [], []
    for layout in (base_layout, runtime_layout, debug_layout):
        files, origins = image_validation.image_inventory(layout)
        with tempfile.TemporaryDirectory(prefix="syncd-accounts-") as temporary:
            root = Path(temporary)
            source_base_validation.extract_selected(layout, files, origins, {"etc/group"}, root)
            rows.append((root / "etc/group").read_text().splitlines())
        inventories.append(files)
    verify_inherited_accounts(inventories[0], inventories[1], rows[0], rows[1], add_ssh=True)
    verify_inherited_accounts(inventories[1], inventories[2], rows[1], rows[2], add_ssh=False)
    return {"base_groups": len(rows[0]), "runtime_groups": len(rows[1]), "debug_groups": len(rows[2]),
            "declared_addition": "_ssh:x:104:", "ordered_rows_and_metadata_preserved": True}


def verify_parent_files(parent, runtime, debug, configuration, alternatives):
    """Preserve every parent file except separately verified image-owned changes."""
    runtime_changes = set(configuration) | {"etc/group", "var/lib/dpkg/status"}
    debug_changes = {path for item in alternatives for path in item["paths"]} | {"var/lib/dpkg/status"}
    for path, expected in parent.items():
        if path not in runtime_changes:
            check.require(runtime.get(path) == expected, "runtime changed inherited parent file: " + path)
    for path, expected in runtime.items():
        if path not in debug_changes:
            check.require(debug.get(path) == expected, "debug changed inherited runtime file: " + path)
    return {"parent_paths": len(parent), "runtime_paths": len(runtime),
            "runtime_exceptions": sorted(runtime_changes), "debug_exceptions": sorted(debug_changes)}


def manifest_command():
    return ["make", "-s", "-f", "tools/bazel/prepare_manifests.mk",
            "MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk platform/vs/docker-syncd-vs.mk",
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "BLDENV=trixie", "CONFIGURED_PLATFORM=vs", "CONFIGURED_ARCH=amd64",
            "INCLUDE_FIPS=y", "INCLUDE_VS_DASH_SAI=y", "ENABLE_ASAN=n", "ENABLE_SYNCD_RPC=n", "DBG_IMAGE_MARK=dbg", "DOCKERS_PATH=dockers",
            "PLATFORM_PATH=platform/vs", "manifests"]


def verify_environment(parent, installed):
    def entries(values):
        pairs = [value.split("=", 1) for value in values]
        check.require(all(len(pair) == 2 for pair in pairs), "invalid OCI environment entry")
        result = dict(pairs)
        check.require(len(result) == len(pairs), "duplicate OCI environment key")
        return result
    expected = {**entries(parent), "DEBIAN_FRONTEND": "noninteractive"}
    check.require(entries(installed) == expected, "Syncd changed inherited OCI environment")


def build_images(args, directory):
    check.require(platform.machine() == "x86_64" and platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie",
                  "Syncd acceptance requires native AMD64/Trixie")
    receipt = {"status": "building", "architecture": "amd64", "commands": [],
               "review_registry_revision": os.environ.get("SONIC_BAZEL_REGISTRY_REVISION")}
    execute = partial(command_log.execute, cwd=ROOT)
    try:
        clean_source(directory)
        receipt.update(identity())
        execute(manifest_command(), directory, receipt, "manifests")
        layouts = [BASE, *[OWNER + name for name in IMAGES]]
        receipt["action_audit"] = bazel_commands.audit_targets(args.bazel, OPTIONS, [*TARGETS.values(), *layouts],
            workspace=ROOT, output=directory / "action-audit.json")
        # Retain completed independent source actions even if another target fails.
        build_options = [*OPTIONS, "--keep_going"]
        build.collect_archives(ROOT, directory, receipt, TARGETS, bazel=args.bazel, options=build_options)
        execute([args.bazel, "build", *build_options, *layouts], directory, receipt, "layouts")
        receipt["layouts"] = {name: execute([args.bazel, "cquery", *OPTIONS, "--output=files", OWNER + name],
            directory, receipt, name + ".layout").strip() for name in IMAGES}
        receipt["base_layout"] = execute([args.bazel, "cquery", *OPTIONS, "--output=files", BASE], directory, receipt, "base.layout").strip()
        receipt["resolution"] = resolution.collect(ROOT, directory, bazel=[args.bazel])
        clean_source(directory)
        check.require(identity()["revision"] == receipt["revision"], "source changed during build")
        receipt["status"] = "built"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        (directory / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def validate_images(directory):
    clean_source(directory)
    receipt = json.loads((directory / "build-receipt.json").read_text())
    check.require(receipt["status"] == "built" and identity()["revision"] == receipt["revision"], "build/source identity differs")
    for name, artifact in receipt["artifacts"].items():
        check.require(check.sha(directory / name) == artifact["sha256"], "artifact changed: " + name)
    runtime, debug = [ROOT / receipt["layouts"][name] for name in IMAGES]
    accounts = validate_groups(*[oci_layout.validate_layout(path, "linux/amd64") for path in
        (ROOT / receipt["base_layout"], runtime, debug)])
    policy = json.loads((directory / "policy.json").read_text())
    source = {"source_contract_path": directory / "source-contract.json", "source_runtime_tar": directory / "source-runtime.tar",
              "source_debug_tar": directory / "source-debug.tar", "source_receipt": directory / "source-receipt.json",
              "base_path": ROOT / receipt["base_layout"]}
    inputs = {**source, "runtime_path": runtime, "debug_path": debug,
              "runtime_manifest_path": ROOT / "target/bazel-manifests/docker-syncd-vs/manifest.json",
              "debug_manifest_path": ROOT / "target/bazel-manifests/docker-syncd-vs-dbg/manifest.json",
              "apt_lock_path": ROOT / "platform/vs/docker-syncd-vs/bazel/apt.lock.json",
              "dpkg_filter_path": ROOT / "dockers/docker-base-trixie/dpkg_01_drop",
              "apt_layer": directory / "runtime-apt.tar", "debug_tools_layer": directory / "debug-tools.tar",
              "runtime_links_layer": directory / "installation-links.tar",
              "runtime_apt_selection": directory / "runtime-apt.json", "debug_apt_selection": directory / "debug-apt.json",
              "runtime_archive": directory / (IMAGES[0] + ".gz"), "debug_archive": directory / (IMAGES[1] + ".gz"),
              "base_symbols": directory / "base-debug.tar", "base_metadata": directory / "base-packages.json",
              "config_metadata": directory / "config-engine-packages.json", "workspace_module": ROOT / "MODULE.bazel",
              "policy": directory / "policy.json", "readelf": Path(shutil.which("readelf")), "objcopy": Path(shutil.which("objcopy"))}
    # Reuse the existing full Syncd/parent/P4 validator. The standalone producer
    # exports the same Bazel archives directly; Make export parity remains in its original test.
    validated = validate_syncd(inputs, source_root=ROOT, verify_make=False)
    images, own, inherited, p4 = (validated[name] for name in ("images", "native", "source_base", "p4"))
    pairs = {item["path"]: item for item in [*inherited["paired_symbols"], *own["paired_symbols"]]}
    inventories, summaries = {}, {}
    parent_environment = oci_layout.validate_layout(ROOT / receipt["base_layout"], "linux/amd64").config["config"].get("Env", [])
    for name in IMAGES:
        layout = oci_layout.validate_layout(ROOT / receipt["layouts"][name], "linux/amd64")
        files, _ = image_validation.image_inventory(layout)
        variant = "debug" if name.endswith("-dbg") else "runtime"
        for path, artifact in [("var/lib/dpkg/status", variant + ".status"),
                               ("usr/share/sonic/bazel/syncd" + ("-debug" if variant == "debug" else "") + "-packages.json", variant + "-packages.json")]:
            check.require(files[path]["sha256"] == check.sha(directory / artifact), "installed package state differs: " + path)
        check.require(files["usr/bin/syncd"]["mode"] == 0o755, "installed Syncd is not executable")
        verify_environment(parent_environment, layout.config["config"].get("Env", []))
        inventories[name] = files
        summaries[name] = {"config_digest": layout.manifest["config"]["digest"], "rootfs": layout.config["rootfs"],
                           "manifest_digest": layout.descriptor["digest"], "native_paths": sorted(pairs)}
    preserved = verify_parent_files(
        image_validation.image_inventory(oci_layout.validate_layout(ROOT / receipt["base_layout"], "linux/amd64"))[0],
        inventories[IMAGES[0]], inventories[IMAGES[1]],
        check.payload(directory / "config-files.tar", require_root=False), policy["image_validation"]["debug_alternatives"])
    native = {path: inventories[IMAGES[0]][path] for path in pairs}
    result = {"status": "passed", **identity(), "architecture": "amd64",
              "images": summaries, "native": native, "source_debug_pairs": list(pairs.values()),
              "accounts": accounts,
              "preserved_parent": preserved,
              "package_images": images, "source_native": own, "inherited_source": inherited, "p4": p4,
              "artifacts": receipt["artifacts"]}
    (directory / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "passed", "source_elf_debug_pairs": len(pairs)}))


def incremental(args, directory):
    receipt = json.loads((directory / "build-receipt.json").read_text())
    clean_source(directory)
    check.require(identity()["revision"] == receipt["revision"] and receipt["status"] == "built", "source/build identity differs")
    labels = [OWNER + name + suffix for name in IMAGES for suffix in ("", ".gz")]
    labels += [BASE, OWNER + "source_runtime_payloads", OWNER + "debug_packages"]
    paths = {label: ROOT / subprocess.check_output([args.bazel, "cquery", *OPTIONS, "--output=files", label], cwd=ROOT, text=True).strip() for label in labels}
    def snapshot(stage):
        start = time.monotonic()
        with (directory / ("incremental-" + stage + ".log")).open("w") as log:
            subprocess.run([args.bazel, "build", *OPTIONS, *labels], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        return {"seconds": time.monotonic() - start, "sha256": {label: (
            oci_layout.validate_layout(path, "linux/amd64").descriptor["digest"] if path.is_dir() else check.sha(path)) for label, path in paths.items()}}
    result = {"baseline": snapshot("baseline"), "noop": snapshot("noop")}
    check.require(result["baseline"]["sha256"] == result["noop"]["sha256"], "no-op changed outputs")
    for name in IMAGES:
        check.require(result["baseline"]["sha256"][OWNER + name + ".gz"] == receipt["artifacts"][name + ".gz"]["sha256"], "baseline archive differs")
    path = ROOT / "platform/vs/docker-syncd-vs/supervisord.conf"
    original = path.read_bytes(); changed = b"# Syncd incremental probe\n" + original
    try:
        path.write_bytes(changed)
        result["edit"] = snapshot("edit")
        changed_labels = {label for label, digest in result["edit"]["sha256"].items() if digest != result["baseline"]["sha256"][label]}
        check.require(changed_labels == {OWNER + name + suffix for name in IMAGES for suffix in ("", ".gz")}, "config edit changed unrelated source outputs")
    finally:
        check.require(path.read_bytes() in (original, changed), "concurrent config edit; refusing restoration")
        path.write_bytes(original)
    result["restore"] = snapshot("restore")
    check.require(result["restore"]["sha256"] == result["baseline"]["sha256"], "restoration changed output bytes")
    clean_source(directory)
    (directory / "incremental.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "validate", "incremental"))
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--bazel", default="bazel")
    args = parser.parse_args(); directory = args.artifacts.resolve(); directory.mkdir(parents=True, exist_ok=True)
    if args.phase == "build": build_images(args, directory)
    elif args.phase == "validate": validate_images(directory)
    else: incremental(args, directory)

if __name__ == "__main__":
    main()
