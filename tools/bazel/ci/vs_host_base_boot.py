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
import zlib

import pexpect

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from validate_sonic_version import read_installer_version

# Recovery media used by platform/vs/onie.mk (EFI-capable ISO also boots with BIOS).
ONIE_PATH = "onie/efi/onie-recovery-x86_64-kvm_x86_64-r0.iso"
ONIE_SHA256 = "23d2d1240ac0c3b82eabcb73032201d93b34c85a196b819ee02d2e1853974b9c"
ACTIVE_MODULES = ("virtio_blk", "virtio_net", "squashfs", "overlay")
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


def configured_modules():
    names = []
    for line in Path("files/initramfs-tools/modules").read_text().splitlines():
        fields = line.partition("#")[0].split()
        if fields:
            names.append(fields[0].replace("-", "_"))
    return names


def package_modules(package, required):
    """Collect requested modules from the kernel image package, including built-ins."""
    releases = set()
    records = {}
    with subprocess.Popen(["dpkg-deb", "--fsys-tarfile", package], stdout=subprocess.PIPE) as proc:
        with proc.stdout, tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for entry in archive:
                if not entry.isfile():
                    continue
                path = entry.name.removeprefix("./")
                if path.startswith("boot/vmlinuz-"):
                    releases.add(path.removeprefix("boot/vmlinuz-"))
                location = re.fullmatch(r"(?:usr/)?lib/modules/([^/]+)/(.*)", path)
                if not location:
                    continue
                release, relative = location.groups()
                if relative == "modules.builtin":
                    with archive.extractfile(entry) as manifest:
                        for builtin in manifest.read().decode().splitlines():
                            name = Path(builtin).stem.replace("-", "_")
                            if name in required:
                                if name in records:
                                    raise ValueError(f"duplicate {name} in {package}")
                                records[name] = {"kind": "builtin", "release": release, "path": builtin}
                match = re.fullmatch(r"kernel/.*/([^/]+)\.ko(?:\.(?:xz|gz|zst))?", relative)
                if match and (name := match[1].replace("-", "_")) in required:
                    if name in records:
                        raise ValueError(f"duplicate {name} in {package}")
                    with archive.extractfile(entry) as module:
                        records[name] = {"kind": "module", "release": release,
                                         "path": relative, "sha256": digest(module)}
            # Drain tar's zero padding so dpkg-deb can exit normally.
            while proc.stdout.read(1024 * 1024):
                pass
        if proc.wait():
            raise ValueError(f"could not read the kernel package {package}")
    if len(releases) != 1 or set(records) != set(required):
        raise ValueError(f"{package} has kernel(s) {sorted(releases)}; missing modules: {sorted(set(required) - records.keys())}")
    (release,) = releases
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
    except (pexpect.EOF, pexpect.TIMEOUT) as exc:
        # pexpect's default exception includes its last send (which could be a password).
        raise RuntimeError(f"serial console {type(exc).__name__}; see serial log") from None
    finally:
        child.logfile_read = None
        if child.isalive():
            child.kill(signal.SIGTERM)
            # QEMU flushes qcow2 writes and tears down guest RAM before exiting.
            # pexpect.close() normally allows just a fraction of a second.
            try:
                child.expect(pexpect.EOF, timeout=90)
            except pexpect.TIMEOUT:
                child.kill(signal.SIGKILL)
                child.expect(pexpect.EOF, timeout=30)
                raise RuntimeError("QEMU did not stop cleanly after SIGTERM") from None
        child.close()


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
    if filename == "(builtin)":
        builtins = {pathlib.Path(line).stem.replace("-", "_"): line
                    for line in (directory / "modules.builtin").read_text().splitlines()}
        module = {"kind": "builtin", "release": release, "path": builtins[name]}
        active = pathlib.Path("/sys/module", name).is_dir()
    else:
        path = pathlib.Path(filename).resolve()
        vermagic = subprocess.check_output(["/sbin/modinfo", "-F", "vermagic", name], text=True).split()[0]
        module = {"kind": "module", "release": vermagic, "path": str(path.relative_to(directory)),
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        active = name in loaded and pathlib.Path("/sys/module", name).is_dir()
    result["modules"][name] = dict(module, loaded=active)
print("HOST_BASE_GUEST " + json.dumps(result, separators=(",", ":")))
"""


def check_guest(observed, release, modules, version):
    if observed["release"] != release or observed["version"] != version:
        raise ValueError(f"unexpected booted kernel or SONiC version: {observed}")
    if set(observed["modules"]) != set(modules):
        raise ValueError("module inventory differs from the selected kernel package")
    for name, expected in modules.items():
        actual = dict(observed["modules"][name])
        active = actual.pop("loaded")
        if actual != expected or (name in ACTIVE_MODULES and not active):
            raise ValueError(f"{name}: guest {observed['modules'][name]}, expected {expected}, "
                             f"required active: {name in ACTIVE_MODULES}")


def probe_guest(common, log, username, timeout, modules):
    with vm(common + ["-snapshot"], log) as child:
        child.expect_exact(GRUB, timeout=180)
        child.send("\r")
        child.expect(r"sonic login:", timeout=timeout)
        child.sendline(username)
        # Keep authentication and shell echo out of the serial logs.
        child.logfile_read = None
        child.expect_exact("Password:")
        child.sendline(os.environ.get("SONIC_PASSWORD", "YourPaSsWoRd"))
        prompt = re.escape(username) + r"@sonic:[^\r\n]*[#$] "
        child.expect(prompt, timeout=120)
        child.sendline("stty -echo")
        child.expect(prompt, timeout=120)
        child.logfile_read = log
        probe = f"MODULES={tuple(sorted(modules))!r}\nVERSION_KEYS={VERSION_KEYS!r}\n" + GUEST_PROBE
        # Keep the serial input comfortably below the TTY's single-line limit.
        encoded = base64.b64encode(zlib.compress(probe.encode())).decode()
        child.sendline(f"""python3 -c 'import base64,zlib; exec(zlib.decompress(base64.b64decode("{encoded}")))'""")
        child.expect(r"(?m)^HOST_BASE_GUEST (\{[^\r\n]*\})\r*$", timeout=120)
        return json.loads(child.match[1])


def boot(args, work, result):
    installer = args.installer.resolve(strict=True)
    result["installer_sha256"] = file_digest(installer)
    result["kernel_deb_sha256"] = file_digest(args.kernel_deb)
    initramfs = configured_modules()
    result["initramfs_configured_modules"] = initramfs
    result["required_active_modules"] = list(ACTIVE_MODULES)
    release, modules = package_modules(args.kernel_deb, set(ACTIVE_MODULES).union(initramfs))
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
            rejected = child.expect_exact([GRUB, "Failure: Unable to install image", "Kernel panic"],
                                          timeout=args.timeout)
            if rejected:
                raise RuntimeError("ONIE failed to install sonic-vs.bin; see serial log")
        result["install_seconds"] = round(time.monotonic() - start, 3)
        print("Booting the disk with both recovery media removed", flush=True)
        start = time.monotonic()
        observed = probe_guest(common, log, args.username, args.timeout, modules)
        result["boot_probe_seconds"] = round(time.monotonic() - start, 3)
        result["guest"] = observed
        check_guest(observed, release, modules, version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-revision", help="full git SHA used to build the installer")
    parser.add_argument("--verifier-revision", help="full git SHA of the boot verifier")
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--kernel-deb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--onie-iso", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per install/boot phase")
    parser.add_argument("--username", default=os.environ.get("SONIC_USERNAME", "admin"))
    args = parser.parse_args()
    for revision in (args.build_revision, args.verifier_revision):
        if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision):
            parser.error("expected a 40-character git SHA")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {"verified": False, "worker": os.environ.get("RUNNER_NAME") or socket.gethostname(),
              "installer": args.installer.name, "kernel_deb": args.kernel_deb.name,
              "build_revision": args.build_revision, "verifier_revision": args.verifier_revision}
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
