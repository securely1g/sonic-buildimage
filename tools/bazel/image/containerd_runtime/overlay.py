#!/usr/bin/env python3
"""Configure Docker to use the containerd store prepared by the Bazel build."""

import argparse
import json
from pathlib import Path
import shlex


CONTAINERD_CONFIG = '''# Generated only for the Bazel VS image.
version = 2
root = "/var/lib/docker/containerd-store"
state = "/run/containerd"
disabled_plugins = ["io.containerd.grpc.v1.cri"]

[grpc]
  address = "/run/containerd/containerd.sock"
'''

CONTAINERD_DROPIN = '''[Unit]
After=local-fs.target
RequiresMountsFor=/var/lib/docker

[Service]
ExecStart=
ExecStart=/usr/bin/containerd --config /etc/containerd/sonic-bazel.toml
'''

INITRAMFS_EXTRACTOR = '''# Bazel containerd store: preserve overlayfs opaque-directory metadata.
sonic_bazel_extract_containerd()
{
    chroot "${rootmnt}" /usr/bin/tar --extract --numeric-owner --same-permissions \\
        --xattrs --xattrs-include='*' --acls --directory=/var/lib/docker \\
        --use-compress-program=/usr/bin/zstd "$@"
}

'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def docker_command(unit):
    commands = []
    # SONiC's maintained override supplies the complete ExecStart line. Preserve
    # its networking and fd:// socket-activation settings.
    for line in unit.replace("\\\n", " ").splitlines():
        if line.startswith("ExecStart="):
            value = line.partition("=")[2].strip()
            if not value:
                commands.clear()
            else:
                commands.append(shlex.split(value))
    require(len(commands) == 1 and commands[0] and commands[0][0] == "/usr/bin/dockerd",
            "SONiC Docker override must contain one /usr/bin/dockerd ExecStart")
    original = iter(commands[0])
    result = []
    for token in original:
        if token in {"--storage-driver", "--containerd", "--containerd-namespace"}:
            require(next(original, None) is not None, "Docker option is missing a value")
        elif not any(token.startswith(name + "=") for name in
                     ("--storage-driver", "--containerd", "--containerd-namespace")):
            result.append(token)
    # systemd accepts double-quoted words, but does not interpret shell quoting.
    return " ".join(json.dumps(word) if any(c.isspace() or c in '\\"' for c in word) else word
                    for word in result)


def initramfs_script(contents):
    if INITRAMFS_EXTRACTOR in contents:
        require(contents.count("sonic_bazel_extract_containerd ") == 3,
                "unexpected existing containerd initramfs extraction commands")
        return contents
    replacements = {
        "pzstd -d -q ${rootmnt}/host/$image_dir/":
            'sonic_bazel_extract_containerd --file "/host/$image_dir/',
        " -c | tar x --numeric-owner -C ${rootmnt}/var/lib/docker": '"',
        'tar xz --numeric-owner -f "${rootmnt}/host/$image_dir/':
            'sonic_bazel_extract_containerd --file "/host/$image_dir/',
        '" -C "${rootmnt}/var/lib/docker"': '"',
        "tar xz --numeric-owner -C ${rootmnt}/var/lib/docker":
            "sonic_bazel_extract_containerd --file=-",
    }
    for old, new in replacements.items():
        require(contents.count(old) == 1, "unexpected initramfs dockerfs extraction contract: " + old)
        contents = contents.replace(old, new)
    contents = contents.replace("Detected zstd compression, extracting with pzstd...",
                                "Detected zstd compression, extracting with target tar...")
    require(contents.count("extract_dockerfs()\n") == 1, "missing initramfs extraction function")
    return contents.replace("extract_dockerfs()\n", INITRAMFS_EXTRACTOR + "extract_dockerfs()\n")


def install(rootfs, architecture):
    require(architecture in {"amd64", "arm64"}, "unsupported target architecture")
    rootfs = Path(rootfs).resolve(strict=True)
    original = rootfs / "etc/systemd/system/docker.service.d/docker.service.conf"
    command = docker_command(original.read_text())
    config_path = rootfs / "etc/docker/daemon.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    require(isinstance(config, dict), "Docker daemon configuration must be an object")
    features = config.setdefault("features", {})
    require(isinstance(features, dict), "Docker features must be an object")
    require(not config.get("userns-remap"), "containerd image store does not support userns-remap")
    require(not config.get("storage-opts"), "legacy Docker storage options require explicit migration")
    features["containerd-snapshotter"] = True
    config.update({"storage-driver": "overlayfs", "containerd": "/run/containerd/containerd.sock",
                   "containerd-namespace": "moby"})
    docker_dropin = '''[Unit]
Requires=containerd.service
After=containerd.service

[Service]
ExecStart=
ExecStart={command}
'''.format(command=command)
    outputs = {
        "etc/docker/daemon.json": (json.dumps(config, indent=2, sort_keys=True) + "\n", 0o644),
        "etc/containerd/sonic-bazel.toml": (CONTAINERD_CONFIG, 0o644),
        "etc/systemd/system/containerd.service.d/sonic-bazel.conf": (CONTAINERD_DROPIN, 0o644),
        "etc/systemd/system/docker.service.d/zz-sonic-bazel-containerd.conf": (docker_dropin, 0o644),
        "etc/initramfs-tools/scripts/init-bottom/union-mount":
            (initramfs_script((rootfs / "etc/initramfs-tools/scripts/init-bottom/union-mount").read_text()), 0o755),
    }
    # Validate all destination paths before touching the restored host root.
    for relative in outputs:
        destination = rootfs / relative
        require(destination.resolve().is_relative_to(rootfs), "rootfs output escapes through a symlink")
        require(not destination.is_symlink(), "rootfs output must not be a symlink")
    for relative, (contents, mode) in outputs.items():
        destination = rootfs / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents)
        destination.chmod(mode)
    return sorted(outputs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    print(json.dumps({"installed": install(args.rootfs, args.architecture)}, sort_keys=True))


if __name__ == "__main__":
    main()
