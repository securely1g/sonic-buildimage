#!/usr/bin/env python3
"""Restore directory ownership of Bazel's default root-container startup state."""

import argparse
from contextlib import ExitStack
import os
from pathlib import Path


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def mount_id(descriptor):
    for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise ValueError("Cannot determine the startup directory's mount")


def repair_directory(descriptor, device, mount, uid, gid):
    os.fchown(descriptor, uid, gid)
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                if os.fstat(child).st_dev == device and mount_id(child) == mount:
                    repair_directory(child, device, mount, uid, gid)
            finally:
                os.close(child)


def repair_startup(home):
    if not home.is_absolute():
        raise ValueError("The container home must be an absolute path")
    with ExitStack() as descriptors:
        descriptor = os.open("/", DIRECTORY_FLAGS)
        descriptors.callback(os.close, descriptor)
        for component in home.parts[1:]:
            descriptor = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.callback(os.close, descriptor)
        mount = mount_id(descriptor)
        device = os.fstat(descriptor).st_dev
        for component in (".cache", "bazel", "_bazel_root"):
            try:
                os.mkdir(component, 0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.callback(os.close, descriptor)
            if os.fstat(descriptor).st_dev != device or mount_id(descriptor) != mount:
                raise ValueError("Refusing startup repair on a redirected cache mount")
        repair_directory(descriptor, device, mount, os.geteuid(), os.getegid())
    return home / ".cache/bazel/_bazel_root"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise ValueError("Startup repair is restricted to GitHub Actions")
        if os.environ.get("HOME") != "/github/home":
            raise ValueError("Expected the container home /github/home")
        if os.geteuid() != 0 or not Path("/.dockerenv").is_file():
            raise ValueError("Startup repair requires a root job container")
        state = repair_startup(Path("/github/home"))
    except (OSError, ValueError) as error:
        parser.exit(1, "Cannot repair Bazel startup ownership: " + str(error) + "\n")
    print(f"Restored startup directory ownership to {os.geteuid()}:{os.getegid()}: {state}")
    print("Regular files, symlinks and repository/disk caches were not modified.")


if __name__ == "__main__":
    main()
