#!/usr/bin/env python3
"""Verify AMD64 Syncd runtime behavior and both service images' APT operations."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.bazel_commands import inspect_actions
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.ci.validate_runtime import validate as validate_runtime
from tools.bazel.ci.validate_dpkg_state import validate as validate_dpkg_state
from tools.bazel.oci.image_validation import configured_files, validate_images
from tools.bazel.oci.native_packages import validate_native
from tools.bazel.oci.oci_inventory import apply_layer, resolve_path
from tools.bazel.oci.oci_layout import validate_layout
from tools.bazel.oci.package_contract import read_policy
from tools.bazel.oci.source_base_validation import validate_source_base

OWNER = "//platform/vs/docker-syncd-vs"
CONFIG_ENGINE = "//dockers/docker-config-engine-trixie"
ORCHAGENT = "//dockers/docker-orchagent"
TARGETS = {
    "runtime": OWNER + ":docker-syncd-vs",
    "debug": OWNER + ":docker-syncd-vs-dbg",
    "runtime_archive": OWNER + ":docker-syncd-vs.gz",
    "debug_archive": OWNER + ":docker-syncd-vs-dbg.gz",
    "base": CONFIG_ENGINE + ":docker-config-engine-trixie",
    "policy": OWNER + ":package_policy",
    "runtime_handoff": OWNER + ":runtime_import_manifest",
    "debug_handoff": OWNER + ":debug_import_manifest",
    "source_runtime": OWNER + ":source_runtime_payloads",
    "ordinary_source_layer": OWNER + ":source_runtime_layer",
    "source_debug": OWNER + ":rdeps_debug_symbols",
    "source_receipt": OWNER + ":source_package_receipt",
    "source_contract": OWNER + ":source_contract",
    "runtime_selection": OWNER + ":runtime_apt_selection",
    "debug_selection": OWNER + ":debug_apt_selection",
    "base_symbols": CONFIG_ENGINE + ":docker-config-engine-trixie.debug_symbols",
    "base_metadata": "//dockers/docker-base-trixie:runtime_package_metadata",
    "config_metadata": CONFIG_ENGINE + ":runtime_package_metadata",
    "runtime_configured": OWNER + ":configured_debian",
    "runtime_rootfs": OWNER + ":rootfs",
    "runtime_metadata": OWNER + ":runtime_package_metadata",
    "debug_configured": OWNER + ":configured_debug",
    "debug_metadata": OWNER + ":debug_package_metadata",
    "runtime_installation": OWNER + ":installation_receipt",
    "debug_installation": OWNER + ":debug_installation_receipt",
    "dpkg_filter": "//dockers/docker-base-trixie:dpkg_01_drop",
    "orchagent_runtime": ORCHAGENT + ":docker-orchagent",
    "orchagent_debug": ORCHAGENT + ":docker-orchagent-dbg",
    "orchagent_runtime_archive": ORCHAGENT + ":docker-orchagent.gz",
    "orchagent_debug_archive": ORCHAGENT + ":docker-orchagent-dbg.gz",
}


def save(path, value):
    value = dict(value, _generated={
        "notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
        "generator": "tools/bazel/ci/validate_syncd_image.py",
    })
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def build_inputs(bazel, options, output):
    """Audit the actual selected graph, build it, and query its declared outputs."""
    targets = sorted(set(TARGETS.values()))
    expression = "set(" + " ".join(targets) + ")"
    actions = output / "actions.json"
    with actions.open("w") as result, (output / "aquery.log").open("w") as log:
        subprocess.run(bazel + ["aquery", *options, "--include_file_write_contents", "--output=jsonproto", "deps(" + expression + ")"],
                       stdout=result, stderr=log, check=True)
    audit = inspect_actions(actions)
    audit["targets"] = targets
    audit["deb_generation_authorized"] = False
    save(output / "action-audit.json", audit)
    require(audit["action_count"] > 0, "Image build action graph is empty")
    require(not (audit["deb_outputs"] or audit["deb_packaging_actions"] or audit["packaging_wrappers"]),
            "DEB or packaging wrapper found in image build action graph")
    with (output / "build.log").open("w") as log:
        subprocess.run(bazel + ["build", *options, *targets], stdout=log, stderr=subprocess.STDOUT, check=True)
    formatter = "str(target.label) + '\\t' + repr([f.path for f in target.files.to_list()])"
    with (output / "cquery.log").open("w") as log:
        result = subprocess.run(bazel + ["cquery", *options, expression, "--output=starlark",
                                       "--starlark:expr=" + formatter],
                                stdout=subprocess.PIPE, stderr=log, text=True, check=True)
    paths = {}
    for line in result.stdout.splitlines():
        label, files = line.split("\t", 1)
        label = label[label.index("//"):]
        files = ast.literal_eval(files)
        if label not in targets or len(files) != 1:
            raise ValueError("Expected one output for each requested image input: " + label)
        path = Path(files[0]).absolute()
        if not path.exists():
            raise ValueError("Bazel output is missing: " + str(path))
        paths[label] = path
    if set(paths) != set(targets):
        raise ValueError("Bazel did not return every requested image input")
    return {key: paths[label] for key, label in TARGETS.items()}


def group_records(layout):
    """Read the image's final group file without extracting files on the host."""
    for layer in reversed(layout.layers):
        with tarfile.open(layer) as archive:
            matches = [member for member in archive if member.name.removeprefix("./") == "etc/group"]
            if not matches:
                continue
            member = matches[-1]
            require(member.isfile() and member.size < 65536, "image group database is not a bounded regular file")
            raw = archive.extractfile(member).read()
            records = {}
            gids = set()
            for line in raw.decode().splitlines():
                fields = line.split(":")
                require(len(fields) == 4 and fields[0] and fields[2].isdigit(), "invalid image group record")
                require(fields[0] not in records and int(fields[2]) not in gids, "duplicate image group name or ID")
                records[fields[0]] = line
                gids.add(int(fields[2]))
            return records, hashlib.sha256(raw).hexdigest()
    raise ValueError("image has no group database")


def validate_package_state(paths, policy):
    """Bind the final images' package databases to their checked build outputs."""
    reports = {}
    base = validate_layout(paths["base"], "linux/" + policy["architecture"])
    base_groups, _ = group_records(base)
    for variant in ("runtime", "debug"):
        layout = validate_layout(paths[variant], "linux/" + policy["architecture"])
        files = {}
        for layer in layout.layers:
            apply_layer(layer, files)
        groups, group_sha = group_records(layout)
        require(all(groups.get(name) == value for name, value in base_groups.items()) and
                "_ssh" in groups and resolve_path("etc/group", files) == "etc/group" and
                files.get("etc/group", {}).get("sha256") == group_sha,
                "installed groups change inherited identities or omit the OpenSSH group")
        ssh_gid = int(groups["_ssh"].split(":")[2])
        agent = files.get("usr/bin/ssh-agent", {})
        require(agent.get("kind") == "file" and agent.get("uid") == 0 and
                agent.get("gid") == ssh_gid and agent.get("mode") == 0o2755,
                "ssh-agent lacks its installed OpenSSH group ownership and setgid mode")
        metadata_name = "syncd-packages.json"
        metadata_path = paths[variant + "_metadata"]
        name = "usr/share/sonic/bazel/" + metadata_name
        require(resolve_path(name, files) == name, "installed package state is redirected: " + name)
        require(files.get(name) == {
            "kind": "file", "uid": 0, "gid": 0, "mode": 0o644,
            "size": metadata_path.stat().st_size, "sha256": sha(metadata_path),
        }, "installed package state differs from its checked output: " + name)
        installation = paths[variant + "_installation"]
        configured_files(paths[variant + "_configured"], installation, policy["architecture"])
        state = json.loads(metadata_path.read_bytes())
        require(state.get("schema") == 1 and state.get("architecture") == policy["architecture"] and
                state.get("installation_sha256") == sha(installation) and
                state.get("rootfs_sha256") == sha(paths[variant + "_configured"]),
                "installed package metadata belongs to a different APT installation")
        require("var/lib/dpkg/status" in state["info_files"], "installed metadata lacks the final status file")
        for name, expected_file in state["info_files"].items():
            require(resolve_path(name, files) == name and all(files.get(name, {}).get(key) == value
                    for key, value in dict(expected_file, kind="file", uid=0, gid=0).items()),
                    "installed dpkg metadata differs from its checked output: " + name)
        check = state.get("dependency_check", {})
        require(check.get("status") == "satisfied", "installed package dependencies are not satisfied")
        if variant == "debug":
            require(set(policy["debug_tools"]) <= set(check.get("packages", {})),
                    "debug package database is missing installed tools")
        reports[variant] = {
            "image_manifest_digest": layout.descriptor["digest"],
            "status_sha256": state["info_files"]["var/lib/dpkg/status"]["sha256"],
            "installation_sha256": sha(installation),
            "metadata_sha256": sha(paths[variant + "_metadata"]),
            "package_count": len(check["packages"]),
            "group_file_sha256": group_sha,
            "ssh_agent": {key: agent[key] for key in ("uid", "gid", "mode", "sha256")},
        }
    return reports


def validate_dpkg_images(paths, output):
    """Exercise exact source-built service images; this profile remains AMD64-only."""
    cases = (
        ("syncd", "runtime", ""),
        ("syncd", "debug", ""),
        ("orchagent", "runtime", "orchagent_"),
        ("orchagent", "debug", "orchagent_"),
    )
    results = {}
    for image, variant, prefix in cases:
        name = image + "-" + variant
        try:
            layout = validate_layout(paths[prefix + variant], "linux/amd64")
            archive = paths[prefix + variant + "_archive"]
            # Each image installs the same additional command and dependencies
            # while preserving its existing Debian packages and source layers.
            receipt = image + "-packages.json"
            result = validate_dpkg_state(
                architecture="amd64", output=output / ("dpkg-" + name), archive=archive,
                archive_sha256=sha(archive), config_digest=layout.manifest["config"]["digest"],
                installed_receipts=["/usr/share/sonic/bazel/" + receipt],
                installed_source_receipts=[
                    "/usr/share/sonic/bazel/base-packages.json",
                    "/usr/share/sonic/bazel/config-engine-packages.json",
                ])
            results[name] = {"status": result["status"], "archive_sha256": sha(archive),
                             "image_manifest_digest": layout.descriptor["digest"],
                             "receipt_sha256": sha(output / ("dpkg-" + name) / "receipt.json")}
        except Exception as error:
            results[name] = {"status": "failed", "error": str(error)}
        finally:
            save(output / "dpkg-images.json", {"architecture": "amd64", "images": results})
    require(all(result["status"] == "passed" for result in results.values()),
            "Installed package transactions failed; see dpkg-images.json and the per-image logs")
    return results


def run(*, bazel, options, output, redis_server):
    output.mkdir(parents=True, exist_ok=True)
    # The VS/FIPS Make profile selects this same source-built base. Passing it
    # explicitly also covers direct validation outside the Make wrapper.
    options = [*options, "--//dockers/docker-orchagent:source_config_engine=true"]
    os.environ["BAZEL"] = bazel[0]
    os.environ["BAZEL_NATIVE_STARTUP_ARGS"] = json.dumps(bazel[1:])
    launcher = Path(__file__).resolve().parents[1] / "oci/native_build.py"
    paths = build_inputs([sys.executable, str(launcher)], options, output)
    save(output / "outputs.json", {key: str(value) for key, value in paths.items()})
    validate_inputs(paths, output=output, redis_server=redis_server)


def validate_inputs(paths, *, output, redis_server):
    """Validate the declared outputs of the audited source-image build."""
    workspace = Path.cwd()
    owner = workspace / "platform/vs/docker-syncd-vs"
    contract = paths["source_contract"]
    policy = read_policy(paths["policy"])
    source_arguments = {
        "source_contract_path": contract,
        "source_runtime_tar": paths["source_runtime"],
        "source_debug_tar": paths["source_debug"],
        "source_receipt": paths["source_receipt"],
        "base_path": paths["base"],
    }
    native = validate_native(paths["runtime_handoff"], paths["debug_handoff"],
                             policy=policy, **source_arguments)
    save(output / "native-packages.json", native)
    images = validate_images(
        paths["runtime"], paths["debug"], paths["runtime_handoff"], paths["debug_handoff"],
        workspace / "target/bazel-manifests/docker-syncd-vs/manifest.json",
        workspace / "target/bazel-manifests/docker-syncd-vs-dbg/manifest.json", workspace,
        policy=policy, apt_lock_path=owner / "bazel/apt.lock.json",
        dpkg_filter_path=paths["dpkg_filter"],
        configured_runtime_tar=paths["runtime_configured"], configured_debug_tar=paths["debug_configured"],
        runtime_rootfs_tar=paths["runtime_rootfs"], runtime_installation=paths["runtime_installation"],
        debug_installation=paths["debug_installation"],
        runtime_package_metadata=paths["runtime_metadata"],
        runtime_source_layer=paths["ordinary_source_layer"],
        debug_metadata_files={"usr/share/sonic/bazel/syncd-packages.json": paths["debug_metadata"]},
        runtime_apt_selection=paths["runtime_selection"], debug_apt_selection=paths["debug_selection"],
        runtime_archive=paths["runtime_archive"], debug_archive=paths["debug_archive"],
        **source_arguments)
    save(output / "images.json", images)
    package_state = validate_package_state(paths, policy)
    save(output / "installed-package-state.json", package_state)
    inherited = validate_source_base(
        paths["runtime"], paths["debug"],
        metadata_paths=[paths["base_metadata"], paths["config_metadata"]],
        source_debug_tar=paths["base_symbols"], architecture=policy["architecture"])
    save(output / "source-base.json", inherited)
    # Common is used by both source graphs. Require its identity to agree before
    # deduplicating it in the installed loader/GDB probes.
    paired = {row["path"]: row for row in native["paired_symbols"]}
    additions = []
    for row in inherited["paired_symbols"]:
        previous = paired.get(row["path"])
        if previous:
            if any(previous[key] != row[key] for key in ("build_id", "debug_path")):
                raise ValueError("Base and Syncd source symbol identities differ: " + row["path"])
        else:
            additions.append(row)
    native["inherited_base_symbol_pairs"].extend(additions)
    native["elf_count"] += len(additions)
    native["external_needed"] = sorted(set(native["external_needed"]) | set(inherited["external_needed"]))
    combined = {"schema": 1, "native": native, "images": images,
                "source_base": inherited, "installed_package_state": package_state}
    report = output / "static-validation.json"
    save(report, combined)
    result = validate_runtime(paths["runtime_archive"], paths["debug_archive"], report,
                              owner / "config/runtime_smoke.json", output / "installed-validation.json", redis_server)
    validate_dpkg_images(paths, output)
    if not result["passed"]:
        raise ValueError("Installed image checks failed; see installed-validation.json")
    print(json.dumps({"passed": True, "report": str(output / "installed-validation.json"),
                      "native_elfs": native["elf_count"], "base_source_pairs": len(inherited["paired_symbols"])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--startup-arg", action="append", default=[])
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--redis-server", type=Path, required=True)
    args = parser.parse_args()
    run(bazel=[args.bazel, *args.startup_arg], options=[*args.bazel_arg, "--config=syncd-dash"],
        output=args.output.absolute(), redis_server=args.redis_server)


if __name__ == "__main__":
    main()
