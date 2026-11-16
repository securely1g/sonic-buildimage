"""Opt-in archive binding for the 20 runtime families and three shared parents.

This checks Make's actual selection and retained archives. Owner payload, ELF,
service, loaded-image and incremental tests remain separate prerequisites.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import vs_archive_reuse as reuse
from tools.bazel.ci.artifact_validation import require, sha

BASE = "docker-base-trixie"
CONFIG = "docker-config-engine-trixie"
SWSS = "docker-swss-layer-trixie"
PARENTS = (BASE, CONFIG, SWSS)
SUPERVISOR = "/usr/local/bin/supervisord"
# family: (parent, entrypoint, additional Make-owned environment keys)
RUNTIMES = {
    "docker-dash-ha": (SWSS, SUPERVISOR, ()),
    "docker-database": (CONFIG, "/usr/local/bin/docker-database-init.sh", ("INCLUDE_SYSTEM_EVENTD",)),
    "docker-eventd": (CONFIG, SUPERVISOR, ("IMAGE_VERSION",)),
    "docker-fpm-frr": (SWSS, "/usr/bin/docker_init.sh", ()),
    "docker-lldp": (CONFIG, "/usr/bin/docker-lldp-init.sh", ("IMAGE_VERSION",)),
    "docker-macsec": (SWSS, SUPERVISOR, ()),
    "docker-mux": (CONFIG, "/usr/bin/docker-init.sh", ()),
    "docker-orchagent": (CONFIG, "/usr/bin/docker-init.sh", ()),
    "docker-router-advertiser": (CONFIG, "/usr/bin/docker-init.sh", ("IMAGE_VERSION",)),
    "docker-sflow": (SWSS, SUPERVISOR, ()),
    "docker-snmp": (CONFIG, "/usr/bin/docker-snmp-init.sh", ("IMAGE_VERSION", "PYTHONOPTIMIZE")),
    "docker-sonic-gnmi": (CONFIG, SUPERVISOR, ("IMAGE_VERSION",)),
    "docker-sonic-mgmt-framework": (CONFIG, SUPERVISOR, ()),
    "docker-sonic-otel": (CONFIG, SUPERVISOR, ("IMAGE_VERSION",)),
    "docker-sonic-p4rt": (CONFIG, SUPERVISOR, ()),
    "docker-sysmgr": (CONFIG, SUPERVISOR, ()),
    "docker-teamd": (SWSS, "/usr/bin/docker-teamd-init.sh", ()),
    "docker-dash-engine": (BASE, "/usr/bin/start.sh", ()),
    "docker-gbsyncd-vs": (CONFIG, SUPERVISOR, ("IMAGE_VERSION",)),
    "docker-syncd-vs": (CONFIG, SUPERVISOR, ()),
}
LEGACY = ("docker-nat", "docker-gnmi-sidecar", "docker-restapi-sidecar", "docker-gnmi-watchdog",
          "docker-bmp-watchdog", "docker-dhcp-relay", "docker-platform-monitor", "docker-sonic-bmp")
ENGINE = "docker-dash-engine"
LOCAL = "docker-macsec"
ARCHIVE_PATH = re.compile(r"target/(docker-[a-z0-9-]+)\.gz")
PROFILE = {"architecture": "amd64", "platform": "vs", "distribution": "trixie",
           "builder": "y", "fips": "y", "dash": "y", "asan": "n", "syncd_rpc": "n",
           "cross_build": False, "multiarch_qemu": False}
CONTRACT = {"parents": PARENTS, "runtimes": RUNTIMES, "legacy": LEGACY,
            "runtime_only_installer": ENGINE, "local_package": LOCAL, "profile": PROFILE}


def stable_bytes(path):
    before = reuse.file_identity(path.stat())
    require(path.stat().st_size <= reuse.MAX_JSON, "selection or manifest is too large")
    data = path.read_bytes()
    require(before == reuse.file_identity(path.stat()), "input changed during validation")
    return data


def identity(path):
    before = reuse.file_identity(path.stat())
    value = {"sha256": sha(path), "bytes": path.stat().st_size}
    require(before == reuse.file_identity(path.stat()), "archive changed during validation")
    return value


def environment(values):
    require(isinstance(values, list), "environment must be a list")
    result = {}
    for value in values:
        require(isinstance(value, str) and "=" in value, "invalid environment value")
        key, item = value.split("=", 1)
        require(key and key not in result, "duplicate environment key")
        result[key] = item
    return result


def record_selection(workspace, installer, machine, environ):
    """Record values already resolved by the successful Make installer recipe."""
    require(installer in reuse.INSTALLERS, "unsupported VS installer")
    names = {"architecture": "VS_ARCHITECTURE", "platform": "VS_PLATFORM", "distribution": "VS_DISTRIBUTION",
             "builder": "VS_BUILDER", "fips": "VS_FIPS", "dash": "VS_DASH", "asan": "VS_ASAN", "syncd_rpc": "VS_SYNCD_RPC"}
    profile = {name: environ.get(key) for name, key in names.items()}
    profile.update(cross_build=environ.get("VS_CROSS_BUILD") == "y",
                   multiarch_qemu=environ.get("VS_MULTIARCH_QEMU") == "y")
    version = environ.get("SONIC_IMAGE_VERSION", "")
    eventd = environ.get("include_system_eventd")
    debug = environ.get("install_debug_image")
    installers = []
    for item in environ.get("installer_images", "").split():
        fields = item.split("|")
        require(len(fields) == 4, "invalid installer image record")
        package, source, selected_machine, reference = fields
        if selected_machine not in ("", machine):
            continue
        path, separator, tag = reference.rpartition(":")
        require(separator, "installer image record is missing its version separator")
        installers.append({"path": path, "package": package, "source": source, "version": tag})
    local = []
    for item in environ.get("sonic_local_packages", "").split():
        fields = item.split("|")
        require(len(fields) == 4, "invalid local package record")
        archive, path, owner, enabled = fields
        local.append({"archive": archive, "path": path, "owner": owner, "enabled": enabled})
    selection = {"schema": 1, "profile": "final23", "installer": installer, "make": profile,
                 "image_version": version, "include_system_eventd": eventd,
                 "install_debug_tools": debug, "debug": debug == "y",
                 "installer_images": installers, "local_packages": local}
    # No profile, inclusion, archive or workspace validation in the image recipe.
    # The separate capture/verify commands validate the recorded build outputs.
    path = workspace / "target" / (installer + ".selection.json")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(selection, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)
    return selection


def validate_selection(workspace, selection):
    require(selection.get("schema") == 1 and selection.get("profile") == "final23" and
            selection.get("make") == PROFILE and type(selection.get("debug")) is bool,
            "unsupported final23 Make selection")
    installer = selection.get("installer")
    require(installer in reuse.INSTALLERS, "unsupported VS installer")
    version = selection.get("image_version")
    require(isinstance(version, str) and version and len(version) < 256 and
            not any(c in version for c in "\r\n\0$"), "invalid image version")
    require(selection.get("include_system_eventd") in ("y", "n"), "invalid eventd setting")
    require(selection.get("install_debug_tools") in ("y", "n") and
            selection["debug"] == (selection["install_debug_tools"] == "y"), "invalid debug setting")
    expected = {}
    for family in RUNTIMES:
        suffix = "-dbg" if selection["debug"] and family != ENGINE else ""
        expected[family] = "target/" + family + suffix + ".gz"
    seen = {}
    for channel in ("installer_images", "local_packages"):
        for item in selection[channel]:
            path = item["path"]
            if channel == "local_packages":
                require(item.get("archive") == Path(path).name, "local package archive name differs")
            match = ARCHIVE_PATH.fullmatch(path)
            require(match is not None and path not in seen, "invalid or duplicate selected archive")
            require(selection["debug"] or not match[1].endswith("-dbg"), "normal installer selects debug archive")
            family = match[1].removesuffix("-dbg")
            require(family in RUNTIMES or family in LEGACY, "unreviewed selected Docker family: " + family)
            require(family not in seen.values(), "multiple variants selected for Docker family")
            seen[path] = family
            if family in RUNTIMES:
                require(path == expected[family], "wrong runtime/debug variant: " + family)
                require((channel == "local_packages") == (family == LOCAL), "wrong installer channel: " + family)
    require(set(RUNTIMES).issubset(seen.values()), "final23 selection omits an in-scope runtime family")
    selected_paths = [item["path"] for item in selection["installer_images"]]
    data = stable_bytes(workspace / "target" / (installer + ".dockers"))
    require(data.decode().splitlines() == selected_paths, "Make .dockers differs from resolved installer selection")
    for name, value in ((".arch", "amd64"), (".platform", "vs")):
        require(stable_bytes(workspace / name).decode().strip() == value, "workspace profile differs")


def require_prefix(child, parent):
    count = len(parent["layers"])
    require(len(child["layers"]) > count and child["layers"][:count] == parent["layers"] and
            child["rootfs"][:count] == parent["rootfs"], "archive does not extend the exact parent layers")


def capture(workspace, *, installer="sonic-vs.bin", debug=False):
    started = time.monotonic()
    files, archives, settings, identities = {}, {}, {}, {}

    def track(path):
        files[path] = identity(workspace / path)
        identities[path] = reuse.file_identity((workspace / path).stat())

    selection_path = "target/" + installer + ".selection.json"
    selection = json.loads(stable_bytes(workspace / selection_path))
    validate_selection(workspace, selection)
    require(selection["installer"] == installer and selection["debug"] == debug, "installer profile differs")
    for path in (selection_path, "target/" + installer + ".dockers", ".arch", ".platform"):
        track(path)

    def inspect(name, entrypoint, *, empty=False):
        stamp = "target/.container-build-method/" + name + ".gz"
        require(stable_bytes(workspace / stamp).decode().strip() == "bazel", "archive builder differs: " + name)
        manifest, manifest_sha = reuse.read_manifest(workspace, name)
        archive_path = "target/" + name + ".gz"
        value = reuse.archive_snapshot(workspace / archive_path, name + ":latest", manifest,
                    entrypoint=entrypoint, empty_parent=empty, config_check=lambda value: settings.update({name: value}))
        # All settings remain private. Only inherited equality and their digest are published.
        environment(settings[name].get("Env", []))
        archives[name] = value
        files[archive_path] = {key: value[key] for key in ("sha256", "bytes")}
        identities[archive_path] = reuse.file_identity((workspace / archive_path).stat())
        for path in (stamp, "target/bazel-manifests/" + name + "/manifest.json"):
            track(path)
        require(files["target/bazel-manifests/" + name + "/manifest.json"]["sha256"] == manifest_sha,
                "Make manifest changed during archive inspection")
        return manifest

    def inherit(name, parent, entrypoint, overrides):
        require_prefix(archives[name], archives[parent])
        expected = copy.deepcopy(settings[parent])
        expected["Entrypoint"] = entrypoint
        expected.pop("Cmd", None)
        actual = copy.deepcopy(settings[name])
        require(actual.pop("Cmd", None) is None, "child command differs")
        env = environment(expected.pop("Env", []))
        env.update(overrides)
        require(environment(actual.pop("Env", [])) == env, "child environment differs: " + name)
        for value in (actual, expected):
            value.get("Labels", {}).pop(reuse.LABEL, None)
        require(actual == expected, "child changes undeclared image configuration: " + name)

    inspect(BASE, [], empty=True)
    inspect(CONFIG, [], empty=True)
    require_prefix(archives[CONFIG], archives[BASE])
    require(archives[CONFIG]["settings_sha256"] == archives[BASE]["settings_sha256"], "config-engine changes base settings")
    inspect(SWSS, [SUPERVISOR])
    inherit(SWSS, CONFIG, [SUPERVISOR], {"DEBIAN_FRONTEND": "noninteractive"})
    for name, (parent, entrypoint, keys) in RUNTIMES.items():
        manifest = inspect(name, [entrypoint])
        selected = next((item for item in selection["installer_images"]
                         if Path(item["path"]).name.removesuffix(".gz").removesuffix("-dbg") == name), None)
        if selected is not None:
            require(manifest.get("package", {}).get("name") == selected["package"] and
                    manifest["package"].get("version") == selected["version"],
                    "archive package metadata differs from resolved Make selection")
        overrides = {"DEBIAN_FRONTEND": "noninteractive"}
        for key in keys:
            overrides[key] = (selection["image_version"] if key == "IMAGE_VERSION" else
                              selection["include_system_eventd"] if key == "INCLUDE_SYSTEM_EVENTD" else "1")
        inherit(name, parent, [entrypoint], overrides)
        if keys and name != "docker-snmp":
            path = "target/bazel-manifests/" + name + "/image.env"
            lines = stable_bytes(workspace / path).decode().splitlines()
            require(environment(lines) == overrides, "declared environment differs from Make selection")
            track(path)
        # Engine has no Make debug side output; its separate image driver owns debug acceptance.
        if name == ENGINE:
            continue
        symbols = name + "-dbg"
        debug_manifest = inspect(symbols, [entrypoint])
        expected = copy.deepcopy(manifest)
        version = expected["package"]["version"]
        expected["package"]["version"] += ".dbg" if "+" in version else "+dbg"
        require(debug_manifest == expected, "debug Make manifest differs beyond version suffix")
        require_prefix(archives[symbols], archives[name])
        require(archives[symbols]["settings_sha256"] == archives[name]["settings_sha256"], "debug changes runtime settings")
    legacy = {}
    for item in selection["installer_images"] + selection["local_packages"]:
        family = Path(item["path"]).name.removesuffix(".gz").removesuffix("-dbg")
        if family in LEGACY:
            track(item["path"])
            stamp = "target/.container-build-method/" + Path(item["path"]).name
            builder = stable_bytes(workspace / stamp).decode().strip() if (workspace / stamp).exists() else "make"
            require(builder in ("make", "bazel"), "unknown legacy builder")
            if (workspace / stamp).exists():
                track(stamp)
            legacy[item["path"]] = {**files[item["path"]], "builder": builder}
    require(json.loads(stable_bytes(workspace / selection_path)) == selection, "Make selection changed during capture")
    require(all(reuse.file_identity((workspace / path).stat()) == value for path, value in identities.items()),
            "VS archives or inputs changed during capture")
    return {"schema": 2, "profile": "final23", "contract_sha256": reuse.json_digest(CONTRACT),
            "installer": installer, "selection": selection, "archives": archives, "files": files, "legacy": legacy,
            "scope": "Archive and selection binding only; owner and loaded-image checks remain required.",
            "validation_seconds": {"capture": time.monotonic() - started}}


def verify(workspace, receipt):
    started = time.monotonic()
    expected = set(PARENTS) | set(RUNTIMES) | {name + "-dbg" for name in RUNTIMES if name != ENGINE}
    require(receipt.get("schema") == 2 and receipt.get("profile") == "final23" and
            receipt.get("contract_sha256") == reuse.json_digest(CONTRACT) and
            set(receipt.get("archives", {})) == expected, "unsupported final23 receipt")
    validate_selection(workspace, receipt["selection"])
    required = {".arch", ".platform", "target/" + receipt["installer"] + ".selection.json",
                "target/" + receipt["installer"] + ".dockers"}
    for name in expected:
        archive = "target/" + name + ".gz"
        required.update((archive, "target/.container-build-method/" + name + ".gz",
                         "target/bazel-manifests/" + name + "/manifest.json"))
        require(receipt["files"].get(archive) == {key: receipt["archives"][name][key] for key in ("sha256", "bytes")},
                "archive identity is missing from receipt")
    required.update("target/bazel-manifests/" + name + "/image.env" for name, (_, _, keys) in RUNTIMES.items()
                    if keys and name != "docker-snmp")
    required.update(item["path"] for item in receipt["selection"]["installer_images"] + receipt["selection"]["local_packages"])
    require(required.issubset(receipt["files"]), "receipt omits an archive or declared input")
    for path, recorded in receipt["files"].items():
        require(not Path(path).is_absolute() and ".." not in Path(path).parts, "unsafe receipt path")
        require(identity(workspace / path) == recorded, "VS archive or input changed after capture: " + path)
    result = copy.deepcopy(receipt)
    result["validation_seconds"]["verify"] = time.monotonic() - started
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--installer", choices=reuse.INSTALLERS, required=True)
    parser.add_argument("--machine", required=True)
    args = parser.parse_args()
    record_selection(args.workspace, args.installer, args.machine, os.environ)


if __name__ == "__main__":
    main()
