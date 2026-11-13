#!/usr/bin/env python3
"""Observe fixed original archives using the existing archive and file owners.

No image conversion, OCI manifest construction, build, install or source acceptance.
The operator must authenticate the frozen original API and receipt files before
calling this module; every such file is independently bound here by size and SHA.
"""

import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
from types import SimpleNamespace

CHUNK = 1024 * 1024
MAX_JSON = 16 * CHUNK
MAX_ARCHIVE = 8 * 1024**3
MAX_EXPANDED = 64 * 1024**3
MAX_FILES = 256
HEX = re.compile(r"[0-9a-f]{64}")
SCOPES = {
    "swss": ("docker-config-engine-trixie",
             "docker-orchagent", "docker-orchagent-dbg"),
    "syncd": ("docker-config-engine-trixie",
              "docker-syncd-vs", "docker-syncd-vs-dbg"),
}


def require(value, message):
    if not value:
        raise ValueError(message)


def original_json(root, row, original):
    """Read only a frozen original regular file, through the qualified holder."""
    relative = Path(row["path"])
    require(not relative.is_absolute() and relative.parts and
            all(x not in (".", "..") for x in relative.parts), "Invalid evidence path")
    require(type(row["bytes"]) is int and 0 < row["bytes"] <= MAX_JSON and
            HEX.fullmatch(row["sha256"]), "Invalid evidence identity")
    with original.HeldFile(root / relative) as held:
        require(held.initial.st_size == row["bytes"], "Original evidence size differs")
        data = os.pread(held.fd, row["bytes"] + 1, 0)
        require(len(data) == row["bytes"] and
                hashlib.sha256(data).hexdigest() == row["sha256"],
                "Original evidence digest differs")
        held.verify()
    return json.loads(data)


def original_proof(row, evidence_root, original):
    """Require the actual successful original job, build and validation output."""
    evidence = row["evidence"]
    metadata = original_json(evidence_root, evidence["metadata"], original)
    receipt = original_json(evidence_root, evidence["receipt"], original)
    validation = original_json(evidence_root, evidence["validation"], original)
    run, job, artifact = (metadata[key] for key in ("run", "job", "artifact"))
    expected = evidence["producer"]
    require(run["id"] == expected["run_id"] == job["run_id"] == artifact["workflow_run"]["id"] and
            job["id"] == expected["job_id"] and artifact["id"] == expected["artifact_id"],
            "Original producer identity differs")
    require(run["head_sha"] == expected["head_sha"] == artifact["workflow_run"]["head_sha"] and
            run["repository"]["full_name"] == expected["repository"] and
            artifact["digest"] == expected["artifact_digest"], "Original source/artifact identity differs")
    require(run["status"] == "completed" and job["status"] == "completed" and
            job["conclusion"] == "success", "Original producer job did not succeed")
    numbers = []
    for role in ("build_step", "validation_step", "upload_step"):
        steps = [x for x in job["steps"] if x["name"] == expected[role]]
        require(len(steps) == 1 and steps[0]["status"] == "completed" and
                steps[0]["conclusion"] == "success", "Original required step did not succeed")
        numbers.append(steps[0]["number"])
    require(numbers[0] <= numbers[1] < numbers[2], "Original validation did not precede upload")
    require(receipt["revision"] == expected["tested_revision"] and
            receipt["status"] in ("built", "passed"), "Original build receipt differs")
    archive = receipt["artifacts"][row["archive"]["name"]]
    require(archive["sha256"] == row["archive"]["sha256"] and
            archive["bytes"] == row["archive"]["bytes"], "Original archive receipt differs")
    if evidence["kind"] == "installed-policy":
        require(receipt["status"] == "passed" and
                receipt["installed_images_policy_sha256"] == evidence["validation"]["sha256"],
                "Original policy is not bound to its passed receipt")
        images = [x for x in validation["images"] if x["name"] == row["name"]]
        require(len(images) == 1, "Original policy image is missing or duplicated")
        image = images[0]
        require(image["archive_sha256"] == archive["sha256"], "Original policy archive differs")
        return {"config_digest": image["config_digest"], "rootfs": image["expected_rootfs"]}
    require(evidence["kind"] == "image-validation" and validation["status"] == "passed",
            "Original image validation did not pass")
    if "revision" in validation:
        require(validation["revision"] == expected["tested_revision"], "Original validation source differs")
    image = validation["images"][row["name"]] if "images" in validation else validation
    if "archive" in image:
        require(image["archive"]["sha256"] == archive["sha256"], "Original validation archive differs")
    return {"config_digest": image["config_digest"], "rootfs": image["rootfs"]}


class HeldPath:
    """Expose the two existing archive_snapshot path operations on the held FD."""
    def __init__(self, held):
        self.held = held

    def open(self, mode):
        require(mode == "rb", "Archive holder is read-only")
        self.held.verify()
        os.lseek(self.held.fd, 0, os.SEEK_SET)
        return os.fdopen(os.dup(self.held.fd), "rb")

    def stat(self):
        self.held.verify()
        return os.fstat(self.held.fd)


def inspect_archive(row, archive_root, scratch, manifest, proof, owners, *,
                    audit_only=False, inventory_reader=None):
    """Copy regular original layer bytes for existing inventory checks, not extraction."""
    original, archive_owner, checks = owners
    require(inventory_reader is None or audit_only, "Alternate inventory is audit-only")
    require(not audit_only or manifest is None, "Audit does not accept a Make manifest")
    expected = row["archive"]
    require(Path(expected["name"]).name == expected["name"] and
            type(expected["bytes"]) is int and 0 < expected["bytes"] <= MAX_ARCHIVE and
            HEX.fullmatch(expected["sha256"]), "Invalid fixed archive identity")
    with original.HeldFile(archive_root / expected["name"]) as held:
        require(held.initial.st_size == expected["bytes"] and
                original.hash_range(held.fd, 0, held.initial.st_size) == expected["sha256"],
                "Original archive bytes differ")
        files, documents, json_bytes = {}, {}, 0
        holder = HeldPath(held)
        with holder.open("rb") as source, gzip.GzipFile(fileobj=source) as decoded:
            meter = original.Meter(decoded, MAX_EXPANDED)
            with tarfile.open(fileobj=meter, mode="r|", tarinfo=original.BoundedTarInfo) as archive:
                for member in archive:
                    name = original.clean_name(member.name)
                    require(member.isfile() or member.isdir(), "Unsupported outer archive member")
                    if member.isdir():
                        continue
                    require(name not in files and len(files) < MAX_FILES, "Duplicate or excessive archive members")
                    is_json = name == "manifest.json" or archive_owner.CONFIG_NAME.fullmatch(name)
                    require(is_json or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}\.tar\.gz", name),
                            "Unexpected original archive member")
                    maximum = MAX_JSON if is_json else MAX_ARCHIVE
                    if is_json:
                        json_bytes += member.size
                        require(json_bytes <= MAX_JSON and len(documents) < 2,
                                "Original archive JSON exceeds its aggregate bound")
                    destination = scratch / (str(len(files)) + ".member")
                    with archive.extractfile(member) as content, destination.open("xb") as output:
                        record = original.copy_member(content, output, member.size, maximum)
                    files[name] = {"path": destination, "sha256": record["sha256"], "bytes": record["bytes"]}
                    if is_json:
                        documents[name] = json.loads(destination.read_bytes())
            while meter.read(CHUNK):
                pass
        held.verify()
        if audit_only:
            entries = documents.get("manifest.json")
            require(isinstance(entries, list) and len(entries) == 1 and
                    isinstance(entries[0], dict), "Audit requires one original image")
            config_name = entries[0].get("Config")
            require(isinstance(config_name, str) and config_name in files and
                    "sha256:" + files[config_name]["sha256"] == proof["config_digest"],
                    "Original config differs before reading its label")
            config = documents.get(config_name)
            require(isinstance(config, dict), "Original config is missing")
            label = config.get("config", {}).get("Labels", {}).get(archive_owner.LABEL)
            require(isinstance(label, str), "Original manifest label is absent")
            manifest = json.loads(label)
            require(isinstance(manifest, dict), "Original manifest label is not an object")
        settings = []
        snapshot = archive_owner.archive_snapshot(
            holder, row["tag"], manifest, entrypoint=row["entrypoint"],
            empty_parent=row["empty_parent"], config_check=settings.append)
        require(snapshot["sha256"] == expected["sha256"] and snapshot["bytes"] == expected["bytes"],
                "Archive changed during observation")
        require(snapshot["config_digest"] == proof["config_digest"] and
                {"type": "layers", "diff_ids": snapshot["rootfs"]} == proof["rootfs"],
                "Archive config/rootfs differs from original successful validation")
        layers = documents["manifest.json"][0]["Layers"]
        require([{k: files[name][k] for k in ("sha256", "bytes")} for name in layers] == snapshot["layers"],
                "Copied layer identities differ from the observed archive")
        # image_inventory needs only layer paths; this is not a synthesized OCI layout.
        read_inventory = checks.image_inventory if inventory_reader is None else inventory_reader
        inventory, _ = read_inventory(SimpleNamespace(layers=[files[name]["path"] for name in layers]))
        held.verify()
    require(len(settings) == 1, "Original config was not observed exactly once")
    result = {"snapshot": snapshot, "settings": settings[0], "files": inventory}
    if audit_only:
        result.update(audit_only=True, Make_manifest_equivalence_claimed=False)
    return result


def policy_from_observations(declarations, observations, checks):
    """Keep original stored-layer ancestry and all existing declared file checks."""
    require(not any(row.get("audit_only", False) for row in observations.values()),
            "Audit-only observations cannot produce installed policy")
    rows, pairs, checked = [], [], {}
    for declaration in declarations:
        require(declaration["architecture"] == "amd64", "Installed policy architecture differs")
        pairs.extend(declaration.get("symbol_pairs", []))
        for declared in declaration["images"]:
            name = declared["name"]
            require(name not in checked and name in observations, "Missing or duplicated installed image")
            observed = observations[name]
            snapshot, files = observed["snapshot"], observed["files"]
            require(snapshot["tag"] == declared["tag"], "Owner-declared image tag differs")
            checks.verify_directory_aliases(files, declared.get("directory_aliases", {}))
            expected = {}
            for path, metadata in declared.get("files", {}).items():
                item = checks.verify_file(files, path, metadata)
                expected[path] = {k: item[k] for k in ("kind", "sha256", "mode", "uid", "gid")}
            parent_name = declared.get("parent")
            if parent_name:
                require(parent_name in checked, "Installed parent must precede its child")
                parent = checked[parent_name]
                n = len(parent["snapshot"]["layers"])
                require(snapshot["layers"][:n] == parent["snapshot"]["layers"] and
                        snapshot["rootfs"][:n] == parent["snapshot"]["rootfs"],
                        "Original archive does not extend its declared parent: " + name)
                for path in declared.get("inherit_files", []):
                    item = checks.verify_file(parent["files"], path, {})
                    checks.verify_file(files, path, item, "Image changes an inherited file")
                    expected[path] = {k: item[k] for k in ("kind", "sha256", "mode", "uid", "gid")}
            rows.append({**copy.deepcopy(declared), "files": expected,
                         "archive_sha256": snapshot["sha256"], "config_digest": snapshot["config_digest"],
                         "expected_rootfs": {"type": "layers", "diff_ids": snapshot["rootfs"]},
                         "expected_config": copy.deepcopy(observed["settings"])})
            checked[name] = observed
    require(set(checked) == set(observations), "Unexpected observed images")
    return {"architecture": "amd64", "images": rows, "symbol_pairs": pairs}


def observe(plan, declarations, archive_root, evidence_root, scratch, owners):
    """Run the fixed reviewed scope after its existing declaration targets finish.

The caller retains the source/build proof for those unchanged write_file outputs,
and separately retains its authentic operator workflow/run identity. This API
does not manufacture either producer provenance or declaration-build evidence.
"""
    require(plan["status"] == "FROZEN_REVIEWED_ORIGINAL_ARCHIVES" and
            plan["scope"] in SCOPES, "Original policy plan is incomplete")
    names = SCOPES[plan["scope"]]
    require(tuple(x["name"] for x in plan["images"]) == names and
            tuple(x["name"] for d in declarations for x in d["images"]) == names,
            "Original policy scope differs from the fixed owner declarations")
    original = owners[0]
    proofs = {row["name"]: original_proof(row, evidence_root, original) for row in plan["images"]}
    # Fail an already-proven ancestry mismatch before decoding any large archive.
    parents = {x["name"]: x.get("parent") for d in declarations for x in d["images"]}
    for name in names:
        if parents[name]:
            parent = proofs[parents[name]]["rootfs"]["diff_ids"]
            require(proofs[name]["rootfs"]["diff_ids"][:len(parent)] == parent,
                    "Original validation proves an incompatible parent: " + name)
    observed = {}
    for row in plan["images"]:
        manifest = original_json(evidence_root, row["make_manifest"], original)
        # Only disposable layer copies live here; original archives stay held
        # under archive_root. Inventory results contain metadata, not paths.
        with tempfile.TemporaryDirectory(dir=scratch, prefix=row["name"] + "-") as directory:
            observed[row["name"]] = inspect_archive(
                row, archive_root, Path(directory), manifest, proofs[row["name"]], owners)
    return policy_from_observations(declarations, observed, owners[2])
