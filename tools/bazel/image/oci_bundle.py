#!/usr/bin/env python3
"""Convert service images to OCI archives for containerd store assembly.

Skopeo owns format conversion. This helper preserves SONiC image names, checks
the converted platform, and packages an integrity manifest; it never constructs
containerd's private database or an unpacked snapshotter directory.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def reference(value):
    """Expand familiar Docker names to the names Docker looks up in containerd."""
    if not isinstance(value, str) or not value or "@" in value or any(c.isspace() for c in value):
        raise ValueError("expected a tagged Docker image reference")
    name, separator, tag = value.rpartition(":")
    if not separator or not re.fullmatch(r"[\w][\w.-]{0,127}", tag, re.ASCII):
        raise ValueError("expected an explicit image tag: " + value)
    components = name.split("/")
    if any(not p or p in (".", "..") for p in components):
        raise ValueError("invalid image repository: " + value)
    if len(components) == 1:
        name = "docker.io/library/" + name
    elif not ("." in components[0] or ":" in components[0] or components[0] == "localhost"):
        name = "docker.io/" + name
    if name.startswith("index.docker.io/"):
        name = "docker.io/" + name[len("index.docker.io/"):]
    if name.startswith("docker.io/") and name.count("/") == 1:
        name = "docker.io/library/" + name[len("docker.io/"):]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*", name):
        raise ValueError("invalid image repository: " + value)
    return name + ":" + tag


def docker_tags(path):
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            if member.name.removeprefix("./") != "manifest.json":
                continue
            if not member.isfile() or member.size > 16 * 1024 * 1024:
                raise ValueError("invalid Docker archive manifest")
            manifest = json.load(archive.extractfile(member))
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise ValueError("expected exactly one service image per Docker archive")
            tags = manifest[0].get("RepoTags")
            if not isinstance(tags, list) or not tags:
                raise ValueError("Docker archive has no repository tags")
            return [reference(tag) for tag in tags]
    raise ValueError("Docker archive has no manifest.json")


def inspect_oci(path, architecture, expected_reference=None):
    # Inspect metadata without unpacking any filesystem layer. Skopeo has
    # already authenticated every copied blob; verify metadata references too.
    with tarfile.open(path, "r:") as archive:
        members = {}
        for member in archive:
            name = member.name.removeprefix("./")
            if name in members or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
                raise ValueError("duplicate or unsafe OCI archive member: " + name)
            if member.isfile():
                members[name] = member

        def read(name):
            member = members.get(name)
            if member is None or member.size > 16 * 1024 * 1024:
                raise ValueError("missing or oversized OCI metadata: " + name)
            return archive.extractfile(member).read()

        def blob(descriptor):
            digest = descriptor.get("digest", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("invalid OCI metadata digest")
            data = read("blobs/sha256/" + digest[7:])
            if len(data) != descriptor.get("size") or hashlib.sha256(data).hexdigest() != digest[7:]:
                raise ValueError("OCI metadata integrity mismatch")
            return json.loads(data)

        if json.loads(read("oci-layout")).get("imageLayoutVersion") != "1.0.0":
            raise ValueError("unsupported OCI layout")
        index = json.loads(read("index.json"))
        descriptors = index.get("manifests", [])
        if len(descriptors) != 1:
            raise ValueError("expected one platform image in converted OCI archive")
        descriptor = descriptors[0]
        if expected_reference is not None and descriptor.get("annotations", {}).get("org.opencontainers.image.ref.name") != expected_reference:
            raise ValueError("OCI archive reference differs from import manifest")
        manifest = blob(descriptor)
        config = blob(manifest["config"])
        if (config.get("os"), config.get("architecture")) != ("linux", architecture):
            raise ValueError("service image does not match target platform")
        if (config.get("config", {}).get("Labels") or {}).get("org.sonic.bazel.host-metadata-only") == "true":
            raise ValueError("cannot ship a metadata-only staging image")
        layers = manifest.get("layers", [])
        if len(layers) != len(config.get("rootfs", {}).get("diff_ids", [])):
            raise ValueError("OCI layer count differs from configuration")
        for layer in layers:
            digest = layer.get("digest", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("invalid OCI layer digest")
            member = members.get("blobs/sha256/" + digest[7:])
            if member is None or member.size != layer.get("size"):
                raise ValueError("OCI layer is missing or has the wrong size")
        return descriptor["digest"], config


def convert(source, source_format, name, architecture, version, output, skopeo="skopeo"):
    if not re.fullmatch(r"docker-[a-z0-9][a-z0-9_.-]*", name):
        raise ValueError("invalid SONiC service image name")
    if architecture not in ("amd64", "arm64"):
        raise ValueError("unsupported target architecture")
    primary = reference(name + ":latest")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("OCI output directory must be empty")
    archive = output / (name + ".oci.tar")
    with tempfile.TemporaryDirectory(prefix="sonic-oci-") as temporary:
        temporary = Path(temporary)
        if source_format == "oci-archive":
            # Skopeo 1.18's archive source tries to restore TAR ownership and
            # fails without root. OCI archives contain opaque layer blobs;
            # extract only regular layout files with standard, rootless tar.
            seen = set()
            with tarfile.open(source, "r:*") as incoming:
                for member in incoming:
                    path = member.name.removeprefix("./").rstrip("/")
                    if not path and member.isdir():
                        continue
                    allowed = path in {"oci-layout", "index.json"} or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", path)
                    directory = path in {".", "blobs", "blobs/sha256"} and member.isdir()
                    if path in seen or not (directory or allowed and member.isfile()):
                        raise ValueError("unsafe or unexpected OCI archive member: " + member.name)
                    seen.add(path)
            unpacked = temporary / "source"
            unpacked.mkdir()
            subprocess.run(["tar", "--extract", "--file", str(Path(source).resolve()),
                            "--directory", str(unpacked), "--no-same-owner", "--no-same-permissions"], check=True)
            source, source_format = unpacked, "oci"
        if source_format == "docker-archive":
            tags = docker_tags(source)
        else:
            tags = []
            index = json.loads((Path(source) / "index.json").read_text())
            for descriptor in index.get("manifests", []):
                annotations = descriptor.get("annotations", {})
                for key in ("io.containerd.image.name", "org.opencontainers.image.ref.name"):
                    value = annotations.get(key)
                    if value:
                        tags.append(reference(value if ":" in value else name + ":" + value))
        tags = sorted(set(tags + [primary, reference(name + ":" + version)]))
        layout = temporary / "destination"
        subprocess.run([skopeo, "--override-os", "linux", "--override-arch", architecture,
                        "copy", "--format", "oci", "--all",
                        "--dest-compress-format", "zstd", "--dest-compress-level", "6",
                        source_format + ":" + str(Path(source).resolve()),
                        "oci:" + str(layout) + ":" + primary], check=True)
        subprocess.run(["tar", "--create", "--file", str(archive.resolve()),
                        "--directory", str(layout), "--sort=name", "--mtime=@0",
                        "--owner=0", "--group=0", "--numeric-owner", "--format=gnu", "."], check=True)
    digest, _config = inspect_oci(archive, architecture, primary)
    record = {"archive": "images/" + archive.name, "sha256": sha256(archive),
              "reference": primary, "tags": tags, "digest": digest}
    (output / "manifest.json").write_bytes(canonical({"schema": 1, "architecture": architecture,
                                                     "images": [record]}))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    convert_parser = commands.add_parser("convert")
    for name in ("source", "name", "version", "output"):
        convert_parser.add_argument("--" + name, required=True)
    convert_parser.add_argument("--source-format", choices=("docker-archive", "oci", "oci-archive"), required=True)
    convert_parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    convert_parser.add_argument("--skopeo", default="skopeo")
    args = parser.parse_args()
    convert(args.source, args.source_format, args.name, args.architecture, args.version, args.output, args.skopeo)


if __name__ == "__main__":
    main()
