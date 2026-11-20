"""Select owner products and installed-probe fixtures from the VS Bazel build."""

from functools import lru_cache
from importlib import import_module
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from tools.bazel.ci import artifact_validation as check

FAMILIES = {
    "docker-dash-engine": "dash_engine", "docker-dash-ha": "dash_ha",
    "docker-database": "database", "docker-eventd": "eventd", "docker-fpm-frr": "frr",
    "docker-gbsyncd-vs": "gbsyncd", "docker-lldp": "lldp", "docker-macsec": "macsec",
    "docker-mux": "mux", "docker-orchagent": "orchagent", "docker-router-advertiser": "radv",
    "docker-sflow": "sflow", "docker-snmp": "snmp", "docker-sonic-gnmi": "gnmi",
    "docker-sonic-mgmt-framework": "mgmt_framework", "docker-sonic-otel": "otel",
    "docker-sonic-p4rt": "p4rt", "docker-sysmgr": "sysmgr", "docker-teamd": "teamd",
}

# Publish the original fixture only where a service uses it. Project large
# source archives to member metadata once on the builder.
FIXTURES = {"test-redis.tar", "test-services.tar", "dbus-mock", "gnmi-installed-probe.py",
            "scapy-2.6.1.dev0-py3-none-any.whl", "eventd-runtime.tar"}
EXTRA_OWNERS = {"source-runtime.tar", "mux-source.tar", "eventd-runtime.tar", "snmp_install.tar",
                "lldpd-owner.tar", "dbsyncd-runtime.tar", "framework-runtime.tar", "gnmi-runtime.tar",
                "sflowtool-runtime.tar", "psample-runtime.tar", "hsflowd-runtime.tar"}


def owner_archive(name):
    return name in EXTRA_OWNERS or name.endswith((".source.tar", "-source.tar"))


def preparation_targets(image, debug_selected):
    from tools.bazel.ci.vs_standalone_probes import extra_standalone_targets

    result = dict(extra_standalone_targets(image, debug_selected))
    if image not in FAMILIES:
        return result
    module = import_module("tools.bazel.ci." + FAMILIES[image] + "_image")
    if image == "docker-gbsyncd-vs":
        result.update({"base.source.tar": "//dockers/docker-base-trixie:source_runtime",
                       **{n: module.TARGETS[n] for n in ("source-contract.json", "source-receipt.json")}})
        if debug_selected:
            result["policy.json"] = module.TARGETS["policy.json"]
    for name, target in module.TARGETS.items():
        if not debug_selected and ("debug" in name or "symbols" in name or name.startswith("openssh")):
            continue
        if image == "docker-dash-engine" and name in ("pi.source.tar", "bmv2.source.tar", "nnpy.source.tar"):
            # These inputs precede the path normalization and dpkg excludes in engine.source.tar.
            continue
        target_name = target.rsplit(":", 1)[-1]
        if debug_selected and target_name in {"all_debug_symbols", "source_debug_payloads", "docker-config-engine-trixie.debug_symbols"}:
            result[name] = target
        if target_name in {"files", "ci_config"} and name.endswith(".tar"):
            result[name] = target
        if not debug_selected and image == "docker-sonic-p4rt" and name.startswith(("sairedis", "saimetadata")):
            continue
        if name in FIXTURES or owner_archive(name) or name.endswith((".status", "packages.json")):
            result[name] = target
    return result


@lru_cache(maxsize=128)
def source_members(archive):
    """Read source ELFs independently of the installed OCI inventory."""
    rows = {}
    with tempfile.TemporaryDirectory(prefix="vs-owner-elf-") as tmp, tarfile.open(archive) as tar:
        binary = Path(tmp) / "elf"
        for member in tar:
            if not member.isfile():
                continue
            name = check.path_name(member.name)
            with tar.extractfile(member) as stream:
                header = stream.read(20)
                if header[:4] != b"\x7fELF" or name.startswith("usr/lib/debug/") or name.endswith((".dwo", ".dwp")):
                    continue
                with binary.open("wb") as output:
                    output.write(header)
                    shutil.copyfileobj(stream, output)
            rows[name] = {**check.metadata(member), "sha256": check.sha(binary),
                          **check.elf_info(binary, dynamic=True)}
    return rows


def prepare(image, version, variant, layout_dir, source_paths, bundle_dir, *, make_manifests=None):
    """Export independent owner expectations; the per-container job compares them to the image."""
    from tools.bazel.ci.vs_standalone_probes import extra_standalone_targets
    native, owned, artifacts = {}, {}, {}
    standalone = extra_standalone_targets(image, variant == "debug")
    if make_manifests is not None:
        # Both DASH engine targets explicitly use the runtime :labels target.
        name = image + ("-dbg" if variant == "debug" and image != "docker-dash-engine" else "")
        for file in ("manifest.json", "image.env", "components.labels"):
            source = make_manifests / name / file
            if source.is_file():
                artifacts[variant + ".make." + file] = source
    for name, path in source_paths.items():
        if name in FIXTURES or name in standalone or name.startswith("config-engine") and owner_archive(name):
            artifacts[name] = path
        if name.startswith("openssh") and variant == "runtime":
            continue
        if image == "docker-sonic-p4rt" and name.startswith(("sairedis", "saimetadata")) and variant == "runtime":
            continue
        if owner_archive(name):
            owned[name] = source_members(path)
            native.update(owned[name])
        elif name.endswith(("files.tar", "config.tar")) or ("debug" in name and name.endswith(".tar")):
            if "debug" not in name or variant == "debug":
                owned[name] = {k: v for k, v in check.payload(path, require_root=False).items()
                               if v["kind"] != "directory"}
        elif name.endswith((".status", "packages.json")):
            artifacts[name] = path
        elif name not in FIXTURES and name != "p4rt.debug.tar":
            artifacts[name] = path
    out = bundle_dir / (variant + ".owners.json")
    out.write_text(json.dumps({"native": native, "owners": {name: list(rows) if owner_archive(name)
                                 else rows for name, rows in owned.items()}}, sort_keys=True) + "\n")
    artifacts[out.name] = out
    return artifacts


def selected_images(directory, default):
    """Standalone receipts cover both variants. VS receipts follow the configured installer selection."""
    default = tuple(default)
    path = directory / "bundle.json"
    if not path.is_file():
        return default
    bundle = json.loads(path.read_text())
    selected = default if bundle["debug_enabled"] and bundle["supports_debug"] else default[:1]
    check.require({r["file"] for r in bundle["archives"].values()} == {n + ".gz" for n in selected},
                  "VS export does not match the selected service variants")
    return selected


def selected_artifacts(directory, default):
    path = directory / "bundle.json"
    if path.is_file():
        bundle = json.loads(path.read_text())
        return set(bundle["fixtures"]) | {v["file"] for v in bundle["archives"].values()}
    return set(default)


def install_redis_config(directory, report, fixture, owner_name):
    """Install the one database configuration file used by the isolated Redis process."""
    artifacts = report["artifacts"]
    name = "redis-config-fixture.tar" if "redis-config-fixture.tar" in artifacts else owner_name
    archive = directory / name
    check.require(check.sha(archive) == artifacts[name]["sha256"], "Config-engine source TAR changed")
    with tarfile.open(archive) as source:
        entries = [item for item in source if str(Path(item.name)) == "run/redis/sonic-db/database_config.json"]
        check.require(len(entries) == 1 and entries[0].isfile(), "missing or duplicate declared database configuration")
        (fixture / "database_config.json").write_bytes(source.extractfile(entries[0]).read())
