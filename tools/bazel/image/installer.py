#!/usr/bin/env python3
"""Assemble the VS payload and ONIE shell archive from declared build inputs.

These actions preserve the installer/install.sh and installer/sharch_body.sh
contracts without invoking Make, a Docker daemon, mounts, or a mutable rootfs.
The host filesystem and service store are independently cacheable inputs.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import tarfile
import time
import zipfile


COPY_SIZE = 1024 * 1024
ZIP_LIMIT = (1 << 32) - 1


def safe_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("invalid archive path: {!r}".format(value))
    return value


def check_file(path, magic=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("missing or empty input: {}".format(path))
    if magic is not None:
        with path.open("rb") as stream:
            if stream.read(len(magic)) != magic:
                raise ValueError("unexpected input format: {}".format(path))
    return path


def zip_info(name, size, mode, epoch):
    # ZIP timestamps cannot represent Unix epoch zero. Keep them independent of
    # the local timezone and source mtimes, as with normalized tar metadata.
    info = zipfile.ZipInfo(name, time.gmtime(max(epoch, 315532800))[:6])
    info.create_system = 3
    info.external_attr = mode << 16
    info.file_size = size
    info.compress_type = (
        zipfile.ZIP_STORED
        if name.endswith(("/", ".gz", ".squashfs"))
        else zipfile.ZIP_DEFLATED
    )
    return info


def create_payload(squashfs, dockerfs, boot_tar, platform_tar, output, epoch=0):
    squashfs = check_file(squashfs, b"hsqs")
    dockerfs = check_file(dockerfs, b"\x1f\x8b")
    platform_tar = check_file(platform_tar, b"\x1f\x8b")
    boot_tar = check_file(boot_tar)
    if epoch < 0:
        raise ValueError("epoch must be nonnegative")
    output = Path(output)
    # Python conservatively requests ZIP64 above 2 GiB. ONIE requires ordinary
    # ZIP, whose actual limit is 4 GiB. Set the actual limit and still reject
    # oversized members, total offsets, and entry counts through allowZip64.
    old_limit = zipfile.ZIP64_LIMIT
    zipfile.ZIP64_LIMIT = ZIP_LIMIT
    try:
        with tarfile.open(boot_tar, "r:*") as boot:
            members = sorted(boot.getmembers(), key=lambda item: item.name)
            seen = set()
            for member in members:
                member.name = member.name.removeprefix("./").rstrip("/")
                safe_path(member.name)
                if not (member.name == "boot" or member.name.startswith("boot/")):
                    raise ValueError("boot archive contains a non-boot path")
                if not (member.isfile() or member.isdir()):
                    raise ValueError("boot archive must contain regular files and directories")
                if member.name in seen:
                    raise ValueError("duplicate boot archive path: " + member.name)
                seen.add(member.name)
            if not any(name.startswith("boot/vmlinuz-") for name in seen):
                raise ValueError("boot archive has no kernel")
            if not any(name.startswith("boot/initrd.img-") for name in seen):
                raise ValueError("boot archive has no initramfs")
            with zipfile.ZipFile(output, "w", allowZip64=False) as payload:
                for member in members:
                    name = member.name + ("/" if member.isdir() else "")
                    kind = stat.S_IFDIR if member.isdir() else stat.S_IFREG
                    info = zip_info(name, member.size, kind | member.mode, epoch)
                    if member.isdir():
                        payload.writestr(info, b"")
                    else:
                        with boot.extractfile(member) as source, payload.open(info, "w") as dest:
                            shutil.copyfileobj(source, dest, COPY_SIZE)
                files = [("platform.tar.gz", platform_tar), ("fs.squashfs", squashfs)]
                # Match the native installer: large stores travel alongside the
                # payload inside the shell archive, avoiding unsupported ZIP64.
                if dockerfs.stat().st_size < ZIP_LIMIT:
                    files.append(("dockerfs.tar.gz", dockerfs))
                for name, source_path in files:
                    size = source_path.stat().st_size
                    info = zip_info(name, size, stat.S_IFREG | 0o644, epoch)
                    with source_path.open("rb") as source, payload.open(info, "w") as dest:
                        shutil.copyfileobj(source, dest, COPY_SIZE)
        os.chmod(output, 0o644)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        zipfile.ZIP64_LIMIT = old_limit


def load_config(path):
    config = {
        "arch": "amd64", "machine": "vs", "platform": "x86_64-vs-r0",
        "partition_size": 32768, "raw_image": "target/sonic-vs.raw",
        "extra_cmdline": "", "demo_type": "OS", "secure_upgrade_mode": "no_sign",
        "epoch": 0,
    }
    supplied = json.loads(Path(path).read_text())
    if not isinstance(supplied, dict) or set(supplied) - (set(config) | {"image_version"}):
        raise ValueError("unknown installer configuration fields")
    config.update(supplied)
    if config["arch"] != "amd64" or config["machine"] != "vs":
        raise ValueError("this installer action supports VS on amd64")
    if config["demo_type"] != "OS" or config["secure_upgrade_mode"] != "no_sign":
        raise ValueError("this installer action supports unsigned OS images only")
    for field in ("image_version", "platform"):
        if not isinstance(config.get(field), str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", config[field]):
            raise ValueError("invalid installer " + field)
    if type(config["partition_size"]) is not int or config["partition_size"] <= 0:
        raise ValueError("partition_size must be a positive integer")
    if type(config["epoch"]) is not int or config["epoch"] < 0:
        raise ValueError("epoch must be a nonnegative integer")
    safe_path(config["raw_image"])
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", config["raw_image"]):
        raise ValueError("invalid raw_image")
    if not isinstance(config["extra_cmdline"], str) or any(c in config["extra_cmdline"] for c in "\r\n\x00"):
        raise ValueError("invalid extra_cmdline")
    return config


def read_files(path):
    manifest = json.loads(Path(path).read_text())
    if not isinstance(manifest, list):
        raise ValueError("installer files manifest must be a list")
    result = {}
    for entry in manifest:
        if not isinstance(entry, dict) or set(entry) - {"path", "source", "mode"}:
            raise ValueError("invalid installer file entry")
        name = safe_path(entry["path"])
        if name in result or name in {"machine.conf", "fs.zip", "dockerfs.tar.gz"}:
            raise ValueError("duplicate or reserved installer path: " + name)
        source = Path(entry["source"])
        if not source.is_file():
            raise ValueError("missing installer file: " + str(source))
        mode = entry.get("mode", 0o755 if name.endswith((".sh", ".py")) else 0o644)
        if type(mode) is not int or mode not in (0o644, 0o755):
            raise ValueError("installer mode must be 0644 or 0755")
        result[name] = (source, mode)
    required = {"install.sh", "sharch_body.sh", "default_platform.conf", "onie-image.conf", "platforms_asic"}
    if not required.issubset(result):
        raise ValueError("installer files are missing: " + ", ".join(sorted(required - set(result))))
    return result


class HashWriter:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha1()
        self.size = 0

    def write(self, data):
        self.digest.update(data)
        self.size += len(data)
        return self.stream.write(data)

    def flush(self):
        self.stream.flush()


def render(template, replacements):
    for key, value in replacements.items():
        token = "%%" + key + "%%"
        if token not in template:
            raise ValueError("installer template is missing " + token)
        template = template.replace(token, str(value))
    return template


def create_onie(payload, files, config_file, output, dockerfs=None):
    payload = check_file(payload, b"PK\x03\x04")
    config = load_config(config_file)
    sources = read_files(files)
    with zipfile.ZipFile(payload) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not {"fs.squashfs", "platform.tar.gz"}.issubset(names):
            raise ValueError("invalid SONiC payload members")
        for name in names:
            safe_path(name.rstrip("/"))
        if "dockerfs.tar.gz" not in names:
            if dockerfs is None:
                raise ValueError("payload requires a separate dockerfs input")
            sources["dockerfs.tar.gz"] = (check_file(dockerfs, b"\x1f\x8b"), 0o644)
    script = sources["install.sh"][0].read_text()
    script = render(script, {
        "DEMO_TYPE": config["demo_type"], "ARCH": config["arch"],
        "IMAGE_VERSION": config["image_version"],
        "ONIE_IMAGE_PART_SIZE": config["partition_size"],
        "EXTRA_CMDLINE_LINUX": shlex.quote(config["extra_cmdline"]),
        "OUTPUT_RAW_IMAGE": config["raw_image"],
    })
    wrapper = sources["sharch_body.sh"][0].read_text()
    if not wrapper.endswith("\nexit_marker\n") or wrapper.splitlines().count("exit_marker") != 1:
        raise ValueError("shell archive template requires one final exit_marker line")
    inline = {
        "install.sh": script.encode(),
        "machine.conf": ("machine={machine}\nplatform={platform}\n".format(**config)).encode(),
    }
    sources["fs.zip"] = (payload, 0o644)
    all_names = set(sources) | set(inline)
    directories = {"installer"}
    for name in all_names:
        directories.update(str(parent) for parent in PurePosixPath("installer/" + name).parents if str(parent) != ".")
    records = []
    for directory in sorted(directories):
        info = tarfile.TarInfo(directory)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.mtime = config["epoch"]
        records.append((info, None))
    for name in sorted(all_names):
        info = tarfile.TarInfo("installer/" + name)
        info.mode = sources.get(name, (None, 0o644))[1]
        info.mtime = config["epoch"]
        content = inline[name] if name in inline else sources[name][0]
        info.size = len(content) if isinstance(content, bytes) else content.stat().st_size
        records.append((info, content))
    # Compute the exact GNU tar length without reading file contents. This keeps
    # the native digits-only size assignment while streaming/hashing each large
    # payload once; only the fixed-width SHA1 field is rewritten afterward.
    tar_size = 2 * tarfile.BLOCKSIZE
    for info, _content in records:
        tar_size += len(info.tobuf(tarfile.GNU_FORMAT))
        tar_size += ((info.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    tar_size += (-tar_size) % tarfile.RECORDSIZE
    header = render(wrapper, {"IMAGE_SHA1": "0" * 40, "PAYLOAD_IMAGE_SIZE": tar_size}).encode()
    output = Path(output)
    try:
        with output.open("w+b") as dest:
            dest.write(header)
            writer = HashWriter(dest)
            with tarfile.open(fileobj=writer, mode="w|", format=tarfile.GNU_FORMAT) as archive:
                for info, content in records:
                    if content is None:
                        archive.addfile(info)
                    elif isinstance(content, bytes):
                        archive.addfile(info, io.BytesIO(content))
                    else:
                        with content.open("rb") as stream:
                            archive.addfile(info, stream)
            if writer.size != tar_size:
                raise ValueError("shell archive length differs from declared input sizes")
            finished = render(wrapper, {
                "IMAGE_SHA1": writer.digest.hexdigest(),
                "PAYLOAD_IMAGE_SIZE": writer.size,
            }).encode()
            if len(finished) != len(header):
                raise ValueError("shell archive header size changed")
            dest.seek(0)
            dest.write(finished)
        os.chmod(output, 0o755)
    except Exception:
        output.unlink(missing_ok=True)
        raise


def prepare_inputs(source, config_file, output):
    """Freeze the native installer source contract as explicit Bazel inputs.

    This preparation is deliberately separate from the timed build. Its result
    is a source/configuration bundle, never an old assembled image. Generated
    native installer/platforms and platforms_asic files are not reused.
    """
    source = Path(source).resolve()
    output = Path(output)
    config = load_config(config_file)
    selected = {}
    for path in sorted((source / "installer").rglob("*")):
        relative = path.relative_to(source / "installer")
        if (not path.is_file() or "__pycache__" in relative.parts
                or relative.parts[0] == "platforms"
                or str(relative) in {"platforms_asic", "machine.conf", "fs.zip", "dockerfs.tar.gz"}
                or path.suffix == ".pyc"):
            continue
        selected[str(relative)] = path
    selected["onie-image.conf"] = source / "onie-image.conf"
    arch_config = source / "onie-image-amd64.conf"
    if arch_config.is_file():
        selected["onie-image-amd64.conf"] = arch_config
    platform_config = source / "platform/vs/platform_amd64.conf"
    if not platform_config.is_file():
        platform_config = source / "platform/vs/platform.conf"
    selected["platform.conf"] = platform_config
    asic_platforms = set()
    provenance_inputs = {}
    for vendor in sorted((source / "device").iterdir()):
        if not vendor.is_dir():
            continue
        for platform in sorted(vendor.iterdir()):
            if not platform.is_dir():
                continue
            asic_file = platform / "platform_asic"
            if asic_file.is_file():
                provenance_inputs[str(asic_file.relative_to(source))] = hashlib.sha256(asic_file.read_bytes()).hexdigest()
                if "vs" in asic_file.read_text().splitlines():
                    asic_platforms.add(platform.name)
            if platform.name.startswith("x86_64"):
                for suffix in ("", ".override"):
                    path = platform / ("installer.conf" + suffix)
                    if path.is_file():
                        name = "platforms/" + platform.name + suffix
                        if name in selected:
                            raise ValueError("ambiguous native platform installer configuration: " + name)
                        selected[name] = path
    # Native Make generates this device's platform_asic from the platform module
    # declaration. It is absent from a clean source tree, unlike 4/6-ASIC files.
    platform_makefile = source / "platform/vs/platform-modules-vs.mk"
    platform_text = platform_makefile.read_text()
    matches = re.findall(r"^\$\(VS_PLATFORM_MODULE\)_PLATFORM\s*[:?+]?=\s*(\S+)\s*$", platform_text, re.M)
    if len(matches) != 1 or matches[0] != "x86_64-kvm_x86_64-r0":
        raise ValueError("unsupported VS platform module declaration")
    asic_platforms.add(matches[0])
    provenance_inputs[str(platform_makefile.relative_to(source))] = hashlib.sha256(platform_text.encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    try:
        entries = []
        for name, path in sorted(selected.items()):
            safe_path(name)
            if not path.is_file():
                raise ValueError("missing installer input: " + str(path))
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            dest = output / "files" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            dest.chmod(mode)
            entries.append({"path": name, "source": "files/" + name, "mode": mode})
            provenance_inputs[str(path.relative_to(source))] = hashlib.sha256(dest.read_bytes()).hexdigest()
        platforms = output / "files/platforms_asic"
        platforms.write_text("".join(name + "\n" for name in sorted(asic_platforms)))
        platforms.chmod(0o644)
        entries.append({"path": "platforms_asic", "source": "files/platforms_asic", "mode": 0o644})
        (output / "files-manifest.json").write_text(json.dumps(entries, indent=2) + "\n")
        (output / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
        provenance = {
            "source_root": str(source),
            "source_sha256": dict(sorted(provenance_inputs.items())),
            "config_sha256": hashlib.sha256((output / "config.json").read_bytes()).hexdigest(),
            "platforms_asic": sorted(asic_platforms),
            "platform_override_files": sum(name.startswith("platforms/") for name in selected),
        }
        (output / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    except Exception:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    payload = stages.add_parser("payload")
    for name in ("squashfs", "dockerfs", "boot-tar", "platform-tar", "output"):
        payload.add_argument("--" + name, required=True)
    payload.add_argument("--epoch", type=int, default=0)
    onie = stages.add_parser("onie")
    for name in ("payload", "files", "config", "output"):
        onie.add_argument("--" + name, required=True)
    onie.add_argument("--dockerfs")
    prepare = stages.add_parser("prepare-inputs")
    for name in ("source", "config", "output"):
        prepare.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.stage == "payload":
        create_payload(args.squashfs, args.dockerfs, args.boot_tar, args.platform_tar, args.output, args.epoch)
    elif args.stage == "onie":
        create_onie(args.payload, args.files, args.config, args.output, args.dockerfs)
    else:
        prepare_inputs(args.source, args.config, args.output)


if __name__ == "__main__":
    main()
