#!/usr/bin/env python3
"""Record fresh native VS predecessors at the pre-container boundary.

Make owns package and service-image production. This helper never downloads a
prepared image and never executes a captured environment as shell code.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import host
import installer
import prepare_host_inputs

OUTPUT = Path("target/bazel-native")
IDENTITY_KEYS = {
    "SONIC_BAZEL_SOURCE_COMMIT", "SONIC_BAZEL_SOURCE_BRANCH", "SOURCE_DATE_EPOCH",
    "CONFIGURED_ARCH", "CONFIGURED_PLATFORM", "TARGET_MACHINE", "IMAGE_TYPE", "IMAGE_DISTRO", "SONIC_IMAGE_VERSION",
}
AMBIENT_KEYS = {
    "PWD", "OLDPWD", "HOME", "USER", "LOGNAME", "PATH", "SHELL", "SHLVL", "_",
    "MAKEFLAGS", "MAKELEVEL", "MFLAGS", "DOCKER_HOST", "RUSTUP_HOME",
    "SONIC_BAZEL_HOST_SNAPSHOT", "SONIC_BAZEL_BUILD_STAGE", "SONIC_BAZEL_HOST_FINALIZE",
    "SONIC_BAZEL_IMAGE_STAGE", "SONIC_BAZEL_REQUESTED_STAGE",
    "BASH_ENV", "ENV", "CDPATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "SSH_AUTH_SOCK", "DOCKER_CONFIG",
    "SONIC_BUILD_SLAVE_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "WGETRC",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def file_info(path):
    require(path.is_file() and not path.is_symlink(), "expected regular native input: " + str(path))
    require(path.stat().st_size > 0, "empty native input: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def git(source, *arguments):
    return subprocess.check_output(["git", "-C", str(source), *arguments], text=True).strip()


def source_identity(source):
    commit = git(source, "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "invalid source commit")
    submodules = {}
    for line in git(source, "submodule", "status", "--recursive").splitlines():
        # strip() removes the first line's normal leading space, but not the
        # mismatch, uninitialized or conflict markers, which must fail closed.
        require(line[0] not in "-+U", "submodule does not match its recorded gitlink: " + line)
        fields = line.split()
        require(len(fields) >= 2 and re.fullmatch(r"[0-9a-f]{40}", fields[0]),
                "invalid submodule identity")
        submodules[fields[1]] = fields[0]
    return {"source_commit": commit, "source_branch": git(source, "rev-parse", "--abbrev-ref", "HEAD"),
            "source_submodules": submodules}


def capture_environment(source, environment):
    sources = [source / "build_debian.sh", source / "slave.mk"]
    sources.extend((source / "files/build_templates").rglob("*.j2"))
    names = set(IDENTITY_KEYS)
    for path in sources:
        names.update(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", path.read_text()))
    captured = {}
    for name, value in environment.items():
        if name not in names or name in AMBIENT_KEYS or name.startswith(("BAZEL_", "AWS_")):
            continue
        upper = name.upper()
        if any(word in upper for word in ("PASSWORD", "TOKEN", "SECRET", "PROXY", "SIGNING", "CERT", "CAFILE")):
            if name != "CHANGE_DEFAULT_PASSWORD" or value not in ("y", "n"):
                continue
        require(isinstance(value, str) and "\0" not in value, "non-string template environment: " + name)
        captured[name] = value
    # The finalizer explicitly supports unsigned images; signing material and
    # empty Make defaults must not enter its declared environment.
    captured["SECURE_UPGRADE_MODE"] = "no_sign"
    return captured


def inventory(environment):
    def value(name):
        return environment.get("BAZEL_" + name, "")
    def words(name):
        return value(name).split()
    flags = dict(item.split("=", 1) for item in words("CONFIG_FLAGS"))
    result = {
        "schema": 1, "image": "sonic-vs.bin", "platform": value("PLATFORM"),
        "arch": value("ARCH"), "distro": value("DISTRO"),
        "image_version": value("IMAGE_VERSION"), "build_timestamp": value("BUILD_TIMESTAMP"),
        "build_number": value("BUILD_NUMBER"), "selected_dockers": words("SELECTED_DOCKERS"),
        "installed_dockers": words("INSTALLED_DOCKERS"), "local_packages": words("LOCAL_PACKAGES"),
        "remote_packages": words("REMOTE_PACKAGES"), "rfs_depends": words("RFS_DEPENDS"),
        "image_installs": words("IMAGE_INSTALLS"), "image_files": words("IMAGE_FILES"),
        "bazel_prerequisites": words("SWSS_PREREQUISITES"), "config_flags": flags,
        "source_built_by_bazel": ["docker-orchagent.gz"],
        "native_swss_deb": value("SWSS"),
        "native_swss_scope": "Native service recipes retain their SWSS DEB dependencies; the orchagent archive is built only by Bazel.",
    }
    require((result["platform"], result["arch"], result["distro"]) == ("vs", "amd64", "trixie"),
            "native image preparation requires AMD64 Trixie VS")
    require(not result["remote_packages"], "remote SONiC service packages are unsupported")
    require("docker-orchagent.gz" in result["installed_dockers"], "VS inventory has no orchagent service")
    for key, value in flags.items():
        if key in {"SECURE_UPGRADE_MODE", "BAZEL_MIN_READINESS"}:
            expected = "no_sign" if key == "SECURE_UPGRADE_MODE" else "bazel_disabled"
            require(value.strip('\"\'') == expected, "unsupported native option: " + key)
        else:
            require(value in ("", "n"), "unsupported native option: " + key)
    return result


def begin(source, environment):
    result = inventory(environment)
    host.validate_source_identity(source, {"arch": result["arch"], "platform": result["platform"]})
    host.validate_organization_hooks(source)
    identity = source_identity(source)
    epoch = environment.get("SOURCE_DATE_EPOCH", "")
    require(re.fullmatch(r"[0-9]+", epoch), "SOURCE_DATE_EPOCH is required")
    identity.update(source_date_epoch=epoch, image_version=result["image_version"])
    output = source / OUTPUT
    require(not output.exists() and not output.is_symlink(), "native input directory already exists")
    output.mkdir(parents=True)
    write_json(output / "inventory.json", result)
    write_json(output / "source.json", identity)


def finish(source, environment):
    output = source / OUTPUT
    original = json.loads((output / "source.json").read_text())
    current = source_identity(source)
    require(all(current[key] == original[key] for key in current), "source identity changed during native build")
    captured = capture_environment(source, environment)
    expected = {"SONIC_BAZEL_SOURCE_COMMIT": original["source_commit"],
                "SONIC_BAZEL_SOURCE_BRANCH": original["source_branch"],
                "SOURCE_DATE_EPOCH": original["source_date_epoch"],
                "SONIC_IMAGE_VERSION": original["image_version"]}
    require(all(captured.get(key) == value for key, value in expected.items()),
            "host environment differs from the current source identity")
    with (output / "host-onie.squashfs").open("rb") as stream:
        require(stream.read(4) == b"hsqs", "native predecessor is not a SquashFS snapshot")
    env_path = output / "captured-host-environment.json"
    write_json(env_path, captured)
    env_path.chmod(0o600)
    prepare_host_inputs.prepare(source, env_path, output / "host-onie.squashfs", output)
    inv = json.loads((output / "inventory.json").read_text())
    builtins, local = host.image_names(captured)
    require((builtins, local) == (set(inv["installed_dockers"]), set(inv["local_packages"])),
            "captured host environment differs from the evaluated image inventory")
    images = {name: "target/" + name for name in sorted((builtins | local) - {"docker-orchagent.gz"})}
    write_json(output / "images.json", images)
    configuration = {
        "arch": "amd64", "machine": "vs", "image_version": original["image_version"],
        "epoch": int(original["source_date_epoch"]),
        "partition_size": int(captured["ONIE_IMAGE_PART_SIZE"]),
    }
    write_json(output / "installer-config.json", configuration)
    installer.load_config(output / "installer-config.json")
    inputs = set(images.values()) | set(inv["bazel_prerequisites"])
    inputs.update(path.relative_to(source).as_posix() for path in output.iterdir() if path.is_file())
    files = {}
    for name in sorted(inputs):
        path = Path(name)
        require(not path.is_absolute() and ".." not in path.parts, "invalid native input path: " + name)
        require((source / path).resolve().is_relative_to(source.resolve()), "native input escapes checkout")
        files[name] = file_info(source / path)
    receipt = {"schema": 1, **original, "source_boundary": "before-container-loading",
               "files": files, "scope": inv["native_swss_scope"]}
    write_json(output / "provenance.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("begin", "finish"))
    args = parser.parse_args()
    source = Path.cwd().resolve()
    if args.command == "begin":
        begin(source, os.environ)
    else:
        finish(source, os.environ)


if __name__ == "__main__":
    main()
