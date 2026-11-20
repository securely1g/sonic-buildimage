#!/usr/bin/env python3
"""Install a VS .bin on an empty QEMU disk and verify its running kernel and modules."""

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

import pexpect

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from validate_sonic_version import read_installer_version

# Recovery media used by platform/vs/onie.mk (EFI-capable ISO also boots with BIOS).
ONIE_PATH = "onie/efi/onie-recovery-x86_64-kvm_x86_64-r0.iso"
ONIE_SHA256 = "23d2d1240ac0c3b82eabcb73032201d93b34c85a196b819ee02d2e1853974b9c"
MODULES = ("virtio_blk", "virtio_net", "squashfs", "overlay")
GRUB = "The highlighted entry will be executed"
VERSION_KEYS = ("build_version", "commit_id", "asic_type", "kernel_version")


def digest(stream):
    checksum = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        checksum.update(chunk)
    return checksum.hexdigest()


def file_digest(path):
    with path.open("rb") as stream:
        return digest(stream)


def package_modules(package):
    """Get the ABI and the four required module hashes from the actual selected .deb."""
    stems = set()
    records = {}
    with subprocess.Popen(["dpkg-deb", "--fsys-tarfile", package], stdout=subprocess.PIPE) as proc:
        with proc.stdout, tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for entry in archive:
                path = entry.name.removeprefix("./")
                if path.startswith("boot/vmlinuz-") and entry.isfile():
                    stems.add(path.removeprefix("boot/vmlinuz-"))
                match = re.fullmatch(r"(?:usr/)?lib/modules/([^/]+)/(kernel/.*/([^/]+)\.ko(?:\.(?:xz|gz|zst))?)", path)
                if match and entry.isfile() and match[3] in MODULES:
                    name = match[3]
                    if name in records:
                        raise ValueError(f"duplicate {name} in {package}")
                    with archive.extractfile(entry) as module:
                        records[name] = {"release": match[1], "path": match[2], "sha256": digest(module)}
            # Drain tar's zero padding so dpkg-deb can exit normally.
            while proc.stdout.read(1024 * 1024):
                pass
        if proc.wait():
            raise ValueError(f"could not read the kernel package {package}")
    if len(stems) != 1 or set(records) != set(MODULES):
        raise ValueError(f"{package} has kernel(s) {sorted(stems)} and module(s) {sorted(records)}")
    (release,) = stems
    if any(item["release"] != release for item in records.values()):
        raise ValueError("module ABI differs from the selected kernel image")
    return release, records


@contextlib.contextmanager
def vm(args, log):
    child = pexpect.spawn("qemu-system-x86_64", args, encoding="utf-8", codec_errors="replace",
                          echo=False, timeout=90)
    child.logfile_read = log
    try:
        yield child
    except (pexpect.EOF, pexpect.TIMEOUT):
        # pexpect's default exception includes its last send (which could be a password).
        raise RuntimeError(f"serial console {type(exc).__name__}; see serial log") from None
    finally:
        child.logfile_read = None
        if child.isalive():
            child.kill(signal.SIGTERM)
        child.close(force=True)


# Run inside the newly installed guest as an ordinary user. Check all four
# modules are active and return the bytes found by modinfo for comparison.
GUEST_PROBE = """
import hashlib, json, os, pathlib, subprocess, yaml
release = os.uname().release
directory = pathlib.Path("/lib/modules", release).resolve()
loaded = {line.split()[0] for line in pathlib.Path("/proc/modules").read_text().splitlines()}
version = yaml.safe_load(pathlib.Path("/etc/sonic/sonic_version.yml").read_text())
result = {"release": release, "version": {k: version[k] for k in VERSION_KEYS}, "modules": {}}
for name in MODULES:
    filename = subprocess.check_output(["/sbin/modinfo", "-F", "filename", name], text=True).strip()
    path = pathlib.Path(filename).resolve()
    vermagic = subprocess.check_output(["/sbin/modinfo", "-F", "vermagic", name], text=True).split()[0]
    result["modules"][name] = {"path": str(path.relative_to(directory)), "release": vermagic,
                               "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "loaded": name in loaded and pathlib.Path("/sys/module", name).is_dir()}
print("HOST_BASE_GUEST " + json.dumps(result, separators=(",", ":")))
"""


def check_guest(observed, release, modules, version):
    if observed["release"] != release or observed["version"] != version:
        raise ValueError(f"unexpected booted kernel or SONiC version: {observed}")
    for name in MODULES:
        actual = observed["modules"][name]
        if actual != dict(modules[name], loaded=True):
            raise ValueError(f"{name}: guest {actual}, expected {modules[name]} and loaded")


def boot(args, work, result):
    installer = args.installer.resolve(strict=True)
    result["installer_sha256"] = file_digest(installer)
    result["kernel_deb_sha256"] = file_digest(args.kernel_deb)
    release, modules = package_modules(args.kernel_deb)
    metadata = read_installer_version(installer)
    version = {key: metadata[key] for key in VERSION_KEYS}
    if any(not isinstance(value, str) or not value.strip() for value in version.values()):
        raise ValueError(f"missing SONiC version metadata: {version}")
    result.update(expected_release=release, expected_modules=modules, installer_version=version)

    iso = args.onie_iso
    if not iso:
        iso = work / "onie.iso"
        mirror = os.environ.get("BUILD_PUBLIC_URL", "https://packages.trafficmanager.net/public")
        with urllib.request.urlopen(f"{mirror.rstrip('/')}/{ONIE_PATH}", timeout=120) as src, iso.open("wb") as dest:
            shutil.copyfileobj(src, dest)
    result["onie_iso_sha256"] = file_digest(iso)
    if result["onie_iso_sha256"] != ONIE_SHA256:
        raise ValueError("ONIE recovery ISO checksum mismatch")

    disk = work / "empty.qcow2"
    seed = work / "seed.ext2"
    contents = work / "seed"
    contents.mkdir(mode=0o755)
    subprocess.run(["cp", "--reflink=auto", "--", installer, contents / "onie-installer.bin"], check=True)
    with seed.open("xb") as stream:
        stream.truncate(installer.stat().st_size + max(128 << 20, installer.stat().st_size // 10))
    subprocess.run(["mkfs.ext2", "-q", "-F", "-m", "0", "-d", contents, seed], check=True)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", disk, "24G"], check=True)
    accel = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg,thread=multi"
    result["accel"] = accel
    common = ["-machine", "q35,smm=on", "-accel", accel, "-smp", "4", "-m", "10240",
              "-display", "none", "-monitor", "none", "-serial", "stdio",
              "-netdev", "user,id=vs,restrict=on", "-device", "virtio-net-pci,netdev=vs",
              "-drive", f"file={disk},if=virtio,index=0,format=qcow2"]
    install = common + ["-boot", "order=cd,once=d", "-cdrom", str(iso),
                        "-drive", f"file={seed},if=virtio,index=1,format=raw"]
    serial = args.output.with_suffix(".serial.log")
    with serial.open("w", buffering=1) as log:
        print("Installing the supplied sonic-vs.bin from ONIE on the empty disk", flush=True)
        start = time.monotonic()
        with vm(install, log) as child:
            child.expect_exact(GRUB, timeout=180)
            child.send("\x1b[B\r")  # ONIE: Embed
            child.expect_exact("ONIE: Install OS", timeout=args.timeout)
            child.expect_exact(GRUB, timeout=120)
            child.send("\r")
            child.expect_exact(GRUB, timeout=args.timeout)
        result["install_seconds"] = round(time.monotonic() - start, 3)
        print("Booting the disk with both recovery media removed", flush=True)
        start = time.monotonic()
        with vm(common, log) as child:
            child.expect_exact(GRUB, timeout=180)
            child.send("\r")
            child.expect(r"sonic login:", timeout=args.timeout)
            child.sendline(args.username)
            # Keep authentication and shell echo out of the serial logs.
            child.logfile_read = None
            child.expect_exact("Password:")
            child.sendline(os.environ.get("SONIC_PASSWORD", "YourPaSsWoRd"))
            prompt = re.escape(args.username) + r"@sonic:[^\r\n]*[#$] "
            child.expect(prompt, timeout=120)
            child.sendline("stty -echo")
            child.expect(prompt, timeout=120)
            child.logfile_read = log
            probe = f"MODULES={MODULES!r}\nVERSION_KEYS={VERSION_KEYS!r}\n" + GUEST_PROBE
            encoded = base64.b64encode(probe.encode()).decode()
            child.sendline(f"printf %s '{encoded}' | base64 -d | python3")
            child.expect(r"(?m)^HOST_BASE_GUEST (\{[^\r\n]*\})\r*$", timeout=120)
            observed = json.loads(child.match[1])
        result["boot_probe_seconds"] = round(time.monotonic() - start, 3)
        result["guest"] = observed
        check_guest(observed, release, modules, version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--kernel-deb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--onie-iso", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per install/boot phase")
    parser.add_argument("--username", default=os.environ.get("SONIC_USERNAME", "admin"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {"verified": False, "worker": os.environ.get("RUNNER_NAME") or socket.gethostname(),
              "installer": args.installer.name, "kernel_deb": args.kernel_deb.name}
    try:
        with tempfile.TemporaryDirectory(prefix="vs-host-boot-", dir=args.work_dir) as temporary:
            boot(args, Path(temporary), result)
        result["verified"] = True
        return 0
    except Exception as error:
        result["error"] = str(error)
        print(f"Boot verification failed: {error}", file=sys.stderr)
        return 1
    finally:
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
