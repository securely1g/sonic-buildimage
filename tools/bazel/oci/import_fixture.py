#!/usr/bin/env python3
"""Small Docker and OCI fixtures shared by import and consumer tests."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

BASE_FILES = {"etc/import-base": b"base layer\n"}
SECOND_FILES = {"etc/import-base": b"updated by second layer\n", "etc/import-second": b"second layer\n"}
OVERLAY_FILES = {"etc/import-consumer": b"added by rules_oci\n"}


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def add_bytes(archive, name, data, mtime=0):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = 0o644
    entry.mtime = mtime
    archive.addfile(entry, io.BytesIO(data))


def layer_tar(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in files.items():
            add_bytes(archive, name, payload)
    return data.getvalue()


def image_fixture(architecture="amd64", os_name="linux"):
    layers = [layer_tar(BASE_FILES), layer_tar(SECOND_FILES)]
    config = {
        "architecture": architecture,
        "os": os_name,
        "config": {
            "Env": ["BASE_FIXTURE=preserved"],
            "Entrypoint": ["/fixture-entrypoint"],
            "WorkingDir": "/fixture-workdir",
            "Labels": {"sonic.import.fixture": "preserved"},
        },
        "rootfs": {"type": "layers", "diff_ids": [digest(layer) for layer in layers]},
        "history": [{"created_by": "base fixture"}, {"created_by": "second fixture"}],
    }
    # Deliberately noncanonical JSON: importing must preserve the actual bytes.
    return json.dumps(config, indent=2).encode() + b"\n", layers


def docker_archive(path, config, layers, *, mtime=0, extras=()):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        add_bytes(archive, "config.json", config, mtime)
        names = ["layer{}/layer.tar".format(i) for i in range(len(layers))]
        for name, layer in zip(names, layers):
            add_bytes(archive, name, layer, mtime)
        manifest = [{"Config": "config.json", "RepoTags": ["import-base:latest"], "Layers": names}]
        add_bytes(archive, "manifest.json", json.dumps(manifest).encode(), mtime)
        for name, data in extras:
            add_bytes(archive, name, data, mtime)
    data = payload.getvalue()
    if path.suffix == ".gz":
        data = gzip.compress(data, mtime=mtime)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def oci_archive(path, config, layers, *, descriptor_platform=None, extras=()):
    blobs = {digest(config): config}
    for layer in layers:
        blobs[digest(layer)] = layer
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest(config), "size": len(config)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": digest(layer), "size": len(layer)} for layer in layers],
    }
    manifest_bytes = json.dumps(manifest).encode()
    blobs[digest(manifest_bytes)] = manifest_bytes
    parsed = json.loads(config)
    descriptor = {
        "mediaType": manifest["mediaType"], "digest": digest(manifest_bytes), "size": len(manifest_bytes),
        "annotations": {"org.opencontainers.image.ref.name": "base"},
        "platform": descriptor_platform if descriptor_platform is not None else {"os": parsed["os"], "architecture": parsed["architecture"]},
    }
    with tarfile.open(path, "w:gz" if path.suffix == ".gz" else "w") as archive:
        add_bytes(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add_bytes(archive, "index.json", json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode())
        for key, data in blobs.items():
            add_bytes(archive, "blobs/" + key.replace(":", "/"), data)
        for name, data in extras:
            add_bytes(archive, name, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    config, layers = image_fixture(args.architecture)
    docker_archive(args.first, config, layers, mtime=946684800)
    docker_archive(args.second, config, layers, mtime=1700000000)
    args.overlay.write_bytes(layer_tar(OVERLAY_FILES))


if __name__ == "__main__":
    main()
