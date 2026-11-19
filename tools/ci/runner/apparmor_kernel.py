#!/usr/bin/env python3
"""Allow Bazel's kernel-build sandbox on a trusted SONiC runner."""
import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

PROFILE_NAME = "sonic-vs-kernel-sandbox"
PROFILE_DIR = Path("/etc/apparmor.d")
PROFILES = Path("/sys/kernel/security/apparmor/profiles")
RESTRICTION = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
CONTENTS = """# Managed by SONiC runner bootstrap for trusted kernel builds.
# The sandbox executes build commands; this is not a boundary for untrusted code.
abi <abi/4.0>,
include <tunables/global>
profile sonic-vs-kernel-sandbox /sonic/target/bazel-kernel-state/*/build/output/install/*/linux-sandbox flags=(unconfined) {
  userns,
}
"""


def restriction_enabled(restriction=RESTRICTION):
    try:
        value = restriction.read_text().strip()
    except FileNotFoundError:
        return False
    if value not in ("0", "1"):
        raise RuntimeError(f"Unexpected AppArmor user-namespace restriction value in {restriction}: {value!r}")
    return value == "1"


def read_managed_profile(profile):
    """Refuse another administrator's profile or a runner-writable policy file."""
    info = profile.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError(f"{profile} must be a root-owned regular file, not writable by group or others")
    contents = profile.read_text()
    if contents != CONTENTS:
        raise RuntimeError(f"{profile} contains other policy; review it before installing the SONiC profile")
    return contents


def install_profile(directory=PROFILE_DIR, parser="apparmor_parser"):
    """Parse first, persist only the managed policy, and reload this profile only."""
    profile = directory / PROFILE_NAME
    if profile.exists() or profile.is_symlink():
        read_managed_profile(profile)
    directory_info = directory.stat()
    if directory.is_symlink() or directory_info.st_uid != 0 or directory_info.st_mode & 0o022:
        raise RuntimeError(f"{directory} must be a root-owned directory, not writable by group or others")
    with tempfile.NamedTemporaryFile(mode="w", prefix=f".{PROFILE_NAME}.", dir=directory,
                                     delete=False) as temporary:
        staged = Path(temporary.name)
        temporary.write(CONTENTS)
    try:
        subprocess.run([parser, "-Q", "-K", str(staged)], check=True)
        staged.chmod(0o644)
        os.chown(staged, 0, 0)
        subprocess.run([parser, "-r", "-T", str(staged)], check=True)
        # The loaded profile name/attachment are declared in its contents, not
        # derived from the temporary filename. Persist only after a valid load.
        os.replace(staged, profile)
    finally:
        staged.unlink(missing_ok=True)


def check_kernel_sandbox(directory=PROFILE_DIR, profiles=PROFILES, restriction=RESTRICTION):
    if not restriction_enabled(restriction):
        print("AppArmor: unprivileged user-namespace restriction is not enabled")
        return
    profile = directory / PROFILE_NAME
    try:
        read_managed_profile(profile)
    except FileNotFoundError as error:
        raise RuntimeError("AppArmor blocks Bazel's kernel sandbox: run sudo python3 "
                           "tools/ci/runner/apparmor_kernel.py --install on the host, then retry preflight") from error
    try:
        loaded = profiles.read_text().splitlines()
    except PermissionError:
        print("NOTE: AppArmor loaded profiles are unreadable; managed kernel policy is configured. "
              "An administrator must verify it is loaded and test the sandbox inside the builder.")
        return
    except FileNotFoundError as error:
        raise RuntimeError("AppArmor namespace restriction is enabled but its policy interface is unavailable") from error
    if f"{PROFILE_NAME} (unconfined)" not in loaded:
        raise RuntimeError("The SONiC kernel sandbox AppArmor profile is not loaded; rerun "
                           "sudo python3 tools/ci/runner/apparmor_kernel.py --install on the host")
    print("AppArmor: managed kernel sandbox profile is loaded; validate actual sandbox execution inside the builder")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install and load the scoped host profile (root)")
    args = parser.parse_args()
    if not args.install:
        check_kernel_sandbox()
        return
    if os.geteuid() != 0:
        parser.error("Installation requires root")
    if not restriction_enabled(RESTRICTION):
        print("AppArmor: unprivileged user-namespace restriction is not enabled; no change needed")
        return
    if not PROFILES.exists():
        raise RuntimeError("AppArmor policy interface is unavailable; run this installer on the host")
    apparmor_parser = shutil.which("apparmor_parser")
    if apparmor_parser is None:
        raise RuntimeError("AppArmor namespace restriction is enabled; install the apparmor package first")
    install_profile(PROFILE_DIR, apparmor_parser)
    print("AppArmor: installed and loaded the SONiC kernel sandbox profile; host-wide restrictions are unchanged")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Kernel sandbox AppArmor setup failed: {error}")
