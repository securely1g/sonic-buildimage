#!/usr/bin/env python3
"""Reclaim selected local build caches on an idle, dedicated host agent."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess

from .cleanup_vs_workspace import check_container_users, check_mounts


CACHE_NAMES = ("bazel", "bazel-disk", "bazel-repo")


def remove_cache(path):
    if path.resolve() != path or not path.is_dir():
        raise ValueError(f"Local cache path changed during cleanup checks: {path}")
    print(f"Removing local build cache: {path}", flush=True)
    subprocess.run(
        ["sudo", "-n", "rm", "--recursive", "--force", "--one-file-system",
         "--", str(path)], check=True)
    if path.exists() or path.is_symlink():
        raise ValueError(f"Local cache still exists after cleanup: {path}")


def within_retention_budget(path, max_gib, min_free_gib):
    size = int(subprocess.check_output(
        ["sudo", "-n", "du", "--summarize", "--one-file-system",
         "--block-size=1", "--", str(path)], text=True).partition("\t")[0])
    if size < 0:
        raise ValueError(f"Invalid cache size for {path}")
    free = shutil.disk_usage(path).free
    print(f"VS cache: {size / 1024**3:.2f} GiB; free: {free / 1024**3:.2f} GiB",
          flush=True)
    return size <= max_gib * 1024**3 and free >= min_free_gib * 1024**3


def cleanup(runner_temp, runner_workspace, *, remove_package_cache=False,
            retain_vs_cache_max_gib=0, min_free_gib=200):
    if retain_vs_cache_max_gib < 0 or min_free_gib < 0:
        raise ValueError("Cache size and free-space limits must be nonnegative")
    for label, root in (("RUNNER_TEMP", runner_temp), ("runner workspace", runner_workspace)):
        if not root.is_absolute() or not root.is_dir():
            raise ValueError(f"{label} must be an existing absolute directory")
    cache_groups = (
        (runner_temp.resolve(strict=True) / "_github_home" / ".cache", CACHE_NAMES),
        (runner_workspace.resolve(strict=True) / "vs-cache",
         ("bazel", "packages") if remove_package_cache else ("bazel",)),
    )
    paths = []
    for cache_root, names in cache_groups:
        if cache_root.resolve() != cache_root:
            raise ValueError(f"Refusing redirected cache directory: {cache_root}")
        for name in names:
            path = cache_root / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError(f"Refusing unexpected local cache path: {path}")
            if path.exists():
                paths.append(path)
    if not paths:
        print("No selected local build caches to remove")
        return
    for path in paths:
        check_container_users(path)
        check_mounts(path)
    vs_cache = runner_workspace.resolve(strict=True) / "vs-cache" / "bazel"
    # Remove obsolete caches first, so their space is available to the retention decision.
    if retain_vs_cache_max_gib:
        paths.sort(key=lambda path: path == vs_cache)
    for path in paths:
        if path.resolve() != path or not path.is_dir():
            raise ValueError(f"Local cache path changed during cleanup checks: {path}")
        if path == vs_cache and retain_vs_cache_max_gib:
            retain = within_retention_budget(path, retain_vs_cache_max_gib, min_free_gib)
            if not retain:
                # Prefer keeping downloaded/extracted toolchains over bulky action outputs.
                disk_cache = path / "disk_cache"
                if disk_cache.exists() or disk_cache.is_symlink():
                    remove_cache(disk_cache)
                    retain = within_retention_budget(path, retain_vs_cache_max_gib, min_free_gib)
            if retain:
                print(f"Retaining bounded VS build cache: {path}", flush=True)
                continue
            print("VS cache exceeds retention size or free-space budget", flush=True)
        remove_cache(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-workspace", type=Path, required=True)
    parser.add_argument("--remove-package-cache", action="store_true",
                        help="Also remove vs-cache/packages after NFS cache validation")
    parser.add_argument("--retain-vs-cache-max-gib", type=int, default=0,
                        help="Retain vs-cache/bazel within this on-disk size (0 removes it)")
    parser.add_argument("--min-free-gib", type=int, default=200,
                        help="Minimum free space required to retain the VS cache")
    args = parser.parse_args()
    try:
        cleanup(Path(os.environ["RUNNER_TEMP"]), args.runner_workspace,
                remove_package_cache=args.remove_package_cache,
                retain_vs_cache_max_gib=args.retain_vs_cache_max_gib,
                min_free_gib=args.min_free_gib)
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Runner local cache cleanup failed: {error}\n")


if __name__ == "__main__":
    main()
