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


def observe(path, expected_tag, entrypoint, *, expected_sha256=None,
            allow_absent_image_version=False, include_repo_tags=False, include_loader_metadata=False):
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
                    manifest_member = name == "manifest.json"
                    require(not manifest_member or member.size <= MAX_JSON, "Docker JSON exceeds limit")
                    # Docker save may name both config and binary layers by digest.
                    # Retain only bounded object candidates; select JSON by manifest
                    # after hashing the complete archive, regardless of member order.
                    keep = manifest_member or (CONFIG_NAME.fullmatch(name) and member.size <= MAX_JSON)
                    data = bytearray() if keep else None
                    object_started = manifest_member
                    member_hash = hashlib.sha256()
                    with archive.extractfile(member) as stream:
                        for chunk in iter(lambda: stream.read(CHUNK), b""):
                            member_hash.update(chunk)
                            if data is not None:
                                if not object_started:
                                    prefix = chunk.lstrip(b" \t\r\n")
                                    if prefix and not prefix.startswith(b"{"):
                                        json_bytes -= len(data)
                                        data = None
                                        continue
                                    object_started = bool(prefix)
                                json_bytes += len(chunk)
                                require(json_bytes <= MAX_JSON, "Docker JSON exceeds limit")
                                data.extend(chunk)
                    files[name] = member_hash.hexdigest()
                    if data is not None:
                        documents[name] = bytes(data)
            # Include the full gzip stream and verify its trailer.
            for _ in iter(lambda: bounded.read(CHUNK), b""):
                pass
        require(before == file_identity(os.fstat(source.fileno())) == file_identity(path.lstat()),
                "archive changed during observation")
    digest = reader.hasher.hexdigest()
    require(expected_sha256 is None or digest == expected_sha256,
            "archive differs from original runtime receipt")
    manifest_raw = documents.get("manifest.json")
    manifest = json.loads(manifest_raw) if manifest_raw is not None else None
    require(isinstance(manifest, list) and len(manifest) == 1 and isinstance(manifest[0], dict),
            "Docker archive must describe one image")
    entry = manifest[0]
    name = path_name(entry.get("Config", ""))
    config_raw = documents.get(name)
    config = json.loads(config_raw) if config_raw is not None else None
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
    require(len(versions) == 1 or (allow_absent_image_version and not versions),
            "expected exactly one image version")
    version = versions[0] if versions else None
    # Only the version is public; never publish arbitrary environment/labels.
    safe_version = version if version is not None and re.fullmatch(r"[A-Za-z0-9._+:-]{1,128}", version) else None
    checks = {"linux_amd64": config.get("os") == "linux" and config.get("architecture") == "amd64",
              "tag_matches": entry.get("RepoTags") == [expected_tag],
              "entrypoint_matches": settings.get("Entrypoint") == entrypoint,
              "image_version_matches": version == "bazel-ci"}
    result = {"status": "ORIGINAL_ARCHIVE_CONFIGURATION_OBSERVED", "sha256": digest,
            "bytes": reader.size, "config_sha256": files[name], "expected_tag": expected_tag,
            "image_version": safe_version, "image_version_sha256": hashlib.sha256(version.encode()).hexdigest() if version is not None else None,
            "checks": checks, "matches_expected_configuration": all(checks.values()),
            "full_layer_validation": False, "independent_Make_manifest_checked": False,
            "source_admission": False, "VS_assembly_admission": False}
    if include_repo_tags:
        tags = entry.get("RepoTags")
        require(isinstance(tags, list) and 1 <= len(tags) <= 16 and
                all(isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", tag)
                    for tag in tags) and len(set(tags)) == len(tags), "Docker repository tags exceed observation bounds")
        result["repo_tags"] = tags
    if include_loader_metadata:
        labels = settings.get("Labels")
        label = labels.get("com.azure.sonic.manifest") if isinstance(labels, dict) else None
        latest_tag = expected_tag.rsplit(":", 1)[0] + ":latest"
        facts = {"expected_latest_tag": latest_tag,
                 "latest_tag_present": isinstance(entry.get("RepoTags"), list) and latest_tag in entry["RepoTags"],
                 "manifest_label_is_string": isinstance(label, str), "manifest_label_within_bound": False,
                 "manifest_label_json_valid": False, "manifest_label_jq_truthy": False,
                 "manifest_label_kind": None, "manifest_label_sha256": None, "manifest_label_bytes": None}
        if isinstance(label, str) and len(label) <= 65536:
            try:
                raw_label = label.encode("utf-8")
                require(len(raw_label) <= 65536, "Manifest label exceeds observation bound")
                facts.update(manifest_label_within_bound=True, manifest_label_bytes=len(raw_label),
                             manifest_label_sha256=hashlib.sha256(raw_label).hexdigest())
                def reject_constant(_):
                    raise ValueError("Nonstandard JSON literal")
                decoded = json.loads(label, parse_constant=reject_constant)
                kind = ("null" if decoded is None else "boolean" if isinstance(decoded, bool) else
                        "object" if isinstance(decoded, dict) else "array" if isinstance(decoded, list) else
                        "string" if isinstance(decoded, str) else "number")
                facts.update(manifest_label_json_valid=True, manifest_label_kind=kind,
                             manifest_label_jq_truthy=decoded is not None and decoded is not False)
            except (ValueError, UnicodeError, RecursionError):
                pass
        result["make_loader_observation"] = facts
    if include_repo_tags or allow_absent_image_version:
        result["image_version_entries"] = len(versions)
        result["absent_image_version_allowed"] = allow_absent_image_version
    return result


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
