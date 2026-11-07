#!/usr/bin/env python3
"""Build DASH engine images, then verify installed payloads and incremental outputs."""

import argparse
from functools import partial
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.build_helpers import cache_options
from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci import bazel_commands, build, cache_reuse, command_log, public_artifacts, resolution
from tools.bazel.oci import oci_layout

OWNER = "//platform/vs/docker-dash-engine:"
IMAGES = ("docker-dash-engine", "docker-dash-engine-dbg")
BASE = "//dockers/docker-base-trixie:docker-base-trixie"
OPTIONS = ["--jobs=2", "--local_resources=cpu=2", "--local_resources=memory=8000",
           "--platforms=@sonic_build_infra//platforms:x86_64_trixie",
           "--host_platform=@sonic_build_infra//platforms:x86_64_trixie"]
TARGETS = {
    **{name + ".gz": OWNER + name + ".gz" for name in IMAGES},
    "runtime-apt.json": OWNER + "runtime_apt_selection",
    "engine.runtime-apt.tar": OWNER + "debian_runtime_payload",
    "debug-apt.json": OWNER + "debug_apt_selection",
    "engine.debug.tar": OWNER + "all_debug_symbols",
    "engine.debug-tools.tar": OWNER + "debug_tools",
    "engine.source.tar": OWNER + "source_runtime",
    "engine.files.tar": OWNER + "files",
    "engine.packages.json": OWNER + "runtime_package_metadata",
    "engine.status": OWNER + "installed_packages",
    "engine-debug.packages.json": OWNER + "debug_package_metadata",
    "engine-debug.status": OWNER + "debug_installed_packages",
    "base.packages.json": "//dockers/docker-base-trixie:runtime_package_metadata",
    "base.source.tar": "//dockers/docker-base-trixie:source_runtime",
    "openssh.source.tar": OWNER + "debug_client",
    "pi.source.tar": "@p4lang_pi//:runtime",
    "bmv2.source.tar": "@p4lang_bmv2//:runtime",
    "nnpy.source.tar": "@nnpy//:runtime",
}

# The failed baseline executed PI twice and BMv2 once. The initial build can
# contain other configured instances; these are observations, not a contract.
FOREIGN_TARGETS = frozenset(("@@p4lang-pi++_repo_rules+pi_source//:sdk",
                            "@@p4lang-bmv2++_repo_rules+bmv2_source//:bmv2_build"))
FOREIGN_PAYLOADS = ("pi.source.tar", "bmv2.source.tar")
FOREIGN_INSTANCE_LIMIT = 16
DIAGNOSTIC_LIMIT = 2 * 1024 * 1024
EXPLAIN_LIMIT = 4 * 1024 * 1024
EXPLAIN_REASONS = {
    "unconditional execution is requested": "unconditional",
    "no entry in the cache (action is new)": "no-cache-entry",
    "cache entry is corrupted": "corrupt-cache-entry",
    "One of the files has changed": "input-or-output-metadata",
    "action command has changed (try --verbose_explanations for more info)": "action-key",
    "Effective client environment has changed (try --verbose_explanations for more info)": "client-environment",
    "the set of files on which this action depends has changed": "dependency-set",
}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _spawn_digest(value):
    if value is None:
        return None
    check.require(isinstance(value, dict) and set(value) <= {"hash", "sizeBytes", "hashFunctionName"},
                  "invalid foreign digest")
    size = value.get("sizeBytes", "0")
    if isinstance(size, str):
        check.require(re.fullmatch(r"0|[1-9][0-9]*", size) is not None, "invalid foreign digest size")
        size = int(size)
    function = value.get("hashFunctionName")
    check.require(function in (None, "SHA-256", "SHA256"), "unexpected foreign digest function")
    return {"hash": public_artifacts.digest(value.get("hash")),
            "size_bytes": public_artifacts.integer(size), "hash_function": function}


def _private_files(values):
    check.require(isinstance(values, list) and len(values) <= 250000, "invalid foreign file list")
    rows = []
    for value in values:
        check.require(isinstance(value, dict) and set(value) <= {
            "path", "digest", "isTool", "symlinkTargetPath"}, "invalid foreign file")
        path, link, tool = value.get("path"), value.get("symlinkTargetPath"), value.get("isTool", False)
        check.require(isinstance(path, str) and path and
                      (link is None or isinstance(link, str)) and type(tool) is bool,
                      "invalid foreign file identity")
        rows.append([path, _spawn_digest(value.get("digest")), tool, link])
    check.require(len({row[0] for row in rows}) == len(rows), "duplicate foreign file identity")
    return sorted(rows, key=lambda row: row[0])


def _private_pairs(values):
    check.require(isinstance(values, list), "invalid foreign key-value list")
    rows = []
    for value in values:
        check.require(isinstance(value, dict) and set(value) <= {"name", "value"} and
                      isinstance(value.get("name"), str) and value["name"] and
                      isinstance(value.get("value", ""), str), "invalid foreign key-value entry")
        rows.append([value["name"], value.get("value", "")])
    check.require(len({row[0] for row in rows}) == len(rows), "duplicate foreign key-value name")
    return sorted(rows)


def foreign_diagnostics(execution, counts, payloads):
    """Project only fixed foreign spawns; raw paths/commands never leave the log.

    The existing reader bounds a record, not the raw file on disk. Persistent
    local action-cache hits have no spawn record and remain unobserved here.
    """
    selected = []
    for record in cache_reuse.json_records(execution):
        check.require(isinstance(record, dict), "invalid foreign spawn record")
        if record.get("targetLabel") not in FOREIGN_TARGETS or record.get("mnemonic") != "CcConfigureMakeRule":
            continue
        target = record["targetLabel"]
        check.require(len(selected) < FOREIGN_INSTANCE_LIMIT,
                      "too many configured foreign spawns")
        arguments = record.get("commandArgs", [])
        check.require(isinstance(arguments, list) and all(isinstance(x, str) for x in arguments),
                      "invalid foreign arguments")
        environment = _private_pairs(record.get("environmentVariables", []))
        platform = record.get("platform", {})
        check.require(isinstance(platform, dict) and set(platform) <= {"properties"}, "invalid foreign platform")
        platform = _private_pairs(platform.get("properties", []))
        inputs = _private_files(record.get("inputs", []))
        outputs = _private_files(record.get("actualOutputs", []))
        listed = record.get("listedOutputs", [])
        check.require(isinstance(listed, list) and all(isinstance(x, str) and x for x in listed) and
                      len(set(listed)) == len(listed), "invalid foreign listed outputs")
        listed = sorted(listed)
        row = {"target": target, "mnemonic": "CcConfigureMakeRule", "runner": record.get("runner"),
               "cache_hit": record.get("cacheHit", False), "cacheable": record.get("cacheable", False),
               "remote_cacheable": record.get("remoteCacheable", False),
               "action_digest": _spawn_digest(record.get("digest")),
               "arguments_sha256": _fingerprint(arguments), "environment_sha256": _fingerprint(environment),
               "platform_sha256": _fingerprint(platform), "listed_outputs_sha256": _fingerprint(listed),
               "inputs_sha256": _fingerprint(inputs), "actual_outputs_sha256": _fingerprint(outputs),
               "input_count": len(inputs), "actual_output_count": len(outputs), "listed_output_count": len(listed),
               "input_digest_unobserved": sum(value[1] is None for value in inputs),
               "output_digest_unobserved": sum(value[1] is None for value in outputs),
               "unrecorded_listed_outputs": sum(not any(value[0] == name or value[0].startswith(name + "/")
                                                        for value in outputs) for name in listed)}
        selected.append(row)
    result = {"schema": 1, "scope": "recorded-foreign-spawns-only",
              "spawn_counts": {key: counts[key] for key in ("action_count", "cache_hit_count", "executed_count")},
              "observations": sorted(selected, key=lambda row: (row["target"], row["listed_outputs_sha256"])),
              "unobserved_targets": sorted(set(FOREIGN_TARGETS) - {row["target"] for row in selected}),
              "payloads": payloads}
    validate_foreign_diagnostics(result)
    return result


def validate_foreign_diagnostics(value):
    """Closed public schema for the fixed diagnostic; no raw spawn records."""
    check.require(isinstance(value, dict) and set(value) == {
        "schema", "scope", "spawn_counts", "observations", "unobserved_targets", "payloads"} and
        type(value["schema"]) is int and value["schema"] == 1 and
        value["scope"] == "recorded-foreign-spawns-only", "invalid foreign diagnostics")
    counts = value["spawn_counts"]
    check.require(isinstance(counts, dict) and set(counts) == {
        "action_count", "cache_hit_count", "executed_count"}, "invalid foreign spawn counts")
    for count in counts.values(): public_artifacts.integer(count)
    check.require(counts["action_count"] == counts["cache_hit_count"] + counts["executed_count"],
                  "inconsistent foreign spawn counts")
    observations = value["observations"]
    check.require(isinstance(observations, list) and len(observations) <= min(FOREIGN_INSTANCE_LIMIT, counts["action_count"]),
                  "invalid foreign observations")
    hash_keys = {name + "_sha256" for name in ("arguments", "environment", "platform", "listed_outputs", "inputs", "actual_outputs")}
    count_keys = {"input_count", "actual_output_count", "listed_output_count", "input_digest_unobserved",
                  "output_digest_unobserved", "unrecorded_listed_outputs"}
    targets = []
    for row in observations:
        check.require(isinstance(row, dict) and set(row) == hash_keys | count_keys | {
            "target", "mnemonic", "runner", "cache_hit", "cacheable", "remote_cacheable", "action_digest"},
            "invalid foreign observation fields")
        check.require(row["target"] in FOREIGN_TARGETS and row["mnemonic"] == "CcConfigureMakeRule",
                      "unexpected foreign action")
        public_artifacts.label(row["target"])
        targets.append(row["target"])
        check.require(isinstance(row["runner"], str) and re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", row["runner"]),
                      "invalid foreign runner")
        for key in ("cache_hit", "cacheable", "remote_cacheable"):
            check.require(type(row[key]) is bool, "invalid foreign cache flag")
        check.require(row["cache_hit"] == (row["runner"] in ("disk cache hit", "remote cache hit")),
                      "inconsistent foreign cache result")
        for key in hash_keys: public_artifacts.digest(row[key])
        for key in count_keys: public_artifacts.integer(row[key])
        check.require(row["input_digest_unobserved"] <= row["input_count"] and
                      row["output_digest_unobserved"] <= row["actual_output_count"] and
                      row["unrecorded_listed_outputs"] <= row["listed_output_count"], "invalid foreign missing counts")
        digest = row["action_digest"]
        if digest is not None:
            check.require(isinstance(digest, dict) and set(digest) == {"hash", "size_bytes", "hash_function"},
                          "invalid public action digest")
            public_artifacts.digest(digest["hash"]); public_artifacts.integer(digest["size_bytes"])
            check.require(digest["hash_function"] in (None, "SHA-256", "SHA256"), "invalid public digest function")
    check.require(value["unobserved_targets"] == sorted(set(FOREIGN_TARGETS) - set(targets)),
                  "invalid unobserved foreign targets")
    check.require(isinstance(value["payloads"], dict) and set(value["payloads"]) == set(FOREIGN_PAYLOADS),
                  "invalid foreign payload identities")
    for digest in value["payloads"].values():
        if digest is not None: public_artifacts.digest(digest)
    public_artifacts.safe_json(value)
    check.require(len((json.dumps(value, indent=2) + "\n").encode()) <= DIAGNOSTIC_LIMIT,
                  "foreign diagnostic exceeds public bound")


def compare_foreign_diagnostics(initial, baseline):
    """Compare recorded fingerprints only; absent spawns are not cache proof."""
    for value in (initial, baseline): validate_foreign_diagnostics(value)
    def groups(value):
        result = {}
        for row in value["observations"]:
            result.setdefault((row["target"], row["listed_outputs_sha256"]), []).append(row)
        return result
    before, after = groups(initial), groups(baseline)
    rows = []
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key, []), after.get(key, [])
        status = ("unobserved-initial" if not left else "unobserved-baseline" if not right else
                  "matched" if len(left) == len(right) == 1 else "ambiguous")
        row = {"target": key[0], "listed_outputs_sha256": key[1], "status": status,
               "initial_instances": len(left), "baseline_instances": len(right),
               "changed_recorded_fields": [], "unobserved_fields": []}
        if status == "matched":
            left, right = left[0], right[0]
            fields = ("arguments_sha256", "environment_sha256", "platform_sha256", "inputs_sha256",
                      "actual_outputs_sha256", "input_count", "actual_output_count", "cacheable",
                      "remote_cacheable", "unrecorded_listed_outputs")
            row["changed_recorded_fields"] = [field for field in fields if left[field] != right[field]]
            if left["action_digest"] is None or right["action_digest"] is None:
                row["unobserved_fields"].append("action_digest")
            elif left["action_digest"] != right["action_digest"]:
                row["changed_recorded_fields"].append("action_digest")
            for field in ("input_digest_unobserved", "output_digest_unobserved"):
                if left[field] or right[field]: row["unobserved_fields"].append(field)
        rows.append(row)
    payloads = {name: ("unobserved" if initial["payloads"][name] is None or baseline["payloads"][name] is None else
                      "same-reported-sha256" if initial["payloads"][name] == baseline["payloads"][name] else "changed")
                for name in FOREIGN_PAYLOADS}
    result = {"instances": rows, "payloads": payloads}
    public_artifacts.safe_json(result)
    return result


def _public_input_path(value):
    """Publish known source/tool-owner paths without normalizing rejected text."""
    owners = (r"(?:p4lang-pi\+\+_repo_rules\+pi_source|p4lang-bmv2\+\+_repo_rules\+bmv2_source|"
              r"sonic-build-infra\+|rules_foreign_cc\+|bazel_tools)")
    try:
        public_artifacts.relative_name(value)
        public_artifacts.safe_text(value)
        check.require(len(value) <= 1024 and str(PurePosixPath(value)) == value, "noncanonical input path")
        check.require(re.fullmatch(r"(?:external/" + owners + r"/|bazel-out/[A-Za-z0-9_.+-]+/bin/external/" +
                                  owners + r"/|tools/bazel/|platform/vs/docker-dash-engine/).+", value),
                      "input path outside public owner scope")
        return value
    except ValueError:
        return None


def _foreign_inputs(execution):
    result = {}
    count = 0
    for record in cache_reuse.json_records(execution):
        check.require(isinstance(record, dict), "invalid foreign input record")
        target = record.get("targetLabel")
        if target not in FOREIGN_TARGETS or record.get("mnemonic") != "CcConfigureMakeRule": continue
        count += 1
        check.require(count <= FOREIGN_INSTANCE_LIMIT, "too many foreign input instances")
        outputs = record.get("listedOutputs", [])
        check.require(isinstance(outputs, list) and all(isinstance(x, str) and x for x in outputs) and
                      len(set(outputs)) == len(outputs), "invalid foreign input instance")
        key = (target, _fingerprint(sorted(outputs)))
        result.setdefault(key, []).append({row[0]: row for row in _private_files(record.get("inputs", []))})
    return result


def foreign_input_differences(initial_log, baseline_log):
    """Bounded changed-input metadata; unknown/private paths remain hashes."""
    initial, baseline = _foreign_inputs(initial_log), _foreign_inputs(baseline_log)
    result = []
    def identity(value):
        if value is None: return None
        return {"identity_sha256": _fingerprint(value), "digest": value[1], "is_tool": value[2],
                "symlink_target_sha256": None if value[3] is None else _fingerprint(value[3])}
    for key in sorted(initial.keys() | baseline.keys()):
        before, after = initial.get(key, []), baseline.get(key, [])
        status = ("unobserved-initial" if not before else "unobserved-baseline" if not after else
                  "matched" if len(before) == len(after) == 1 else "ambiguous")
        row = {"target": key[0], "listed_outputs_sha256": key[1], "status": status,
               "changed_input_count": None, "truncated": False, "changes": []}
        if status == "matched":
            before, after = before[0], after[0]
            changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
            row["changed_input_count"] = len(changed)
            row["truncated"] = len(changed) > 64
            row["changes"] = [{"path_sha256": _fingerprint(path), "public_path": _public_input_path(path),
                               "initial": identity(before.get(path)), "baseline": identity(after.get(path))}
                              for path in changed[:64]]
        result.append(row)
    validate_foreign_input_differences(result)
    return result


def validate_foreign_input_differences(value):
    check.require(isinstance(value, list) and len(value) <= 2 * FOREIGN_INSTANCE_LIMIT, "invalid foreign input differences")
    for row in value:
        check.require(isinstance(row, dict) and set(row) == {
            "target", "listed_outputs_sha256", "status", "changed_input_count", "truncated", "changes"} and
            row["target"] in FOREIGN_TARGETS, "invalid foreign input difference fields")
        public_artifacts.digest(row["listed_outputs_sha256"])
        check.require(row["status"] in ("matched", "unobserved-initial", "unobserved-baseline", "ambiguous") and
                      type(row["truncated"]) is bool and isinstance(row["changes"], list), "invalid foreign input state")
        if row["status"] == "matched":
            count = public_artifacts.integer(row["changed_input_count"])
            check.require(len(row["changes"]) == min(count, 64) and row["truncated"] == (count > 64),
                          "inconsistent foreign input truncation")
        else:
            check.require(row["changed_input_count"] is None and not row["changes"] and not row["truncated"],
                          "unobserved foreign input comparison")
        for change in row["changes"]:
            check.require(isinstance(change, dict) and set(change) == {
                "path_sha256", "public_path", "initial", "baseline"}, "invalid foreign input change")
            public_artifacts.digest(change["path_sha256"])
            if change["public_path"] is not None:
                check.require(_public_input_path(change["public_path"]) == change["public_path"] and
                              _fingerprint(change["public_path"]) == change["path_sha256"], "unsafe public input path")
            check.require(change["initial"] is not None or change["baseline"] is not None, "empty input change")
            for state in (change["initial"], change["baseline"]):
                if state is None: continue
                check.require(isinstance(state, dict) and set(state) == {
                    "identity_sha256", "digest", "is_tool", "symlink_target_sha256"} and type(state["is_tool"]) is bool,
                    "invalid input digest state")
                public_artifacts.digest(state["identity_sha256"])
                if state["symlink_target_sha256"] is not None: public_artifacts.digest(state["symlink_target_sha256"])
                digest = state["digest"]
                if digest is not None:
                    check.require(isinstance(digest, dict) and set(digest) == {"hash", "size_bytes", "hash_function"},
                                  "invalid input file digest")
                    public_artifacts.digest(digest["hash"]); public_artifacts.integer(digest["size_bytes"])
                    check.require(digest["hash_function"] in (None, "SHA-256", "SHA256"), "invalid input digest function")
    public_artifacts.safe_json(value)
    check.require(len((json.dumps(value, indent=2) + "\n").encode()) <= DIAGNOSTIC_LIMIT, "foreign input differences exceed bound")


def foreign_explain(path):
    """Recognize only two fixed progress descriptions and Bazel reason strings.

    Descriptions do not identify a configured instance. The first cache-miss
    reason can mask later differences; absence is not proof of a cache hit.
    The private log includes build options and is never a public artifact.
    """
    result = {"scope": "fixed-progress-descriptions-unbound-configurations", "status": "unobserved",
              "matched_lines": 0, "unrecognized_lines": 0, "reasons": []}
    if path.is_file():
        with path.open("rb") as stream: raw = stream.read(EXPLAIN_LIMIT + 1)
        partial = len(raw) > EXPLAIN_LIMIT
        raw = raw[:EXPLAIN_LIMIT]
        if partial: raw = raw.rsplit(b"\n", 1)[0]
        counts = {}
        for line in raw.splitlines():
            for name in ("sdk", "bmv2_build"):
                prefix = ("Executing Foreign Cc - Configure: Building " + name + ": ").encode()
                if not line.startswith(prefix): continue
                reason = EXPLAIN_REASONS.get(line[len(prefix):].removesuffix(b".").decode("utf-8", errors="replace"))
                result["matched_lines"] += 1
                if reason is None: result["unrecognized_lines"] += 1
                else: counts[(name, reason)] = counts.get((name, reason), 0) + 1
        result["status"] = "partial" if partial else "complete"
        result["reasons"] = [{"description": name, "reason": reason, "count": count}
                             for (name, reason), count in sorted(counts.items())]
    validate_foreign_explain(result)
    return result


def validate_foreign_explain(value):
    check.require(isinstance(value, dict) and set(value) == {
        "scope", "status", "matched_lines", "unrecognized_lines", "reasons"} and
        value["scope"] == "fixed-progress-descriptions-unbound-configurations" and
        value["status"] in ("unobserved", "complete", "partial"), "invalid foreign explain summary")
    for key in ("matched_lines", "unrecognized_lines"): public_artifacts.integer(value[key])
    check.require(isinstance(value["reasons"], list) and len(value["reasons"]) <= 2 * len(EXPLAIN_REASONS),
                  "invalid foreign explain rows")
    keys = set()
    total = value["unrecognized_lines"]
    for row in value["reasons"]:
        check.require(isinstance(row, dict) and set(row) == {"description", "reason", "count"} and
                      row["description"] in ("sdk", "bmv2_build") and row["reason"] in EXPLAIN_REASONS.values(),
                      "invalid foreign explain reason")
        public_artifacts.integer(row["count"])
        check.require(row["count"] > 0, "empty foreign explain reason")
        key = (row["description"], row["reason"])
        check.require(key not in keys, "duplicate foreign explain reason"); keys.add(key)
        total += row["count"]
    check.require(total == value["matched_lines"] and (value["status"] != "unobserved" or total == 0),
                  "inconsistent foreign explain count")
    public_artifacts.safe_json(value)


ARCHIVE_MEMBER_LIMIT = 4096
ARCHIVE_BYTE_LIMIT = 256 * 1024 * 1024
ARCHIVE_COMPARISON_SECONDS = 120
ARCHIVE_CHANGE_LIMIT = 64
ARCHIVE_COMPARISON_SCOPE = "parsed TAR member bytes/metadata/order; raw header encoding and padding excluded"
ARCHIVE_COMPARISON_REASONS = frozenset(("archive-byte-limit", "member-limit", "deadline", "invalid-archive"))


class _ArchiveComparisonLimit(ValueError):
    pass


def _archive_deadline(deadline):
    if time.monotonic() >= deadline:
        raise _ArchiveComparisonLimit("deadline")


class _ArchiveComparisonStream:
    """Bound each private TAR/file stream while reusing the existing hash reader."""
    def __init__(self, stream, deadline):
        self.stream, self.deadline, self.bytes = stream, deadline, 0

    def read(self, size=-1):
        _archive_deadline(self.deadline)
        remaining = ARCHIVE_BYTE_LIMIT - self.bytes
        data = self.stream.read(min(size, remaining + 1) if size >= 0 else remaining + 1)
        self.bytes += len(data)
        _archive_deadline(self.deadline)
        if self.bytes > ARCHIVE_BYTE_LIMIT:
            raise _ArchiveComparisonLimit("archive-byte-limit")
        return data


def _archive_members(path, deadline):
    _archive_deadline(deadline)
    if not path.is_file() or path.stat().st_size > ARCHIVE_BYTE_LIMIT:
        raise _ArchiveComparisonLimit("archive-byte-limit")
    rows, headers, order, decoded = {}, {}, [], 0
    with path.open("rb") as raw:
        # These two declared outputs are uncompressed TARs. Do not decode another
        # archive layer or extract files, follow links, or run any payload.
        with tarfile.open(fileobj=_ArchiveComparisonStream(raw, deadline), mode="r|") as archive:
            for member in archive:
                _archive_deadline(deadline)
                if len(order) >= ARCHIVE_MEMBER_LIMIT:
                    raise _ArchiveComparisonLimit("member-limit")
                name = check.path_name(member.name)
                metadata = check.metadata(member)
                check.require(metadata["kind"] != "other" and not member.issparse(), "unsupported comparison member")
                check.require(member.size >= 0 and (member.uid, member.gid) == (0, 0), "invalid comparison metadata")
                key = _fingerprint(name)
                order.append(key)
                # Raw paths, names, link targets and PAX values stay private.
                headers.setdefault(key, []).append(_fingerprint([
                    member.name, member.type.hex(), member.size, member.mode,
                    member.uid, member.gid, member.uname, member.gname,
                    member.mtime, member.linkname, member.devmajor, member.devminor,
                    member.pax_headers,
                ]))
                row = {"kind": metadata["kind"], "size": 0, "content_sha256": None,
                       "metadata_sha256": _fingerprint(metadata)}
                if member.isfile():
                    decoded += member.size
                    if decoded > ARCHIVE_BYTE_LIMIT:
                        raise _ArchiveComparisonLimit("archive-byte-limit")
                    with archive.extractfile(member) as stream:
                        contents = check.file_metadata(_ArchiveComparisonStream(stream, deadline), name)
                    check.require(contents["size"] == member.size, "truncated comparison member")
                    row.update(size=contents["size"], content_sha256=contents["sha256"])
                if key in rows:
                    check.require(row["kind"] == "directory" and rows[key] == row, "duplicate comparison member")
                rows[key] = row
    _archive_deadline(deadline)
    for key, row in rows.items():
        row["header_sha256"] = _fingerprint(headers[key])
    summary = {"member_count": len(order), "regular_file_bytes": decoded,
               "content_sha256": _fingerprint([[key, row["kind"], row["size"], row["content_sha256"]] for key, row in sorted(rows.items())]),
               "metadata_sha256": _fingerprint([[key, row["metadata_sha256"]] for key, row in sorted(rows.items())]),
               "header_sha256": _fingerprint([[key, row["header_sha256"]] for key, row in sorted(rows.items())]),
               "order_sha256": _fingerprint(order)}
    return summary, rows


def compare_foreign_archives(directory, live_paths):
    """Compare only the two initial staged TARs with their existing live outputs."""
    check.require(set(live_paths) == set(FOREIGN_PAYLOADS), "unexpected comparison archive scope")
    result = {"schema": 1, "scope": ARCHIVE_COMPARISON_SCOPE, "status": "complete", "archives": {}}
    deadline = time.monotonic() + ARCHIVE_COMPARISON_SECONDS
    try:
        for name in FOREIGN_PAYLOADS:
            initial, old = _archive_members(directory / name, deadline)
            baseline, new = _archive_members(live_paths[name], deadline)
            changed = [key for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)]
            result["archives"][name] = {
                "initial": initial, "baseline": baseline,
                "same_file_contents": initial["content_sha256"] == baseline["content_sha256"],
                "same_installation_metadata": initial["metadata_sha256"] == baseline["metadata_sha256"],
                "same_header_metadata": initial["header_sha256"] == baseline["header_sha256"],
                "same_order": initial["order_sha256"] == baseline["order_sha256"],
                "changed_member_count": len(changed), "truncated": len(changed) > ARCHIVE_CHANGE_LIMIT,
                "changes": [{"path_sha256": key, "initial": old.get(key), "baseline": new.get(key)}
                            for key in changed[:ARCHIVE_CHANGE_LIMIT]],
            }
        _archive_deadline(deadline)
    except (ValueError, OSError, tarfile.TarError) as error:
        reason = str(error) if isinstance(error, _ArchiveComparisonLimit) else "invalid-archive"
        result.update(status="unavailable", reason=reason)
    validate_foreign_archive_comparison(result)
    return result


def validate_foreign_archive_comparison(value):
    check.require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1 and
                  value.get("scope") == ARCHIVE_COMPARISON_SCOPE and value.get("status") in ("complete", "unavailable"), "invalid archive comparison")
    complete = value["status"] == "complete"
    check.require(set(value) == ({"schema", "scope", "status", "archives"} if complete else {"schema", "scope", "status", "archives", "reason"}),
                  "invalid archive comparison fields")
    check.require(complete or value["reason"] in ARCHIVE_COMPARISON_REASONS, "invalid archive comparison reason")
    archives = value["archives"]
    check.require(isinstance(archives, dict) and set(archives) <= set(FOREIGN_PAYLOADS) and
                  (not complete or set(archives) == set(FOREIGN_PAYLOADS)), "invalid archive comparison scope")
    summary_hashes = {name + "_sha256" for name in ("content", "metadata", "header", "order")}
    same_keys = {"same_file_contents", "same_installation_metadata", "same_header_metadata", "same_order"}
    for row in archives.values():
        check.require(isinstance(row, dict) and set(row) == same_keys | {
            "initial", "baseline", "changed_member_count", "truncated", "changes"}, "invalid archive difference")
        for phase in ("initial", "baseline"):
            summary = row[phase]
            check.require(isinstance(summary, dict) and set(summary) == summary_hashes | {"member_count", "regular_file_bytes"},
                          "invalid archive summary")
            for key, limit in (("member_count", ARCHIVE_MEMBER_LIMIT), ("regular_file_bytes", ARCHIVE_BYTE_LIMIT)):
                check.require(type(summary[key]) is int and 0 <= summary[key] <= limit, "archive summary exceeds limit")
            for key in summary_hashes: public_artifacts.digest(summary[key])
        for key, digest in (("same_file_contents", "content_sha256"), ("same_installation_metadata", "metadata_sha256"),
                            ("same_header_metadata", "header_sha256"), ("same_order", "order_sha256")):
            check.require(type(row[key]) is bool and row[key] == (row["initial"][digest] == row["baseline"][digest]),
                          "archive comparison flag differs")
        count, changes = row["changed_member_count"], row["changes"]
        check.require(type(count) is int and 0 <= count <= 2 * ARCHIVE_MEMBER_LIMIT and
                      type(row["truncated"]) is bool and row["truncated"] == (count > ARCHIVE_CHANGE_LIMIT) and
                      isinstance(changes, list) and len(changes) == min(count, ARCHIVE_CHANGE_LIMIT), "invalid archive change count")
        paths = []
        for change in changes:
            check.require(isinstance(change, dict) and set(change) == {"path_sha256", "initial", "baseline"}, "invalid archive change")
            public_artifacts.digest(change["path_sha256"]); paths.append(change["path_sha256"])
            check.require(change["initial"] != change["baseline"], "unchanged archive member")
            for phase in ("initial", "baseline"):
                state = change[phase]
                if state is None: continue
                check.require(isinstance(state, dict) and set(state) == {
                    "kind", "size", "content_sha256", "metadata_sha256", "header_sha256"} and
                    state["kind"] in ("file", "directory", "symlink", "hardlink") and
                    type(state["size"]) is int and 0 <= state["size"] <= ARCHIVE_BYTE_LIMIT, "invalid archive member state")
                public_artifacts.digest(state["metadata_sha256"]); public_artifacts.digest(state["header_sha256"])
                if state["kind"] == "file": public_artifacts.digest(state["content_sha256"])
                else: check.require(state["size"] == 0 and state["content_sha256"] is None, "invalid nonfile archive state")
        check.require(paths == sorted(set(paths)), "duplicate or unordered archive changes")
    check.require(len((json.dumps(value, indent=2) + "\n").encode()) <= DIAGNOSTIC_LIMIT, "archive comparison exceeds public bound")


def root_options():
    cache = os.environ.get("SONIC_CONTAINER_BAZEL_CACHE_DIR", os.environ.get("SONIC_SWSS_BAZEL_CACHE_DIR", ""))
    extra = os.environ.get("SONIC_CONTAINER_BAZEL_OPTIONS", os.environ.get("SONIC_SWSS_BAZEL_OPTIONS", ""))
    return OPTIONS[:-2] + cache_options(cache) + shlex.split(extra) + OPTIONS[-2:]


def prepare_manifests(execute, directory, receipt, version):
    # The legacy recipe has a manifest but no image.env producer.
    execute(["make", "-f", "tools/bazel/prepare_manifests.mk",
             "MANIFEST_METADATA=rules/docker-base-trixie.mk platform/vs/docker-dash-engine.mk",
             "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "BLDENV=trixie", "CONFIGURED_PLATFORM=vs",
             "CONFIGURED_ARCH=amd64", "INCLUDE_FIPS=y", "INCLUDE_VS_DASH_SAI=y", "ENABLE_ASAN=n",
             "DOCKERS_PATH=dockers", "PLATFORM_PATH=platform/vs", "SONIC_IMAGE_VERSION=" + version, "manifests"],
            directory, receipt, "manifests")



def clean_source(directory):
    check.require(not ROOT.is_relative_to(directory), "artifact directory cannot contain the source checkout")
    if directory.is_relative_to(ROOT):
        relative = directory.relative_to(ROOT)
        check.require(len(relative.parts) > 1 and relative.parts[0] == "artifacts",
                      "in-checkout evidence must use artifacts/<name>")
    subprocess.run(["git", "diff", "--exit-code", "--ignore-submodules=none", "HEAD"], cwd=ROOT, check=True)
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    # Earlier archive/config-engine CI steps retain their evidence here too.
    # The reserved artifacts tree supplies no source inputs; tracked files and
    # submodules are still covered by the complete HEAD diff above.
    unexpected = [name for name in untracked if name and
                  not (len(Path(name).parts) > 1 and Path(name).parts[0] == "artifacts")]
    check.require(not unexpected, "untracked source inputs: " + repr(unexpected))


def source_identity():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    headers = subprocess.check_output(["git", "cat-file", "-p", "HEAD"], cwd=ROOT, text=True).split("\n\n", 1)[0]
    # The default Actions checkout is shallow: revision traversal hides its
    # parent list, but the immutable commit object retains the actual parents.
    parents = [line.split()[1] for line in headers.splitlines() if line.startswith("parent ")]
    expected_head, expected_base = os.environ.get("CI_HEAD_SHA"), os.environ.get("CI_BASE_SHA")
    if expected_head:
        check.require(expected_head in [revision, *parents], "tested source does not contain the requested PR head")
    if expected_base:
        check.require(expected_base in parents, "tested merge does not contain the requested base")
    return {"revision": revision, "tested_revision_parents": parents}


def build_images(args, directory):
    check.require(platform.machine() == "x86_64" and
                  platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie",
                  "DASH engine image acceptance requires native AMD64/Trixie")
    receipt = {"status": "building", "architecture": "amd64", "image_version": args.image_version, "commands": [],
               "invocation": {"bazel": args.bazel,
                              "review_registry_revision": os.environ.get("SONIC_BAZEL_REGISTRY_REVISION")}}
    execute = partial(command_log.execute, cwd=ROOT)
    try:
        clean_source(directory)
        receipt.update(source_identity())
        prepare_manifests(execute, directory, receipt, args.image_version)
        layouts = [BASE, *[OWNER + name for name in IMAGES]]
        receipt["action_audit"] = bazel_commands.audit_targets(args.bazel, root_options(), [*TARGETS.values(), *layouts],
            workspace=ROOT, output=directory / "action-audit.json")
        # Materialize layouts and archives together. A later layout build can
        # rebuild source packages after their archives have already been copied.
        execution = directory / "build.execution.json"
        build.collect_archives(ROOT, directory, receipt, TARGETS, bazel=args.bazel,
                               options=root_options(), additional_targets=layouts, execution_log=execution)
        counts = cache_reuse.collect(execution, directory / "build-events.jsonl", TARGETS[IMAGES[0] + ".gz"],
                                     allow_empty=True)
        receipt["foreign_diagnostics"] = foreign_diagnostics(
            execution, counts, {name: receipt["artifacts"][name]["sha256"] for name in FOREIGN_PAYLOADS})
        receipt["layouts"] = {name: execute([args.bazel, "cquery", *root_options(), "--output=files", OWNER + name],
            directory, receipt, name + ".layout").strip() for name in IMAGES}
        receipt["base_layout"] = execute([args.bazel, "cquery", *root_options(), "--output=files", BASE],
            directory, receipt, "base.layout").strip()
        receipt["resolution"] = resolution.collect(ROOT, directory, bazel=[args.bazel])
        clean_source(directory)
        check.require(source_identity()["revision"] == receipt["revision"], "build checkout changed")
        receipt["status"] = "built"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        (directory / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def validate_images(directory):
    from tools.bazel.ci.dash_engine_validate import validate
    return validate(directory)


def incremental_actions(execution, events, log, *, no_op=False, evidence=None):
    """Distinguish proven cache materialization from actual image/source work."""
    result = cache_reuse.collect(execution, events, OWNER + IMAGES[0] + ".gz", allow_empty=True,
                                 include_action_summary=no_op)
    result["phase"] = "incremental"
    if evidence is not None:
        evidence.update(result)
        result = evidence
    executed = {}
    for action in result["actions"]:
        if not action["cache_hit"]:
            mnemonic = action["mnemonic"]
            executed[mnemonic] = executed.get(mnemonic, 0) + action["count"]
    check.require(not any(key.startswith(("Cpp", "Rustc", "GoCompile", "GoLink")) for key in executed),
                  "image-only probe unexpectedly compiled source owners: " + repr(executed))
    if no_op:
        check.require(result["executed_count"] == 0, "no-op build executed actions: " + repr(executed))
        # Compare this build's process summary with its validated spawn records.
        # Internal Bazel bookkeeping is absent from the spawn log; every cache
        # hit must agree by runner and count. Persistent action-cache hits are
        # reported separately from processes by Bazel's SpawnStats; bind them
        # to this invocation's BEP statistics instead of counting them as spawns.
        matches = re.findall(r"^INFO: ([0-9]+) process(?:es)?(?:: (.*))?\.$", log.read_text(), re.M)
        check.require(len(matches) == 1, "missing or ambiguous no-op process summary")
        total, details = matches[0]
        counts = {}
        result["process_summary"] = {"total": int(total), "runners": counts}
        for item in details.split(", ") if details else []:
            match = re.fullmatch(r"([0-9]+) (internal|action cache hit|disk cache hit|remote cache hit)", item)
            check.require(match is not None, "no-op process summary contains actual or unknown work")
            count, runner = match.groups()
            check.require(runner not in counts, "duplicate no-op process runner")
            counts[runner] = int(count)
        processes = {name: count for name, count in counts.items() if name != "action cache hit"}
        check.require(sum(processes.values()) == int(total), "no-op process count differs")
        metrics = result["build_action_summary"]
        runners = metrics["runners"]
        check.require(set(runners) <= {"total", "internal", "disk cache hit", "remote cache hit"},
                      "no-op BEP contains actual or unknown work")
        check.require(int(total) == metrics["actions_executed"] == runners["total"] and
                      processes == {name: count for name, count in runners.items() if name != "total"},
                      "no-op process and BEP runner evidence differ")
        # SpawnStats deliberately prints only '0 processes.' for a zero total,
        # even when the persistent action-cache counter is nonzero.
        check.require((int(total) > 0 and counts.get("action cache hit", 0) == metrics["action_cache_hits"]) or
                      (int(total) == 0 and not counts), "no-op process and BEP action-cache evidence differ")
        for runner in ("disk cache hit", "remote cache hit"):
            expected = sum(action["count"] for action in result["actions"] if action["runner"] == runner)
            check.require(counts.get(runner, 0) == expected, "no-op process and cache evidence differ")
    return executed, result


def incremental(args, directory):
    clean_source(directory)
    identity = source_identity()
    baseline = json.loads((directory / "build-receipt.json").read_text())
    check.require(baseline["status"] == "built" and identity["revision"] == baseline["revision"],
                  "incremental checkout differs from built source")
    root = build.execution_root(directory / "build-events.jsonl")
    def output(label):
        paths = subprocess.check_output([args.bazel, "cquery", *root_options(), "--output=files", label], cwd=ROOT, text=True).splitlines()
        check.require(len(paths) == 1, "expected one declared output: " + label)
        return root / paths[0]
    archives = {name: output(OWNER + name + ".gz") for name in IMAGES}
    owners = {name: output(TARGETS[name]) for name in ("engine.source.tar", "base.source.tar")}
    diagnostic_owners = {name: output(TARGETS[name]) for name in FOREIGN_PAYLOADS}
    base = output(BASE)
    layouts = {name: root / path for name, path in baseline["layouts"].items()}
    targets = [OWNER + name + suffix for name in IMAGES for suffix in ("", ".gz")]
    targets += [BASE, *[TARGETS[name] for name in owners]]
    def snapshot():
        return {"images": {name: oci_layout.validate_layout(path, "linux/amd64").descriptor["digest"] for name, path in layouts.items()},
                "archives": {name: check.sha(path) for name, path in archives.items()},
                "owners": {name: check.sha(path) for name, path in owners.items()},
                "base": oci_layout.validate_layout(base, "linux/amd64").descriptor["digest"]}
    timings, actions, cache_evidence = {}, {}, {}
    def timed_build(name):
        started = time.monotonic()
        execution = directory / ("incremental-" + name + ".execution.json")
        log_path = directory / ("incremental-" + name + ".log")
        events = directory / ("incremental-" + name + ".events.jsonl")
        explain = ["--explain=" + str(directory / "incremental-baseline.explain.log")] if name == "baseline" else []
        with log_path.open("w") as log:
            result = subprocess.run([args.bazel, "build", *root_options(),
                "--execution_log_json_file=" + str(execution),
                "--build_event_json_file=" + str(events),
                *explain, *targets], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        timings[name] = round(time.monotonic() - started, 3)
        result.check_returncode()
        cache_evidence[name] = {}
        actions[name], _ = incremental_actions(execution, events, log_path, no_op=name == "no-op",
                                               evidence=cache_evidence[name])
    evidence = {"status": "running", "timings_seconds": timings, "executed_actions": actions,
                "spawn_cache_evidence": cache_evidence,
                "foreign_diagnostics": {"initial": baseline["foreign_diagnostics"]}}
    try:
        validate_foreign_diagnostics(evidence["foreign_diagnostics"]["initial"])
        try:
            evidence["foreign_payloads_before_baseline"] = {
                name: check.sha(path) if path.is_file() else None for name, path in diagnostic_owners.items()}
        except Exception:
            evidence["foreign_diagnostics_error"] = "foreign-diagnostics-unavailable"
        timed_build("baseline")
        original = snapshot()
        evidence["baseline"] = original
        try:
            evidence["foreign_diagnostics"]["baseline"] = foreign_diagnostics(
                directory / "incremental-baseline.execution.json", cache_evidence["baseline"],
                {name: check.sha(path) if path.is_file() else None for name, path in diagnostic_owners.items()})
            evidence["foreign_comparison"] = compare_foreign_diagnostics(
                evidence["foreign_diagnostics"]["initial"], evidence["foreign_diagnostics"]["baseline"])
            evidence["foreign_input_differences"] = foreign_input_differences(
                directory / "build.execution.json", directory / "incremental-baseline.execution.json")
            evidence["foreign_explain"] = foreign_explain(directory / "incremental-baseline.explain.log")
            evidence["foreign_archive_comparison"] = compare_foreign_archives(directory, diagnostic_owners)
            check.require(evidence["foreign_archive_comparison"]["status"] == "complete", "foreign archive comparison unavailable")
        except Exception:
            # Preserve the original byte-drift error if both checks fail. A
            # diagnostic failure alone remains fatal before the edit probes.
            evidence["foreign_diagnostics_error"] = "foreign-diagnostics-unavailable"
        check.require(all(original["archives"][name] == baseline["artifacts"][name + ".gz"]["sha256"] for name in IMAGES),
                      "incremental baseline differs from verified archives")
        check.require("foreign_diagnostics_error" not in evidence, "foreign diagnostics unavailable")
        timed_build("no-op")
        unchanged = snapshot()
        evidence["unchanged"] = unchanged
        check.require(unchanged == original, "no-op build changed installed source/image bytes")
    except Exception as error:
        evidence.update(status="failed", error=str(error))
        (directory / "incremental.json").write_text(json.dumps(evidence, indent=2) + "\n")
        raise
    source = ROOT / "platform/vs/docker-dash-engine/start.sh"
    saved, mode = source.read_bytes(), source.stat().st_mode
    evidence["input"] = str(source.relative_to(ROOT))
    try:
        source.write_bytes(saved + b"\n# Bazel incremental source input probe\n")
        timed_build("launcher-source-edit")
        changed = snapshot()
        evidence["changed"] = changed
        check.require(all(changed[field][name] != original[field][name] for field in ("images", "archives") for name in IMAGES),
                      "startup edit did not invalidate both images and archives")
        check.require(changed["owners"] == original["owners"] and changed["base"] == original["base"],
                      "startup edit changed an unrelated source owner or base")
        evidence["status"] = "passed"
    except Exception as error:
        evidence.update(status="failed", error=str(error))
        raise
    finally:
        source.write_bytes(saved)
        source.chmod(mode)
        try:
            timed_build("restored")
            restored = snapshot()
            evidence["restored"] = restored
            check.require(restored == original, "restoring startup did not reproduce exact archive bytes")
            clean_source(directory)
            check.require(source_identity() == identity, "incremental checkout changed")
        except Exception as error:
            evidence.update(status="failed", restoration_error=str(error))
            raise
        finally:
            (directory / "incremental.json").write_text(json.dumps(evidence, indent=2) + "\n")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "validate", "incremental"))
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--image-version", default="bazel-dash-engine-ci")
    parser.add_argument("--bazel", default="tools/bazel/ci/review_bazel.sh")
    args = parser.parse_args()
    directory = args.artifacts.resolve(); directory.mkdir(parents=True, exist_ok=True)
    if args.phase == "build": build_images(args, directory)
    elif args.phase == "validate": validate_images(directory)
    else: incremental(args, directory)


if __name__ == "__main__":
    main()
