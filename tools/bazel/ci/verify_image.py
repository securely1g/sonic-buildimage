#!/usr/bin/env python3
"""Verify the complete ONIE/ZIP byte chain without extracting a VS installer.

This is an offline artifact check. It proves that the final installer contains
the declared Bazel filesystem, Docker store, boot files and platform archive;
it does not execute the installer, decode SquashFS, or claim boot coverage.
"""

import argparse
import datetime
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tarfile
import time
import zipfile


CHUNK = 1024 * 1024
HEADER_LIMIT = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(stream):
    result = hashlib.sha256()
    size = 0
    while data := stream.read(CHUNK):
        result.update(data)
        size += len(data)
    return {"sha256": result.hexdigest(), "bytes": size}


def file_info(path, magic=None):
    path = Path(path)
    require(path.is_file() and path.stat().st_size, "missing or empty input: " + str(path))
    with path.open("rb") as stream:
        if magic is not None:
            require(stream.read(len(magic)) == magic, "unexpected input format: " + str(path))
            stream.seek(0)
        return digest(stream)


def archive_path(name):
    path = PurePosixPath(name)
    require(bool(name) and not path.is_absolute() and ".." not in path.parts
            and str(path) == name, "invalid archive path: " + repr(name))
    return name


def small_member(archive, member):
    require(member.isfile() and member.size <= HEADER_LIMIT,
            "invalid metadata member: " + member.name)
    return archive.extractfile(member).read()


def inspect_tar(path, compressed=False, boot=False):
    """Read every tar body and every gzip member, including the gzip trailer."""
    members = {}
    with (gzip.open(path, "rb") if compressed else Path(path).open("rb")) as stream:
        with tarfile.open(fileobj=stream, mode="r|", bufsize=CHUNK) as archive:
            for member in archive:
                name = member.name.removeprefix("./").rstrip("/") or "."
                archive_path(name)
                require(name not in members, "duplicate tar member: " + name)
                require(member.isfile() or member.isdir() or member.issym()
                        or member.islnk() or member.ischr() or member.isblk()
                        or member.isfifo(), "unsupported tar member: " + name)
                if boot:
                    require((name == "boot" or name.startswith("boot/"))
                            and (member.isfile() or member.isdir()), "invalid boot member: " + name)
                value = {"mode": member.mode, "directory": member.isdir()}
                if boot and member.isdir():
                    value.update(bytes=0, sha256=hashlib.sha256(b"").hexdigest())
                if member.isfile():
                    with archive.extractfile(member) as content:
                        info = digest(content)
                    require(info["bytes"] == member.size, "truncated tar member: " + name)
                    if boot:
                        value.update(info)
                members[name] = value
        # Tar EOF precedes the gzip footer. Draining validates the CRC/length
        # even when tarfile stopped before the final compressed member ended.
        while stream.read(CHUNK):
            pass
    require(members, "empty tar archive: " + str(path))
    return members


def inspect_payload(path, inputs, boot=None):
    result = {}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate ZIP member")
        require({"fs.squashfs", "platform.tar.gz"} <= set(names), "missing required ZIP members")
        paths = set()
        for member in archive.infolist():
            directory = member.is_dir()
            name = archive_path(member.filename[:-1] if directory else member.filename)
            require(name not in paths, "duplicate ZIP path: " + name)
            paths.add(name)
            require(name in {"fs.squashfs", "platform.tar.gz", "dockerfs.tar.gz"}
                    or name == "boot" or name.startswith("boot/"), "unexpected ZIP member: " + name)
            require(name.startswith("boot") or not directory, "invalid ZIP directory: " + name)
            require(name != "boot" or directory, "boot must be a directory")
            require(not member.flag_bits & 1, "encrypted ZIP member: " + name)
            require(member.extract_version < 45, "ONIE payload must not require ZIP64: " + name)
            require(member.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    "unsupported ZIP compression: " + name)
            mode = member.external_attr >> 16
            require(stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode),
                    "invalid ZIP file type: " + name)
            with archive.open(member) as content:
                info = digest(content)  # Reading to EOF checks this member's CRC.
            require(info["bytes"] == member.file_size, "ZIP member size mismatch: " + name)
            require(directory or info["bytes"] > 0, "empty ZIP file: " + name)
            result[member.filename] = info | {"mode": stat.S_IMODE(mode), "directory": directory}
            if name in inputs:
                require(info == inputs[name], "ZIP bytes differ from declared input: " + name)
        kernels = {name[len("boot/vmlinuz-"):] for name in names if name.startswith("boot/vmlinuz-")}
        initrds = {name[len("boot/initrd.img-"):] for name in names if name.startswith("boot/initrd.img-")}
        require(kernels and kernels == initrds, "ZIP kernel and initramfs versions must match")
        require(all(not result["boot/vmlinuz-" + version]["directory"]
                    and not result["boot/initrd.img-" + version]["directory"] for version in kernels),
                "kernel and initramfs must be regular files")
        if boot is not None:
            actual = {name.rstrip("/"): info for name, info in result.items() if name.startswith("boot")}
            require(actual == boot, "ZIP boot members differ from declared boot archive")
    # Reject ZIP64 EOCD records/locators even for an archive with small members.
    with Path(path).open("rb") as stream:
        stream.seek(max(0, Path(path).stat().st_size - 65557))
        tail = stream.read()
    eocd = tail.rfind(b"PK\x05\x06")
    require(eocd >= 0 and eocd + 22 <= len(tail), "missing ZIP end record")
    fields = struct.unpack_from("<4s4H2LH", tail, eocd)
    require(eocd + 22 + fields[-1] == len(tail), "trailing bytes after ZIP end record")
    require(fields[1:3] == (0, 0) and fields[3] == fields[4] == len(result)
            and fields[5] != 0xFFFFFFFF and fields[6] != 0xFFFFFFFF,
            "unsupported ZIP64 or split ZIP payload")
    require(tail[max(0, eocd - 20):eocd - 16] != b"PK\x06\x07", "ZIP64 locator is not ONIE-compatible")
    return {"members": result, "kernel_versions": sorted(kernels),
            "dockerfs_in_zip": "dockerfs.tar.gz" in result}


def inspect_onie(path, payload, dockerfs, dockerfs_in_zip):
    complete_hash = hashlib.sha256()
    tar_hash = hashlib.sha1()
    header = bytearray()
    with Path(path).open("rb") as stream:
        while len(header) <= HEADER_LIMIT:
            line = stream.readline(HEADER_LIMIT + 1)
            require(line, "missing ONIE exit_marker")
            header.extend(line)
            if line == b"exit_marker\n":
                break
        require(len(header) <= HEADER_LIMIT and header.startswith(b"#!/bin/sh\n"), "invalid ONIE shell header")
        offset = stream.tell()
        declared_sha = re.findall(rb"^payload_sha1=([0-9a-f]{40})$", header, re.M)
        declared_size = re.findall(rb"^payload_image_size=([0-9]+)$", header, re.M)
        require(len(declared_sha) == len(declared_size) == 1, "missing or ambiguous ONIE payload metadata")
        require(int(declared_size[0]) == Path(path).stat().st_size - offset,
                "ONIE payload length mismatch")
        complete_hash.update(header)
        size = 0
        while data := stream.read(CHUNK):
            complete_hash.update(data)
            tar_hash.update(data)
            size += len(data)
        require(tar_hash.hexdigest() == declared_sha[0].decode(), "ONIE payload SHA1 mismatch")
        stream.seek(offset)
        found = {}
        metadata = {}
        with tarfile.open(fileobj=stream, mode="r|", bufsize=CHUNK) as archive:
            for member in archive:
                name = archive_path(member.name.rstrip("/"))
                require(name == "installer" or name.startswith("installer/"), "unexpected ONIE member: " + name)
                require(name not in found, "duplicate ONIE member: " + name)
                require(member.isfile() or member.isdir(), "unsafe ONIE member type: " + name)
                require(member.uid == member.gid == 0, "ONIE member is not owned by root: " + name)
                found[name] = {"mode": member.mode, "directory": member.isdir()}
                if member.isfile():
                    if name in {"installer/install.sh", "installer/machine.conf", "installer/platforms_asic"}:
                        data = small_member(archive, member)
                        metadata[name] = data.decode("utf-8")
                        info = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    else:
                        with archive.extractfile(member) as content:
                            info = digest(content)
                    require(info["bytes"] == member.size, "truncated ONIE member: " + name)
                    found[name].update(info)
                    expected = {"installer/fs.zip": payload, "installer/dockerfs.tar.gz": dockerfs}.get(name)
                    if expected is not None:
                        require(info == expected, "ONIE bytes differ from declared input: " + name)
        required = {"installer/fs.zip", "installer/install.sh", "installer/machine.conf",
                    "installer/default_platform.conf", "installer/onie-image.conf", "installer/platforms_asic"}
        require(required <= found.keys() and all(not found[name]["directory"] for name in required),
                "missing required ONIE installer files")
        require(found["installer/install.sh"]["mode"] & 0o111, "install.sh is not executable")
        require(("installer/dockerfs.tar.gz" in found) != dockerfs_in_zip,
                "Docker store must appear exactly once, in ZIP or ONIE tar")
        require(metadata["installer/machine.conf"] == "machine=vs\nplatform=x86_64-vs-r0\n",
                "unexpected VS machine identity")
        require('arch="amd64"' in metadata["installer/install.sh"], "installer is not AMD64")
        versions = re.findall(r'^image_version="([A-Za-z0-9_.+-]+)"$', metadata["installer/install.sh"], re.M)
        require(len(versions) == 1, "missing or ambiguous installer image version")
        require("x86_64-kvm_x86_64-r0" in metadata["installer/platforms_asic"].splitlines(),
                "installer lacks the single-ASIC VS platform")
    return {"sha256": complete_hash.hexdigest(), "bytes": len(header) + size,
            "payload_sha1": tar_hash.hexdigest(), "payload_bytes": size,
            "image_version": versions[0], "members": found}


def verify(installer, payload, dockerfs, squashfs, boot=None, platform=None):
    inputs = {
        "fs.zip": file_info(payload, b"PK\x03\x04"),
        "fs.squashfs": file_info(squashfs, b"hsqs"),
        "dockerfs.tar.gz": file_info(dockerfs, b"\x1f\x8b"),
    }
    archives = {"dockerfs_members": len(inspect_tar(dockerfs, compressed=True))}
    if platform is not None:
        inputs["platform.tar.gz"] = file_info(platform, b"\x1f\x8b")
        archives["platform_members"] = len(inspect_tar(platform, compressed=True))
    boot_members = None
    if boot is not None:
        inputs["boot.tar"] = file_info(boot)
        boot_members = inspect_tar(boot, boot=True)
    zipped = inspect_payload(payload, inputs, boot_members)
    onie = inspect_onie(installer, inputs["fs.zip"], inputs["dockerfs.tar.gz"], zipped["dockerfs_in_zip"])
    return {"inputs": inputs, "archives": archives, "payload": zipped, "installer": onie}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("installer", "payload", "dockerfs", "squashfs", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("boot", "platform"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args(argv)
    started = time.monotonic()
    report = {"schema": 1, "status": "failed", "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "scope": "Full ONIE checksum and member hashes; complete ZIP CRC/member byte chain; "
                       "Docker/platform tar and gzip decoding; optional exact boot/platform comparison. "
                       "SquashFS magic/hash only. No Docker runtime, filesystem decode, boot or forwarding claim."}
    try:
        report.update(verify(args.installer, args.payload, args.dockerfs, args.squashfs, args.boot, args.platform))
        report["status"] = "passed"
    except Exception as error:
        report.update(error=str(error), error_type=type(error).__name__)
        print("Image verification failed: " + str(error), file=sys.stderr)
    finally:
        report["wall_seconds"] = time.monotonic() - started
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "report": str(args.output), "wall_seconds": report["wall_seconds"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
