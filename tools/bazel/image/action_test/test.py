"""Exercise public Bazel rule actions with declared fixtures and tool runfiles.

The fixture tools make the action boundary observable. Independent format tests
remain in assembly_test.py; this test does not claim SquashFS or qcow2 acceptance.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile


parser = argparse.ArgumentParser()
for name in ("squashfs", "docker-store", "installer", "disk", "architecture"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()

with tarfile.open(args.squashfs) as archive:
    member = archive.getmember("usr/bin/action-probe")
    if (member.uid, member.gid, member.mode) != (1001, 42, 0o4750) or archive.extractfile(member).read() != b"probe":
        raise AssertionError("SquashFS action fixture lost the declared layer metadata or bytes")

with tarfile.open(args.docker_store) as archive:
    repositories = json.load(archive.extractfile("image/overlay2/repositories.json"))["Repositories"]
    if "action-probe:action-probe" not in repositories["action-probe"]:
        raise AssertionError("Docker store action fixture lost its configured tag")
    member = next(member for member in archive if member.name.endswith("/diff/usr/bin/action-probe"))
    if archive.extractfile(member).read() != b"probe":
        raise AssertionError("Docker store action fixture lost its layer bytes")
    config = next(member for member in archive if member.name.startswith("image/overlay2/imagedb/content/sha256/"))
    if json.load(archive.extractfile(config))["architecture"] != args.architecture:
        raise AssertionError("Docker store action fixture selected the wrong architecture")

body = Path(args.installer).read_bytes().split(b"exit_marker\n", 1)[1]
with tarfile.open(fileobj=io.BytesIO(body)) as archive:
    archive.getmember("installer/onie-image-" + args.architecture + ".conf")
    with zipfile.ZipFile(io.BytesIO(archive.extractfile("installer/fs.zip").read())) as payload:
        if payload.read("boot/vmlinuz-action-probe") != b"kernel" or payload.read("fs.squashfs") != Path(args.squashfs).read_bytes():
            raise AssertionError("ONIE action fixture lost its declared kernel or rootfs bytes")

if Path(args.disk).read_bytes() != b"PR70 action fixture: qcow2\n":
    raise AssertionError("Disk action fixture did not reach the declared qcow2 tool")

outputs = {name: getattr(args, name) for name in ("squashfs", "docker_store", "installer", "disk")}
receipt = {
    "architecture": args.architecture,
    "outputs": {name: {"sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "size": Path(path).stat().st_size} for name, path in outputs.items()},
    "scope": "Public Bazel action wiring and helper/tool runfiles with declared fixtures. SquashFS and qcow2 tools are stubs; tar-split metadata is a stub. This does not establish image format, initramfs, product image, installer boot, or runtime acceptance.",
}
Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"], "action-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
