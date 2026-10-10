#!/usr/bin/env python3
"""Verify syncd-vs package SONAME links and matching native debug artifacts."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
sys.path.insert(0, str(Path(__file__).absolute().parent))
from tools.bazel.ci.artifact_validation import elf_header, elf_info, path_name, require, sha, verify_debuglink
import validate_payloads

REQUIRED_PACKAGES = {"syncd-vs", "libsairedis", "libsaimetadata", "libsaivs", "libswsscommon", "libyang3"}
REQUIRED_SONAMES = {"libsairedis": "libsairedis.so.0", "libsaimetadata": "libsaimetadata.so.0",
                    "libsaivs": "libsaivs.so.0", "libswsscommon": "libswsscommon.so.0", "libyang3": "libyang.so.3"}
GAP_PACKAGES = {"libsai", "p4lang-pi", "p4lang-bmv2", "p4lang-p4c", "libnl-3-200",
                "libnl-genl-3-200", "libnl-route-3-200", "libnl-nf-3-200", "libnl-cli-3-200",
                # The retained FIPS client has stripped ELF files with build
                # IDs/debuglinks, but Make supplies no matching symbol package.
                "openssh-client"}


def collect(manifest_path, temporary, prefix):
    manifest = json.loads(manifest_path.read_bytes())
    files = {}
    count = 0
    with tarfile.open(manifest_path.parent / "payload.tar", "r:") as archive:
        members = iter(archive)
        for record in manifest["packages"]:
            for _ in range(record["payload_members"]):
                member = next(members, None)
                require(member is not None, "aggregate package payload ended before its recorded segments")
                normalized = validate_payloads.normalized_member(member)
                if normalized is None:
                    continue
                name = path_name(normalized.name)
                kind = ("file" if member.isfile() else "symlink" if member.issym() else
                        "hardlink" if member.islnk() else "directory" if member.isdir() else "other")
                item = {"kind": kind, "package": record["package"], "path": name}
                if kind in ("symlink", "hardlink"):
                    item["linkname"] = normalized.linkname
                elif kind == "file":
                    stream = archive.extractfile(member)
                    first = stream.read(64)
                    header = elf_header(first, name)
                    if header:
                        output = temporary / (prefix + "-" + str(count))
                        count += 1
                        with output.open("wb") as target:
                            target.write(first)
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                target.write(chunk)
                        item["file"] = output
                        item.update(header)
                files[name] = item
        require(next(members, None) is None, "aggregate package payload has unrecorded members")
    return files


def collect_source(archive_path, receipt, temporary, variant):
    """Extract source ELFs; runtime owners come from the checked runtime receipt.

    Symbols come from the same shared collector used by Orchagent. Their owning
    runtime ELF is established below through build ID, DWARF and debuglink CRC,
    rather than duplicating a symbol inventory for every package in the receipt.
    """
    owners = {}
    if variant == "runtime":
        for package in receipt["packages"]:
            for name, metadata in package["files"].items():
                require(name not in owners or metadata["kind"] == "directory",
                        "source packages share a non-directory path: " + name)
                owners[name] = package
    files = {}
    with tarfile.open(archive_path, "r:") as archive:
        for index, member in enumerate(archive):
            name = path_name(member.name)
            require(variant == "debug" or name in owners, "source archive member has no package owner: " + name)
            kind = ("file" if member.isfile() else "symlink" if member.issym() else
                    "hardlink" if member.islnk() else "directory")
            item = {"kind": kind, "path": name, "origin": "bazel_source"}
            if variant == "runtime":
                package = owners[name]
                item.update(package=package["package"], source=package["source"],
                            source_runtime_input_tar_sha256=package["input_tar_sha256"])
            else:
                item["package"] = "source_symbols"
            if kind in ("symlink", "hardlink"):
                item["linkname"] = member.linkname
            elif kind == "file":
                stream = archive.extractfile(member)
                first = stream.read(64)
                header = elf_header(first, name)
                if header:
                    output = temporary / ("source-" + variant + "-" + str(index))
                    with output.open("wb") as target:
                        target.write(first)
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            target.write(chunk)
                    item["file"] = output
                    item.update(header)
            files[name] = item
    return files


def merge_source(files, additions):
    """Source outputs must have one owner and cannot silently overwrite Make payloads."""
    for name, item in additions.items():
        require(name not in files or (item["kind"] == files[name]["kind"] == "directory"),
                "source package overlaps a Make payload: " + name)
        files[name] = item


def resolve(files, name):
    for _ in range(32):
        item = files.get(name)
        require(item is not None, "missing library or debug link target: " + name)
        if item["kind"] not in ("symlink", "hardlink"):
            return item
        target = item["linkname"]
        if target.startswith("/"):
            name = target.lstrip("/")
        elif item["kind"] == "hardlink":
            name = posixpath.normpath(target)
        else:
            name = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        require(name != ".." and not name.startswith("../"), "link leaves the image root")
    raise ValueError("library or debug symlink cycle")


def validate_inherited_base(base_path, receipt, debug, debug_manifest, temporary, *, readelf, objcopy):
    """Pair the unchanged base library with its filtered Make symbols and DWZ data."""
    inherited = receipt.get("inherited_files", {}) if receipt else {}
    if not inherited:
        return []
    require(base_path is not None, "inherited native symbol validation requires the managed OCI base")
    import base_debug_symbols
    from tools.bazel.oci.oci_inventory import apply_layer
    from tools.bazel.oci.oci_layout import validate_layout
    contract = base_debug_symbols.read_contract()
    base = validate_layout(base_path, "linux/amd64")
    require(base.descriptor["digest"] == receipt["base_manifest_digest"], "inherited symbols use a different OCI base")
    files = {}
    for layer in base.layers:
        apply_layer(layer, files)
    require(all(files.get(name) == expected for name, expected in inherited.items()),
            "inherited native base files differ from the source receipt")
    name = contract["runtime"]["path"]
    require(inherited.get(name, {}).get("sha256") == contract["runtime"]["sha256"],
            "inherited native runtime identity differs from the symbol contract")
    binary = temporary / "inherited-base-library"
    for layer in reversed(base.layers):
        with tarfile.open(layer, "r:*") as archive:
            members = [member for member in archive if path_name(member.name) == name]
            if not members:
                continue
            member = members[-1]
            require(member.isfile(), "inherited native runtime is not an ELF file")
            with archive.extractfile(member) as stream, binary.open("wb") as output:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    output.write(chunk)
            break
    require(binary.is_file() and sha(binary) == contract["runtime"]["sha256"],
            "inherited runtime ELF bytes differ from the symbol contract")
    info = elf_info(binary, readelf=readelf, timeout=120)
    identifier = contract["runtime"]["build_id"]
    require(info["build_id"] == identifier and info["has_debuglink"] and not info["has_dwarf"],
            "inherited runtime ELF lacks the expected build ID and split symbols")
    debug_name = "usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug"
    symbols = resolve(debug, debug_name)
    require(symbols["package"] == base_debug_symbols.PACKAGE and symbols.get("file") is not None,
            "inherited runtime symbols have an unexpected package owner")
    symbol_info = elf_info(symbols["file"], readelf=readelf, timeout=120)
    require(symbol_info["build_id"] == identifier and symbol_info["has_dwarf"],
            "inherited debug ELF does not match runtime build ID or lacks DWARF")
    verify_debuglink(binary, symbols["file"], name, expected_name=PurePosixPath(debug_name).name,
                     objcopy=objcopy, timeout=120)
    dwz_names = set(contract["files"]) - {debug_name}
    require(len(dwz_names) == 1, "inherited symbol contract must select one DWZ supplement")
    dwz_name = next(iter(dwz_names))
    dwz = resolve(debug, dwz_name)
    require(dwz["package"] == base_debug_symbols.PACKAGE and dwz.get("file") is not None,
            "inherited DWZ data has an unexpected package owner")
    for path, item in ((debug_name, symbols), (dwz_name, dwz)):
        require(sha(item["file"]) == contract["files"][path]["sha256"],
                "inherited symbol bytes differ from the reviewed contract: " + path)
    dwz_info = elf_info(dwz["file"], readelf=readelf, timeout=120)
    require(dwz_info["build_id"] == contract["files"][dwz_name]["build_id"] and dwz_info["has_dwarf"],
            "inherited DWZ build ID or DWARF differs")
    alternate = temporary / "inherited-debugaltlink"
    completed = subprocess.run([objcopy, "--dump-section", ".gnu_debugaltlink=" + str(alternate), str(symbols["file"])],
                               capture_output=True, text=True, timeout=120)
    require(completed.returncode == 0 and alternate.is_file(), "inherited debug ELF lacks its DWZ link")
    link, identifier_bytes = alternate.read_bytes().split(b"\0", 1)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(debug_name), link.decode())).lstrip("/")
    require(target == dwz_name and identifier_bytes.hex() == dwz_info["build_id"],
            "inherited debug alternate link differs from its DWZ path or build ID")
    records = [item for item in json.loads(debug_manifest.read_bytes())["packages"]
               if item["package"] == base_debug_symbols.PACKAGE]
    require(len(records) == 1, "inherited symbols require one checked Make package record")
    record = records[0]
    descriptor = base_debug_symbols.check_record(record)
    return [{"path": name, "origin": "inherited_base", "base_manifest_digest": base.descriptor["digest"],
             "build_id": info["build_id"], "debug_path": debug_name, "dwz_path": dwz_name,
             "dwz_build_id": dwz_info["build_id"], "source_deb_sha256": record["source_sha256"],
             "original_payload_sha256": record["original_payload_sha256"],
             "filtered_payload_sha256": record["payload_sha256"], "filter_contract_sha256": descriptor["contract_sha256"]}]


def validate_native(runtime_manifest, debug_manifest, *, readelf="readelf", objcopy="objcopy",
                    required_packages=REQUIRED_PACKAGES, required_sonames=REQUIRED_SONAMES,
                    gap_packages=GAP_PACKAGES, source_runtime_tar=None,
                    source_debug_tar=None, source_receipt=None, base_path=None, fixture=False):
    source_inputs = (source_runtime_tar, source_debug_tar, source_receipt)
    has_source = all(value is not None for value in source_inputs)
    require(has_source or not any(value is not None for value in source_inputs),
            "source runtime tar, debug tar, and receipt must be supplied together")
    require(fixture or has_source, "native validation requires source runtime tar, debug tar, and receipt")
    receipt = None
    source_debug_sha256 = sha(source_debug_tar) if has_source else None
    if has_source:
        import source_packages
        receipt = source_packages.validate_receipt(
            source_receipt, source_runtime_tar, source_debug_tar,
            contract_path=None if fixture else Path(__file__).with_name("source_packages.json"))
    validate_payloads.validate(runtime_manifest, runtime_manifest.parent / "payload.tar", variant="runtime")
    validate_payloads.validate(debug_manifest, debug_manifest.parent / "payload.tar",
                               variant="debug", runtime_manifest=runtime_manifest)
    with tempfile.TemporaryDirectory(prefix="syncd-native-validation-") as temporary_name:
        temporary = Path(temporary_name)
        runtime = collect(runtime_manifest, temporary, "runtime")
        debug = collect(debug_manifest, temporary, "debug")
        if has_source:
            source_runtime = collect_source(source_runtime_tar, receipt, temporary, "runtime")
            source_debug = collect_source(source_debug_tar, receipt, temporary, "debug")
            merge_source(dict(runtime), source_debug)
            merge_source(dict(debug), source_runtime)
            merge_source(runtime, source_runtime)
            merge_source(debug, source_debug)
        elfs = {name: item for name, item in runtime.items() if "file" in item}
        require(required_packages.issubset({item["package"] for item in elfs.values()}),
                "a required native runtime package has no ELF")
        basenames = {PurePosixPath(name).name for name in runtime}
        checked, gaps, embedded, paired = [], [], [], []
        observed_sonames = {}
        external_needed = set()
        runtime_ids = set()
        for name, item in sorted(elfs.items()):
            require(item["package"] in required_packages or item["package"] in gap_packages,
                    "unclassified native package ELF: " + item["package"] + ":" + name)
            require(item["elf_machine"] == 62 or item["elf_type"] == 1,
                    "non-AMD64 executable or shared library: " + name)
            info = elf_info(item["file"], readelf=readelf, dynamic=True, timeout=120)
            record = {"path": name, "package": item["package"], **info}
            if item.get("origin") == "bazel_source":
                record.update({key: item[key] for key in
                               ("origin", "source", "source_runtime_input_tar_sha256")})
                record["source_debug_tar_sha256"] = source_debug_sha256
                require(info["build_id"] and info["has_debuglink"] and not info["has_dwarf"],
                        "source runtime ELF must be stripped with a build ID and debuglink: " + name)
            if info["soname"]:
                soname_path = str(PurePosixPath(name).parent / info["soname"])
                target = resolve(runtime, soname_path)
                require(target.get("file") is not None and target["file"].read_bytes() == item["file"].read_bytes(),
                        "SONAME path does not resolve to its packaged ELF: " + name)
                observed_sonames.setdefault(item["package"], set()).add(info["soname"])
            for needed in info["needed"]:
                if re.match(r"lib(?:sai|swsscommon|MdioIpcClient|pi)", needed):
                    require(needed in basenames, "missing native runtime dependency " + needed + " for " + name)
                else:
                    external_needed.add(needed)
            identifier = info["build_id"]
            if identifier:
                runtime_ids.add(identifier)
            debug_name = ("usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug") if identifier else None
            if debug_name and debug_name in debug:
                symbols = resolve(debug, debug_name)
                require(symbols.get("file") is not None, "debug path is not an ELF: " + debug_name)
                if item.get("origin") == "bazel_source":
                    require(symbols.get("origin") == "bazel_source",
                            "source runtime requires symbols from its source collector: " + name)
                symbol_info = elf_info(symbols["file"], readelf=readelf, timeout=120)
                require(symbol_info["build_id"] == identifier and symbol_info["has_dwarf"],
                        "debug ELF does not match runtime build ID or lacks DWARF: " + name)
                if info["has_debuglink"]:
                    verify_debuglink(item["file"], symbols["file"], name,
                                     expected_name=PurePosixPath(debug_name).name, objcopy=objcopy, timeout=120)
                record["debug_path"] = debug_name
                paired.append(record)
            elif info["has_dwarf"]:
                embedded.append(record)
            else:
                require(item["package"] in gap_packages, "missing matching debug symbols for required runtime ELF: " + name)
                record["reason"] = "the existing Make debug list supplies no matching symbol package"
                gaps.append(record)
            checked.append(record)
        for package, soname in required_sonames.items():
            require(soname in observed_sonames.get(package, set()), "required SONAME is absent: " + package + ":" + soname)
        inherited_pairs = validate_inherited_base(base_path, receipt, debug, debug_manifest, temporary,
                                                  readelf=readelf, objcopy=objcopy)
        runtime_ids.update(record["build_id"] for record in inherited_pairs)
        unmatched = []
        for name in debug:
            match = re.fullmatch(r"usr/lib/debug/\.build-id/([0-9a-f]{2})/([0-9a-f]+)\.debug", name)
            if match and match[1] + match[2] not in runtime_ids:
                require(debug[name].get("origin") != "bazel_source", "source debug symbols have no runtime ELF: " + name)
                unmatched.append(name)
        return {"schema": 1, "runtime_manifest_sha256": hashlib.sha256(runtime_manifest.read_bytes()).hexdigest(),
                "debug_manifest_sha256": hashlib.sha256(debug_manifest.read_bytes()).hexdigest(),
                "source_receipt_sha256": sha(source_receipt) if has_source else None,
                "source_packages": receipt,
                "inherited_base_symbol_pairs": inherited_pairs,
                "elf_count": len(checked), "paired_symbols": paired, "embedded_symbols": embedded,
                "preserved_make_debug_gaps": gaps, "external_needed": sorted(external_needed),
                "base_debug_symbols_requiring_full_image_validation": sorted(unmatched),
                "remaining_checks": ["resolve external_needed in the actual OCI rootfs", "GDB source and line lookup in the actual debug image"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--debug-manifest", required=True, type=Path)
    parser.add_argument("--source-runtime-tar", required=True, type=Path)
    parser.add_argument("--source-debug-tar", required=True, type=Path)
    parser.add_argument("--source-receipt", required=True, type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--objcopy", default="objcopy")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = validate_native(args.runtime_manifest, args.debug_manifest, readelf=args.readelf, objcopy=args.objcopy,
                                 source_runtime_tar=args.source_runtime_tar, source_debug_tar=args.source_debug_tar,
                                 source_receipt=args.source_receipt, base_path=args.base)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError, subprocess.TimeoutExpired) as error:
        parser.exit(1, "syncd-vs native package validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
