#!/usr/bin/env python3
"""Allocate fresh installer scratch on a qualifying filesystem."""

import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile


MIN_FREE = 300 * 1024**3


def check_filesystem(path, expected_device):
    if path.resolve() != path:
        raise ValueError("build area must not contain symlinks")
    if path.stat().st_dev != expected_device:
        raise ValueError("build area must stay on the selected filesystem")
    result = subprocess.run(["findmnt", "--json", "--output", "FSTYPE,OPTIONS", "--target", str(path)],
                            check=True, capture_output=True, text=True, timeout=10)
    mounts = json.loads(result.stdout)["filesystems"]
    if len(mounts) != 1 or mounts[0]["fstype"] != "ext4":
        raise ValueError("build area must use ext4")
    if {"ro", "nodev", "nosuid", "noexec"} & set(mounts[0]["options"].split(",")):
        raise ValueError("build area filesystem cannot support image construction")
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError("build area must be writable by the runner")
    free = shutil.disk_usage(path).free
    if free < MIN_FREE:
        raise ValueError("VS native source build needs at least 300 GiB of free disk")
    return free


def allocate(homes, temporary=Path("/var/tmp")):
    """Prefer a durable account home; keep fallback scratch uniquely owned."""
    candidates = [(Path(home) / ".local/share/sonic-vs-installer", "persistent")
                  for home in dict.fromkeys(homes) if home]
    candidates.append((temporary, "host temporary-directory retention"))
    errors = []
    for parent, retention in candidates:
        try:
            parent = parent.absolute()
            if "," in str(parent) or "\n" in str(parent):
                raise ValueError("invalid build parent")
            existing = parent
            while not existing.exists() and not existing.is_symlink():
                existing = existing.parent
            # A qualifying home may use a separate filesystem from the OS.
            # Keep this candidate's new directories on its checked device.
            device = existing.stat().st_dev
            check_filesystem(existing, device)
            # Existing per-user directories must belong to this runner. The
            # shared fallback is safe only with standard sticky-directory rules.
            if parent == temporary:
                mode = parent.stat().st_mode
                if parent.stat().st_uid != 0 or not mode & stat.S_ISVTX:
                    raise ValueError("temporary parent must be root-owned and sticky")
            else:
                if existing.stat().st_uid != os.getuid():
                    raise ValueError("persistent parent must belong to the runner")
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if (parent.is_symlink() or parent.stat().st_uid != os.getuid()
                        or parent.stat().st_mode & 0o022):
                    raise ValueError("persistent parent must belong to the runner")
            check_filesystem(parent, device)
            private = Path(tempfile.mkdtemp(prefix="sonic-vs-installer-", dir=parent))
            root = private / "build"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            free = check_filesystem(root, device)
            if private.stat().st_uid != os.getuid() or stat.S_IMODE(private.stat().st_mode) != 0o700:
                raise ValueError("new build area must be private and runner-owned")
            state = root / "state"
            state.mkdir()
            record = {"schema": 1, "build_root": str(root), "device": root.stat().st_dev,
                      "filesystem": "ext4", "free_bytes_at_allocation": free,
                      "scratch_retention": retention, "automatic_cleanup": False,
                      "cache_policy": "Existing repository/package caches stay at the supplied cache root."}
            (state / "build-area.json").write_text(json.dumps(record, indent=2) + "\n")
            return root / "checkout", state
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            errors.append(str(parent) + ": " + str(error))
    raise ValueError("no suitable installer build area:\n" + "\n".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args()
    # Account and service homes, including a service home on /data, are
    # candidates only after the same live filesystem and ownership checks.
    workspace, state = allocate([pwd.getpwuid(os.getuid()).pw_dir, os.environ.get("HOME")])
    with args.github_env.open("a") as output:
        output.write("VS_WORKSPACE=" + str(workspace) + "\nVS_STATE=" + str(state) + "\n")
    print((state / "build-area.json").read_text(), end="")


if __name__ == "__main__":
    main()
