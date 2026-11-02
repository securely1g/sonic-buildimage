#!/usr/bin/env python3
"""Diagnostic public OBS downloads; no extraction, descriptor repair, or build."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit


BASE = "https://downloadcontent.opensuse.org/repositories/home:/p4lang/Debian_11/"
PACKAGES = {"p4lang-pi": "0.1.3-2", "p4lang-bmv2": "1.15.0-9", "p4lang-p4c": "1.2.4.2-3"}
MAX_SOURCE_BYTES = 512 * 1024 * 1024
METRICS = ["http_code", "time_namelookup", "time_connect", "time_appconnect",
           "time_starttransfer", "time_total", "size_download", "num_redirects"]


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return {"bytes": path.stat().st_size, "sha256": h.hexdigest()}


def descriptor_files(data, package, version):
    """Accept only the exact two native source filenames and strong checksums."""
    if len(data) > 1024 * 1024:
        raise ValueError("descriptor_too_large")
    fields = {}
    key = None
    for line in data.decode("utf-8").splitlines():
        if line.startswith("-----BEGIN PGP SIGNATURE-----"):
            break
        if line.startswith((" ", "\t")) and key:
            fields[key] += "\n" + line.strip()
        elif re.match(r"^[A-Za-z][A-Za-z0-9-]*:", line):
            key, value = line.split(":", 1)
            if key in fields:
                raise ValueError("duplicate_descriptor_field")
            fields[key] = value.strip()
        else:
            key = None
    if fields.get("Source") != package or fields.get("Version") != version:
        raise ValueError("descriptor_identity_mismatch")
    upstream = version.rsplit("-", 1)[0]
    expected = {f"{package}_{upstream}.orig.tar.gz", f"{package}_{version}.debian.tar.xz"}
    result = {}
    for line in fields.get("Checksums-Sha256", "").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})\s+([1-9][0-9]*)\s+(\S+)", line.strip())
        if not match:
            raise ValueError("invalid_sha256_row")
        sha, size, name = match.groups()
        if name not in expected or name in result or int(size) > MAX_SOURCE_BYTES:
            raise ValueError("unexpected_duplicate_or_oversized_source")
        result[name] = {"sha256": sha, "bytes": int(size)}
    if set(result) != expected:
        raise ValueError("missing_sha256_source")
    return result


def fetch(out, name, maximum, global_deadline):
    """At most three attempts and 300s total per file, within the job budget."""
    record = {"url": BASE + name, "attempts": [], "status": "download_failed"}
    deadline = min(global_deadline, time.monotonic() + 300)
    for attempt in range(1, 4):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            record["status"] = "time_budget_exhausted"
            break
        partial = out / f"{name}.attempt-{attempt}.partial"
        seconds = min(120, remaining)
        command = ["/usr/bin/curl", "--disable", "--silent", "--show-error", "--fail",
                   "--location", "--max-redirs", "5", "--proto", "=https",
                   "--proto-redir", "=https", "--connect-timeout", "15",
                   "--max-time", str(seconds), "--max-filesize", str(maximum),
                   "--output", str(partial), "--write-out",
                   "\n".join("%{" + key + "}" for key in METRICS + ["url_effective"]),
                   BASE + name]
        started = time.monotonic()
        try:
            proc = subprocess.run(command, capture_output=True, timeout=seconds + 2)
            values = proc.stdout.decode("utf-8", errors="replace").splitlines()
            metrics = dict(zip(METRICS, values[:len(METRICS)]))
            details = {"attempt": attempt, "curl_exit": proc.returncode,
                       "elapsed_seconds": round(time.monotonic() - started, 3),
                       "metrics": metrics,
                       "effective_host": urlsplit(values[-1]).hostname if len(values) == 9 else None}
            # Do not publish stderr or redirect query strings, which can contain proxy data.
            details["stderr_bytes"] = len(proc.stderr)
            if partial.exists():
                details["payload"] = digest(partial)
            record["attempts"].append(details)
            if proc.returncode == 0 and metrics.get("http_code") == "200" and partial.is_file():
                partial.rename(out / name)
                record.update(status="downloaded", **digest(out / name))
                return record
            retry = proc.returncode in {5, 6, 7, 18, 28, 35, 52, 55, 56} or metrics.get("http_code") in {
                "408", "429", "500", "502", "503", "504"}
            if not retry:
                break
        except subprocess.TimeoutExpired:
            record["attempts"].append({"attempt": attempt, "status": "process_timeout"})
        if attempt < 3 and deadline - time.monotonic() > 2:
            time.sleep(2)
    return record


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    sources = output / "sources"
    sources.mkdir()
    report = {"schema": 1, "purpose": "public_source_connectivity_and_descriptor_checksums",
              "base_url": BASE, "status": "running", "packages": {},
              "authentication": "HTTPS transport; DSC OpenPGP signatures are not verified",
              "source_revision": os.environ.get("GITHUB_SHA"),
              "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
              "limits": {"overall_seconds": 900, "file_seconds": 300, "attempts_per_file": 3}}
    deadline = time.monotonic() + 900

    def save():
        temporary = output / "report.json.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "report.json")

    save()
    try:
        # Get every DSC before a large archive can consume the remaining budget.
        for package, version in PACKAGES.items():
            entry = report["packages"][package] = {"version": version, "status": "descriptor_failed"}
            name = f"{package}_{version}.dsc"
            entry["descriptor"] = fetch(sources, name, 1024 * 1024, deadline)
            if entry["descriptor"]["status"] == "downloaded":
                try:
                    entry["expected_sources"] = descriptor_files((sources / name).read_bytes(), package, version)
                    entry["status"] = "descriptor_parsed"
                except (ValueError, UnicodeError) as exc:
                    entry["error"] = str(exc)[:200]
            save()
        for package, entry in report["packages"].items():
            if entry["status"] != "descriptor_parsed":
                continue
            entry["sources"] = {}
            for name, expected in entry["expected_sources"].items():
                actual = fetch(sources, name, MAX_SOURCE_BYTES, deadline)
                actual["expected"] = expected
                actual["verified"] = actual["status"] == "downloaded" and all(
                    actual.get(key) == value for key, value in expected.items())
                if actual["status"] == "downloaded" and not actual["verified"]:
                    actual["status"] = "descriptor_checksum_or_size_mismatch"
                entry["sources"][name] = actual
                save()
            entry["status"] = "verified" if all(item["verified"] for item in entry["sources"].values()) else "source_failed"
            save()
        report["status"] = "passed" if all(p["status"] == "verified" for p in report["packages"].values()) else "failed"
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    finally:
        save()
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output))
