#!/usr/bin/env python3
"""Copy fixed original bytes into direct artifact members; never build packages."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import observe_vs_host_binaries as binaries
from tools.bazel.ci import observe_vs_legacy_configs as legacy
from tools.bazel.ci import observe_original_host_siblings as siblings
from tools.bazel.ci import recover_vs_original_inputs as original

# Fixed original bytes for transport only; admission remains separate.
SELECTION_SHA256 = '7ca63811c5032792c117a481b6480d89dc64bb869848d253097ad9bba6e16760'
MAX_SELECTION = 256 * 1024
HOST_ARTIFACT = 14030424431
LEGACY_ARTIFACT = 14037915141
IDENTITY = ("required_name", "cache_name", "cache_bytes", "cache_sha256",
            "member_path", "member_bytes", "member_sha256")
require = original.require


def load_selection(path):
    require(isinstance(SELECTION_SHA256, str) and
            re.fullmatch("[0-9a-f]{64}", SELECTION_SHA256), "Original member selection is not sealed")
    with original.HeldFile(path) as held:
        require(held.initial.st_size <= MAX_SELECTION, "Selection exceeds bound")
        with os.fdopen(os.dup(held.fd), "rb") as stream:
            raw = stream.read(MAX_SELECTION + 1)
        require(len(raw) <= MAX_SELECTION and hashlib.sha256(raw).hexdigest() == SELECTION_SHA256,
                "Selected original manifest differs")
        held.verify()
    value = json.loads(raw)
    require(value.get("schema") == 1 and value.get("status") == "SELECTED_ORIGINAL_MEMBERS",
            "Selection remains incomplete")
    require(isinstance(value.get("entries"), list) and 8 <= len(value["entries"]) <= 74,
            "Unexpected fixed selection size")
    return value


def resolve_rows(selection, host, old_images, companions=None):
    """Select only identity tuples already present in the immutable rosters."""
    result, destinations, legacy_names = [], set(), set()
    for selected in selection["entries"]:
        kind = selected["kind"]
        require(kind in ("deb", "wheel", "docker"), "Unsupported original member kind")
        origin = LEGACY_ARTIFACT if kind == "docker" else HOST_ARTIFACT
        require(selected["artifact_id"] == origin, "Original artifact differs")
        candidates = old_images["rows"] if kind == "docker" else host["inputs"] + (companions["inputs"] if companions else [])
        matches = [row for row in candidates if all(row[key] == selected[key] for key in IDENTITY)]
        require(len(matches) == 1, "Selection is not one exact original tuple")
        row = dict(matches[0])
        name = row["required_name"]
        require(name == Path(name).name and name not in ("", ".", ".."), "Unsafe original name")
        target = ("target/" + name if kind == "docker" else
                  "target/debs/trixie/" + name if kind == "deb" else
                  "target/python-wheels/trixie/" + name)
        if kind == "docker":
            legacy_names.add(name)
            require(name.endswith(".gz") and row["member_path"] == target,
                    "Legacy target differs from original member")
        else:
            require(row["kind"] == kind and row["target_relative_path"] == target,
                    "Host target differs from fixed configured roster")
        require(selected["target_relative_path"] == target and target not in destinations,
                "Repeated or changed destination")
        require(row["cache_name"] == Path(row["cache_name"]).name and row["cache_name"].endswith(".tgz"),
                "Unsafe original object name")
        destinations.add(target)
        row.update(kind=kind, artifact_id=origin, target_relative_path=target)
        result.append(row)
    require(legacy_names == {row["required_name"] for row in old_images["rows"]},
            "All eight original legacy families must be selected exactly once")
    require(len(result) == selection["selected_member_count"] and
            sum(row["member_bytes"] for row in result) == selection["selected_member_bytes"],
            "Selected member totals differ")
    return result


def copy_original(path, row, output):
    """Use the held source and existing bounded copier; no format conversion."""
    configuration = None
    if row["kind"] == "docker":
        configuration = legacy.archives.observe(path, row["expected_tags"][0], row["entrypoint"],
            expected_sha256=row["member_sha256"], include_repo_tags=True, include_loader_metadata=True,
            allow_absent_image_version=row["required_name"] == "docker-nat.gz")
    destination = output / row["target_relative_path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    limit = 256 * binaries.MIB if row["kind"] == "docker" else binaries.MAX_MEMBER
    with original.HeldFile(path) as held:
        require(held.initial.st_size == row["member_bytes"], "Original selected size differs")
        with os.fdopen(os.dup(held.fd), "rb") as source, destination.open("xb") as target:
            copied = original.copy_member(source, target, row["member_bytes"], limit)
        require(copied["sha256"] == row["member_sha256"], "Original selected hash differs")
        held.verify()
    return {"name": row["required_name"], "target_relative_path": row["target_relative_path"],
            "artifact_id": row["artifact_id"], "original": {key: row[key] for key in IDENTITY},
            "copied": copied, "archive_configuration_observation": configuration,
            "source_admission": False, "VS_assembly_admission": False}


def promote(rows, inputs, output, scratch, report=None):
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[(row["artifact_id"], row["cache_name"])].append(row)
    record = report if report is not None else {"results": []}
    result = record["results"]
    require(not result, "Promotion requires a fresh result record")
    for (artifact, name), members in sorted(grouped.items()):
        require(len({(row["cache_bytes"], row["cache_sha256"]) for row in members}) == 1,
                "Conflicting original outer identity")
        limit = 256 * binaries.MIB if artifact == LEGACY_ARTIFACT else binaries.MAX_MEMBER
        record["active_original_object"] = {"artifact_id": artifact, "cache_name": name,
                                             "cache_bytes": members[0]["cache_bytes"],
                                             "cache_sha256": members[0]["cache_sha256"]}
        observed = binaries.inspect_cached(inputs[artifact] / "execution" / name, members, scratch,
                   observer=lambda path, row, unused: copy_original(path, row, output), maximum_member=limit)
        # Retain results only after the owner verifies the complete original group.
        result.extend(observed)
        record.pop("active_original_object")
    require({row["target_relative_path"] for row in result} ==
            {row["target_relative_path"] for row in rows}, "Direct-member output closure differs")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("selection", "host-roster", "legacy-roster", "companion-roster", "host-artifact", "legacy-artifact",
                 "output", "scratch", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "ORIGINAL_MEMBER_PROMOTION_INCOMPLETE", "source_admission": False,
              "VS_assembly_admission": False, "package_or_image_generation": False,
              "cache_restore": False, "results": []}
    output_created = scratch_created = False
    code = 1
    def deadline(*_):
        raise TimeoutError("Original-member copy deadline")
    previous = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(900)
    try:
        require(args.report == args.output / "lineage.json" and args.output != args.scratch,
                "Report must be inside the fresh private output")
        for directory in (args.output.parent, args.scratch.parent,
                          args.host_artifact, args.legacy_artifact):
            require(directory.is_absolute() and directory.resolve() == directory and directory.is_dir(),
                    "Expected canonical private directories")
        args.output.mkdir(mode=0o700)
        output_created = True
        selected = load_selection(args.selection)
        rows = resolve_rows(selected, binaries.load_manifest(args.host_roster), legacy.load_manifest(args.legacy_roster),
                            siblings.load_manifest(args.companion_roster))
        required = selected["selected_member_bytes"] + 256 * binaries.MIB + 64 * binaries.MIB
        require(shutil.disk_usage(args.output.parent).free >= required and
                shutil.disk_usage(args.scratch.parent).free >= 256 * binaries.MIB + 64 * binaries.MIB,
                "Insufficient temporary copy space")
        args.scratch.mkdir(mode=0o700)
        scratch_created = True
        report["selection_sha256"] = SELECTION_SHA256
        report["selection_evidence"] = selected["selection_evidence"]
        promote(rows, {HOST_ARTIFACT: args.host_artifact,
                       LEGACY_ARTIFACT: args.legacy_artifact}, args.output, args.scratch, report)
        report["status"] = "UNCHANGED_ORIGINAL_MEMBERS_COPIED_NOT_ADMITTED"
        code = 0
    except Exception as error:
        report["error_type"] = type(error).__name__
        code = 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if scratch_created:
            try:
                args.scratch.rmdir()
            except OSError as error:
                report["cleanup_error_type"] = type(error).__name__
                code = 1
        if code:
            report["status"] = "ORIGINAL_MEMBER_PROMOTION_INCOMPLETE"
        if output_created:
            with args.report.open("x") as stream:
                json.dump(report, stream, indent=2, sort_keys=True)
                stream.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
