#!/usr/bin/env python3
"""Verify syncd-vs package SONAME links and matching native debug artifacts."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib

sys.path.insert(0, str(Path(__file__).absolute().parent))
import validate_payloads

REQUIRED_PACKAGES = {"syncd-vs", "libsairedis", "libsaimetadata", "libsaivs", "libswsscommon", "libyang3"}
REQUIRED_SONAMES = {"libsairedis": "libsairedis.so.0", "libsaimetadata": "libsaimetadata.so.0",
                    "libsaivs": "libsaivs.so.0", "libswsscommon": "libswsscommon.so.0", "libyang3": "libyang.so.3"}
GAP_PACKAGES = {"libsai", "p4lang-pi", "p4lang-bmv2", "p4lang-p4c", "libnl-3-200",
                "libnl-genl-3-200", "libnl-route-3-200", "libnl-nf-3-200", "libnl-cli-3-200"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=120, check=False)
    require(result.returncode == 0, "ELF inspection failed: " + repr(arguments[:3]) + "\n" + result.stderr)
    return result.stdout


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
                pure = PurePosixPath(member.name)
                require(not pure.is_absolute() and ".." not in pure.parts, "unsafe native package path")
                name = str(pure)
                kind = "file" if member.isfile() else "symlink" if member.issym() else "hardlink" if member.islnk() else "other"
                item = {"kind": kind, "package": record["package"], "path": name}
                if kind in ("symlink", "hardlink"):
                    item["linkname"] = member.linkname
                elif kind == "file":
                    stream = archive.extractfile(member)
                    first = stream.read(64)
                    if first.startswith(b"\x7fELF"):
                        require(len(first) >= 20 and first[:6] == b"\x7fELF\x02\x01", "expected little-endian ELF64: " + name)
                        output = temporary / (prefix + "-" + str(count))
                        count += 1
                        with output.open("wb") as target:
                            target.write(first)
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                target.write(chunk)
                        item["file"] = output
                        item["elf_type"], item["elf_machine"] = struct.unpack_from("<HH", first, 16)
                files[name] = item
        require(next(members, None) is None, "aggregate package payload has unrecorded members")
    return files

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


def details(path, readelf):
    notes = command([readelf, "-n", str(path)])
    sections = command([readelf, "-SW", str(path)])
    dynamic = command([readelf, "-d", str(path)])
    identifier = re.search(r"Build ID: ([0-9a-f]+)", notes)
    soname = re.search(r"\(SONAME\).*\[([^]]+)\]", dynamic)
    return {"build_id": identifier[1] if identifier else None,
            "soname": soname[1] if soname else None,
            "needed": re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic),
            "has_dwarf": ".debug_info" in sections or ".zdebug_info" in sections,
            "has_debuglink": ".gnu_debuglink" in sections}


def validate_native(runtime_manifest, debug_manifest, *, readelf="readelf", objcopy="objcopy",
                    required_packages=REQUIRED_PACKAGES, required_sonames=REQUIRED_SONAMES,
                    gap_packages=GAP_PACKAGES):
    validate_payloads.validate(runtime_manifest, runtime_manifest.parent / "payload.tar", variant="runtime")
    validate_payloads.validate(debug_manifest, debug_manifest.parent / "payload.tar",
                               variant="debug", runtime_manifest=runtime_manifest)
    with tempfile.TemporaryDirectory(prefix="syncd-native-validation-") as temporary_name:
        temporary = Path(temporary_name)
        runtime = collect(runtime_manifest, temporary, "runtime")
        debug = collect(debug_manifest, temporary, "debug")
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
            info = details(item["file"], readelf)
            record = {"path": name, "package": item["package"], **info}
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
                symbol_info = details(symbols["file"], readelf)
                require(symbol_info["build_id"] == identifier and symbol_info["has_dwarf"],
                        "debug ELF does not match runtime build ID or lacks DWARF: " + name)
                if info["has_debuglink"]:
                    link = temporary / "debuglink"
                    command([objcopy, "--dump-section", ".gnu_debuglink=" + str(link), str(item["file"])])
                    raw = link.read_bytes()
                    end = raw.index(0)
                    offset = (end + 4) & ~3
                    require(raw[:end].decode() == PurePosixPath(debug_name).name and len(raw) >= offset + 4 and
                            struct.unpack_from("<I", raw, offset)[0] == zlib.crc32(symbols["file"].read_bytes()),
                            "debug-link filename or checksum differs: " + name)
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
        unmatched = []
        for name in debug:
            match = re.fullmatch(r"usr/lib/debug/\.build-id/([0-9a-f]{2})/([0-9a-f]+)\.debug", name)
            if match and match[1] + match[2] not in runtime_ids:
                unmatched.append(name)
        return {"schema": 1, "runtime_manifest_sha256": hashlib.sha256(runtime_manifest.read_bytes()).hexdigest(),
                "debug_manifest_sha256": hashlib.sha256(debug_manifest.read_bytes()).hexdigest(),
                "elf_count": len(checked), "paired_symbols": paired, "embedded_symbols": embedded,
                "preserved_make_debug_gaps": gaps, "external_needed": sorted(external_needed),
                "base_debug_symbols_requiring_full_image_validation": sorted(unmatched),
                "remaining_checks": ["resolve external_needed in the actual OCI rootfs", "GDB source and line lookup in the actual debug image"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--debug-manifest", required=True, type=Path)
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--objcopy", default="objcopy")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = validate_native(args.runtime_manifest, args.debug_manifest, readelf=args.readelf, objcopy=args.objcopy)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError, subprocess.TimeoutExpired) as error:
        parser.exit(1, "syncd-vs native package validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
