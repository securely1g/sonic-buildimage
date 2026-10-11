"""Describe the actual APT and source-built contents of a container image.

This writes package metadata, not Debian packages, and executes no maintainer
scripts. Source package records are reviewed image inputs; their payloads must
be the declared public source-build TARs used by the image.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile

from debian.deb822 import Deb822
from sonic_apt import dependencies, selection
from tools.bazel.oci.oci_layout import validate_layout


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


def build(base, apt_receipt, controls, source_spec, sources, architecture):
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
    require(inventory_digest(records) == inventory_digest(inherited["packages"]),
            "APT receipt and reconstructed package inventory differ")

    spec = json.loads(source_spec.read_text())
    require(spec.get("schema") == 1 and set(spec["packages"]) == set(sources),
            "source package contract and declared payloads differ")
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
                path = member.name.removeprefix("./")
                paths.add(path)
                if member.isfile():
                    stream = archive.extractfile(member)
                    prefix = stream.read(4)
                    if prefix == b"\x7fELF":
                        digest = hashlib.sha256(prefix)
                        while contents := stream.read(1024 * 1024):
                            digest.update(contents)
                        native_files.append({"path": path, "sha256": digest.hexdigest(),
                                             "mode": member.mode, "uid": member.uid, "gid": member.gid})
        multiarch = {"amd64": "x86_64-linux-gnu", "arm64": "aarch64-linux-gnu"}[architecture]
        expected_paths = {path.format(multiarch=multiarch) for path in item["required_paths"]}
        require(expected_paths and expected_paths <= paths,
                "source package lacks reviewed installed paths: " + name)
        digest = sha(payload)
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
    return status, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--apt-receipt", required=True, type=Path)
    parser.add_argument("--source-spec", required=True, type=Path)
    parser.add_argument("--architecture", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--control", action="append", type=Path, default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--status", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    sources = dict(value.split("=", 1) for value in args.source)
    require(len(sources) == len(args.source), "duplicate source package")
    status, receipt = build(args.base, args.apt_receipt, args.control, args.source_spec,
                            {name: Path(path) for name, path in sources.items()}, args.architecture)
    args.status.write_text(status)
    args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
