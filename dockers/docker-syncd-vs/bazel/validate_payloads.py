#!/usr/bin/env python3
"""Validate the declared syncd-vs package handoff before OCI layer assembly."""

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci.oci_layout import validate_layout

FEATURES = {"include_vs_dash_sai": "y", "include_fips": "y", "enable_asan": "n", "enable_syncd_rpc": "n"}
SOURCE_PACKAGE_NAMES = frozenset({
    "libswsscommon", "libsairedis", "libsaimetadata",
    "libswsscommon-dbgsym", "libsairedis-dbgsym", "libsaimetadata-dbgsym",
})


def reject_source_packages(records, *, allow_base_symbols=False):
    """Reject stale Make libraries and symbols now owned by the shared Bazel targets."""
    unexpected = []
    for record in records:
        name = record.get("package")
        if name not in SOURCE_PACKAGE_NAMES:
            continue
        if allow_base_symbols and name == "libswsscommon-dbgsym":
            import base_debug_symbols
            base_debug_symbols.check_record(record)
        else:
            unexpected.append(name)
    require(not unexpected, "Make handoff contains packages now built from source: " + ", ".join(sorted(set(unexpected))))


MERGED_USR = {"bin": "usr/bin", "lib": "usr/lib", "lib64": "usr/lib64", "sbin": "usr/sbin"}
DIRECTORY_ALIASES = {name: {"linkname": target, "target": target} for name, target in MERGED_USR.items()}
DIRECTORY_ALIASES["var/run"] = {"linkname": "/run", "target": "run"}


def normalized_path(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts, "unsafe package payload path: " + value)
    name = str(path)
    for alias, entry in DIRECTORY_ALIASES.items():
        if name == alias or name.startswith(alias + "/"):
            return entry["target"] + name[len(alias):]
    return name


def normalized_member(member):
    name = str(PurePosixPath(member.name))
    normalized = normalized_path(member.name)
    require(not PurePosixPath(name).name.startswith(".wh."), "package payload uses a reserved OCI whiteout path: " + name)
    if name in DIRECTORY_ALIASES:
        require(member.isdir() or (member.issym() and member.linkname == DIRECTORY_ALIASES[name]["linkname"]),
                "package payload changes a directory alias: " + name)
        require(member.uid == 0 and member.gid == 0 and member.mode == (0o755 if member.isdir() else 0o777),
                "package payload changes directory alias metadata: " + name)
        return None
    require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
            "unsupported package payload member: " + name)
    require(member.sparse is None, "sparse package payload member requires review: " + name)
    output = copy.copy(member)
    output.name = "./" + normalized if normalized != "." else "./"
    output.pax_headers = dict(member.pax_headers)
    output.pax_headers.pop("path", None)
    output.pax_headers.pop("linkpath", None)
    if member.islnk():
        target = str(PurePosixPath(member.linkname))
        require(target not in DIRECTORY_ALIASES, "package hardlink targets a directory alias: " + name)
        output.linkname = "./" + normalized_path(member.linkname)
    return output


def base_aliases(base):
    descriptor, _, _, layers = validate_layout(base, "linux/amd64")
    targets = set(DIRECTORY_ALIASES) | {entry["target"] for entry in DIRECTORY_ALIASES.values()}
    observed = {}
    for layer in layers:
        additions = {}
        removals = set()
        with tarfile.open(layer, "r:*") as archive:
            for member in archive:
                pure = PurePosixPath(member.name)
                require(not pure.is_absolute() and ".." not in pure.parts, "unsafe OCI base path")
                name = str(pure)
                if name in targets:
                    additions[name] = {"kind": "symlink" if member.issym() else "directory" if member.isdir() else "other",
                                       "linkname": member.linkname, "uid": member.uid, "gid": member.gid, "mode": member.mode}
                elif pure.name == ".wh..wh..opq":
                    parent = str(pure.parent)
                    removals.update(target for target in targets if parent == "." or target.startswith(parent + "/"))
                elif pure.name.startswith(".wh."):
                    hidden = str(pure.parent / pure.name[4:])
                    removals.update(target for target in targets if target == hidden or target.startswith(hidden + "/"))
        for name in removals:
            observed.pop(name, None)
        observed.update(additions)
    for name, entry in DIRECTORY_ALIASES.items():
        require(observed.get(name) == {"kind": "symlink", "linkname": entry["linkname"], "uid": 0, "gid": 0, "mode": 0o777} and
                observed.get(entry["target"]) == {"kind": "directory", "linkname": "", "uid": 0, "gid": 0, "mode": 0o755},
                "OCI base has an unsupported directory alias: " + name)
    return descriptor["digest"]


def normalize(payload_path, base, output_path):
    base_digest = base_aliases(base)
    counts = {"input_members": 0, "output_members": 0, "rewritten_members": 0, "skipped_alias_entries": 0}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(payload_path, "r:") as source, tarfile.open(output_path, "w", format=tarfile.PAX_FORMAT) as output:
        for member in source:
            counts["input_members"] += 1
            normalized = normalized_member(member)
            if normalized is None:
                counts["skipped_alias_entries"] += 1
                continue
            counts["output_members"] += 1
            counts["rewritten_members"] += str(PurePosixPath(normalized.name)) != str(PurePosixPath(member.name))
            output.addfile(normalized, source.extractfile(member) if member.isfile() else None)
    return {"base_manifest_digest": base_digest, "directory_aliases": DIRECTORY_ALIASES, **counts}


def require_runtime_fips(records):
    matches = [record for record in records if record.get("package") == "openssh-client"]
    require(len(matches) == 1 and "+fips" in matches[0].get("version", ""),
            "runtime package handoff requires the Make FIPS openssh-client")
    record = matches[0]
    fields = record.get("control_fields", {})
    require(fields.get("Package") == record["package"] and fields.get("Version") == record["version"] and
            fields.get("Architecture") == record.get("architecture") == "amd64",
            "runtime FIPS openssh-client identity differs from its Debian control")


def validate(manifest_path, payload_path, *, variant, runtime_manifest=None):
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    require(manifest.get("schema") == 1 and manifest.get("image") == "docker-syncd-vs",
            "unsupported syncd-vs package manifest")
    require(manifest.get("variant") == variant and manifest.get("architecture") == "amd64" and
            manifest.get("distribution") == "trixie" and manifest.get("features") == FEATURES,
            "syncd-vs package manifest has an unsupported configuration")
    records = manifest.get("packages")
    require(isinstance(records, list) and records, "syncd-vs package manifest has no packages")
    require(all(isinstance(record, dict) for record in records), "invalid syncd-vs package record")
    reject_source_packages(records, allow_base_symbols=variant == "debug")
    names = [record.get("package") for record in records]
    require(all(isinstance(name, str) and name for name in names) and len(names) == len(set(names)),
            "syncd-vs package names are missing or duplicated")
    require(set(manifest.get("required_packages", [])).issubset(names), "syncd-vs required package is absent")
    checked = []
    members_expected = 0
    for record in records:
        source = PurePosixPath(record.get("source_deb", ""))
        require(source.name == str(source) and source.name.endswith(".deb") and record.get("version") and
                record.get("architecture") in ("amd64", "all"), "invalid package identity: " + record["package"])
        for kind in ("source", "payload", "control"):
            require(re.fullmatch(r"[0-9a-f]{64}", record.get(kind + "_sha256", "")) is not None,
                    "invalid package " + kind + " integrity: " + record["package"])
        require(isinstance(record.get("source_size"), int) and record["source_size"] > 0 and
                isinstance(record.get("payload_size"), int) and record["payload_size"] > 0 and
                isinstance(record.get("payload_members"), int) and record["payload_members"] > 0,
                "invalid package payload size or member count: " + record["package"])
        members_expected += record["payload_members"]
        checked.append({name: record[name] for name in
                        ("package", "version", "architecture", "source_sha256", "payload_sha256")})
    if variant == "runtime":
        require_runtime_fips(records)
    else:
        require("openssh-client" not in names, "debug package handoff must inherit runtime FIPS openssh-client")
    payload = manifest.get("payload", {})
    require(payload.get("path") == "payload.tar" and payload_path.resolve() == (manifest_path.parent / "payload.tar").resolve(),
            "declared syncd-vs payload file differs from the manifest")
    require(payload_path.is_file() and payload_path.stat().st_size == payload.get("size") and
            sha(payload_path) == payload.get("sha256"), "changed aggregate package payload")
    members = 0
    with tarfile.open(payload_path, "r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts, "unsafe package payload path: " + member.name)
            require(not name.name.startswith(".wh."), "package payload uses a reserved OCI whiteout path: " + member.name)
            members += 1
    require(members == payload.get("members") == members_expected, "aggregate package member count differs")
    if any(record.get("base_debug_symbols") is not None for record in records):
        import base_debug_symbols
        base_debug_symbols.validate_aggregate(manifest, payload_path)
    if variant == "debug":
        require(runtime_manifest is not None, "debug payload validation requires the runtime manifest")
        runtime_bytes = runtime_manifest.read_bytes()
        require(hashlib.sha256(runtime_bytes).hexdigest() == manifest.get("runtime_manifest_sha256"),
                "debug package handoff was prepared for a different runtime handoff")
        runtime = json.loads(runtime_bytes)
        require(runtime.get("schema") == 1 and runtime.get("image") == "docker-syncd-vs" and
                runtime.get("variant") == "runtime" and runtime.get("features") == FEATURES,
                "invalid runtime package manifest")
        reject_source_packages(runtime["packages"])
        require_runtime_fips(runtime["packages"])
        runtime_packages = {record["package"]: record for record in runtime["packages"]}
        for record in records:
            previous = runtime_packages.get(record["package"])
            require(previous is None or previous["source_sha256"] == record["source_sha256"],
                    "debug package handoff changes a runtime package: " + record["package"])
        require(set(manifest.get("debug_apt_packages", [])) == {"gdb", "gdbserver", "sshpass", "strace", "vim"},
                "debug package handoff has an unsupported tool set")
    else:
        require(runtime_manifest is None and "runtime_manifest_sha256" not in manifest and
                not manifest.get("debug_apt_packages"), "runtime package handoff contains debug inputs")
    return {"schema": 1, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "payload_sha256": payload["sha256"], "variant": variant, "package_count": len(records), "packages": checked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=("runtime", "debug"))
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--out-tar", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        receipt = validate(args.manifest, args.payload, variant=args.variant, runtime_manifest=args.runtime_manifest)
        receipt["normalization"] = normalize(args.payload, args.base, args.out_tar)
        require(sha(args.payload) == receipt["payload_sha256"], "aggregate package payload changed during normalization")
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        else:
            print(json.dumps(receipt, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd-vs payload validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
