"""Validate SWSS install inventory and source-built runtime libraries."""

import ast
from pathlib import Path, PurePosixPath

from tools.bazel.ci.artifact_validation import path_name, require, sha


# DASH and SWSS link this source-built shared library. Keep the matching runtime
# and symbols in the final layer instead of installing a second Debian build.
MULTIARCH = {62: "x86_64-linux-gnu", 183: "aarch64-linux-gnu"}


def library_directory(expected_machine):
    require(expected_machine in MULTIARCH, "unsupported runtime ELF architecture")
    return "usr/lib/" + MULTIARCH[expected_machine]


def protobuf_runtime(expected_machine):
    return library_directory(expected_machine) + "/libprotobuf.so.32.0.12"


def source_protobuf_contract(combined, protobuf, expected_machine=62):
    runtime = protobuf_runtime(expected_machine)
    soname = library_directory(expected_machine) + "/libprotobuf.so.32"
    require(protobuf.get(runtime, {}).get("elf_machine") == expected_machine,
            "missing source-built protobuf runtime for the selected architecture")
    for name, item in protobuf.items():
        if item["kind"] != "directory":
            require(combined.get(name) == dict(item, uid=0, gid=0),
                    "runtime layer changes source protobuf bytes or modes: " + name)
    require(combined.get(soname, {}).get("linkname") ==
            "libprotobuf.so.32.0.12", "incorrect source protobuf SONAME link")
    require({name for name, item in combined.items()
             if "/libprotobuf.so" in name and item["kind"] != "directory"} ==
            {runtime, soname},
            "conflicting full protobuf runtime in declared layer")


def shared_library_contract(combined, expected_machine):
    """Keep Common's CLI library and Sairedis libraries with their SONAME links."""
    directory = library_directory(expected_machine)
    binaries = set()
    for library in ("libswsscommon", "libsonicdbcli", "libsairedis", "libsaimeta", "libsaimetadata"):
        filename = library + ".so.0.0.0"
        path = directory + "/" + filename
        item = combined.get(path, {})
        require(item.get("kind") == "file" and item.get("elf_machine") == expected_machine,
                "missing runtime library for the selected architecture: " + path)
        link = combined.get(directory + "/" + library + ".so.0", {})
        require(link.get("kind") == "symlink" and link.get("linkname") == filename,
                "incorrect runtime SONAME link: " + library)
        binaries.add(path)
    return binaries


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
