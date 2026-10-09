#!/usr/bin/env python3
"""Publish checked payload tars from the existing syncd-vs Make DEBs.

This is a Make preparation step. It extracts packages with dpkg-deb and never
invokes Bazel or creates a DEB. Immutable generations keep concurrent Bazel
readers on a complete input set while an updated generation is published.
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


SCHEMA = 1
IMAGE = "docker-syncd-vs"
DEBUG_APT_PACKAGES = {"gdb", "gdbserver", "sshpass", "strace", "vim"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_switch(value):
    require(value in ("", "n", "y"), "expected an empty, n, or y switch: " + repr(value))
    return "y" if value == "y" else "n"


def control_fields(text):
    result = {}
    current = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")):
            require(current is not None, "control continuation has no field")
            result[current] += "\n" + line
        elif line:
            require(":" in line, "invalid Debian control field: " + line)
            current, value = line.split(":", 1)
            require(current not in result, "duplicate Debian control field: " + current)
            result[current] = value.lstrip()
    return result


def payload_members(path):
    count = 0
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts,
                    "unsafe package payload path: " + member.name)
            count += 1
    require(count > 0, "empty package payload: " + path.name)
    return count


def command(arguments, *, stdout=subprocess.PIPE):
    result = subprocess.run(arguments, stdout=stdout, stderr=subprocess.PIPE, check=False)
    require(result.returncode == 0,
            "package inspection failed: " + repr(arguments[:2]) + "\n" + result.stderr.decode(errors="replace"))
    return result.stdout


def prepare_package(source, index, temporary, dpkg_deb):
    require(source.is_file(), "missing Make DEB: " + str(source))
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+~_:-]*\.deb", source.name) is not None,
            "unsupported Make DEB filename: " + source.name)
    copied = temporary / "inputs" / source.name
    shutil.copyfile(source, copied)
    source_sha = sha(copied)
    require(sha(source) == source_sha, "Make DEB changed while it was copied: " + str(source))

    fields = control_fields(command([dpkg_deb, "--field", str(copied)]).decode())
    for name in ("Package", "Version", "Architecture"):
        require(fields.get(name), "missing Debian " + name + ": " + source.name)
    require(fields["Architecture"] in ("amd64", "all"), "wrong Debian architecture: " + source.name)

    control = command([dpkg_deb, "--ctrl-tarfile", str(copied)])
    control_files = {}
    with tarfile.open(fileobj=io.BytesIO(control), mode="r:") as archive:
        for member in archive:
            if member.isfile():
                name = str(PurePosixPath(member.name))
                control_files[name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()

    payload_name = "payloads/{:04d}-{}.tar".format(index, source.name)
    payload = temporary / payload_name
    with payload.open("wb") as stream:
        command([dpkg_deb, "--fsys-tarfile", str(copied)], stdout=stream)
    result = {
        "package": fields["Package"],
        "version": fields["Version"],
        "architecture": fields["Architecture"],
        "source_deb": source.name,
        "source_sha256": source_sha,
        "source_size": copied.stat().st_size,
        "control_sha256": hashlib.sha256(control).hexdigest(),
        "control_files": control_files,
        "control_fields": fields,
        "_payload": payload_name,
        "payload_sha256": sha(payload),
        "payload_size": payload.stat().st_size,
        "payload_members": payload_members(payload),
    }
    return result


def check_generation(path, manifest_bytes, payload):
    require((path / "manifest.json").is_file() and (path / "manifest.json").read_bytes() == manifest_bytes,
            "existing package generation has a different manifest: " + str(path))
    require({item.name for item in path.iterdir()} == {"manifest.json", "payload.tar"},
            "existing package generation has a different file set: " + str(path))
    archive = path / payload["path"]
    require(archive.is_file() and archive.stat().st_size == payload["size"] and sha(archive) == payload["sha256"],
            "existing package generation has a damaged payload: " + str(archive))


def prepare(args):
    require(args.architecture == "amd64" and args.distribution == "trixie",
            "syncd-vs OCI package preparation supports native AMD64 Trixie only")
    features = {
        "include_vs_dash_sai": normalized_switch(args.include_vs_dash_sai),
        "include_fips": normalized_switch(args.include_fips),
        "enable_asan": normalized_switch(args.enable_asan),
        "enable_syncd_rpc": normalized_switch(args.enable_syncd_rpc),
    }
    require(features == {"include_vs_dash_sai": "y", "include_fips": "y", "enable_asan": "n", "enable_syncd_rpc": "n"},
            "unsupported syncd-vs OCI feature configuration")
    require(args.package, "the syncd-vs package handoff is empty")
    require("GNU tar" in command([args.tar, "--version"]).decode().splitlines()[0],
            "syncd-vs package preparation requires GNU tar")
    if args.variant == "debug":
        require(args.runtime_manifest is not None, "debug preparation requires the runtime manifest")
        require(set(args.debug_apt_package) == DEBUG_APT_PACKAGES,
                "syncd-vs OCI debug tools differ from the supported Make package set")
    else:
        require(args.runtime_manifest is None and not args.debug_apt_package,
                "runtime preparation cannot consume debug inputs")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    generations = output.parent / ("." + output.name + ".generations")
    generations.mkdir(exist_ok=True)
    lock_path = output.parent / ("." + output.name + ".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=generations) as temporary_name:
            temporary = Path(temporary_name)
            (temporary / "inputs").mkdir()
            (temporary / "payloads").mkdir()
            sources = []
            seen_sources = {}
            for value in args.package:
                source = Path(value)
                if source.name in seen_sources:
                    require(source.resolve() == seen_sources[source.name],
                            "different Make DEBs share a filename: " + source.name)
                    continue
                seen_sources[source.name] = source.resolve()
                sources.append(source)
            records = [prepare_package(source, index, temporary, args.dpkg_deb)
                       for index, source in enumerate(sources)]
            package_names = [record["package"] for record in records]
            require(len(package_names) == len(set(package_names)), "different DEBs provide the same package name")
            require(set(args.required_package).issubset(package_names),
                    "missing required syncd-vs packages: " + ", ".join(sorted(set(args.required_package) - set(package_names))))
            aggregate = temporary / "payload.tar"
            shutil.copyfile(temporary / records[0]["_payload"], aggregate)
            for record in records[1:]:
                command([args.tar, "--concatenate", "--file", str(aggregate), str(temporary / record["_payload"])])
            aggregate_members = payload_members(aggregate)
            require(aggregate_members == sum(record["payload_members"] for record in records),
                    "aggregate package payload member count differs")
            for record in records:
                del record["_payload"]
            shutil.rmtree(temporary / "payloads")
            payload = {"path": "payload.tar", "sha256": sha(aggregate), "size": aggregate.stat().st_size,
                       "members": aggregate_members}
            manifest = {
                "schema": SCHEMA,
                "image": IMAGE,
                "variant": args.variant,
                "architecture": args.architecture,
                "distribution": args.distribution,
                "features": features,
                "required_packages": sorted(set(args.required_package)),
                "debug_apt_packages": sorted(set(args.debug_apt_package)),
                "packages": records,
                "payload": payload,
            }
            if args.runtime_manifest is not None:
                runtime_path = Path(args.runtime_manifest)
                runtime_bytes = runtime_path.read_bytes()
                runtime = json.loads(runtime_bytes)
                require(runtime.get("schema") == SCHEMA and runtime.get("image") == IMAGE and
                        runtime.get("variant") == "runtime" and runtime.get("features") == features,
                        "invalid or incompatible runtime package manifest")
                runtime_packages = {record["package"]: record for record in runtime["packages"]}
                for record in records:
                    previous = runtime_packages.get(record["package"])
                    require(previous is None or previous["source_sha256"] == record["source_sha256"],
                            "debug preparation would replace a runtime package: " + record["package"])
                manifest["runtime_manifest_sha256"] = hashlib.sha256(runtime_bytes).hexdigest()
            manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            shutil.rmtree(temporary / "inputs")
            generation = generations / hashlib.sha256(manifest_bytes).hexdigest()
            if generation.exists():
                check_generation(generation, manifest_bytes, payload)
            else:
                os.rename(temporary, generation)
            require(not output.exists() or output.is_symlink(),
                    "refusing to replace a non-symlink package handoff: " + str(output))
            target = os.path.relpath(generation, output.parent)
            if not output.is_symlink() or os.readlink(output) != target:
                temporary_link = output.parent / ("." + output.name + ".link-" + str(os.getpid()))
                try:
                    temporary_link.symlink_to(target, target_is_directory=True)
                    os.replace(temporary_link, output)
                finally:
                    temporary_link.unlink(missing_ok=True)
            return {"manifest": str(output / "manifest.json"), "generation": generation.name,
                    "package_count": len(records), "variant": args.variant}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--variant", required=True, choices=("runtime", "debug"))
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--include-vs-dash-sai", required=True)
    parser.add_argument("--include-fips", required=True)
    parser.add_argument("--enable-asan", required=True)
    parser.add_argument("--enable-syncd-rpc", required=True)
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument("--required-package", action="append", default=[])
    parser.add_argument("--debug-apt-package", action="append", default=[])
    parser.add_argument("--runtime-manifest")
    parser.add_argument("--dpkg-deb", default="dpkg-deb")
    parser.add_argument("--tar", default="tar")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args), sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd-vs package preparation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
