#!/usr/bin/env python3
"""Check frozen VS inputs and plan archive reuse; never execute an assembly."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import shlex
import sys

# Native source ownership is pinned independently of the consumer checkout.
# The workflow supplies the immutable consumer head/tree; producer receipts
# and the actual checkout must agree with that same source context.
WORKER = "df17080469e74093c373e55522bf5c029d3451fc"
CATALOG = "e6ef022c94dd2fdeeac364dfd591f9701cd37c29"
KERNEL_PRODUCER = "1ea1efba4f9c4c1db52d557f476c79aafaa42991"
KERNEL_ORIGINALS = {
    "kernel-packages.json": "79d293bb00859b94435ed865b794c364233eb018984c87cd8b0745eb7861d6d0",
    "kernel-provenance.json": "5394ec6d65b8448b78531fc38be3a18caad4e298f83524eaa9269eaa5c1f019f",
}
FAMILIES = ("p4rt", "syncd", "gearbox")
FLAGS = ["SONIC_DEB_BUILD_POLICY=reuse-only", "SONIC_BAZEL_KERNEL_PREPARE=verify",
         "SONIC_BAZEL_VS_ARCHIVE_PROFILE=final23", "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y",
         "INCLUDE_VS_DASH_SAI=y", "INCLUDE_FIPS=y", "ENABLE_ASAN=n", "ENABLE_SYNCD_RPC=n",
         "CROSS_BUILD_ENVIRON=n", "MULTIARCH_QEMU_ENVIRON=n", "INSTALL_DEBUG_TOOLS=n",
         "SONIC_CONFIG_BUILD_JOBS=1", "SONIC_CONFIG_MAKE_JOBS=2",
         "SONIC_IMAGE_VERSION=bazel-ci", "SONIC_DPKG_CACHE_METHOD=none",
         "SONIC_DPKG_CACHE_METHOD_OVERRIDE=none", "SONIC_BAZEL_CACHE_POLICY=remote-only",
         "SONIC_ROOTFS_DOCKER_NETWORK=none"]
MAX_JSON = 2 * 1024**2
MAX_CAPTURE_LINE = 256 * 1024

def require(condition, message):
    if not condition:
        raise ValueError(message)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def relative(value):
    require(isinstance(value, str) and value and len(value) <= 1024 and
            not any(c in value for c in "\0\r\n") and
            str(PurePosixPath(value)) == value and not value.startswith("/") and
            not set(PurePosixPath(value).parts) & {"..", ".git"}, "Unsafe input path")
    return value

def safe(root, name):
    path = root
    for part in PurePosixPath(relative(name)).parts:
        path = path / part
        require(not path.is_symlink(), "Symlink in input path: " + name)
    require(path.is_file(), "Missing original input: " + name)
    return path

def bounded_data(path, expected_sha256=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_JSON, "Oversized or nonregular input")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_JSON + 1)
        after = os.fstat(fd)
        fields = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
        require(len(raw) == before.st_size and fields(before) == fields(after), "Input changed while reading")
        if expected_sha256 is not None:
            require(digest(raw) == expected_sha256, "Input differs from its independent hash")
        return raw
    finally:
        os.close(fd)

def data(path, expected_sha256=None):
    return strict_json(bounded_data(path, expected_sha256))


def make_input_capture(path, expected_sha256=None):
    """Read public Make data without granting package or assembly admission.

    Preserve the original bytes and literal scalar values. The separate
    configured-closure comparison must check completeness and actual inputs.
    """
    raw = bounded_data(path, expected_sha256)
    text = raw.decode("utf-8")
    require(text.endswith("\n") and all(c in "\n\t" or c.isprintable() for c in text),
            "Make capture has invalid control characters or termination")
    lines = text[:-1].split("\n")
    require(len(lines) >= 3 and lines[0] == "sonic-vs-make-inputs-v1"
            and lines[-1] == "end\tsonic-vs-make-inputs-v1", "Make capture framing differs")
    require(all(len(line.encode("utf-8")) <= MAX_CAPTURE_LINE for line in lines),
            "Make capture line exceeds its bound")
    schemas = {
        "variable": ("variables", ("value",)),
        "installer": ("installer", ("value",)),
        "deb": ("debs", ("depends", "wheel_depends", "rdepends", "phonies",
                         "cache_override", "cache_mode", "platform", "machine")),
        "wheel": ("wheels", ("depends", "debs_depends", "files", "cache_override", "cache_mode")),
        "rfs": ("rfs", ("dependent_rfs", "installer", "machine")),
        "file": ("files", ("path", "depends", "phonies")),
        "body-glob": ("body_globs", ("matches",)),
        "optional-input": ("optional_inputs", ("present",)),
    }
    installer_fields = {"DEPENDS", "INSTALLS", "LAZY_INSTALLS", "LAZY_BUILD_INSTALLS",
                        "PYTHON_WHEELS", "FILES", "RFS_DEPENDS", "MACHINE", "IMAGE_TYPE", "DOCKERS"}
    lists = {"depends", "wheel_depends", "rdepends", "phonies", "debs_depends",
             "files", "dependent_rfs", "matches"}
    result = {name: {} for name, fields in schemas.values()}
    extra_exports = None
    for line in lines[1:-1]:
        columns = line.split("\t")
        kind = columns[0]
        if kind == "extra-exports-present":
            require(len(columns) == 2 and columns[1] in ("y", "n") and extra_exports is None,
                    "Make capture extra-exports record differs or is duplicated")
            extra_exports = columns[1] == "y"
            continue
        require(kind in schemas, "Unknown Make capture row kind")
        name, fields = schemas[kind]
        require(len(columns) == len(fields) + 2, "Make capture row width differs")
        key = columns[1]
        require(re.fullmatch(r"[!-~]{1,1024}", key) is not None,
                "Make capture key is empty, oversized or ambiguous")
        require(key not in result[name], "Duplicate Make capture key")
        if kind == "variable":
            require(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) is not None,
                    "Make variable name differs")
            result[name][key] = columns[2]
        elif kind == "installer":
            require(key in installer_fields, "Unknown installer capture field")
            result[name][key] = columns[2]
        elif kind == "optional-input":
            require(columns[2] in ("present", "absent"), "Optional input presence differs")
            result[name][key] = columns[2] == "present"
        elif kind == "body-glob":
            result[name][key] = columns[2].split()
        else:
            result[name][key] = {field: value.split() if field in lists else value
                                 for field, value in zip(fields, columns[2:])}
    require(extra_exports is not None, "Make capture extra-exports record is missing")
    result.update(raw_bytes=raw, bytes=len(raw), sha256=digest(raw), extra_exports_present=extra_exports)
    return result


def file_digest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode), "Input is not a regular file")
        value = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024**2), b""):
                value.update(block)
        after = os.fstat(fd)
        fields = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        require(fields(before) == fields(after), "Input changed while hashing")
        return {"bytes": before.st_size, "sha256": value.hexdigest()}
    finally:
        os.close(fd)

def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("Nonfinite JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)

def make_path(path, *, directory=False):
    # These are future direct Make argv assignments, not shell snippets. Reject
    # Make expansion syntax and ambiguous filesystem spellings as well.
    path = Path(path)
    text = str(path)
    require(path.is_absolute() and re.fullmatch(r"/[A-Za-z0-9_./-]+", text)
            and ".." not in path.parts, "Unsafe Make workspace path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        require(not current.is_symlink(), "Symlink in Make workspace path")
    if directory:
        require(path.is_dir(), "Missing immutable workspace")
    return text

def kernel_arguments(root, value, args):
    name = relative(value["kernel_context"])
    require(name in value["groups"]["profile"], "Derived consumer kernel context is not hash-bound")
    context_path = safe(root, name)
    context_sha256 = value["files"][name]["sha256"]
    require(re.fullmatch("[0-9a-f]{64}", context_sha256)
            and digest(context_path.read_bytes()) == context_sha256,
            "Kernel consumer context differs from frozen inventory")
    context = data(context_path)
    require(context["producer"]["commit"] == KERNEL_PRODUCER
            and context["producer"]["registry"] == "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main"
            and context["consumer"]["commit"] == args.merge
            and context["consumer"]["registry"] == "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" + CATALOG
            and context["catalog"]["commit"] == CATALOG, "Original producer or consumer identity differs")
    for filename, expected in KERNEL_ORIGINALS.items():
        matches = [p for p in value["groups"]["kernel"] if Path(p).name == filename]
        require(len(matches) == 1 and value["files"][matches[0]]["sha256"] == expected,
                "Original kernel metadata hash differs")
    manifest_name = next(p for p in value["groups"]["kernel"] if Path(p).name == "kernel-packages.json")
    original = data(safe(root, manifest_name))
    packages = {str(Path(manifest_name).parent / item["name"]):
                {"bytes": item["size"], "sha256": item["sha256"]} for item in original["packages"]}
    debs = {p for p in value["groups"]["kernel"] if p.endswith(".deb")}
    require(len(packages) == 5 and set(packages) == debs
            and all(value["files"][p] == row for p, row in packages.items()),
            "Five DEBs differ from the original manifest")
    # Actual source tuples, catalog equivalence and all16 effective values are
    # still checked by the unchanged Make -> prepare.py verify path. Merely
    # forwarding these arguments is not kernel acceptance.
    return ["SONIC_REVIEW_REGISTRY=" + context["consumer"]["registry"],
            "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT=" + make_path(context_path),
            "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT_SHA256=" + context_sha256,
            "SONIC_BAZEL_KERNEL_PRODUCER_WORKSPACE=" + make_path(args.producer_workspace, directory=True),
            "SONIC_BAZEL_KERNEL_REGISTRY_WORKSPACE=" + make_path(args.registry_workspace, directory=True)]

def bazel_marker_archives(value):
    """Select only imported modern archives with separately admitted build evidence."""
    from tools.bazel.ci import vs_final23, vs_reuse_stage
    expected = {"target/" + name + ".gz" for name in (*vs_final23.PARENTS, *vs_final23.RUNTIMES)}
    expected.update("target/" + name + "-dbg.gz" for name in vs_final23.RUNTIMES
                    if name != vs_final23.ENGINE)
    proofs = vs_reuse_stage.bazel_builds(value)
    require(len(expected) == 42 and set(proofs) == expected
            and expected <= set(value["groups"]["archives"]),
            "Every modern archive requires its original Bazel build evidence")
    return sorted(expected)


def archive_paths(receipt, value):
    paths = {"target/" + name + ".gz" for name in receipt["archives"]} | set(receipt["legacy"])
    require(len(receipt["archives"]) == 42, "All42 final23 archives are required")
    selected = {row["path"] for row in receipt["selection"]["installer_images"] + receipt["selection"]["local_packages"]}
    require(selected <= paths, "Selected legacy archive omitted from verified receipt")
    require(paths == set(value["groups"]["archives"]), "Frozen archive closure differs from resolved Make selection")
    require(all(re.fullmatch(r"target/docker-[a-z0-9-]+\.gz", p) for p in paths), "Unsafe old-file target")
    require(all(receipt["files"][p] == value["files"][p] for p in paths), "Archive changed after input check")
    return sorted(paths)

def package_paths(value):
    """Validate explicit DEB admission in the independently pinned inventory.

    This does not infer source compatibility from a hash or create admission.
    The external complete Make-closure review must name every reused package.
    """
    names = value.get("admitted_debs")
    require(type(names) is list and names and all(type(p) is str for p in names)
            and len(names) == len(set(names)), "Explicit reviewed DEB list is required")
    expected = {p for group in ("packages", "kernel") for p in value["groups"][group]
                if p.endswith(".deb")}
    require(set(names) == expected, "Reviewed DEBs differ from the frozen package closure")
    require(all(re.fullmatch(r"target/debs/trixie/[A-Za-z0-9_+~.-]+\.deb", p)
                and p in value["files"] and value["files"][p]["bytes"] > 0 for p in names),
            "Only existing reviewed Trixie DEB inputs may be reused")
    return sorted(names)


def wheel_paths(value):
    """Reuse only explicit wheel inputs covered by the pinned closure review."""
    names = value.get("admitted_wheels")
    require(type(names) is list and all(type(p) is str for p in names)
            and len(names) == len(set(names)), "Explicit reviewed wheel list is required")
    expected = {p for p in value["groups"]["packages"] if p.endswith(".whl")}
    require(set(names) == expected, "Reviewed wheels differ from the frozen package closure")
    require(all(re.fullmatch(r"target/python-wheels/trixie/[A-Za-z0-9_+~.-]+\.whl", p)
                and p in value["files"] and value["files"][p]["bytes"] > 0 for p in names),
            "Only existing reviewed Trixie wheel inputs may be reused")
    return sorted(names)


def package_cache_overrides(debs, wheels):
    """Use the same explicit cache policy for capture and final assembly."""
    require(len(debs) == len(set(debs)) and
            all(re.fullmatch(r"target/debs/trixie/[A-Za-z0-9_+~.-]+\.deb", p) for p in debs),
            "Only explicitly admitted Trixie DEBs may use the package old-file list")
    require(len(wheels) == len(set(wheels)) and
            all(re.fullmatch(r"target/python-wheels/trixie/[A-Za-z0-9_+~.-]+\.whl", p) for p in wheels),
            "Only explicitly admitted Trixie wheels may use the wheel old-file list")
    return [Path(p).name + "_CACHE_OVERRIDE=none" for p in sorted([*debs, *wheels])]


def make_command(paths, kernel, *, debs=(), wheels=()):
    require(paths and len(paths) == len(set(paths)) and
            all(re.fullmatch(r"target/docker-[a-z0-9-]+\.gz", p) for p in paths),
            "Only admitted Docker archives may use the archive old-file list")
    old_files = sorted([*paths, *debs, *wheels])
    # Ignore only admitted file producers. Their distinct host installation
    # targets still traverse their wheel/DEB dependencies and check the files.
    # Never ignore .platform, *-install, boot checks or bazel-kernel-ensure.
    cache_overrides = package_cache_overrides(debs, wheels)
    return ["make", "-f", "slave.mk", "--jobs=1", *["--old-file=" + p for p in old_files],
            "target/sonic-vs.bin", *FLAGS, *cache_overrides, *kernel]

def save_capture(path, value):
    make_path(path)
    require(path.parent.is_dir(), "Receipt parent must already exist")
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    require(len(raw) <= MAX_JSON, "Oversized archive capture")
    with path.open("xb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    return digest(raw)

def verify_after(args):
    root, value = inputs(args)
    require(args.receipt is not None and re.fullmatch(r"[0-9a-f]{64}", args.receipt_sha256 or ""),
            "Original archive capture and its independent hash are required")
    make_path(args.receipt)
    capture = data(args.receipt, args.receipt_sha256)
    require(capture.get("schema") == 1 and capture.get("kind") == "sonic-final23-preassembly-capture"
            and capture["source"] == value["source"] and capture.get("assembly") == assembly_domain(value, tested_merge=args.merge, tested_tree=args.source_tree)
            and capture["manifest_sha256"] == args.manifest_sha256,
            "Original preassembly capture identity differs")
    sys.path.insert(0, str(root))
    from tools.bazel.ci import vs_final23
    verified = vs_final23.verify(root, capture["archive_receipt"])
    archive_paths(verified, value)
    installer = file_digest(safe(root, "target/sonic-vs.bin"))
    require(installer["bytes"] > 0, "Installer output is empty")
    return {"status": "ARCHIVES_UNCHANGED_INSTALLER_PRESENT_ONLY", "source": value["source"],
            "assembly": assembly_domain(value, tested_merge=args.merge, tested_tree=args.source_tree),
            "installer": installer, "original_capture_sha256": args.receipt_sha256,
            "scope": "Read-only archive verification; no overwrite of the preassembly receipt. Installed-image/runtime, owner CI and complete VS acceptance remain separate."}

def source_domain(args):
    expected = {"head": args.source_head, "tree": args.source_tree,
                "merge": args.merge, "controller": WORKER, "catalog": CATALOG, "pr": 206}
    require(all(isinstance(expected[key], str) and re.fullmatch("[0-9a-f]{40}", expected[key])
                for key in ("head", "tree", "merge")), "Missing immutable workflow source context")
    return expected


def assembly_domain(value, *, tested_merge, tested_tree):
    require(all(isinstance(item, str) and re.fullmatch("[0-9a-f]{40}", item)
                for item in (tested_merge, tested_tree)), "Missing immutable assembly source context")
    expected = {"commit": tested_merge, "tree": tested_tree}
    require(type(value.get("assembly")) is dict and value["assembly"] == expected,
            "Actual assembly source domain differs from workflow context")
    return expected


def required_make_packages(capture, admitted_debs, admitted_wheels):
    """Compare configured package reads with independently admitted originals.

    This covers packages and host install dependencies, including recipe-body
    reads absent from direct Make prerequisites. It deliberately leaves source
    files, selected RFS recipes and other host hooks for the assembly reviewer;
    it does not authorize assembly or execute Make.
    """
    variables, installer = capture["variables"], capture["installer"]
    deb_rows, wheel_rows = capture["debs"], capture["wheels"]
    def words(name):
        require(name in variables, "Required Make variable is missing: " + name)
        return variables[name].split()
    expected_flags = dict(flag.split("=", 1) for flag in FLAGS
                          if flag.split("=", 1)[0] not in {"SONIC_CONFIG_BUILD_JOBS",
                              "SONIC_CONFIG_MAKE_JOBS", "SONIC_BAZEL_CACHE_POLICY"})
    expected_flags.update(CONFIGURED_ARCH="amd64", CONFIGURED_PLATFORM="vs",
                          BLDENV="trixie", TARGET_PATH="target",
                          IMAGE_DISTRO_DEBS_PATH="target/debs/trixie",
                          DEBS_PATH="target/debs/trixie",
                          PYTHON_WHEELS_PATH="target/python-wheels/trixie")
    require(all(variables.get(k) == v for k, v in expected_flags.items()),
            "Configured Make package profile differs")
    deb_registry = ("SONIC_ONLINE_DEBS", "SONIC_COPY_DEBS", "SONIC_MAKE_DEBS",
                    "SONIC_DPKG_DEBS", "SONIC_PYTHON_STDEB_DEBS",
                    "SONIC_DERIVED_DEBS", "SONIC_EXTRA_DEBS", "SONIC_BAZEL_KERNEL_DEBS")
    require(set(deb_rows) == {n for k in deb_registry for n in words(k)},
            "Configured DEB registration capture is incomplete")
    require(set(wheel_rows) == set(words("SONIC_PYTHON_WHEELS")),
            "Configured wheel registration capture is incomplete")
    require(set(capture["rfs"]) == set(words("SONIC_RFS_TARGETS")),
            "Configured RFS registration capture is incomplete")
    file_registry = ("SONIC_COPY_FILES", "SONIC_ONLINE_FILES", "SONIC_MAKE_FILES")
    require(set(capture["files"]) == {n for k in file_registry for n in words(k)},
            "Configured file registration capture is incomplete")
    require(set(installer) == {"DEPENDS", "INSTALLS", "LAZY_INSTALLS", "LAZY_BUILD_INSTALLS",
                              "PYTHON_WHEELS", "FILES", "RFS_DEPENDS", "MACHINE",
                              "IMAGE_TYPE", "DOCKERS"}, "Installer capture is incomplete")
    require(installer["MACHINE"] == "vs" and installer["IMAGE_TYPE"] == "onie",
            "Expected the VS ONIE binary installer")

    required_debs, required_wheels = set(), set()
    host_debs, host_wheels, selected_files, selected_rfs = set(), set(), set(), set()
    host_phonies = set()
    def package_name(name, suffix):
        require(re.fullmatch(r"[A-Za-z0-9_+~.-]+" + re.escape(suffix), name) is not None,
                "Invalid configured package name")
    def deb(name, install=False):
        package_name(name, ".deb")
        require(name in deb_rows, "Selected DEB has no configured registration: " + name)
        require(re.search(r"_(amd64|all)\.deb$", name) is not None,
                "Selected DEB is outside the AMD64 package scope")
        required_debs.add(name)
        if not install or name in host_debs:
            return
        host_debs.add(name)
        row = deb_rows[name]
        host_phonies.update(row["phonies"])
        for item in row["depends"]:
            deb(item, install=True)
        for item in row["wheel_depends"]:
            wheel(item, install=True)
    def wheel(name, install=False):
        package_name(name, ".whl")
        require(name in wheel_rows, "Selected wheel has no configured registration: " + name)
        required_wheels.add(name)
        if not install or name in host_wheels:
            return
        host_wheels.add(name)
        for item in wheel_rows[name]["depends"]:
            wheel(item, install=True)
        for item in wheel_rows[name]["debs_depends"]:
            deb(item, install=True)
    def file_input(name):
        require(name in capture["files"], "Selected file has no configured registration: " + name)
        selected_files.add(name)
        if name in words("SONIC_MAKE_FILES"):
            row = capture["files"][name]
            host_phonies.update(row["phonies"])
            for item in row["depends"]:
                deb(item, install=True)
    def rfs(name):
        if name in selected_rfs:
            return
        require(name in capture["rfs"], "Selected RFS has no configured registration: " + name)
        row = capture["rfs"][name]
        require(row["installer"] == "sonic-vs.bin" and row["machine"] == installer["MACHINE"],
                "Dependent RFS requires a separate installer closure review")
        selected_rfs.add(name)
        for item in row["dependent_rfs"]:
            rfs(item)
        for key in ("INITRAMFS_TOOLS", "LINUX_KERNEL"):
            for item in words(key):
                deb(item)

    for key in ("INSTALLS", "LAZY_INSTALLS", "LAZY_BUILD_INSTALLS"):
        for name in installer[key].split():
            deb(name)
    for name in installer["DEPENDS"].split():
        deb(name, install=True)
    for name in installer["PYTHON_WHEELS"].split():
        wheel(name)
    direct_debs = (
        "INITRAMFS_TOOLS", "LINUX_KERNEL", "LINUX_KBUILD", "SONIC_DEVICE_DATA",
        "IFUPDOWN2", "MAKEDUMPFILE", "KDUMP_TOOLS", "LIBPAM_RADIUS", "LIBNSS_RADIUS",
        "LIBPAM_TACPLUS", "LIBNSS_TACPLUS", "MONIT", "OPENSSH_SERVER",
        "PYTHON3_SWSSCOMMON", "SONIC_DB_CLI", "SONIC_NETTOOLS", "SONIC_RSYSLOG_PLUGIN",
        "SONIC_UTILITIES_DATA", "SONIC_CTRMGRD_RS", "SONIC_HOST_SERVICES_RS",
        "SONIC_HOST_SERVICES_DATA", "SOCAT", "BASH", "BASH_TACPLUS", "AUDISP_TACPLUS",
        "SYSLOG_COUNTER", "SEDUTIL", "SONIC_INSTALLER_EXTRA_DEBS", "RASDAEMON")
    for key in direct_debs:
        for name in words(key):
            deb(name)
    for name in words("LINUX_KBUILD"):
        deb(name, install=True)
    if "y" in variables["ENABLE_ZTP"]:
        for name in words("SONIC_ZTP"):
            deb(name)
    if "y" in variables["INCLUDE_FIPS"]:
        for key in ("SYMCRYPT_OPENSSL", "FIPS_BASEIMAGE_INSTALLERS",
                    "FIPS_BASEIMAGE_PYTHON_INSTALLERS"):
            for name in words(key):
                deb(name)
    direct_wheels = (
        "SONIC_UTILITIES_PY3", "SONIC_PY_COMMON_PY2", "SONIC_PY_COMMON_PY3",
        "SONIC_CONFIG_ENGINE_PY2", "SONIC_CONFIG_ENGINE_PY3", "SONIC_PLATFORM_COMMON_PY3",
        "REDIS_DUMP_LOAD_PY2", "REDIS_DUMP_LOAD_PY3",
        "SONIC_PLATFORM_API_PY2", "SONIC_PLATFORM_API_PY3", "SONIC_YANG_MODELS_PY3",
        "SONIC_CTRMGRD", "SONIC_YANG_MGMT_PY3", "SYSTEM_HEALTH", "SONIC_HOST_SERVICES_PY3")
    for key in direct_wheels:
        for name in words(key):
            wheel(name)
    if "y" in variables["PDDF_SUPPORT"]:
        for key in ("PDDF_PLATFORM_API_BASE_PY2", "PDDF_PLATFORM_API_BASE_PY3"):
            for name in words(key):
                wheel(name)
    for name in installer["FILES"].split():
        file_input(name)
    for name in words("SONIC_CTRMGRD"):
        for item in wheel_rows[name]["files"]:
            file_input(item)
    for name in installer["RFS_DEPENDS"].split():
        rfs(name)

    # The extension reads SWSS runtime siblings and Redis PY3 even though the
    # direct installer prerequisite list alone does not describe those reads.
    for key in ("LIBSWSSCOMMON", "PYTHON_SWSSCOMMON", "PYTHON3_SWSSCOMMON"):
        for name in words(key):
            deb(name)
    for name in words("LIBSWSSCOMMON"):
        for item in deb_rows[name]["rdepends"]:
            deb(item)
    ssh_version = variables["OPENSSH_VERSION_FULL"]
    require(re.fullmatch(r"[A-Za-z0-9_+~.-]+", ssh_version) is not None,
            "Unsupported configured OpenSSH filename version")
    for stem in ("openssh-server", "openssh-client", "openssh-sftp-server"):
        deb(stem + "_" + ssh_version + "_amd64.deb")

    # Compare every captured match, including excluded kernel debug matches,
    # with the actual admitted directory contents; do not accept one good match
    # while an unreviewed sibling would also be consumed by a shell wildcard.
    import fnmatch
    kernel_pattern = "linux-image-" + variables["LINUX_KERNEL_VERSION"] + "-*_amd64.deb"
    patterns = set(['audisp-tacplus_*.deb', 'bash-tacplus_*.deb', 'bash_*.deb', 'ifupdown2_*.deb', 'kdump-tools_*.deb', 'libnss-radius_*.deb', 'libnss-tacplus_*.deb', 'libpam-radius-auth_*.deb', 'libpam-tacplus_*.deb', 'libtac2_*.deb', 'libyang3_*.deb', 'makedumpfile_*.deb', 'monit_*.deb', 'python3-libyang_*.deb', 'rasdaemon_*.deb', 'sedutil_*.deb', 'sonic-ctrmgrd-rs_*.deb', 'sonic-db-cli_*.deb', 'sonic-device-data_*.deb', 'sonic-host-services-data_*.deb', 'sonic-host-services-rs_*.deb', 'sonic-nettools_*.deb', 'sonic-rsyslog-plugin_*.deb', 'sonic-utilities-data_*.deb', 'sonic-ztp_*.deb', 'syslog-counter_*.deb', 'initramfs-tools-core_*.deb', 'initramfs-tools_*.deb'])
    patterns.update(stem + "_" + ssh_version + "_*.deb"
                    for stem in ("openssh-server", "openssh-client", "openssh-sftp-server"))
    patterns.add(kernel_pattern)
    require(set(capture["body_globs"]) == patterns, "Recipe-body glob capture is incomplete")
    admitted_debs, admitted_wheels = set(admitted_debs), set(admitted_wheels)
    require({"ENABLE_ZTP", "INCLUDE_SYSTEM_EVENTD", "BUILD_REDUCE_IMAGE_SIZE"} <= variables.keys(),
            "Recipe-body condition capture is incomplete")
    for pattern in sorted(patterns):
        if pattern == "sonic-ztp_*.deb" and variables["ENABLE_ZTP"] != "y":
            continue
        if pattern == "sonic-rsyslog-plugin_*.deb" and not (
                variables["INCLUDE_SYSTEM_EVENTD"] == "y" and variables["BUILD_REDUCE_IMAGE_SIZE"] != "y"):
            continue
        actual = capture["body_globs"][pattern]
        expected = {p for p in admitted_debs
                    if str(PurePosixPath(p).parent) == variables["IMAGE_DISTRO_DEBS_PATH"]
                    and fnmatch.fnmatchcase(PurePosixPath(p).name, pattern)}
        require(len(actual) == len(set(actual)) and set(actual) == expected,
                "Recipe-body glob differs from admitted originals: " + pattern)
        consumed = [p for p in actual if pattern != kernel_pattern or not any(
            fnmatch.fnmatchcase(PurePosixPath(p).name, excluded)
            for excluded in ("*-dbg_*", "*-dbgsym_*"))]
        require(consumed, "Required recipe-body package is absent: " + pattern)
        for path in consumed:
            deb(PurePosixPath(path).name)

    deb_paths = {variables["IMAGE_DISTRO_DEBS_PATH"] + "/" + name for name in required_debs}
    wheel_paths = {variables["PYTHON_WHEELS_PATH"] + "/" + name for name in required_wheels}
    require(deb_paths <= admitted_debs,
            "Configured DEBs are not admitted: " + ", ".join(sorted(deb_paths - admitted_debs)))
    require(wheel_paths <= admitted_wheels,
            "Configured wheels are not admitted: " + ", ".join(sorted(wheel_paths - admitted_wheels)))
    for names, rows in ((required_debs, deb_rows), (required_wheels, wheel_rows)):
        require(all(rows[name]["cache_override"] in ("", "none") for name in names),
                "Selected package enables a Make cache override")
    return {
        "required_debs": sorted(deb_paths), "required_wheels": sorted(wheel_paths),
        "host_deb_installs": sorted(host_debs), "host_wheel_installs": sorted(host_wheels),
        "selected_files": sorted(selected_files), "selected_rfs": sorted(selected_rfs),
        "host_phonies_requiring_review": sorted(host_phonies),
        "scope": "Package closure only; source files, RFS recipes, host hooks and daemon qualification remain required.",
    }



# These are the reviewed recipe bodies in the existing assembly domain, plus
# the tracked inert organization hook. They are source checks, not receipts for
# package construction or native image validation.
REVIEWED_MAKE_SOURCES = {'Makefile': 'e868b2ba04a905e26bc1768ed1ae597ca8137af77c416f4b0440ad10444b7826', 'Makefile.cache': 'efb6ebf31c36f557560852243e179cd9f1d89387da42340f70ed7a3eeac8fb97', 'Makefile.work': '089b017d2fd512e455215c89bccb496403ab9c73b88a9e79f29d8ddb650d0a5c', 'build_debian.sh': '467c9c2f4c00e795979f427eb5c945ed9995f2d37f093ef2b324c67ee9784bf4', 'build_image.sh': 'b798b1999550a26e532a510ab92038c53ac615f527a1e95c04cdeeee37717830', 'files/build_templates/organization_extensions.sh': '3df60a480aefbb2bea60326c787b2b1fe43dee1a6ed0c0d3ec608f25c13f3e25', 'files/build_templates/sonic_debian_extension.j2': 'd9fe0ce6515d60fafd401da7d1d76612622667520dc3f6699db800be538aa9e0', 'platform/vs/one-image.mk': '1305cdf2f07cf31aff9bd73ce0a702de8a35b8aa6fca1a9e1a3affbd73376554', 'platform/vs/platform-modules-vs.mk': 'f18a736a9546a6d62f7cf17159b32d86a2f5a4dc5c9442f22c0d964d4d4da28d', 'platform/vs/rules.mk': '5e7148c5a0cf6b489da19f884c9e8e452d2f674dde12c7f4c5abefeeb90277f8', 'rules/config': '06481ef828ab6e0494c8593f299904ea7e3bbdd9a3b028905726b8ce6dea3d2f', 'rules/functions': 'cf558fd455e0f3247f7d55c8b6b874d7e14213258c730fc395b114e47b0b974b', 'rules/sonic-ctrmgrd.mk': 'd9ba237eef43bb70d5b0010c46113cc82601e3c7651b736dd0dff387fd26b256', 'slave.mk': '854ddbe0695a40027996659b6f2306c7fbc037ab5a35bb5335a5acfc886b2efa', 'tools/bazel/docker.mk': 'f27a44d0d7b62bbaeff0d9ba4e4ad87fe9ffa627e81d9ebef2208896c6ed421c'}
CTR_COPY_SOURCES = {'container': 'fae5a5b772431b757f713d71992a3a878c56e599ec7b6d43c457335be85088bc', 'container_startup.py': '0e4f444b01d6b836903f76ab69d68575c4cedad185559c61f91fe899b7c5c7a4', 'ctrmgrd.service': '62b4e93475823ee1ff69293056130eee04fe78999b7e9f61291bbdd267d3c54b', 'readiness_probe.sh': 'f7c281688c3a198b20d76a10cdd3d24c9c98c835fac2efe5aed6d84f649ce842', 'remote_ctr.config.json': '8ebaec29f6e1149d6544da52919c24286f044706ddab0408cc7cacc2bbc6ce85'}

def configured_source_closure(root, capture, packages, kernel_packages):
    """Verify the selected copy/RFS/host-hook closure before returning build argv."""
    variables = capture["variables"]
    require(not capture["extra_exports_present"], "Extra recipe exports require review")
    require(variables.get("SECURE_UPGRADE_MODE") == '"no_sign"'
            and variables.get("ENABLE_ORGANIZATION_EXTENSIONS") == "y"
            and variables.get("SONIC_PACKAGES") == "",
            "Signing, organization or remote-package configuration differs")
    optional = {
        "rules/config.organization": False,
        "files/build_templates/build_debian.organization.sh": False,
        "files/build_templates/organization_extensions.sh": True,
        "files/build_templates/sonic_debian_extension_organization_packages.j2": False,
        "files/build_templates/sonic_debian_extension_organization_docker_images.j2": False,
    }
    require(capture["optional_inputs"] == optional, "Optional hook declarations differ")
    for name, present in optional.items():
        candidate = root / name
        require(not any(part.is_symlink() for part in (candidate, *candidate.parents)),
                "Symlink in optional hook path")
        require(os.path.lexists(candidate) == present, "Optional hook presence differs: " + name)
    sources = {}
    for name, expected in REVIEWED_MAKE_SOURCES.items():
        record = file_digest(safe(root, name))
        require(record["sha256"] == expected, "Reviewed Make source changed: " + name)
        sources[name] = record

    require(set(packages["selected_files"]) == set(CTR_COPY_SOURCES),
            "Selected source files differ from the five ctrmgr copies")
    require(variables["FILES_PATH"] == "target/files/trixie",
            "Configured source-copy output directory differs")
    copy_files = set(variables["SONIC_COPY_FILES"].split())
    other_files = set((variables["SONIC_ONLINE_FILES"] + " " + variables["SONIC_MAKE_FILES"]).split())
    for name, expected in CTR_COPY_SOURCES.items():
        require(name in copy_files and name not in other_files,
                "Selected payload is not a source-copy rule")
        require(capture["files"][name] == {
            "path": "src/sonic-ctrmgrd/ctrmgr", "depends": [], "phonies": []},
            "Selected source-copy declaration differs")
        source = "src/sonic-ctrmgrd/ctrmgr/" + name
        record = file_digest(safe(root, source))
        require(record["sha256"] == expected, "Selected ctrmgr source changed: " + name)
        sources[source] = record
        output = root / variables["FILES_PATH"] / name
        if os.path.lexists(output):
            require(file_digest(safe(root, str(output.relative_to(root)))) == record,
                    "Existing copied payload differs from its reviewed source")

    selected_rfs = "sonic-vs.bin__vs__rfs.squashfs"
    require(packages["selected_rfs"] == [selected_rfs]
            and capture["rfs"][selected_rfs] == {
                "dependent_rfs": [], "installer": "sonic-vs.bin", "machine": "vs"},
            "Selected VS rootfs recipe differs")
    require(not packages["host_phonies_requiring_review"]
            and not packages["host_wheel_installs"],
            "Additional host installation hooks require review")
    kbuild = variables["LINUX_KBUILD"].split()
    require(len(kbuild) == 1, "Expected one configured host kbuild package")
    common_headers = capture["debs"][kbuild[0]]["depends"]
    require(len(common_headers) == 1
            and common_headers[0].startswith("linux-headers-")
            and "-common-sonic_" in common_headers[0] and common_headers[0].endswith("_all.deb"),
            "Expected the observed common-headers dependency")
    host = set(kbuild + common_headers)
    require(set(packages["host_deb_installs"]) == host
            and host <= {PurePosixPath(name).name for name in kernel_packages},
            "Host installation differs from observed kbuild/common headers")
    return {"sources": sources, "selected_files": packages["selected_files"],
            "selected_rfs": packages["selected_rfs"],
            "host_deb_installs": packages["host_deb_installs"],
            "organization_hook": "exact reviewed inert source",
            "scope": "Configured source/copy/RFS/host-hook closure only; native, kernel and runtime checks remain separate."}


def inventory(args):
    root = args.workspace.resolve()
    require(args.manifest.is_file() and not args.manifest.is_symlink() and
            re.fullmatch("[0-9a-f]{64}", args.manifest_sha256), "Invalid frozen inventory")
    require(args.manifest.stat().st_size <= MAX_JSON, "Oversized frozen inventory")
    raw = args.manifest.read_bytes()
    require(len(raw) <= MAX_JSON and digest(raw) == args.manifest_sha256,
            "Inventory differs from independently reviewed original inputs")
    value = strict_json(raw)
    require(type(value.get("schema")) is int and value["schema"] == 1 and value.get("kind") == "sonic-final23-reuse-input-inventory",
            "Unsupported inventory; this is not a replacement producer receipt")
    source = value["source"]
    expected_source = source_domain(args)
    require(type(source) is dict and all(source.get(key) == expected
            for key, expected in expected_source.items()), "Immutable workflow/native source domain differs")
    assembly_domain(value, tested_merge=args.merge, tested_tree=args.source_tree)
    environment = value["build_environment"]
    require(set(environment) == {"BUILD_NUMBER", "BUILD_TIMESTAMP", "SOURCE_DATE_EPOCH", "BUILD_HOSTNAME", "SONIC_IMAGE_VERSION"},
            "Original Make image identity is incomplete")
    require(all(isinstance(v, str) and v and len(v) <= 256 and not any(c in v for c in "\0\r\n")
                for v in environment.values()), "Invalid original image identity")
    require(environment["SONIC_IMAGE_VERSION"] == "bazel-ci", "Current archive image version must be bazel-ci")
    if args.require_environment:
        require(all(os.environ.get(k) == v for k, v in environment.items()), "Make image identity differs from frozen inputs")
    rows = value["files"]
    require(isinstance(rows, dict) and 0 < len(rows) <= 20000, "Invalid frozen input inventory")
    for name, row in rows.items():
        require(set(row) == {"bytes", "sha256"} and type(row["bytes"]) is int and row["bytes"] >= 0
                and re.fullmatch("[0-9a-f]{64}", row["sha256"]), "Invalid original file identity")
        relative(name)
    return root, value


def inputs(args):
    root, value = inventory(args)
    rows = value["files"]
    for name, row in rows.items():
        require(file_digest(safe(root, name)) == row, "Original input differs: " + name)
    groups = value["groups"]
    require(set(groups) == {"archives", "kernel", "packages", "native", "profile", "source"},
            "Incomplete input categories")
    for names in groups.values():
        require(isinstance(names, list) and names and len(names) == len(set(names)) and
                set(names) <= rows.keys(), "Missing or duplicate original input group")
    kernel = groups["kernel"]
    require(len(kernel) == 7 and sum(n.endswith(".deb") for n in kernel) == 5 and
            {Path(n).name for n in kernel if n.endswith(".json")} ==
            {"kernel-packages.json", "kernel-provenance.json"}, "Five original kernel DEBs and both records required")
    require(set(value["native"]) == set(FAMILIES), "All three206 worker domains required")
    for family, item in value["native"].items():
        require(set(item) == {"request", "provenance", "acceptance"}, "Unexpected worker evidence pointer")
        for key in ("request", "provenance"):
            require(item[key] in groups["native"], "Worker original is not hash-bound")
        prefix = relative(item["acceptance"])
        directory = root
        for part in PurePosixPath(prefix).parts:
            directory = directory / part
            require(not directory.is_symlink(), "Symlink in acceptance path")
        require(directory.is_dir(), "Missing original acceptance directory")
        members = list(directory.iterdir())
        require(members and len(members) <= 100 and
                all(p.is_file() and not p.is_symlink() and
                    prefix + "/" + p.name in groups["native"] for p in members),
                "Every original acceptance member must be hash-bound")
    # The actual Make capture is created after these original inputs are
    # materialized. plan() requires its independent hash and compares its full
    # package/source graph; a pre-existing boolean review is not admission.
    return root, value

def request_provenance(provenance, original_request):
    """Bind original request bytes without changing supplied provenance."""
    require(type(provenance) is dict, "Invalid original worker provenance")
    request_sha256 = digest(original_request)
    bound = dict(provenance)
    if "request_sha256" in bound:
        require(bound["request_sha256"] == request_sha256,
                "Supplied provenance request hash differs from original request bytes")
    else:
        bound["request_sha256"] = request_sha256
    return bound

def verify_native_inputs(root, value, args):
    """Reuse canonical native admission before any frontend host preparation."""
    # The caller binds this exact frozen assembly import closure first.
    sys.path.insert(0, str(root))
    from tools.bazel.ci import image_worker_receipt as worker
    source_domain(args)
    common = None
    workers = {}
    for family, item in value["native"].items():
        request_path = safe(root, item["request"])
        request = worker.validate_request(data(request_path))
        require(worker.image(request) == family and request["pr"] == 206 and request["head"] == args.source_head
                and request["merge"] == args.merge and request["tree"] == args.source_tree and request["controller"] == WORKER
                and request["catalog"] == CATALOG, "Worker output belongs to another source domain")
        identity = {k: request[k] for k in ("repository", "workflow",
                    "pr", "head", "base", "merge", "tree", "catalog", "controller")}
        require(common is None or common == identity, "Worker requests describe different source trees")
        common = identity
        contract_path = "tools/bazel/ci/" + family + "_worker_contract.json"
        require(contract_path in value["groups"]["source"], "Checked-in contract is not bound")
        contract = data(safe(root, contract_path))
        provenance = request_provenance(data(safe(root, item["provenance"])),
                                        request_path.read_bytes())
        acceptance = root / relative(item["acceptance"])
        workers[family] = worker.verify_evidence(acceptance, request, provenance, contract)
        # Full-payload files must separately match the same accepted public receipt.
        public = data(safe(root, item["acceptance"] + "/public-receipt.json"))
        for name in ("docker-" + ("sonic-p4rt" if family == "p4rt" else
                                 "gbsyncd-vs" if family == "gearbox" else "syncd-vs") + suffix + ".gz"
                     for suffix in ("", "-dbg")):
            require(value["files"].get("target/" + name) ==
                    {k: public["artifacts"][name][k] for k in ("bytes", "sha256")},
                    "Full image bytes differ from the original accepted worker receipt")
    return common, workers


def plan(args):
    root, value = inputs(args)
    common, workers = verify_native_inputs(root, value, args)
    from tools.bazel.ci import vs_final23
    bazel_marker_archives(value)
    receipt = vs_final23.capture(root, installer="sonic-vs.bin", debug=False)
    vs_final23.verify(root, receipt)
    paths = archive_paths(receipt, value)
    debs = package_paths(value)
    wheels = wheel_paths(value)
    kernel = kernel_arguments(root, value, args)
    expected_capture = safe(root, "target/sonic-vs.bin.make-inputs.tsv")
    require(args.make_inputs == expected_capture
            and re.fullmatch(r"[0-9a-f]{64}", args.make_inputs_sha256 or ""),
            "The current configured Make capture and its independent hash are required")
    configured = make_input_capture(expected_capture, args.make_inputs_sha256)
    packages = required_make_packages(configured, debs, wheels)
    source_closure = configured_source_closure(root, configured, packages, value["groups"]["kernel"])
    require(args.receipt is not None, "An exclusive output path for the original archive capture is required")
    capture_sha = save_capture(args.receipt, {"schema": 1, "kind": "sonic-final23-preassembly-capture",
        "source": value["source"], "assembly": assembly_domain(value, tested_merge=args.merge, tested_tree=args.source_tree),
        "manifest_sha256": args.manifest_sha256, "archive_receipt": receipt,
        "make_inputs_sha256": args.make_inputs_sha256, "configured_packages": packages,
        "configured_sources": source_closure})
    post = [sys.executable, "-B", str(Path(__file__).resolve()), "verify",
            "--workspace", str(root), "--manifest", str(args.manifest),
            "--manifest-sha256", args.manifest_sha256, "--merge", args.merge,
            "--source-head", args.source_head, "--source-tree", args.source_tree,
            "--receipt", str(args.receipt), "--receipt-sha256", capture_sha]
    return {"status": "CONFIGURED_INPUTS_VERIFIED_COMMANDS_ONLY", "source": common, "workers": workers,
            "assembly": assembly_domain(value, tested_merge=args.merge, tested_tree=args.source_tree),
            "original_archive_capture": {"path": str(args.receipt), "sha256": capture_sha},
            "build_argv": make_command(paths, kernel, debs=debs, wheels=wheels), "postbuild_archive_check_argv": post,
            "configured_packages": packages, "configured_sources": source_closure,
            "kernel_verify_argv": ["make", "-f", "slave.mk", "--jobs=1", "bazel-kernel-ensure",
                                   *FLAGS, *package_cache_overrides(debs, wheels), *kernel],
            "admitted_deb_inputs": {p: value["files"][p] for p in debs},
            "admitted_wheel_inputs": {p: value["files"][p] for p in wheels},
            "required_before_assembly": ["current-job VS-specific private-daemon/cgroup/tool qualification",
                                         "canonical kernel prepare.py verify with all16 actual Make fields",
                                         "clean assembly revision/tree/submodules before preparation",
                                         "installer daemon confinement and owned cleanup"],
            "required_after_archive_check": ["authenticated current owner/native validation",
                                             "existing installed-image and installer/version validation with matching policies"],
            "scope": "This planner saves one original archive capture and never executes a child, stages a payload or authorizes assembly. Build and post-build checks are separate; no legacy verify_vs_oci.sh or DEB fallback."}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inputs", "admission", "environment", "metadata", "plan", "verify"))
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--merge", required=True, help="Canonical tested merge commit checked out for assembly")
    parser.add_argument("--source-head", required=True, help="Requested PR head, distinct from the tested merge")
    parser.add_argument("--source-tree", required=True, help="Tree of the canonical tested merge")
    parser.add_argument("--require-environment", action="store_true")
    parser.add_argument("--producer-workspace", type=Path)
    parser.add_argument("--registry-workspace", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--make-inputs", type=Path)
    parser.add_argument("--make-inputs-sha256")
    parser.add_argument("--json-environment", action="store_true")
    args = parser.parse_args()
    if args.mode in {"metadata", "plan"} and (args.producer_workspace is None or args.registry_workspace is None):
        parser.error("metadata/plan require separate original producer and registry workspaces")
    if args.mode == "admission":
        root, value = inputs(args)
        common, workers = verify_native_inputs(root, value, args)
        print(json.dumps({"status": "NATIVE_INPUTS_VERIFIED_BEFORE_PREPARATION_ONLY",
                          "source": common, "workers": workers, "assembly": assembly_domain(value, tested_merge=args.merge, tested_tree=args.source_tree),
                          "scope": "Canonical native inputs only. No preparation, configured Make selection, "
                                   "kernel verification, package admission or assembly authorization."},
                         indent=2, sort_keys=True))
        return
    if args.mode == "environment":
        environment = dict(inputs(args)[1]["build_environment"])
        # Preparation runs host-hook Make before the assembly argv exists.
        # Export the same policies early without changing original image identity.
        environment.update(item.split("=", 1) for item in FLAGS
                           if item.startswith(("SONIC_DEB_BUILD_POLICY=", "SONIC_BAZEL_KERNEL_PREPARE=",
                                               "SONIC_DPKG_CACHE_METHOD=", "SONIC_DPKG_CACHE_METHOD_OVERRIDE=",
                                               "SONIC_BAZEL_CACHE_POLICY=", "SONIC_ROOTFS_DOCKER_NETWORK=")))
        if args.json_environment:
            print(json.dumps(environment, sort_keys=True))
        else:
            for key, value in environment.items():
                print("export " + key + "=" + shlex.quote(value))
        return
    if args.mode == "metadata":
        root, value = inputs(args)
        kernel = kernel_arguments(root, value, args)
        markers = "VS_REUSE_BAZEL_ARCHIVES=" + " ".join(bazel_marker_archives(value))
        print(json.dumps({"status": "METADATA_COMMAND_ONLY", "argv": ["make", "--jobs=1", "-f", "slave.mk",
              "-f", str(Path(__file__).with_name("vs_reuse_selection.mk")), "vs-reuse-selection", *FLAGS, markers,
              *package_cache_overrides(package_paths(value), wheel_paths(value)), *kernel],
              "scope": "Run only after admitted worker preparation: reviewed assembly slave.mk parsing has host setup side effects."}, indent=2))
        return
    if args.mode == "verify":
        result = verify_after(args)
    elif args.mode == "plan":
        result = plan(args)
    else:
        root, value = inputs(args)
        result = {
            "status": "FROZEN_INPUT_FILES_PRESENT_ONLY", "source": value["source"],
            "assembly": assembly_domain(value, tested_merge=args.merge, tested_tree=args.source_tree),
            "scope": "File presence/identity only; no VS qualification or kernel/source compatibility claim."}
    print(json.dumps(result, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
