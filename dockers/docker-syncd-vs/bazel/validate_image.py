#!/usr/bin/env python3
"""Validate syncd-vs OCI configuration, installed payloads, and archive handoff."""

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile

ROOT = Path(__file__).absolute().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).absolute().parent))
from tools.bazel.ci.artifact_validation import path_name as member_name, require, sha
from tools.bazel.oci import oci_inventory
from tools.bazel.oci.oci_inventory import assert_overlay_paths
from tools.bazel.oci.oci_layout import validate_layout
import validate_payloads


def image(directory):
    return validate_layout(directory, "linux/amd64")


def apply_layer(path, files, *, merged_usr=False, checked_overlay=False):
    return oci_inventory.apply_layer(
        path, files, checked_overlay=checked_overlay,
        normalize_member=validate_payloads.normalized_member if merged_usr else None)


VIM_ALTERNATIVES = tuple("etc/alternatives/" + name for name in ("editor", "ex", "rview", "vi", "view"))


def apply_debug_tools_layer(path, files):
    """Allow the explicit Vim alternative update without changing inherited ELFs."""
    entries = {}
    apply_layer(path, entries)
    old_link = {"kind": "symlink", "mode": 0o777, "uid": 0, "gid": 0, "linkname": "/usr/bin/vim.tiny"}
    new_link = dict(old_link, linkname="/usr/bin/vim.basic")
    for name in VIM_ALTERNATIVES:
        require(files.get(name) == old_link and entries.get(name) == new_link,
                "unexpected debug Vim alternative: " + name)
    inherited_vim = oci_inventory.resolve_path("usr/bin/vim.tiny", files)
    require(files.get(inherited_vim, {}).get("elf_machine") == 62 and
            entries.get("usr/bin/vim.basic", {}).get("elf_machine") == 62,
            "debug Vim alternatives require the expected AMD64 binaries")
    # Approve only those exact links, then check the entire original tar. The
    # shared path/whiteout checks and APT selection policy remain unchanged.
    checked = dict(files)
    checked.update({name: entries[name] for name in VIM_ALTERNATIVES})
    apply_layer(path, checked, checked_overlay=True)
    assert_payload({name: item for name, item in files.items() if "elf_machine" in item},
                   checked, "debug tools change an inherited ELF")
    files.clear()
    files.update(checked)


def payloads(manifest_path, variant, runtime_manifest=None):
    payload = manifest_path.parent / "payload.tar"
    receipt = validate_payloads.validate(manifest_path, payload, variant=variant, runtime_manifest=runtime_manifest)
    manifest = json.loads(manifest_path.read_bytes())
    files = {}
    apply_layer(payload, files, merged_usr=True)
    owners = {}
    with tarfile.open(payload, "r:") as archive:
        members = iter(archive)
        for record in manifest["packages"]:
            for _ in range(record["payload_members"]):
                member = next(members, None)
                require(member is not None, "aggregate package ownership segments are incomplete")
                normalized = validate_payloads.normalized_member(member)
                if normalized is not None:
                    owners[member_name(normalized.name)] = record["package"]
        require(next(members, None) is None, "aggregate package ownership has extra members")
    return receipt, files, owners, manifest


def dpkg_filtered(files, source_root):
    rules = []
    for line in (source_root / "dockers/docker-base-trixie/dpkg_01_drop").read_text().splitlines():
        match = re.fullmatch(r"(path-exclude|path-include)(?:=|\s+)(\S+)", line.strip())
        if match:
            rules.append((match[1] == "path-include", match[2]))
    require(rules, "dpkg path filtering configuration has no rules")
    result = {}
    for name, item in files.items():
        keep = True
        for include, pattern in rules:
            if fnmatch.fnmatchcase("/" + name, pattern):
                keep = include
        if keep:
            result[name] = item
    return result


def assert_payload(expected, actual, description):
    differences = [name for name, item in expected.items() if actual.get(name) != item]
    require(not differences, description + " differs at: " + ", ".join(differences[:20]))


def expected_state(path):
    contract = json.loads(path.read_bytes())
    require(contract.get("schema") == 1, "invalid runtime package state contract")
    result = {}
    for entry in contract["entries"]:
        name = member_name(entry["path"])
        if entry["kind"] == "symlink":
            result[name] = {key: entry[key] for key in ("kind", "mode", "uid", "gid", "linkname")}
        else:
            result[name] = {key: entry[key] for key in ("kind", "mode", "uid", "gid", "sha256", "size")}
    for entry in contract["aliases"]:
        result[member_name(entry["path"])] = {"kind": "file", **{key: entry[key] for key in ("mode", "uid", "gid", "sha256", "size")}}
    require(len(result) == len(contract["entries"]) + len(contract["aliases"]), "duplicate runtime package state path")
    return result


def docker_archive(path, expected_tag, config_digest, layer_descriptors):
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest) is not None, "invalid expected OCI config digest")
    with path.open("rb") as stream:
        header = stream.read(10)
    require(len(header) == 10 and header[:3] == b"\x1f\x8b\x08" and header[4:8] == b"\0\0\0\0" and not header[3] & 0x08,
            "Docker archive gzip header is not reproducible")
    files = {}
    config_paths = set()
    manifest = None
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            name = member_name(member.name)
            if not member.isfile():
                continue
            require(name not in files, "duplicate Docker archive file: " + name)
            stream = archive.extractfile(member)
            hasher = hashlib.sha256()
            data = bytearray() if name == "manifest.json" else None
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
                if data is not None:
                    require(len(data) + len(chunk) <= 16 * 1024 * 1024, "Docker archive manifest is too large")
                    data.extend(chunk)
            digest = hasher.hexdigest()
            files[name] = {"size": member.size, "sha256": digest}
            if data is not None:
                manifest = json.loads(data)
            if digest == config_digest[7:]:
                config_paths.add(name)
    require(isinstance(manifest, list) and len(manifest) == 1, "Docker archive does not contain one image")
    entry = manifest[0]
    require(entry.get("RepoTags") == [expected_tag], "Docker archive tag differs from Make")
    require(member_name(entry.get("Config", "")) in config_paths, "Docker archive config differs from its OCI image")
    layers = entry.get("Layers")
    require(isinstance(layers, list) and len(layers) == len(layer_descriptors), "Docker archive layer set is incomplete")
    for name, descriptor in zip(layers, layer_descriptors):
        require(files.get(member_name(name)) == {"size": descriptor["size"], "sha256": descriptor["digest"][7:]},
                "Docker archive layer differs from its OCI image: " + name)
    return {"sha256": sha(path), "bytes": path.stat().st_size, "tag": expected_tag,
            "config_digest": config_digest, "layers": len(layer_descriptors)}


def validate_images(runtime_path, debug_path, runtime_handoff, debug_handoff,
                    runtime_manifest_path, debug_manifest_path, source_root, *, fixture=False,
                    base_path=None, apt_layer=None, debug_tools_layer=None,
                    runtime_apt_selection=None, debug_apt_selection=None, package_state_contract=None,
                    runtime_archive=None, debug_archive=None):
    complete_inputs = (base_path, apt_layer, debug_tools_layer, runtime_apt_selection, debug_apt_selection,
                       package_state_contract, runtime_archive, debug_archive)
    complete = all(value is not None for value in complete_inputs)
    require(complete or not any(value is not None for value in complete_inputs), "complete overlay validation inputs must be supplied together")
    require(fixture or complete, "complete validation requires the base, APT layers/selections, package state contract, and archives")
    runtime_descriptor, runtime_manifest, runtime_config, runtime_layers = image(runtime_path)
    debug_descriptor, debug_manifest, debug_config, debug_layers = image(debug_path)
    runtime_settings = runtime_config.get("config", {})
    debug_settings = debug_config.get("config", {})
    require(runtime_settings.get("Entrypoint") == ["/usr/local/bin/supervisord"],
            "syncd-vs runtime entrypoint differs from Dockerfile.j2")
    require(runtime_settings.get("Cmd") is None, "syncd-vs runtime command differs from Dockerfile.j2")
    environment = dict(value.split("=", 1) for value in runtime_settings.get("Env", []))
    require(environment.get("DEBIAN_FRONTEND") == "noninteractive", "syncd-vs runtime environment differs")
    for key in ("Entrypoint", "Cmd", "Env", "User", "WorkingDir"):
        require(debug_settings.get(key) == runtime_settings.get(key), "debug image changes runtime " + key)
    for settings, manifest_path, variant in ((runtime_settings, runtime_manifest_path, "runtime"),
                                              (debug_settings, debug_manifest_path, "debug")):
        label = settings.get("Labels", {}).get("com.azure.sonic.manifest")
        require(isinstance(label, str) and json.loads(label) == json.loads(manifest_path.read_bytes()),
                variant + " OCI manifest label differs from Make")
    runtime_layer_descriptors = runtime_manifest["layers"]
    require(debug_manifest["layers"][:len(runtime_layer_descriptors)] == runtime_layer_descriptors,
            "debug image does not extend the exact runtime OCI layers")
    runtime_diff_ids = runtime_config["rootfs"]["diff_ids"]
    require(debug_config["rootfs"]["diff_ids"][:len(runtime_diff_ids)] == runtime_diff_ids,
            "debug image does not extend the exact runtime rootfs")
    runtime_files = {}
    for layer in runtime_layers:
        apply_layer(layer, runtime_files)
    debug_files = dict(runtime_files)
    for layer in debug_layers[len(runtime_layers):]:
        apply_layer(layer, debug_files)
    runtime_receipt, expected_runtime, runtime_owners, _ = payloads(runtime_handoff, "runtime")
    debug_receipt, expected_debug, _, _ = payloads(debug_handoff, "debug", runtime_handoff)
    expected_runtime = dpkg_filtered(expected_runtime, source_root)
    expected_debug = dpkg_filtered(expected_debug, source_root)
    assert_payload(expected_runtime, runtime_files, "runtime package payload")
    assert_payload(expected_debug, debug_files, "debug package payload")
    require(runtime_owners.get("usr/bin/ssh") == "openssh-client" and
            expected_runtime.get("usr/bin/ssh", {}).get("elf_machine") == 62,
            "runtime image lacks the Make FIPS OpenSSH ELF")
    assert_payload({name: item for name, item in expected_runtime.items() if item["kind"] != "directory"},
                   debug_files, "debug image changes the runtime package payload")
    copied_files = {
        "usr/bin/start.sh": ("start.sh", 0o755),
        "etc/supervisor/conf.d/supervisord.conf": ("supervisord.conf", 0o644),
        "etc/supervisor/critical_processes": ("critical_processes", 0o644),
    }
    static_files = {}
    for installed, (source, mode) in copied_files.items():
        path = source_root / "dockers/docker-syncd-vs/legacy" / source
        expected = {"kind": "file", "mode": mode, "uid": 0, "gid": 0,
                    "sha256": sha(path), "size": path.stat().st_size}
        static_files[installed] = expected
        require(runtime_files.get(installed) == expected, "syncd-vs startup file differs: " + installed)
        require(debug_files.get(installed) == expected, "debug image changes startup file: " + installed)
    runtime_elfs = {name: item for name, item in runtime_files.items() if "elf_machine" in item}
    assert_payload(runtime_elfs, debug_files, "debug image changes a deployed ELF")
    if not fixture:
        require(runtime_files.get("usr/bin/syncd", {}).get("elf_machine") == 62,
                "complete syncd-vs image lacks the AMD64 syncd ELF")
    overlay_report = None
    archive_report = None
    if complete:
        base_descriptor, base_manifest, base_config, base_layers = image(base_path)
        require(runtime_manifest["layers"][:len(base_manifest["layers"])] == base_manifest["layers"] and
                runtime_config["rootfs"]["diff_ids"][:len(base_config["rootfs"]["diff_ids"])] == base_config["rootfs"]["diff_ids"],
                "runtime image does not extend the exact managed OCI base")
        base_settings = base_config.get("config", {})
        base_environment = dict(value.split("=", 1) for value in base_settings.get("Env", []))
        base_environment["DEBIAN_FRONTEND"] = "noninteractive"
        require(environment == base_environment and runtime_settings.get("User") == base_settings.get("User") and
                runtime_settings.get("WorkingDir") == base_settings.get("WorkingDir"),
                "runtime image changes inherited base settings")
        for name, value in base_settings.get("Labels", {}).items():
            if name != "com.azure.sonic.manifest":
                require(runtime_settings.get("Labels", {}).get(name) == value, "runtime image drops a base label: " + name)
        inherited_runtime = {key: value for key, value in base_settings.items() if value is not None}
        inherited_runtime.update(Entrypoint=["/usr/local/bin/supervisord"], Env=runtime_settings["Env"],
                                 Labels=runtime_settings["Labels"])
        inherited_runtime.pop("Cmd", None)
        require({key: value for key, value in runtime_settings.items() if value is not None} == inherited_runtime,
                "runtime image changes an inherited base configuration field")
        inherited_debug = dict(runtime_settings)
        inherited_debug["Labels"] = debug_settings["Labels"]
        require({key: value for key, value in debug_settings.items() if value is not None} ==
                {key: value for key, value in inherited_debug.items() if value is not None},
                "debug image changes an inherited runtime configuration field")
        checked_files = {}
        for layer in base_layers:
            apply_layer(layer, checked_files)
        for layer in runtime_layers[len(base_layers):]:
            apply_layer(layer, checked_files, checked_overlay=True)
        added_debug_layers = debug_layers[len(runtime_layers):]
        require(added_debug_layers, "debug image lacks the tools layer")
        apply_debug_tools_layer(added_debug_layers[0], checked_files)
        for layer in added_debug_layers[1:]:
            apply_layer(layer, checked_files, checked_overlay=True)
        validate_payloads.base_aliases(runtime_path)
        validate_payloads.base_aliases(debug_path)
        lock_sha = sha(source_root / "dockers/docker-syncd-vs/bazel/apt.lock.json")
        runtime_selection = json.loads(runtime_apt_selection.read_bytes())
        debug_selection = json.loads(debug_apt_selection.read_bytes())
        for selection, variant, base_digest, make_sha in (
            (runtime_selection, "runtime", base_descriptor["digest"], runtime_receipt["manifest_sha256"]),
            (debug_selection, "debug", runtime_descriptor["digest"], debug_receipt["manifest_sha256"]),
        ):
            require(selection.get("schema") == 1 and selection.get("variant") == variant and
                    selection.get("base_manifest_digest") == base_digest and selection.get("apt_lock_sha256") == lock_sha and
                    selection.get("make_manifest_sha256") == make_sha, variant + " APT selection identity differs")
        runtime_overlay = {}
        apply_layer(apt_layer, runtime_overlay)
        runtime_overlay.update(expected_runtime)
        require(json.loads(package_state_contract.read_bytes()).get("apt_lock_sha256") == lock_sha,
                "runtime package state contract uses a different APT lock")
        state = expected_state(package_state_contract)
        runtime_overlay.update(state)
        runtime_overlay.update({name: {"kind": "directory", "mode": 0o755, "uid": 0, "gid": 0} for name in
                                (".", "etc", "etc/supervisor", "etc/supervisor/conf.d", "usr", "usr/bin")})
        runtime_overlay.update(static_files)
        assert_payload(runtime_overlay, runtime_files, "runtime OCI overlay payload")
        assert_payload(state, debug_files, "debug image changes runtime generated files")
        debug_overlay = {}
        apply_layer(debug_tools_layer, debug_overlay)
        debug_overlay.update(expected_debug)
        assert_payload(debug_overlay, debug_files, "debug OCI overlay payload")
        overlay_report = {"base_manifest_digest": base_descriptor["digest"], "runtime_entries": len(runtime_overlay),
                          "debug_entries": len(debug_overlay), "package_state_entries": len(state),
                          "debug_vim_alternative_links": list(VIM_ALTERNATIVES),
                          "runtime_apt_selected": len(runtime_selection["selected"]),
                          "runtime_apt_skipped_base": len(runtime_selection["skipped_base"]),
                          "runtime_apt_skipped_make": len(runtime_selection["skipped_make"]),
                          "debug_apt_selected": len(debug_selection["selected"]),
                          "runtime_non_elf_base_changes": runtime_selection["changed_non_elf_base_paths"],
                          "debug_non_elf_base_changes": debug_selection["changed_non_elf_base_paths"]}
        archive_report = {
            "runtime": docker_archive(runtime_archive, "docker-syncd-vs:latest", runtime_manifest["config"]["digest"], runtime_manifest["layers"]),
            "debug": docker_archive(debug_archive, "docker-syncd-vs-dbg:latest", debug_manifest["config"]["digest"], debug_manifest["layers"]),
        }
    changed_non_elf = sorted(name for name, item in runtime_files.items()
                             if "elf_machine" not in item and debug_files.get(name) != item)
    remaining = ["native ELF SONAME and debug-symbol validation", "unpacked rootfs and parent-symlink application", "in-image dynamic loading and debugger lookup",
                 "package-manager database and generated loader/Python cache equivalence"]
    if not complete:
        remaining.append("complete APT, generated-file, and Docker archive checks")
    return {
        "schema": 1, "validation_mode": "fixture" if fixture else "complete",
        "runtime_manifest_digest": runtime_descriptor["digest"], "debug_manifest_digest": debug_descriptor["digest"],
        "runtime_package_manifest_sha256": runtime_receipt["manifest_sha256"],
        "debug_package_manifest_sha256": debug_receipt["manifest_sha256"],
        "runtime_layers": len(runtime_layers), "debug_layers": len(debug_layers),
        "runtime_package_count": runtime_receipt["package_count"], "debug_package_count": debug_receipt["package_count"],
        "runtime_payload_entries": len(expected_runtime), "debug_payload_entries": len(expected_debug),
        "runtime_elf_count": len(runtime_elfs), "allowed_debug_package_replacements": [],
        "changed_non_elf_runtime_paths_in_debug": changed_non_elf, "overlay_checks": overlay_report,
        "archives": archive_report, "remaining_checks": remaining,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--debug", required=True, type=Path)
    parser.add_argument("--runtime-handoff", required=True, type=Path)
    parser.add_argument("--debug-handoff", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--debug-manifest", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--apt-layer", type=Path)
    parser.add_argument("--debug-tools-layer", type=Path)
    parser.add_argument("--runtime-apt-selection", type=Path)
    parser.add_argument("--debug-apt-selection", type=Path)
    parser.add_argument("--package-state-contract", type=Path)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--debug-archive", type=Path)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = validate_images(args.runtime, args.debug, args.runtime_handoff, args.debug_handoff,
                                 args.runtime_manifest, args.debug_manifest, args.source_root, fixture=args.fixture,
                                 base_path=args.base, apt_layer=args.apt_layer, debug_tools_layer=args.debug_tools_layer,
                                 runtime_apt_selection=args.runtime_apt_selection, debug_apt_selection=args.debug_apt_selection,
                                 package_state_contract=args.package_state_contract, runtime_archive=args.runtime_archive,
                                 debug_archive=args.debug_archive)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd-vs OCI validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
