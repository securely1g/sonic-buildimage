#!/usr/bin/env python3
"""Build the checked alternatives and provider-file layer for syncd-vs."""

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
sys.path.insert(0, str(Path(__file__).absolute().parent))
from tools.bazel.ci.artifact_validation import require, sha
import validate_image
import validate_payloads

SCRIPTS = {"preinst", "postinst", "prerm", "postrm", "triggers"}


def safe_path(value):
    path = PurePosixPath(value)
    require(isinstance(value, str) and str(path) == value and value != "." and
            not path.is_absolute() and ".." not in path.parts, "invalid package state path: " + str(value))
    return value


def resolve(files, name):
    name = name.lstrip("/")
    for _ in range(64):
        parts = PurePosixPath(name).parts
        changed = False
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            item = files.get(prefix)
            if item is None:
                continue
            if item["kind"] == "symlink" or (item["kind"] == "hardlink" and index == len(parts)):
                target = item["linkname"]
                if target.startswith("/"):
                    next_name = target.lstrip("/")
                elif item["kind"] == "hardlink":
                    next_name = target
                else:
                    next_name = posixpath.join(posixpath.dirname(prefix), target)
                name = posixpath.normpath(posixpath.join(next_name, *parts[index:]))
                require(name != ".." and not name.startswith("../"), "package state link leaves the image root")
                changed = True
                break
            if index < len(parts):
                require(item["kind"] == "directory", "package state link crosses a non-directory: " + prefix)
        if changed:
            continue
        item = files.get(name)
        if item is None and any(path.startswith(name + "/") for path in files):
            item = {"kind": "directory"}
        require(item is not None and item["kind"] in ("file", "directory"), "package state link target is absent: " + name)
        return name
    raise ValueError("package state link cycle")


def file_bytes(layer, names):
    result = {}
    with tarfile.open(layer, "r:*") as archive:
        for member in archive:
            name = validate_image.member_name(member.name)
            if name in names:
                require(member.isfile(), "provider alias source is not a regular file: " + name)
                result[name] = archive.extractfile(member).read()
    require(set(result) == set(names), "provider alias source is missing from the APT layer")
    return result


def build(contract_path, lock_path, selection_path, base, apt_layer, runtime_layer, make_manifest_path, dockerfile, output):
    contract = json.loads(contract_path.read_bytes())
    require(contract.get("schema") == 1, "unsupported syncd package state contract")
    reference = contract.get("reference", {})
    require(re.fullmatch(r"[0-9a-f]{40}", reference.get("buildimage_revision", "")) is not None and
            all(re.fullmatch(r"[0-9a-f]{64}", reference.get(name, "")) for name in
                ("base_archive_sha256", "runtime_archive_sha256", "legacy_dockerfile_sha256")),
            "package state contract lacks reference identity")
    require(sha(dockerfile) == reference["legacy_dockerfile_sha256"], "legacy Dockerfile changed; review package state")
    require(contract.get("apt_lock_sha256") == sha(lock_path), "APT content lock changed; review package state")
    lock = json.loads(lock_path.read_bytes())
    require(lock.get("version") == 2, "unsupported Distroless package lock")
    selection = json.loads(selection_path.read_bytes())
    require(selection.get("schema") == 1 and selection.get("variant") == "runtime" and
            selection.get("apt_lock_sha256") == sha(lock_path) and
            selection.get("make_manifest_sha256") == sha(make_manifest_path), "APT selection does not match the package state inputs")
    selected = {item["package"]: item for item in selection.get("selected", [])}
    require(len(selected) == len(selection.get("selected", [])), "APT selection repeats a package")
    # Bind the explicitly checked package owners to their reviewed content
    # manifest and generated state; shared checks validate their dependencies.
    by_name = {}
    for name, item in selected.items():
        package = lock["packages"].get(item["key"])
        require(package is not None and package["name"] == name and
                package["architecture"] in ("amd64", "all"),
                "selected package is absent from the checked lock: " + name)
        by_name[name] = package
    for group, field in (("package_controls", "control_sha256"), ("package_payloads", "payload_sha256")):
        expected = contract.get(group)
        require(isinstance(expected, dict) and expected, "package state contract lacks " + group)
        for name, identity in expected.items():
            package = by_name.get(name)
            current = selected.get(name)
            require(package is not None and current is not None, "package state requires a selected APT package: " + name)
            require(identity == {"version": package["version"], field: package[field]} and
                    current.get("version") == identity["version"] and current.get(field) == identity[field],
                    "package state owner changed: " + name)
    make = json.loads(make_manifest_path.read_bytes())
    require(make.get("schema") == 1 and make.get("image") == "docker-syncd-vs" and make.get("variant") == "runtime" and
            make.get("features") == validate_payloads.FEATURES, "invalid Make package state input")
    validate_payloads.reject_source_packages(make.get("packages", []))
    make_packages = {item["package"]: item for item in make.get("packages", [])}
    expected_make = contract.get("make_package_state_inputs", {})
    require(expected_make and len(make_packages) == len(make.get("packages", [])) and set(make_packages) == set(expected_make),
            "Make package set changed; review package state")
    for name, item in make_packages.items():
        actual = {"version": item["version"], "architecture": item["architecture"],
                  "control_fields": item.get("control_fields", {}),
                  "maintainer_scripts": {key: value for key, value in item.get("control_files", {}).items() if key in SCRIPTS}}
        # Some state inputs also pin the reviewed archive, such as the FIPS
        # OpenSSH package moved from the debug handoff into runtime.
        for field in ("source_sha256", "control_sha256"):
            if field in expected_make[name]:
                actual[field] = item.get(field)
        require(actual == expected_make[name], "Make package relationships or scripts changed; review package state: " + name)
    descriptor, _, _, base_layers = validate_image.image(base)
    require(selection.get("base_manifest_digest") == descriptor["digest"], "package state uses a different OCI base")
    base_files = {}
    for layer in base_layers:
        validate_image.apply_layer(layer, base_files)
    files = dict(base_files)
    validate_image.apply_layer(apt_layer, files, checked_overlay=True)
    for name, item in base_files.items():
        if "elf_machine" in item:
            require(files.get(name) == item, "APT layer changes a base ELF before package state: " + name)
    validate_image.apply_layer(runtime_layer, files, checked_overlay=True)
    expected_files = contract.get("make_package_files", {})
    require(isinstance(expected_files, dict) and expected_files, "package state contract lacks Make data inputs")
    for path, identity in expected_files.items():
        safe_path(path)
        require(set(identity) == {"package", "sha256", "size", "mode", "uid", "gid"} and
                identity["package"] in make_packages, "invalid Make package state file contract: " + path)
        expected = {"kind": "file", **{name: identity[name] for name in ("sha256", "size", "mode", "uid", "gid")}}
        require(files.get(path) == expected, "Make package state file changed; review generated links: " + path)
    entries = contract.get("entries")
    aliases = contract.get("aliases")
    require(isinstance(entries, list) and entries and isinstance(aliases, list), "invalid package state entries")
    additions = []
    paths = set()
    for entry in entries:
        path = safe_path(entry["path"])
        require(path not in paths, "duplicate package state path: " + path)
        paths.add(path)
        require(entry.get("uid") == 0 and entry.get("gid") == 0 and isinstance(entry.get("mode"), int) and 0 <= entry["mode"] <= 0o7777,
                "invalid package state ownership or mode: " + path)
        kind = entry.get("kind")
        if kind == "symlink":
            require(set(entry) == {"path", "kind", "mode", "uid", "gid", "linkname"} and
                    isinstance(entry["linkname"], str) and entry["linkname"], "invalid package state link: " + path)
            metadata = {name: entry[name] for name in ("kind", "mode", "uid", "gid", "linkname")}
            data = None
        elif kind == "file":
            require(set(entry) == {"path", "kind", "mode", "uid", "gid", "text", "sha256", "size"} and
                    path.startswith("var/lib/dpkg/alternatives/"), "invalid alternatives state file: " + path)
            data = entry["text"].encode()
            require(len(data) == entry["size"] and hashlib.sha256(data).hexdigest() == entry["sha256"],
                    "alternatives state bytes changed: " + path)
            metadata = {name: entry[name] for name in ("kind", "mode", "uid", "gid", "sha256", "size")}
        else:
            raise ValueError("unsupported package state entry: " + path)
        require("elf_machine" not in files.get(path, {}), "package state would replace an ELF: " + path)
        additions.append((path, metadata, data))
        files[path] = metadata
    alias_sources = {safe_path(entry["source"]) for entry in aliases}
    source_bytes = file_bytes(apt_layer, alias_sources)
    for entry in aliases:
        require(set(entry) == {"package", "source", "path", "sha256", "size", "mode", "uid", "gid"},
                "invalid provider alias entry")
        path = safe_path(entry["path"])
        require(path not in paths and path.endswith("/copyright") and entry["source"].endswith("/copyright") and
                entry["package"] in contract["package_payloads"], "invalid provider copyright alias: " + path)
        paths.add(path)
        data = source_bytes[entry["source"]]
        require(len(data) == entry["size"] and hashlib.sha256(data).hexdigest() == entry["sha256"],
                "provider copyright bytes differ from the native image: " + path)
        require(entry["uid"] == 0 and entry["gid"] == 0 and isinstance(entry["mode"], int) and 0 <= entry["mode"] <= 0o7777, "invalid provider alias metadata")
        require(files.get(entry["source"], {}).get("sha256") == entry["sha256"], "provider alias source was replaced before package state")
        require("elf_machine" not in files.get(path, {}), "provider alias would replace an ELF: " + path)
        metadata = {"kind": "file", **{name: entry[name] for name in ("mode", "uid", "gid", "sha256", "size")}}
        additions.append((path, metadata, data))
        files[path] = metadata
    links = [path for path, metadata, _ in additions if metadata["kind"] == "symlink"]
    for path in links:
        resolve(files, path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w", format=tarfile.GNU_FORMAT) as archive:
        for path, metadata, data in sorted(additions):
            entry = tarfile.TarInfo("./" + path)
            entry.uid, entry.gid, entry.mode, entry.mtime = metadata["uid"], metadata["gid"], metadata["mode"], 0
            if metadata["kind"] == "symlink":
                entry.type = tarfile.SYMTYPE
                entry.linkname = metadata["linkname"]
                archive.addfile(entry)
            else:
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
    return {"schema": 1, "contract_sha256": sha(contract_path), "apt_lock_sha256": sha(lock_path),
            "entries": len(entries), "aliases": len(aliases), "links_checked": len(links),
            "changed_base_paths": sorted(path for path, metadata, _ in additions
                                         if path in base_files and base_files[path] != metadata)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--apt-lock", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--apt-layer", required=True, type=Path)
    parser.add_argument("--runtime-layer", required=True, type=Path)
    parser.add_argument("--make-manifest", required=True, type=Path)
    parser.add_argument("--dockerfile", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.contract, args.apt_lock, args.selection, args.base, args.apt_layer, args.runtime_layer, args.make_manifest, args.dockerfile, args.out), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd package state validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
