#!/usr/bin/env python3
"""Retain base and Make packages when assembling syncd's locked APT layers."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).absolute().parent))
import apt_lock
import validate_image
import validate_payloads


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def base_packages(layers):
    target = "var/lib/dpkg/status"
    status = None
    for layer in layers:
        removed = False
        added = None
        with tarfile.open(layer, "r:*") as archive:
            for member in archive:
                name = validate_image.member_name(member.name)
                pure = PurePosixPath(name)
                if name == target:
                    require(member.isfile(), "base dpkg status is not a regular file")
                    added = archive.extractfile(member).read()
                elif pure.name == ".wh..wh..opq":
                    parent = str(pure.parent)
                    removed |= parent == "." or target.startswith(parent + "/")
                elif pure.name.startswith(".wh."):
                    hidden = str(pure.parent / pure.name[4:])
                    removed |= target == hidden or target.startswith(hidden + "/")
        if removed:
            status = None
        if added is not None:
            status = added
    require(status is not None, "config-engine OCI base lacks dpkg status")
    result = {}
    for paragraph in status.decode().strip().split("\n\n"):
        fields = {}
        for line in paragraph.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.lstrip()
        if fields.get("Status") != "install ok installed":
            continue
        require(fields.get("Package") and fields.get("Version") and fields.get("Architecture") in ("amd64", "all"),
                "invalid or foreign installed package in config-engine OCI base")
        require(fields["Package"] not in result, "duplicate installed package in config-engine OCI base")
        result[fields["Package"]] = fields["Version"]
    require(result, "config-engine OCI base has no installed packages")
    return result


def select(base, lock_path, make_manifest_path, mapping_path, *, variant):
    locked = apt_lock.closure(json.loads(lock_path.read_bytes()), variant)
    mapping = json.loads(mapping_path.read_bytes())
    require(isinstance(mapping, dict) and isinstance(mapping.get("locked"), list) and
            isinstance(mapping.get("hub_paths"), list), "invalid syncd APT input mapping")
    paths = {item["key"]: {kind: Path(item[kind]) for kind in ("payload", "control")} for item in mapping["locked"]}
    require(len(paths) == len(mapping["locked"]) and set(paths) == set(locked),
            "declared syncd APT payloads differ from the checked content lock")
    hub_paths = {Path(path).resolve() for path in mapping["hub_paths"]}
    locked_paths = {item["payload"].resolve() for item in paths.values()}
    require(len(hub_paths) == len(mapping["hub_paths"]) and hub_paths == locked_paths,
            "current Distroless package set differs from the checked syncd content lock")
    for key, package in locked.items():
        for kind in ("payload", "control"):
            path = paths[key][kind]
            require(path.is_file() and path.stat().st_size == package[kind + "_size"] and
                    sha(path) == package[kind + "_sha256"], "changed locked APT " + kind + ": " + key)
    descriptor, _, _, layers = validate_image.image(base)
    installed = base_packages(layers)
    base_files = {}
    for layer in layers:
        validate_image.apply_layer(layer, base_files)
    base_elfs = {name: item for name, item in base_files.items() if "elf_machine" in item}
    make_bytes = make_manifest_path.read_bytes()
    make = json.loads(make_bytes)
    require(make.get("schema") == 1 and make.get("image") == "docker-syncd-vs" and
            make.get("variant") == variant and make.get("architecture") == "amd64" and
            make.get("distribution") == "trixie" and make.get("features") == validate_payloads.FEATURES,
            "invalid syncd Make package manifest")
    make_packages = {item["package"]: item for item in make.get("packages", [])}
    require(make_packages and len(make_packages) == len(make["packages"]), "missing or duplicate Make packages")
    selected, skipped_base, skipped_make, overlaps, duplicates = [], [], [], [], []
    selected_names = {}
    selected_files = {}
    for key, package in locked.items():
        path = paths[key]["payload"]
        name = package["name"]
        if name in make_packages:
            skipped_make.append({"key": key, "package": name, "source_sha256": make_packages[name]["source_sha256"]})
            continue
        if name in installed:
            skipped_base.append({"key": key, "package": name, "selected_version": package["version"],
                                 "base_version": installed[name]})
            continue
        if name in selected_names:
            duplicates.append({"package": name, "kept_key": selected_names[name], "duplicate_key": key})
            continue
        selected_names[name] = key
        files = {}
        validate_image.apply_layer(path, files, checked_overlay=True)
        for filename, item in files.items():
            previous = base_files.get(filename)
            if filename in base_elfs:
                require(previous == item, "APT package " + name + " would replace a base ELF: " + filename)
            if "elf_machine" in selected_files.get(filename, {}):
                require(selected_files[filename] == item, "APT package " + name + " would replace a selected ELF: " + filename)
            elif previous is not None and previous != item and previous["kind"] != "directory":
                overlaps.append({"package": name, "path": filename})
        selected_files.update(files)
        selected.append({"key": key, "package": name, "version": package["version"],
                         "source_sha256": package["sha256"], "payload_sha256": package["payload_sha256"],
                         "control_sha256": package["control_sha256"], "payload_bytes": package["payload_size"], "path": path})
    validate_image.assert_overlay_paths(selected_files, base_files)
    receipt = {
        "schema": 1, "variant": variant, "base_manifest_digest": descriptor["digest"],
        "base_package_count": len(installed), "base_elf_count": len(base_elfs),
        "make_manifest_sha256": hashlib.sha256(make_bytes).hexdigest(), "apt_lock_sha256": sha(lock_path),
        "selected": [{key: value for key, value in item.items() if key != "path"} for item in selected],
        "skipped_base": skipped_base, "skipped_make": skipped_make,
        "duplicate_sources": duplicates,
        "changed_non_elf_base_paths": sorted(overlaps, key=lambda item: (item["path"], item["package"])),
    }
    return [item["path"] for item in selected], receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--make-manifest", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=("runtime", "debug"))
    parser.add_argument("--bsdtar", required=True)
    parser.add_argument("--out-tar", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        selected, receipt = select(args.base, args.lock, args.make_manifest, args.mapping, variant=args.variant)
        args.out_tar.parent.mkdir(parents=True, exist_ok=True)
        if selected:
            result = subprocess.run([args.bsdtar, "--create", "--format", "gnutar", "--file", str(args.out_tar)] +
                                    ["@" + str(path) for path in selected], capture_output=True, text=True, check=False)
            require(result.returncode == 0, "APT layer assembly failed: " + result.stderr)
        else:
            with tarfile.open(args.out_tar, "w", format=tarfile.GNU_FORMAT):
                pass
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd APT payload selection failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
