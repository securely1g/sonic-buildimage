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

HEAD = "cf07bf776521e28063cb316713355391b55e6670"
TREE = "de00df24cafc9fb5b2ba260a24c451014441a83a"
WORKER = "87b459c50a4ee8c90679a1b3756a5bb724c6d765"
CATALOG = "e6ef022c94dd2fdeeac364dfd591f9701cd37c29"
KERNEL_CONTEXT = "8f7c8568ad85fc16360844bc6271f053627771a4b81a881c1899e891735024f3"
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
         "SONIC_IMAGE_VERSION=bazel-ci", "SONIC_DPKG_CACHE_METHOD=rcache"]
MAX_JSON = 2 * 1024**2

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

def data(path, expected_sha256=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_JSON, "Oversized or nonregular JSON input")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_JSON + 1)
        after = os.fstat(fd)
        fields = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
        require(len(raw) == before.st_size and fields(before) == fields(after), "JSON changed while reading")
        if expected_sha256 is not None:
            require(digest(raw) == expected_sha256, "JSON differs from its independent hash")
        return strict_json(raw)
    finally:
        os.close(fd)

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
    require(name in value["groups"]["profile"], "Original kernel context is not hash-bound")
    context_path = safe(root, name)
    require(value["files"][name]["sha256"] == KERNEL_CONTEXT
            and digest(context_path.read_bytes()) == KERNEL_CONTEXT, "Kernel context differs from reviewed cf07 context")
    context = data(context_path)
    require(context["producer"]["commit"] == KERNEL_PRODUCER
            and context["producer"]["registry"] == "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main"
            and context["consumer"]["commit"] == HEAD
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
            "SONIC_BAZEL_KERNEL_PRODUCER_CONTEXT_SHA256=" + KERNEL_CONTEXT,
            "SONIC_BAZEL_KERNEL_PRODUCER_WORKSPACE=" + make_path(args.producer_workspace, directory=True),
            "SONIC_BAZEL_KERNEL_REGISTRY_WORKSPACE=" + make_path(args.registry_workspace, directory=True)]

def archive_paths(receipt, value):
    paths = {"target/" + name + ".gz" for name in receipt["archives"]} | set(receipt["legacy"])
    require(len(receipt["archives"]) == 42, "All42 final23 archives are required")
    selected = {row["path"] for row in receipt["selection"]["installer_images"] + receipt["selection"]["local_packages"]}
    require(selected <= paths, "Selected legacy archive omitted from verified receipt")
    require(paths == set(value["groups"]["archives"]), "Frozen archive closure differs from resolved Make selection")
    require(all(re.fullmatch(r"target/docker-[a-z0-9-]+\.gz", p) for p in paths), "Unsafe old-file target")
    require(all(receipt["files"][p] == value["files"][p] for p in paths), "Archive changed after input check")
    return sorted(paths)

def make_command(paths, kernel):
    require(paths and len(paths) == len(set(paths)) and
            all(re.fullmatch(r"target/docker-[a-z0-9-]+\.gz", p) for p in paths),
            "Only admitted Docker archives may be old-file targets")
    return ["make", "-f", "slave.mk", "--jobs=1", *["--old-file=" + p for p in sorted(paths)],
            "target/sonic-vs.bin", *FLAGS, *kernel]

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
            and capture["source"] == value["source"] and capture["manifest_sha256"] == args.manifest_sha256,
            "Original preassembly capture identity differs")
    sys.path.insert(0, str(root))
    from tools.bazel.ci import vs_final23
    verified = vs_final23.verify(root, capture["archive_receipt"])
    archive_paths(verified, value)
    installer = file_digest(safe(root, "target/sonic-vs.bin"))
    require(installer["bytes"] > 0, "Installer output is empty")
    return {"status": "ARCHIVES_UNCHANGED_INSTALLER_PRESENT_ONLY", "source": value["source"],
            "installer": installer, "original_capture_sha256": args.receipt_sha256,
            "scope": "Read-only archive verification; no overwrite of the preassembly receipt. Installed-image/runtime, owner CI and complete VS acceptance remain separate."}

def inputs(args):
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
    require(source["head"] == HEAD and source["controller"] == WORKER and source["catalog"] == CATALOG
            and source["pr"] == 206 and source["tree"] == TREE and source["merge"] == args.merge and
            re.fullmatch("[0-9a-f]{40}", args.merge), "Original206 source domain differs")
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
    # This is an external review of the complete configured input closure,
    # not a boolean claim inferred from the older27 direct package variables.
    require(value["configured_make_closure"] in groups["profile"], "Configured Make closure proof missing")
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

def plan(args):
    root, value = inputs(args)
    # Source checkout identity and the complete helper import closure must already
    # be bound by the controller checkout and the frozen source inventory.
    sys.path.insert(0, str(root))
    from tools.bazel.ci import image_worker_receipt as worker
    from tools.bazel.ci import vs_final23
    common = None
    workers = {}
    for family, item in value["native"].items():
        request_path = safe(root, item["request"])
        request = worker.validate_request(data(request_path))
        require(worker.image(request) == family and request["pr"] == 206 and request["head"] == HEAD
                and request["merge"] == args.merge and request["tree"] == TREE and request["controller"] == WORKER
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
    receipt = vs_final23.capture(root, installer="sonic-vs.bin", debug=False)
    vs_final23.verify(root, receipt)
    paths = archive_paths(receipt, value)
    kernel = kernel_arguments(root, value, args)
    require(args.receipt is not None, "An exclusive output path for the original archive capture is required")
    capture_sha = save_capture(args.receipt, {"schema": 1, "kind": "sonic-final23-preassembly-capture",
        "source": value["source"], "manifest_sha256": args.manifest_sha256, "archive_receipt": receipt})
    post = [sys.executable, "-B", str(Path(__file__).resolve()), "verify",
            "--workspace", str(root), "--manifest", str(args.manifest),
            "--manifest-sha256", args.manifest_sha256, "--merge", args.merge,
            "--receipt", str(args.receipt), "--receipt-sha256", capture_sha]
    return {"status": "ARCHIVES_VERIFIED_COMMANDS_ONLY", "source": common, "workers": workers,
            "original_archive_capture": {"path": str(args.receipt), "sha256": capture_sha},
            "build_argv": make_command(paths, kernel), "postbuild_archive_check_argv": post,
            "required_before_assembly": ["current-job VS-specific private-daemon/cgroup/tool qualification",
                                         "canonical kernel prepare.py verify with all16 actual Make fields",
                                         "reviewed complete package/rootfs/host-hook closure",
                                         "read from existing package cache with reviewed source/key compatibility and selected package overrides",
                                         "installer daemon confinement and owned cleanup"],
            "required_after_archive_check": ["authenticated current owner/native validation",
                                             "existing installed-image and installer/version validation with matching policies"],
            "scope": "This planner saves one original archive capture and never executes a child, stages a payload or authorizes assembly. Build and post-build checks are separate; no legacy verify_vs_oci.sh or DEB fallback."}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inputs", "environment", "metadata", "plan", "verify"))
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--merge", required=True)
    parser.add_argument("--require-environment", action="store_true")
    parser.add_argument("--producer-workspace", type=Path)
    parser.add_argument("--registry-workspace", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--receipt-sha256")
    args = parser.parse_args()
    if args.mode in {"metadata", "plan"} and (args.producer_workspace is None or args.registry_workspace is None):
        parser.error("metadata/plan require separate original producer and registry workspaces")
    if args.mode == "environment":
        environment = dict(inputs(args)[1]["build_environment"])
        # Preparation runs host-hook Make before the assembly argv exists.
        # Export the same policies early without changing original image identity.
        environment.update(item.split("=", 1) for item in FLAGS
                           if item.startswith(("SONIC_DEB_BUILD_POLICY=", "SONIC_BAZEL_KERNEL_PREPARE=")))
        for key, value in environment.items():
            print("export " + key + "=" + shlex.quote(value))
        return
    if args.mode == "metadata":
        root, value = inputs(args)
        kernel = kernel_arguments(root, value, args)
        print(json.dumps({"status": "METADATA_COMMAND_ONLY", "argv": ["make", "--jobs=1", "-f", "slave.mk",
              "-f", str(Path(__file__).with_name("vs_reuse_selection.mk")), "vs-reuse-selection", *FLAGS, *kernel],
              "scope": "Run only after admitted worker preparation: unchanged slave.mk parsing has host setup side effects."}, indent=2))
        return
    result = verify_after(args) if args.mode == "verify" else plan(args) if args.mode == "plan" else {
        "status": "FROZEN_INPUT_FILES_PRESENT_ONLY", "source": inputs(args)[1]["source"],
        "scope": "File presence/identity only; no VS qualification or kernel/source compatibility claim."}
    print(json.dumps(result, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
