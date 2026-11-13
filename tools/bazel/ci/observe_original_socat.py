#!/usr/bin/env python3
"""Compare three pinned original Socat ELF files without executing them."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import tarfile
import tempfile
import traceback

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import observe_vs_host_binaries as host

original = host.original
require = original.require
MANIFEST_SHA256 = "30696e01cb3b9a1c05bc94cbe42a425d3e2d566322f413feb12762037a6290ba"
MIB = 1024 ** 2
MAX_BINARY = 2 * MIB
MAX_TAR = 16 * MIB
MAX_REPORT = MIB
MAX_SPANS = 64
BINARY_NAME = "usr/bin/socat1"


def load_manifest(path):
    data = path.read_bytes()
    require(len(data) <= 64 * 1024 and hashlib.sha256(data).hexdigest() == MANIFEST_SHA256,
            "Fixed Socat manifest differs")
    value = json.loads(data)
    require(value["schema"] == 1 and len(value["inputs"]) == 3, "Fixed Socat scope differs")
    require(len({r["original_input"]["member_sha256"] for r in value["inputs"]}) == 3,
            "Repeated original Socat package")
    for row in value["inputs"]:
        require(row["original_input"]["required_name"] == "socat_1.8.0.2-1_amd64.deb"
                and row["binary"]["path"] == BINARY_NAME
                and 0 < row["binary"]["bytes"] <= MAX_BINARY, "Unreviewed Socat input")
    return value


def section_metadata(data, binary):
    require(len(data) <= MIB, "ELF section report exceeds bound")
    rows, seen = [], set()
    pattern = re.compile(r"^\s*\[\s*([0-9]+)\]\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+", re.M)
    for match in pattern.finditer(data.decode("ascii", "strict")):
        number, name, kind, address, offset, size = match.groups()
        if int(number) == 0:
            continue
        offset, size = int(offset, 16), int(size, 16)
        require(name not in seen and len(name) <= 128 and len(rows) < 256,
                "Invalid or repeated ELF section")
        seen.add(name)
        require(kind == "NOBITS" or offset + size <= len(binary), "ELF section exceeds original")
        rows.append({"name": name, "type": kind, "offset": offset, "bytes": size,
                     "sha256": None if kind == "NOBITS" else hashlib.sha256(binary[offset:offset+size]).hexdigest()})
    require(".text" in seen, "Upstream ELF section report lacks text")
    return rows


def read_binary(path, row, scratch, expected):
    binary, sections = None, None
    with original.HeldFile(path) as held, os.fdopen(os.dup(held.fd), "rb") as source:
        with host.run_upstream("/usr/bin/dpkg-deb", ["--fsys-tarfile"], source, scratch, MAX_TAR) as stream:
            bounded = original.Meter(stream, MAX_TAR)
            seen = set()
            with tarfile.open(fileobj=bounded, mode="r|", tarinfo=original.BoundedTarInfo) as archive:
                for item in archive:
                    name = original.tar_record(item, seen)["normalized_name"]
                    require(item.isfile() or item.isdir() or item.issym() or item.islnk(),
                            "Unexpected Socat package member")
                    require(0 <= item.size <= MAX_TAR, "Socat package member exceeds bound")
                    if name == BINARY_NAME:
                        require(item.isfile() and item.size == expected["bytes"], "Original Socat ELF size differs")
                        with archive.extractfile(item) as member, tempfile.TemporaryFile(dir=scratch) as content:
                            facts = original.copy_member(member, content, item.size, MAX_BINARY)
                            require(facts["sha256"] == expected["sha256"], "Original Socat ELF differs")
                            content.seek(0); binary = content.read(MAX_BINARY + 1)
                            require(binary.startswith(b"\x7fELF") and len(binary) == item.size,
                                    "Fixed Socat input is not the original ELF")
                            content.seek(0)
                            with host.run_upstream("/usr/bin/readelf", ["--wide", "--section-headers"], content, scratch, MIB) as listing:
                                sections = section_metadata(listing.read(MIB + 1), binary)
                require(getattr(archive, "_recovery_zero_end", False), "Socat TAR end marker missing")
                original.zero_tail(archive.fileobj)
        held.verify()
    require(binary is not None and sections is not None, "Original Socat ELF missing")
    return binary, sections


def differences(left, right, sections):
    require(max(len(left), len(right)) <= MAX_BINARY, "Binary comparison exceeds bound")
    spans, count, ranges, index = [], 0, 0, 0
    limit = max(len(left), len(right))
    while index < limit:
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a == b:
            index += 1
            continue
        start = index
        while index < limit and (left[index] if index < len(left) else None) != (right[index] if index < len(right) else None):
            index += 1
        count += index - start; ranges += 1
        if len(spans) < MAX_SPANS:
            begin, end = max(0, start - 32), min(limit, index + 32, start + 96)
            names = sorted({r["name"] for r in sections if r["type"] != "NOBITS"
                            and start < r["offset"] + r["bytes"] and index > r["offset"]})
            spans.append({"offset": start, "bytes": index - start, "sections": names,
                          "context_offset": begin,
                          "left_hex": left[begin:end].hex(), "right_hex": right[begin:end].hex(),
                          "left_ascii": left[begin:end].decode("ascii", "backslashreplace"),
                          "right_ascii": right[begin:end].decode("ascii", "backslashreplace")})
    return {"different_bytes": count, "ranges": ranges, "reported_ranges": spans,
            "ranges_truncated": ranges > MAX_SPANS, "left_bytes": len(left), "right_bytes": len(right)}


def compare(records):
    require(len(records) == 3, "Three original Socat observations required")
    section_names = sorted({s["name"] for r in records for s in r["sections"]})
    section_rows = []
    for name in section_names:
        values = [next((s for s in r["sections"] if s["name"] == name), None) for r in records]
        section_rows.append({"name": name, "identical": all(v == values[0] for v in values), "values": values})
    return {"sections": section_rows, "pairs": [
        {"left_sha256": records[0]["binary_sha256"], "right_sha256": r["binary_sha256"],
         **differences(records[0]["body"], r["body"], records[0]["sections"])} for r in records[1:]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    def deadline(*_):
        raise TimeoutError("Fixed Socat observation deadline")
    previous = {n: signal.signal(n, deadline) for n in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
    signal.alarm(600)
    report = {"status": "SOCAT_ORIGINAL_COMPARISON_INCOMPLETE", "binary_executed": False,
              "package_generation": False, "package_installation": False, "source_admission": False,
              "records": []}
    active = "manifest"
    try:
        value = load_manifest(args.manifest)
        records = []
        for item in value["inputs"]:
            row, expected = item["original_input"], item["binary"]
            active = row["cache_name"]
            def observer(path, observed_row, scratch):
                body, sections = read_binary(path, observed_row, scratch, expected)
                record = {"name": observed_row["required_name"],
                          "package_sha256": observed_row["member_sha256"],
                          "binary_sha256": expected["sha256"], "binary_bytes": len(body),
                          "sections": sections}
                records.append({**record, "body": body})
                report["records"].append(record)
                return record
            host.inspect_cached(args.artifact / "execution" / row["cache_name"], [row], args.scratch, observer=observer)
        report["comparison"] = compare(records)
        report["status"] = "THREE_ORIGINAL_SOCAT_BINARIES_COMPARED_NOT_ADMITTED"
        code = 0
    except (OSError, ValueError, EOFError, tarfile.TarError, KeyError, TypeError) as error:
        site = traceback.extract_tb(error.__traceback__)[-1]
        report["failure"] = {"fixed_input": active, "error_type": type(error).__name__,
                             "site": {"file": Path(site.filename).name, "line": site.lineno, "function": site.name}}
        if isinstance(error, original.RecoveryError):
            report["failure"]["reason"] = str(error)[:256]
        code = 1
    finally:
        signal.alarm(0)
        for number, handler in previous.items():
            signal.signal(number, handler)
    encoded = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    require(len(encoded) <= MAX_REPORT, "Socat report exceeds bound")
    with args.report.open("xb") as stream:
        stream.write(encoded)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
