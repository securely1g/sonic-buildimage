"""Encrypt one already-observed public job hook for private source review."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from tools.bazel.ci import native_apt_cache_policy as policy


HOOK = policy.MOUNT / "proxy-job-started.sh"
HOOK_SIZE = 4585
HOOK_SHA = "e1a0b2becd466697bc53c47adff8425f3f9912aafcd26f2595343a90d918d4d3"
RECIPIENT_SIZE = 1415
RECIPIENT_SHA = "f10ae6635ebd8db99ad573a0f67cfd29507e64f03082220145643ea4ba62cf50"


def recipient_bytes(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        before = os.fstat(source.fileno())
        policy.require(stat.S_ISREG(before.st_mode) and before.st_size == RECIPIENT_SIZE
                       and before.st_uid == os.getuid() and not before.st_mode & 0o022)
        data = source.read(4097)
        after = os.fstat(source.fileno())
        policy.require(policy.file_identity(before) == policy.file_identity(after)
                       and policy.file_identity(path.lstat()) == policy.file_identity(after)
                       and len(data) == RECIPIENT_SIZE <= 4096
                       and hashlib.sha256(data).hexdigest() == RECIPIENT_SHA)
    return data


def encrypt(data, certificate):
    # The public certificate is immutable pipe data, never a mutable pathname
    # reopened after verification. The original script goes only to stdin.
    read_fd, write_fd = os.pipe()
    try:
        policy.require(len(certificate) <= os.fpathconf(write_fd, "PC_PIPE_BUF"))
        policy.require(os.write(write_fd, certificate) == len(certificate))
        os.close(write_fd)
        write_fd = None
        result = subprocess.run([
            "/usr/bin/openssl", "cms", "-encrypt", "-binary", "-aes-256-gcm",
            "-outform", "DER", "-recip", f"/proc/self/fd/{read_fd}",
        ], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(read_fd,), timeout=20, check=False)
        policy.require(result.returncode == 0 and 0 < len(result.stdout) <= 65536)
        return result.stdout
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def write_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
        output.write(data)


def observe(recipient, parent):
    policy.require(parent.resolve(strict=True) == parent)
    info = parent.stat()
    policy.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                   and stat.S_IMODE(info.st_mode) == 0o700)
    output = parent / "execution"
    policy.require(not os.path.lexists(output))
    certificate = recipient_bytes(recipient)
    original, metadata = policy.read_public_file(HOOK, policy.MOUNT, HOOK_SIZE)
    policy.require(len(original) == metadata["bytes"] == HOOK_SIZE
                   and hashlib.sha256(original).hexdigest() == metadata["sha256"] == HOOK_SHA
                   and metadata["uid"] == 0 and metadata["mode"] == 0o555)
    ciphertext = encrypt(original, certificate)
    report = {
        "schema": 1, "status": "encrypted_for_source_review",
        "source": {"bytes": HOOK_SIZE, "sha256": HOOK_SHA, "uid": 0, "mode": 0o555},
        "recipient": {"bytes": RECIPIENT_SIZE, "sha256": RECIPIENT_SHA},
        "ciphertext": {"name": "encrypted.cms", "bytes": len(ciphertext),
                       "sha256": hashlib.sha256(ciphertext).hexdigest(),
                       "format": "CMS DER AuthEnvelopedData", "cipher": "AES-256-GCM"},
        "source_executed": False, "settings_read": False, "network_probe": False,
        "policy_admitted": False, "apt_invoked": False,
    }
    output.mkdir(mode=0o700)
    write_private(output / "encrypted.cms", ciphertext)
    write_private(output / "lineage.json", (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", type=Path, required=True)
    parser.add_argument("--output-parent", type=Path, required=True)
    args = parser.parse_args()
    try:
        observe(args.recipient, args.output_parent)
    except Exception as error:
        raise SystemExit("Hook source observation failed: " + type(error).__name__) from None


if __name__ == "__main__":
    main()
