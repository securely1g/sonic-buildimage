#!/usr/bin/env python3
"""Opt-in immutable prepared Docker-store cache for native VS ONIE builds."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


VERSION = 1
CHUNK = 1024 * 1024
MAX_JSON = 256 * 1024
CACHE_BUDGET = 16 * 1024**3
ORDERS = ("fast-reboot_order", "warm-reboot_order")
HERE = Path(__file__).resolve().parents[1]
# Only the reviewed, shipped no-op extension is safe without modeling custom hooks.
STOCK_ORGANIZATION_EXTENSIONS_SHA256 = "3df60a480aefbb2bea60326c787b2b1fe43dee1a6ed0c0d3ec608f25c13f3e25"


class UnsupportedProfile(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError("oversized cache metadata")
    return json.loads(data)


def write_json(path, value):
    data = encoded(value)
    if len(data) > MAX_JSON:
        raise ValueError("oversized cache metadata")
    fd, temporary = tempfile.mkstemp(prefix=".docker-store-json-", dir=Path(path).parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def docker_command(root, host, *args):
    return ["sudo", "LANG=C", "DOCKER_HOST=" + host, "chroot", str(root), "docker", *args]


def report(**updates):
    path = os.environ.get("SONIC_INSTALLER_DOCKER_STORE_RECEIPT")
    if path:
        try:
            current = read_json(path) if Path(path).exists() else {"version": VERSION}
            current.update(updates)
            write_json(path, current)
        except (OSError, ValueError):
            pass


def inputs(root, host, template, env):
    if (env.get("TARGET_MACHINE") != "vs" or env.get("IMAGE_TYPE") != "onie"
            or env.get("DOCKER_LOAD_ON_BOOT") == "y"
            or env.get("CROSS_BUILD_ENVIRON") == "y" or env.get("MULTIARCH_QEMU_ENVIRON") == "y"
            or (HERE / ".arch").read_text().strip() != "amd64"
            or env.get("sonic_packages", "").strip()
            or any(env.get(name) == "y" for name in
                   ("include_kubernetes", "include_kubernetes_master", "BUILD_REDUCE_IMAGE_SIZE"))
            or env.get("TAR_OPTIONS")):
        raise UnsupportedProfile("unsupported-profile")
    build_hook = HERE / "files/build_templates/build_debian.organization.sh"
    if build_hook.exists() or build_hook.is_symlink():
        raise UnsupportedProfile("organization-build-hook")
    extension = HERE / "files/build_templates/organization_extensions.sh"
    if extension.is_symlink():
        raise UnsupportedProfile("organization-extension")
    extension_digest = digest(extension) if extension.exists() else None
    if extension_digest not in (None, STOCK_ORGANIZATION_EXTENSIONS_SHA256):
        raise UnsupportedProfile("organization-extension")
    info = json.loads(subprocess.check_output(
        docker_command(root, host, "info", "--format", "{{json .}}"), text=True))
    if info["Images"] != 0 or info["Containers"] != 0:
        raise ValueError("installer Docker daemon is not empty")
    archives = []
    for item in env["installer_images"].split():
        _, _, machine, archive = item.split("|")
        if machine and machine != env["TARGET_MACHINE"]:
            continue
        path = Path(archive.split(":")[0])
        archives.append({"name": path.name, "sha256": digest(path)})
    if not archives or len({entry["name"] for entry in archives}) != len(archives):
        raise ValueError("missing or duplicate installer archives")
    local_archives = []
    for item in env.get("sonic_local_packages", "").split():
        name, archive, owner, enabled = item.split("|")
        local_archives.append({
            "name": name, "sha256": digest(archive), "owner": owner, "enabled": enabled,
        })
    wheel = Path(env["sonic_utilities_py3_wheel_path"])
    files = {
        "rendered-template": digest(template), "utilities-wheel": digest(wheel),
        "shutdown-generator": digest(root / "usr/local/bin/generate_shutdown_order.py"),
        "files/build_templates/organization_extensions.sh": extension_digest,
    }
    for name in ("build_debian.sh", "scripts/installer_docker_store_cache.py",
                 "files/docker/docker", "files/build_templates/packages.json.j2",
                 "files/build_templates/default_manifest", "onie-image.conf"):
        files[name] = digest(HERE / name)
    for name in ("usr/bin/docker", "usr/bin/dockerd", "usr/sbin/dockerd",
                 "usr/bin/containerd", "usr/sbin/containerd", "usr/bin/runc", "usr/sbin/runc",
                 "usr/bin/containerd-shim-runc-v2", "usr/bin/docker-init", "usr/bin/docker-proxy",
                 "usr/libexec/docker/docker-init", "usr/libexec/docker/docker-proxy",
                 "etc/default/docker", "etc/docker/daemon.json", "etc/containerd/config.toml"):
        path = root / name
        files["rootfs/" + name] = digest(path) if path.is_file() else None
    for name in ("tar", "pigz"):
        files["host/" + name] = digest(shutil.which(name))
    packages = subprocess.check_output(
        ["sudo", "chroot", str(root), "dpkg-query", "-W", "-f=${binary:Package}=${Version}\\n"])
    files["installed-package-versions"] = hashlib.sha256(packages).hexdigest()
    mask = os.umask(0)
    os.umask(mask)
    return {
        "version": VERSION, "archives": archives, "local_archives": local_archives, "files": files,
        "installer_images": env["installer_images"],
        "sonic_local_packages": env.get("sonic_local_packages", ""),
        "image_version": env["SONIC_IMAGE_VERSION"],
        "docker": {name: info[name] for name in
                   ("ServerVersion", "Driver", "DriverStatus", "Architecture", "OSType", "KernelVersion")},
        "compression": "tar-pigz-default",
        "umask": mask, "compressor_environment": {name: env.get(name, "") for name in ("PIGZ", "GZIP")},
    }


def copy_checked(source, destination, expected=None):
    hasher, size = hashlib.sha256(), 0
    with Path(source).open("rb") as incoming, Path(destination).open("wb") as outgoing:
        for block in iter(lambda: incoming.read(CHUNK), b""):
            size += len(block)
            if size > CACHE_BUDGET:
                raise ValueError("Docker store exceeds trial cache budget")
            hasher.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    value = {"sha256": hasher.hexdigest(), "size": size}
    if expected is not None and value != expected:
        raise ValueError("cached Docker-store checksum or size differs")
    return value


def install_orders(root, orders):
    if set(orders) != set(ORDERS) or any(
            not isinstance(text, str) or len(text.encode()) > 65536 for text in orders.values()):
        raise ValueError("invalid shutdown metadata")
    for name in ORDERS:
        with tempfile.NamedTemporaryFile() as temporary:
            temporary.write(orders[name].encode())
            temporary.flush()
            subprocess.run(["sudo", "install", "-o", "root", "-g", "root", "-m", "0644",
                            temporary.name, str(root / "etc/sonic" / name)], check=True)


def prepare(root, host, template, state, output, cache, mode):
    started = time.monotonic()
    value = {"version": VERSION, "status": "disabled", "reason": "not-enabled"}
    temporary = None
    try:
        if mode not in ("read", "readwrite") or not cache:
            return False
        expected = inputs(root, host, template, os.environ)
        key = hashlib.sha256(encoded(expected)).hexdigest()
        value.update(status="miss", reason=None, key=key, inputs=expected, output=str(output.resolve()))
        # A miss state is needed later for trusted publication.
        write_json(state, value)
        entry = Path(cache) / key
        if any(path.is_symlink() for path in (entry, entry / "manifest.json", entry / "dockerfs.tar.gz")):
            raise ValueError("cache entry contains symlinks")
        manifest = read_json(entry / "manifest.json")
        if (manifest["version"] != VERSION or manifest["inputs"] != expected
                or set(manifest["orders"]) != set(ORDERS)):
            raise ValueError("cache entry profile differs")
        # Validate all metadata before touching rootfs files.
        if any(not isinstance(text, str) or len(text.encode()) > 65536
               for text in manifest["orders"].values()):
            raise ValueError("invalid shutdown metadata")
        fd, temporary = tempfile.mkstemp(prefix=".docker-store-restore-", dir=output.parent)
        os.close(fd)
        copy_checked(entry / "dockerfs.tar.gz", temporary, manifest["archive"])
        install_orders(root, manifest["orders"])
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
        temporary = None
        info = output.stat()
        restored = dict(value, status="hit",
                        restored_stat=[info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
        write_json(state, restored)
        value = restored
        return True
    except UnsupportedProfile as error:
        value["reason"] = str(error)
        return False
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        value["reason"] = "cache-entry-unavailable" if value["status"] == "miss" else "inputs-unavailable"
        return False
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        value["prepare_seconds"] = round(time.monotonic() - started, 6)
        try:
            write_json(state, value)
        except (OSError, ValueError):
            pass
        print("[ installer Docker-store cache ] " + json.dumps(
            {name: value[name] for name in ("status", "reason", "key", "prepare_seconds") if name in value}),
              flush=True)
        report(status=value["status"], reason=value.get("reason"), key=value.get("key"), mode=mode,
               prepare_seconds=value["prepare_seconds"],
               loading_skipped=(value["status"] == "hit"
                                and not value["inputs"]["sonic_local_packages"].strip()),
               packing_skipped=False)


def hit(state):
    value = read_json(state)
    if value["version"] != VERSION or value["status"] not in ("disabled", "miss", "hit"):
        raise ValueError("unrecognized Docker-store cache state")
    if value["status"] != "hit":
        return False
    info = Path(value["output"]).stat()
    if value["restored_stat"] != [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]:
        raise ValueError("restored Docker store changed after loading was skipped")
    report(packing_skipped=True)
    return True


def publish(root, state, output, cache, mode):
    if mode != "readwrite" or not cache:
        return
    value = read_json(state)
    if value["version"] != VERSION or value["status"] != "miss":
        return
    if value["output"] != str(output.resolve()):
        raise ValueError("publication output differs from prepared state")
    key = hashlib.sha256(encoded(value["inputs"])).hexdigest()
    if value["key"] != key or not re.fullmatch("[0-9a-f]{64}", key):
        raise ValueError("invalid cache publication state")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / ".publish.lock").open("a") as lock:
        # Do not delay the installer behind another producer's multi-GB copy.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (cache / key).exists():
            return
        used = sum(path.stat().st_size for path in cache.glob("*/dockerfs.tar.gz"))
        if used + output.stat().st_size > CACHE_BUDGET:
            print("[ installer Docker-store cache ] publication skipped: 16 GiB budget", flush=True)
            report(publication="budget-exhausted")
            return
        orders = {}
        for name in ORDERS:
            path = root / "etc/sonic" / name
            if path.stat().st_size > 65536:
                raise ValueError("oversized shutdown metadata")
            if path.stat().st_mode & 0o777 != 0o644:
                raise ValueError("unsupported shutdown metadata permissions")
            orders[name] = path.read_text()
        staging = Path(tempfile.mkdtemp(prefix=".publish-", dir=cache))
        started = time.monotonic()
        try:
            archive = copy_checked(output, staging / "dockerfs.tar.gz")
            write_json(staging / "manifest.json", {
                "version": VERSION, "inputs": value["inputs"], "archive": archive, "orders": orders,
            })
            for name in ("dockerfs.tar.gz", "manifest.json"):
                (staging / name).chmod(0o644)
            staging.chmod(0o755)
            # Readers only see complete entries; never overwrite an existing one.
            os.rename(staging, cache / key)
            print("[ installer Docker-store cache ] " + json.dumps({
                "status": "published", "key": key, "bytes": archive["size"],
                "publish_seconds": round(time.monotonic() - started, 6)}), flush=True)
            report(publication="published", published_bytes=archive["size"],
                   publish_seconds=round(time.monotonic() - started, 6))
        finally:
            if staging.exists():
                for name in ("dockerfs.tar.gz", "manifest.json"):
                    (staging / name).unlink(missing_ok=True)
                staging.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "hit", "publish"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--host", default="")
    parser.add_argument("--template")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cache = os.environ.get("SONIC_INSTALLER_DOCKER_STORE_CACHE", "")
    mode = os.environ.get("SONIC_INSTALLER_DOCKER_STORE_CACHE_MODE", "off")
    try:
        if args.action == "prepare":
            return 0 if prepare(args.root, args.host, args.template, args.state, args.output, cache, mode) else 1
        if args.action == "hit":
            return 0 if hit(args.state) else 1
        publish(args.root, args.state, args.output, cache, mode)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        if args.action == "publish":
            print("[ installer Docker-store cache ] publication unavailable; installer retained", flush=True)
            report(publication="unavailable")
            return 0
        return 2 if args.action == "hit" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
