#!/usr/bin/env python3
"""Observe original Docker configuration without authorizing image reuse."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import stat
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.vs_archive_reuse import (
    CHUNK, CONFIG_NAME, HashingReader, MAX_JSON, file_identity, path_name, require,
)

MAX_MEMBERS = 10000
MAX_EXPANDED = 64 * 1024**3


class BoundedArchiveReader(HashingReader):
    """Apply the observation size limit to headers and trailing data too."""
    def read(self, size=-1):
        data = super().read(size)
        require(self.size <= MAX_EXPANDED, "archive exceeds observation limits")
        return data


class ObservationTarInfo(tarfile.TarInfo):
    """Bound extended headers before tarfile allocates or processes them."""
    def _proc_member(self, archive):
        require(self.type != tarfile.GNUTYPE_SPARSE, "sparse TAR metadata is outside observation scope")
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                         tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            count = getattr(archive, "observation_metadata_count", 0) + 1
            total = getattr(archive, "observation_metadata_bytes", 0) + self.size
            require(0 <= self.size <= MAX_JSON and total <= MAX_JSON and count <= 128,
                    "extended TAR metadata exceeds observation limits")
            archive.observation_metadata_count = count
            archive.observation_metadata_bytes = total
        return super()._proc_member(archive)

    def _proc_gnusparse_00(self, *args):
        raise ValueError("sparse TAR metadata is outside observation scope")

    _proc_gnusparse_01 = _proc_gnusparse_00
    _proc_gnusparse_10 = _proc_gnusparse_00


def observe(path, expected_tag, entrypoint, *, expected_sha256=None):
    """Read stored bytes and bounded JSON; never extract or execute layers."""
    documents, files = {}, {}
    expanded = json_bytes = count = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode), "archive must be a regular file")
        before = file_identity(info)
        reader = HashingReader(source)
        with gzip.GzipFile(fileobj=reader, mode="rb") as decoded:
            bounded = BoundedArchiveReader(decoded)
            with tarfile.open(fileobj=bounded, mode="r|", tarinfo=ObservationTarInfo) as archive:
                for member in archive:
                    count += 1
                    expanded += member.size
                    require(count <= MAX_MEMBERS and 0 <= member.size <= MAX_EXPANDED and
                            expanded <= MAX_EXPANDED, "archive exceeds observation limits")
                    name = path_name(member.name)
                    require(member.isfile() or member.isdir(), "unsupported Docker archive member")
                    if member.isdir():
                        continue
                    require(name not in files, "duplicate Docker archive member")
                    keep = name == "manifest.json" or CONFIG_NAME.fullmatch(name)
                    require(not keep or member.size <= MAX_JSON, "Docker JSON exceeds limit")
                    data = bytearray() if keep else None
                    member_hash = hashlib.sha256()
                    with archive.extractfile(member) as stream:
                        for chunk in iter(lambda: stream.read(CHUNK), b""):
                            member_hash.update(chunk)
                            if data is not None:
                                json_bytes += len(chunk)
                                require(json_bytes <= MAX_JSON, "Docker JSON exceeds limit")
                                data.extend(chunk)
                    files[name] = member_hash.hexdigest()
                    if data is not None:
                        documents[name] = json.loads(data)
            # Include the full gzip stream and verify its trailer.
            for _ in iter(lambda: bounded.read(CHUNK), b""):
                pass
        require(before == file_identity(os.fstat(source.fileno())) == file_identity(path.lstat()),
                "archive changed during observation")
    digest = reader.hasher.hexdigest()
    require(expected_sha256 is None or digest == expected_sha256,
            "archive differs from original runtime receipt")
    manifest = documents.get("manifest.json")
    require(isinstance(manifest, list) and len(manifest) == 1 and isinstance(manifest[0], dict),
            "Docker archive must describe one image")
    entry = manifest[0]
    name = path_name(entry.get("Config", ""))
    config = documents.get(name)
    require(isinstance(config, dict), "Docker configuration is missing")
    require(Path(name).name.removesuffix(".json") == files[name], "config digest differs")
    layers = entry.get("Layers")
    require(isinstance(layers, list) and layers and
            all(path_name(layer) in files for layer in layers), "Docker layer is missing")
    settings = config.get("config")
    require(isinstance(settings, dict), "Docker settings are missing")
    environment = settings.get("Env", [])
    require(isinstance(environment, list) and all(isinstance(x, str) and "=" in x for x in environment),
            "Docker environment is invalid")
    versions = [x.split("=", 1)[1] for x in environment if x.startswith("IMAGE_VERSION=")]
    require(len(versions) == 1, "expected exactly one image version")
    version = versions[0]
    # Only the version is public; never publish arbitrary environment/labels.
    safe_version = version if re.fullmatch(r"[A-Za-z0-9._+:-]{1,128}", version) else None
    checks = {"linux_amd64": config.get("os") == "linux" and config.get("architecture") == "amd64",
              "tag_matches": entry.get("RepoTags") == [expected_tag],
              "entrypoint_matches": settings.get("Entrypoint") == entrypoint,
              "image_version_matches": version == "bazel-ci"}
    return {"status": "ORIGINAL_ARCHIVE_CONFIGURATION_OBSERVED", "sha256": digest,
            "bytes": reader.size, "config_sha256": files[name], "expected_tag": expected_tag,
            "image_version": safe_version, "image_version_sha256": hashlib.sha256(version.encode()).hexdigest(),
            "checks": checks, "matches_expected_configuration": all(checks.values()),
            "full_layer_validation": False, "independent_Make_manifest_checked": False,
            "source_admission": False, "VS_assembly_admission": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--entrypoint", required=True, help="JSON list from the selected runtime contract")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        entrypoint = json.loads(args.entrypoint)
        require(isinstance(entrypoint, list) and entrypoint and all(isinstance(x, str) for x in entrypoint),
                "invalid expected entrypoint")
        result = observe(args.input, args.tag, entrypoint, expected_sha256=args.expected_sha256)
        code = 0
    except (OSError, ValueError, EOFError, tarfile.TarError, KeyError, TypeError) as error:
        result = {"status": "ARCHIVE_OBSERVATION_FAILED", "error_type": type(error).__name__,
                  "source_admission": False, "VS_assembly_admission": False}
        code = 1
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
