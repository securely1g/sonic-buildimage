"""Describe the actual APT and source-built contents of a container image.

This writes package metadata, not Debian packages, and executes no maintainer
scripts. Source package records are reviewed image inputs; their payloads must
be the declared public source-build TARs used by the image.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import tarfile

from debian.deb822 import Deb822
from sonic_apt import dependencies, selection
from tools.bazel.oci.oci_layout import validate_layout
from tools.bazel.oci.normalize_layer import normalized_member, normalized_path


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def paragraphs(data):
    return [dict(item) for item in Deb822.iter_paragraphs(data.splitlines())]


def inventory_digest(records):
    normalized = {
        name: dependencies.control_fields(dependencies.package_from_fields(fields, origin=name))
        for name, fields in sorted(records.items())
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(base, apt_receipt, controls, source_spec, sources, architecture, normalization_receipt=None, *, retained_manifest=None):
    layout = validate_layout(base, "linux/" + architecture)
    baseline = selection.base_packages(layout.layers, architecture=architecture)
    original = None
    for layer in layout.layers:
        with tarfile.open(layer) as archive:
            for member in archive:
                if member.name.removeprefix("./") == "var/lib/dpkg/status":
                    original = archive.extractfile(member).read().decode()
    require(original is not None, "base lacks installed package state")
    records = {item["Package"]: item for item in paragraphs(original)
               if item.get("Status") == "install ok installed"}
    require(set(records) == set(baseline), "base status contains ambiguous installed records")
    apt = json.loads(apt_receipt.read_text())
    require(apt.get("base_manifest_digest") == layout.descriptor["digest"],
            "APT receipt belongs to another OCI base")
    inherited = apt.get("dependency_check", {})
    require(inherited.get("status") == "satisfied" and
            inherited.get("base_package_inventory_sha256") == inventory_digest(records),
            "APT receipt does not describe this base's package inventory")
    selected = {item["package"]: item for item in apt["selected"]}
    available = {}
    for path in controls:
        with tarfile.open(path) as archive:
            control = [member for member in archive if member.name.removeprefix("./") == "control"]
            require(len(control) == 1 and control[0].isfile(), "invalid package control archive")
            fields = paragraphs(archive.extractfile(control[0]).read().decode())
        require(len(fields) == 1, "ambiguous Debian package control")
        fields = fields[0]
        name = fields["Package"]
        if name in selected:
            require(fields["Architecture"] in (architecture, "all") and
                    fields["Version"] == selected[name]["version"] and
                    sha(path) == selected[name]["control_sha256"],
                    "APT control differs from selected package: " + name)
            if name in available:
                require(available[name] == fields, "conflicting selected package controls: " + name)
            available[name] = fields
    require(set(available) == set(selected), "missing selected APT package controls")
    for name, fields in available.items():
        records[name] = dict(fields, Status="install ok installed")
    spec = json.loads(source_spec.read_text())
    # Mixed source/imported images already have a checked package contract.
    # Reuse it instead of asking the image to maintain a second source list.
    if isinstance(spec.get("packages"), list):
        items = spec["packages"]
        require(len({item["package"] for item in items}) == len(items), "duplicate source package")
        spec["packages"] = {item["package"]: {
            "control_fields": item["control_fields"], "target": item["source"]["target"],
            "source_revision": item["source"].get("commit", item["source"].get("version")),
            "required_paths": list(item["required_paths"]),
        } for item in items}
    require(spec.get("schema") == 1 and set(spec["packages"]) == set(sources),
            "source package contract and declared payloads differ")
    retained_sources = {}
    if retained_manifest is not None:
        retained = json.loads(retained_manifest.read_text())
        require(apt.get("retained_manifest_sha256") == sha(retained_manifest) and
                retained.get("variant") == "runtime" and retained.get("architecture") == architecture,
                "retained manifest differs from the APT selection")
        imported = retained["packages"]
        names = {item["package"] for item in imported}
        require(len(names) == len(imported) and not names.intersection(sources),
                "ambiguous imported/source package ownership")
        for item in imported:
            fields = dict(item["control_fields"])
            require((fields["Package"], fields["Version"], fields["Architecture"]) ==
                    (item["package"], item["version"], item["architecture"]),
                    "imported package control identity differs")
            fields["Status"] = "install ok installed"
            fields["X-Sonic-Imported-Deb-Sha256"] = item["source_sha256"]
            records[item["package"]] = fields
        retained_sources = {item["package"]: item for item in retained["source_packages"]}
        require(len(retained_sources) == len(retained["source_packages"]) and set(retained_sources) == set(sources),
                "retained source inventory differs from the declared payloads")
        pending = dict(records)
        for name, item in retained_sources.items():
            expected = spec["packages"][name]
            require(inventory_digest({name: item["control_fields"]}) ==
                    inventory_digest({name: expected["control_fields"]}) and
                    item["source"]["target"] == expected["target"] and
                    item["source"].get("commit", item["source"].get("version")) == expected["source_revision"],
                    "retained source identity differs from the source contract: " + name)
            pending[name] = item["control_fields"]
    else:
        pending = records
    require(inventory_digest(pending) == inventory_digest(inherited["packages"]),
            "APT receipt and reconstructed package inventory differ")
    normalization = None
    if normalization_receipt is not None:
        declared = json.loads(normalization_receipt.read_text())
        require(declared.get("base_manifest_digest") == layout.descriptor["digest"],
                "source normalization receipt belongs to another OCI base")
        normalization = {key: declared[key] for key in ("directory_aliases", "root_owned", "modes")}
    provenance = []
    for name, item in sorted(spec["packages"].items()):
        fields = dict(item["control_fields"])
        require(fields["Package"] == name and item.get("source_revision") and item.get("target"),
                "source package lacks identity or provenance: " + name)
        fields["Architecture"] = architecture
        payload = sources[name]
        native_files = []
        with tarfile.open(payload) as archive:
            paths = set()
            for member in archive:
                installed = normalized_member(member, **normalization) if normalization is not None else member
                if installed is None:
                    continue
                path = installed.name.removeprefix("./")
                paths.add(path)
                if member.isfile():
                    stream = archive.extractfile(member)
                    prefix = stream.read(4)
                    if prefix == b"\x7fELF":
                        digest = hashlib.sha256(prefix)
                        while contents := stream.read(1024 * 1024):
                            digest.update(contents)
                        native_files.append({"path": path, "sha256": digest.hexdigest(),
                                             "mode": installed.mode, "uid": installed.uid, "gid": installed.gid})
        multiarch = {"amd64": "x86_64-linux-gnu", "arm64": "aarch64-linux-gnu"}[architecture]
        if retained_sources:
            # The checked source receipt describes the normalized TAR actually
            # installed by this image. Keep the raw input hash as provenance,
            # but report canonical paths and installed ownership from it.
            files = retained_sources[name].get("files")
            require(isinstance(files, dict) and files, "source receipt lacks its installed inventory: " + name)
            installed_native = [{"path": path, **{key: entry[key] for key in ("sha256", "mode", "uid", "gid")}}
                                for path, entry in files.items() if "elf_machine" in entry]
            require(Counter(item["sha256"] for item in installed_native) ==
                    Counter(item["sha256"] for item in native_files),
                    "normalized source inventory changes native bytes: " + name)
            native_files = installed_native
            paths = set(files)
        expected_paths = {path.format(multiarch=multiarch) for path in item["required_paths"]}
        if normalization is not None:
            expected_paths = {normalized_path(path, directory_aliases=normalization["directory_aliases"])
                              for path in expected_paths}
        require(expected_paths and expected_paths <= paths,
                "source package lacks reviewed installed paths: " + name)
        digest = sha(payload)
        require(not retained_sources or digest == retained_sources[name]["input_tar_sha256"],
                "source TAR differs from the retained inventory: " + name)
        for key in ("Installed-Size", "Conffiles", "MD5sum", "SHA256"):
            fields.pop(key, None)
        fields.update({
            "Status": "install ok installed",
            "X-Sonic-Bazel-Target": item["target"],
            "X-Sonic-Bazel-Payload-Sha256": digest,
            "X-Sonic-Source-Revision": item["source_revision"],
        })
        previous = records.get(name)
        records[name] = fields
        provenance.append({
            "package": name, "version": fields["Version"], "target": item["target"],
            "source_revision": item["source_revision"], "payload_sha256": digest,
            "replaced_version": previous["Version"] if previous else None,
            "native_files": sorted(native_files, key=lambda item: item["path"]),
        })
    packages = {name: dependencies.package_from_fields(fields, origin="final image " + name)
                for name, fields in records.items()}
    check = dependencies.validate(packages, architecture=architecture)
    check.update(base_package_inventory_sha256=inventory_digest(records),
                 packages={name: dependencies.control_fields(item) for name, item in sorted(packages.items())})
    status = "\n\n".join(Deb822(fields).dump().strip() for _, fields in sorted(records.items())) + "\n\n"
    receipt = {
        "_generated": {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
                       "generator": "docker-base-trixie/bazel/installed_packages.py"},
        "schema": 1, "architecture": architecture, "source_packages": provenance,
        "apt_selection_sha256": sha(apt_receipt), "source_contract_sha256": sha(source_spec),
        "base_manifest_digest": layout.descriptor["digest"], "dependency_check": check,
    }
    # The debug selector binds to this runtime's exact imported/source handoff,
    # while checking the updated dpkg inventory installed in the runtime image.
    for key in ("variant", "make_manifest_sha256", "source_receipt_sha256", "retained_manifest_sha256"):
        if key in apt:
            receipt[key] = apt[key]
    if normalization_receipt is not None:
        receipt["source_normalization_sha256"] = sha(normalization_receipt)
        receipt["source_normalization"] = normalization
    return status, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--apt-receipt", required=True, type=Path)
    parser.add_argument("--source-spec", required=True, type=Path)
    parser.add_argument("--normalization-receipt", type=Path)
    parser.add_argument("--architecture", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--control", action="append", type=Path, default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--retained-manifest", type=Path)
    parser.add_argument("--status", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    sources = dict(value.split("=", 1) for value in args.source)
    require(len(sources) == len(args.source), "duplicate source package")
    status, receipt = build(args.base, args.apt_receipt, args.control, args.source_spec,
                            {name: Path(path) for name, path in sources.items()}, args.architecture,
                            retained_manifest=args.retained_manifest,
                            normalization_receipt=args.normalization_receipt)
    args.status.write_text(status)
    args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
