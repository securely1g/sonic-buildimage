#!/usr/bin/env python3
"""Prepare opt-in NFS repository caches without moving action or server state."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


MODES = ("off", "downloads", "downloads-and-contents")


def prepare(mode, root, version):
    if mode not in MODES:
        raise ValueError("Unsupported NFS repository cache mode")
    if mode == "off":
        return {}, {"mode": mode}
    if (not root.is_absolute() or "\n" in str(root) or "\r" in str(root) or
            not root.is_dir() or root.resolve() != root):
        raise ValueError("Provision an existing, absolute, nonredirected NFS cache root")
    filesystem = subprocess.check_output(
        ["stat", "--file-system", "--format=%T", "--", str(root)], text=True).strip()
    if filesystem not in ("nfs", "nfs4"):
        raise ValueError(f"Repository cache must be NFS-backed, found {filesystem}")
    if mode == "downloads-and-contents" and version != "8.5.1":
        raise ValueError("Revalidate prepared repository caching before changing Bazel 8.5.1")
    namespace = "uid-" + str(os.getuid())
    paths = {"SONIC_BAZEL_REPOSITORY_CACHE_SOURCE": root / "downloads" / namespace}
    if mode == "downloads-and-contents":
        paths["SONIC_BAZEL_REPO_CONTENTS_CACHE_SOURCE"] = (
            root / "contents" / "bazel-8.5.1" / "vs-amd64" / namespace)
    for path in paths.values():
        current = root
        for component in path.relative_to(root).parts:
            current /= component
            if current.is_symlink():
                raise ValueError(f"Cache directory is redirected: {current}")
            current.mkdir(mode=0o700 if current == path else 0o755, exist_ok=True)
            if current.resolve() != current:
                raise ValueError(f"Cache directory is redirected: {current}")
        if path.stat().st_uid != os.getuid():
            raise ValueError(f"Cache directory is owned by another user: {path}")
        # Probe only a new temporary file; never alter existing cache ownership or locks.
        with tempfile.TemporaryFile(dir=path) as probe:
            probe.write(b"NFS repository cache probe")
            probe.flush()
            fcntl.lockf(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probe.seek(0)
            if probe.read() != b"NFS repository cache probe":
                raise ValueError("NFS repository cache readback failed")
    exports = {name: str(path) for name, path in paths.items()}
    return exports, {"mode": mode, "filesystem": filesystem, "paths": exports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--root", type=Path, default=Path("/nfs/bazel/repository-caches"))
    parser.add_argument("--bazel-version-file", type=Path, default=Path(".bazelversion"))
    parser.add_argument("--github-env", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        version = args.bazel_version_file.read_text().strip() if args.mode != "off" else ""
        exports, receipt = prepare(args.mode, args.root, version)
        with args.github_env.open("a") as output:
            for name in ("SONIC_BAZEL_REPOSITORY_CACHE_SOURCE",
                         "SONIC_BAZEL_REPO_CONTENTS_CACHE_SOURCE"):
                output.write(f"{name}={exports.get(name, '')}\n")
        receipt["setup_seconds"] = time.monotonic() - started
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        print(json.dumps(receipt, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"NFS repository cache preparation failed: {error}\n")


if __name__ == "__main__":
    main()
