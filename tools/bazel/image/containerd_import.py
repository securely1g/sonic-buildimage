"""Validated OCI manifests and the pinned containerd 1.7 build-side CLI."""

import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess

ADDRESS = "/run/containerd/containerd.sock"
NAMESPACE = "moby"
SNAPSHOTTER = "overlayfs"
VERSION = "1.7.28"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REFERENCE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference(value):
    require(isinstance(value, str) and REFERENCE.fullmatch(value), "expected a fully qualified image tag")
    registry = value.split("/", 1)[0]
    require("." in registry or ":" in registry or registry == "localhost", "image tag needs an explicit registry")
    return value


def validate_records(records):
    require(isinstance(records, list) and records, "store must contain images")
    tags, archives = {}, set()
    for record in records:
        require(isinstance(record, dict), "invalid image record")
        primary = reference(record.get("reference"))
        aliases = record.get("tags")
        require(isinstance(aliases, list) and all(isinstance(tag, str) for tag in aliases), "invalid image tags")
        require(primary in aliases and len(aliases) == len(set(aliases)), "missing primary or duplicate image tag")
        require(isinstance(record.get("digest"), str) and DIGEST.fullmatch(record["digest"]), "invalid image digest")
        require(isinstance(record.get("sha256"), str) and SHA256.fullmatch(record["sha256"]), "invalid archive digest")
        name = record.get("archive")
        require(isinstance(name, str) and re.fullmatch(r"images/docker-[a-z0-9][a-z0-9_.-]*\.oci\.tar", name), "invalid OCI archive path")
        require(name not in archives, "duplicate OCI archive")
        archives.add(name)
        for tag in aliases:
            reference(tag)
            require(tag not in tags, "conflicting image tag: " + tag)
            tags[tag] = record["digest"]
    return tags


def read_manifest(path, architecture):
    require(stat.S_ISREG(path.lstat().st_mode), "manifest must be a regular file")
    manifest = json.loads(path.read_text())
    require(type(manifest.get("schema")) is int and manifest["schema"] == 1, "invalid OCI manifest schema")
    require(manifest.get("architecture") == architecture, "OCI manifest architecture mismatch")
    validate_records(manifest.get("images"))
    return manifest


class Ctr:
    def __init__(self, executable="/usr/bin/ctr", address=None, runner=subprocess.run):
        self.prefix = [executable, "--address", address or ADDRESS, "--namespace", NAMESPACE]
        self.runner = runner

    def execute(self, *arguments, check=True):
        result = self.runner(self.prefix + list(arguments), check=False, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
        if check and result.returncode:
            raise RuntimeError("ctr failed: " + " ".join(arguments) + "\n" + (result.stderr or result.stdout)[-4000:])
        return result

    def command(self, *arguments):
        return self.execute(*arguments).stdout

    def ready_images(self, names, unpacked=True):
        # ctr 1.7 --quiet also reports complete-but-not-unpacked images.
        filters = ["name==" + json.dumps(name) for name in sorted(names)]
        result = self.execute("images", "check", "--snapshotter", SNAPSHOTTER, *filters, check=False)
        lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            require(result.returncode == 0, "ctr image check failed without an inventory")
            return {}
        require(lines[0] == ["REF", "TYPE", "DIGEST", "STATUS", "SIZE", "UNPACKED"], "unexpected ctr check table")
        require(result.returncode == 0 or len(lines) > 1, "ctr check failed without image status")
        ready = {}
        seen = set()
        for row in lines[1:]:
            require(len(row) >= 6 and DIGEST.fullmatch(row[2]) and row[-1] in {"true", "false"}, "invalid ctr image status")
            name, digest, status = row[0], row[2], row[3]
            require(name in names and name not in seen, "unexpected or duplicate ctr image")
            seen.add(name)
            if status == "complete" and (unpacked is None or (row[-1] == "true") == unpacked):
                ready[name] = digest
        return ready

    def inventory(self, expected, unpacked=True):
        names = set(self.command("images", "list", "--quiet").splitlines())
        require(names == set(expected), "containerd tag inventory differs from declared images")
        require(self.ready_images(names, unpacked=unpacked) == expected,
                "containerd content, digest or unpacked state differs")
