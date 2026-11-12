#!/usr/bin/env python3
"""Reclaim container-job Bazel caches from previous jobs on an idle, dedicated host agent."""

import argparse
import os
from pathlib import Path
import subprocess

from .cleanup_vs_workspace import check_container_users, check_mounts


CACHE_NAMES = ("bazel", "bazel-disk", "bazel-repo")


def cleanup(runner_temp):
    if not runner_temp.is_absolute() or not runner_temp.is_dir():
        raise ValueError("RUNNER_TEMP must be an existing absolute directory")
    cache_root = runner_temp.resolve(strict=True) / "_github_home" / ".cache"
    if cache_root.resolve() != cache_root:
        raise ValueError(f"Refusing redirected container cache directory: {cache_root}")
    paths = []
    for name in CACHE_NAMES:
        path = cache_root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"Refusing unexpected Bazel cache path: {path}")
        if path.exists():
            paths.append(path)
    if not paths:
        print(f"No container-job Bazel caches to remove under {cache_root}")
        return
    check_container_users(cache_root)
    check_mounts(cache_root)
    for path in paths:
        print(f"Removing container-job Bazel cache: {path}", flush=True)
        subprocess.run(
            ["sudo", "-n", "rm", "--recursive", "--force", "--one-file-system",
             "--", str(path)], check=True)
        if path.exists() or path.is_symlink():
            raise ValueError(f"Bazel cache still exists after cleanup: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        cleanup(Path(os.environ["RUNNER_TEMP"]))
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Runner Bazel cache cleanup failed: {error}\n")


if __name__ == "__main__":
    main()
