#!/usr/bin/env python3
"""Reclaim container-home and VS Bazel caches on an idle, dedicated host agent."""

import argparse
import os
from pathlib import Path
import subprocess

from .cleanup_vs_workspace import check_container_users, check_mounts


CACHE_NAMES = ("bazel", "bazel-disk", "bazel-repo")


def cleanup(runner_temp, runner_workspace):
    for label, root in (("RUNNER_TEMP", runner_temp), ("runner workspace", runner_workspace)):
        if not root.is_absolute() or not root.is_dir():
            raise ValueError(f"{label} must be an existing absolute directory")
    cache_groups = (
        (runner_temp.resolve(strict=True) / "_github_home" / ".cache", CACHE_NAMES),
        (runner_workspace.resolve(strict=True) / "vs-cache", ("bazel",)),
    )
    paths = []
    for cache_root, names in cache_groups:
        if cache_root.resolve() != cache_root:
            raise ValueError(f"Refusing redirected cache directory: {cache_root}")
        for name in names:
            path = cache_root / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError(f"Refusing unexpected Bazel cache path: {path}")
            if path.exists():
                paths.append(path)
    if not paths:
        print("No container-home or VS Bazel caches to remove")
        return
    for path in paths:
        check_container_users(path)
        check_mounts(path)
    for path in paths:
        if path.resolve() != path or not path.is_dir():
            raise ValueError(f"Bazel cache path changed during cleanup checks: {path}")
        print(f"Removing Bazel cache: {path}", flush=True)
        subprocess.run(
            ["sudo", "-n", "rm", "--recursive", "--force", "--one-file-system",
             "--", str(path)], check=True)
        if path.exists() or path.is_symlink():
            raise ValueError(f"Bazel cache still exists after cleanup: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        cleanup(Path(os.environ["RUNNER_TEMP"]), args.runner_workspace)
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Runner Bazel cache cleanup failed: {error}\n")


if __name__ == "__main__":
    main()
