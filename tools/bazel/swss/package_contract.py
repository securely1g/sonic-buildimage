"""Validate source-built SWSS runtime layers and their matching debug symbols."""

import ast
import hashlib
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import tarfile
import zlib


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def path_name(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts, "unsafe archive path: " + value)
    return str(path)


def command(arguments, **kwargs):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=300, **kwargs)
    require(result.returncode == 0, "command failed: " + repr(arguments[:8]) + "\n" + result.stdout + result.stderr)
    return result.stdout


def metadata(member):
    kind = ("file" if member.isfile() else "directory" if member.isdir() else
            "symlink" if member.issym() else "hardlink" if member.islnk() else "other")
    result = {"kind": kind, "mode": member.mode, "uid": member.uid, "gid": member.gid}
    if kind in ("symlink", "hardlink"):
        # Layer flattening may add a harmless ./ prefix to link targets.
        # PurePosixPath removes dot components while retaining .. components.
        result["linkname"] = str(PurePosixPath(member.linkname))
    return result


def payload(path, require_root=True):
    result = {}
    with tarfile.open(path) as archive:
        for member in archive:
            name = path_name(member.name)
            require(name not in result, "duplicate package member: " + name)
            item = metadata(member)
            require(item["kind"] != "other", "unsupported package member: " + name)
            require(not require_root or (member.uid, member.gid) == (0, 0), "non-root package owner: " + name)
            if member.isfile():
                with archive.extractfile(member) as stream:
                    data = stream.read()
                item.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
                if data.startswith(b"\x7fELF"):
                    require(data[:6] == b"\x7fELF\x02\x01", "expected little-endian ELF64: " + name)
                    item["elf_machine"] = struct.unpack_from("<H", data, 18)[0]
            result[name] = item
    return result


# DASH and SWSS link this source-built shared library. Keep the matching runtime
# and symbols in the final layer instead of installing a second Debian build.
PROTOBUF_RUNTIME = "usr/lib/x86_64-linux-gnu/libprotobuf.so.32.0.12"


def source_protobuf_contract(combined, protobuf):
    require(protobuf.get(PROTOBUF_RUNTIME, {}).get("elf_machine") == 62,
            "missing source-built AMD64 protobuf runtime")
    for name, item in protobuf.items():
        if item["kind"] != "directory":
            require(combined.get(name) == dict(item, uid=0, gid=0),
                    "runtime layer changes source protobuf bytes or modes: " + name)
    require(combined.get("usr/lib/x86_64-linux-gnu/libprotobuf.so.32", {}).get("linkname") ==
            "libprotobuf.so.32.0.12", "incorrect source protobuf SONAME link")
    require({name for name, item in combined.items()
             if "/libprotobuf.so" in name and item["kind"] != "directory"} ==
            {PROTOBUF_RUNTIME, "usr/lib/x86_64-linux-gnu/libprotobuf.so.32"},
            "conflicting full protobuf runtime in declared layer")


SWSS_CONTRACT_INPUTS = ("dist/BUILD.bazel", "debian/swss.install")


def swss_contract(source, files):
    # SWSS owns the runtime declarations and compares them with configured
    # Automake in source CI. Read only their literal install inventory here;
    # do not depend on its retired generated production_sources.bzl file.
    required = {"CPP_BINARIES", "LUA_FILES", "LUA_INSTALL_ALIASES"}
    values = {}
    for node in ast.parse((source / "dist/BUILD.bazel").read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in required:
                require(name not in values, "duplicate SWSS install declaration: " + name)
                values[name] = ast.literal_eval(node.value)
    require(values.keys() == required, "missing SWSS source install declarations")

    def label_path(label):
        require(isinstance(label, str) and label.startswith("//") and label.count(":") == 1,
                "expected a source-local SWSS label: " + str(label))
        package, name = label[2:].split(":")
        require(package and name, "empty SWSS source label")
        return Path(path_name(package + "/" + name))

    programs = {"usr/bin/" + label_path(label).name for label in values["CPP_BINARIES"]}
    require(len(programs) == len(values["CPP_BINARIES"]), "duplicate SWSS program path")
    expected = {name: (0o755, None) for name in programs}
    aliases = values["LUA_INSTALL_ALIASES"]
    require(isinstance(aliases, dict) and aliases.keys() <= set(values["LUA_FILES"]),
            "SWSS install alias has no declared Lua file")
    for label in values["LUA_FILES"]:
        name = "usr/share/swss/" + label_path(label).name
        require(name not in expected, "duplicate SWSS install path: " + name)
        expected[name] = (0o644, source / label_path(aliases.get(label, label)))
    for line in (source / "debian/swss.install").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        origin, directory = line.split()
        name = str(PurePosixPath(directory) / PurePosixPath(origin).name)
        require(name not in expected, "duplicate SWSS install path: " + name)
        binary = origin == "target/release/countersyncd"
        expected[name] = (0o755 if directory == "usr/bin" else 0o644, None if binary else source / origin)
        if binary:
            programs.add(name)
    actual = {name for name, item in files.items() if item["kind"] != "directory"}
    require(actual == set(expected), "SWSS package differs from the source install contract")
    require(len(programs) == 30, "expected the complete 30-program SWSS configuration")
    for name, (mode, origin) in expected.items():
        require(files[name]["kind"] == "file" and files[name]["mode"] == mode, "SWSS type or mode: " + name)
        if origin is not None:
            require(files[name]["sha256"] == sha(origin), "SWSS installed data differs from source: " + name)
        else:
            require("elf_machine" in files[name], "SWSS executable is not ELF: " + name)
    return sorted(programs)


def elf_debug(extracted, expected, prebuilt):
    pairs, gaps = [], []
    for name, item in expected.items():
        if "elf_machine" not in item or name.startswith("usr/lib/debug/"):
            continue
        binary = extracted / name
        notes = command(["readelf", "-n", str(binary)])
        match = re.search(r"Build ID: ([0-9a-f]+)", notes)
        require(match is not None, "missing runtime ELF build ID: " + name)
        identifier = match[1]
        debug_name = "usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug"
        sections = command(["readelf", "-SW", str(binary)])
        require(".debug_info" not in sections, "unstripped runtime ELF: " + name)
        if name in prebuilt:
            require(debug_name not in expected, "prebuilt debug-gap declaration is stale: " + name)
            gaps.append({"path": name, "build_id": identifier, "reason": "producer supplies no matching debug artifact"})
            continue
        require(debug_name in expected, "missing embedded debug file: " + name)
        symbols = extracted / debug_name
        require("Build ID: " + identifier in command(["readelf", "-n", str(symbols)]), "debug build ID differs: " + name)
        require(".debug_info" in command(["readelf", "-SW", str(symbols)]), "debug file lacks DWARF: " + name)
        link = extracted / "debuglink"
        command(["objcopy", "--dump-section", ".gnu_debuglink=" + str(link), str(binary)])
        raw = link.read_bytes()
        end = raw.index(0)
        require(raw[:end].decode() == symbols.name, "debuglink filename differs: " + name)
        offset = (end + 4) & ~3
        require(len(raw) >= offset + 4 and struct.unpack_from("<I", raw, offset)[0] == zlib.crc32(symbols.read_bytes()),
                "debuglink CRC differs: " + name)
        pairs.append({"path": name, "build_id": identifier, "debug_path": debug_name})
    require(set(prebuilt) == {item["path"] for item in gaps}, "unmatched prebuilt gap declaration")
    return pairs, gaps
