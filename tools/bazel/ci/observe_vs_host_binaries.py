#!/usr/bin/env python3
"""Compare fixed existing host binaries; never install, build or admit packages."""
import argparse
import base64
import collections
import csv
import email.parser
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import traceback
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import recover_vs_original_inputs as original

MIB = 1024**2
MAX_MEMBER = 128 * MIB
MAX_STREAM = 512 * MIB
MAX_REPORT = 16 * MIB
MANIFEST_SHA256 = "2ceeb63d802c73fa35a698c56718ad8973970bd43baf0f12778cd6b874b25286"
CONTROL_FIELDS = ("Package", "Version", "Architecture", "Multi-Arch", "Depends",
                  "Pre-Depends", "Provides", "Conflicts", "Breaks", "Replaces", "Essential")
require = original.require


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MetadataBudget:
    """Bound retained JSON facts before they enter an accumulating result list."""
    def __init__(self, limit=12 * MIB):
        self.limit, self.used = limit, 0

    def reserve(self, value):
        amount = 0
        for chunk in json.JSONEncoder(sort_keys=True, separators=(",", ":")).iterencode(value):
            amount += len(chunk.encode("utf-8"))
            require(self.used + amount <= self.limit, "Retained metadata exceeds bound")
        self.used += amount


def load_manifest(path):
    with original.HeldFile(path) as held:
        require(held.initial.st_size < MIB and original.hash_range(held.fd, 0, held.initial.st_size)
                == MANIFEST_SHA256, "Fixed observation manifest differs")
        value = json.loads(os.pread(held.fd, held.initial.st_size, 0))
        held.verify()
    require(len(value["required_names"]) == 66 and len(value["inputs"]) == 139 and
            len(value["recovered"]) == 2, "Fixed input roster differs")
    require(len({(r["required_name"], r["member_sha256"]) for r in value["inputs"]}) == 139,
            "Duplicate observation payload")
    require(sum(r["member_bytes"] for r in value["inputs"]) == 60940616,
            "Fixed original payload sizes differ")
    return value


def run_upstream(program, arguments, source, scratch, maximum, seconds=45):
    """One fixed upstream reader, with file/CPU/memory limits and owned cleanup."""
    require(program in ("/usr/bin/dpkg-deb", "/usr/bin/readelf"), "Unexpected reader")
    source.seek(0)
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    output = tempfile.TemporaryFile(dir=scratch)
    try:
        with tempfile.TemporaryFile(dir=scratch) as errors:
            environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(scratch)}
            if os.environ.get("RUNNER_TRACKING_ID"):
                environment["RUNNER_TRACKING_ID"] = os.environ["RUNNER_TRACKING_ID"]
            process = subprocess.Popen(
                [program, *arguments, "/proc/self/fd/" + str(source.fileno())],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                pass_fds=(source.fileno(),), start_new_session=True, preexec_fn=limits,
                env=environment)
            # Reuse the qualified native APT reader's waitid/WNOWAIT pattern:
            # keep the leader unreaped until its owned group has been closed.
            leader_owned = True
            try:
                deadline = time.monotonic() + seconds + 5
                while time.monotonic() < deadline:
                    state = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if state is not None:
                        code = state.si_status if state.si_code == os.CLD_EXITED else -state.si_status
                        break
                    time.sleep(0.05)
                else:
                    raise subprocess.TimeoutExpired(program, seconds + 5)
            except ChildProcessError:
                leader_owned = False
                raise
            finally:
                if leader_owned:
                    # These readers own no transactional state. Close all decoder
                    # children before reaping, including on successful exit.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
            require(code == 0, "Upstream reader failed")
        require(os.fstat(output.fileno()).st_size <= maximum, "Upstream output exceeds bound")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise


def field_document(raw, repeated=()):
    require(len(raw) <= MIB and b"\0" not in raw, "Metadata document exceeds bound")
    document = email.parser.Parser().parsestr(raw.decode("utf-8", "strict"))
    require(not document.defects, "Malformed package metadata")
    for key in document.keys():
        require(key in repeated or len(document.get_all(key)) == 1, "Duplicate metadata field")
    return document


def elf_metadata(source, scratch):
    with run_upstream("/usr/bin/readelf", ["--wide", "--file-header", "--program-headers",
                      "--dynamic", "--version-info"], source, scratch, 2 * MIB) as stream:
        raw = stream.read(2 * MIB + 1)
    require(len(raw) <= 2 * MIB, "ELF report exceeds bound")
    text = raw.decode("utf-8", "strict")
    fields = {}
    for key in ("Class", "Data", "Type", "Machine", "OS/ABI"):
        match = re.search(r"^\s*" + re.escape(key) + r":\s*(.+)$", text, re.M)
        require(match is not None, "ELF header field missing")
        fields[key] = match.group(1).strip()
    fields["needed"] = sorted(re.findall(r"\(NEEDED\).*?\[([^]\n]+)\]", text))
    fields["soname"] = sorted(re.findall(r"\(SONAME\).*?\[([^]\n]+)\]", text))
    fields["rpath"] = sorted(re.findall(r"\((?:RPATH|RUNPATH)\).*?\[([^]\n]+)\]", text))
    fields["interpreter"] = re.findall(r"Requesting program interpreter: ([^]\n]+)", text)
    needs = text.split("Version needs section", 1)
    fields["required_symbol_versions"] = sorted(set(re.findall(
        r"Name: ([A-Za-z0-9_.+:-]+)", needs[1] if len(needs) == 2 else "")))
    return fields


def tar_metadata(stream, scratch, controls=False, budget=None):
    budget = budget or MetadataBudget(4 * MIB)
    rows, timestamps, seen, retained = [], [], set(), {}
    bounded = original.Meter(stream, MAX_STREAM if not controls else 8 * MIB)
    with tarfile.open(fileobj=bounded, mode="r|", tarinfo=original.BoundedTarInfo) as archive:
        for item in archive:
            info = original.tar_record(item, seen)
            name = info["normalized_name"]
            require(item.isfile() or item.isdir() or item.issym() or item.islnk(),
                    "Unsupported package filesystem member")
            require(0 <= item.size <= MAX_MEMBER, "Package member exceeds bound")
            require(not any(ord(c) < 32 or ord(c) == 127 for c in item.linkname)
                    and len(item.linkname) <= 1024, "Invalid link metadata")
            row = {"path": name, "type": info["type"], "bytes": item.size,
                   "mode": item.mode, "uid": item.uid, "gid": item.gid,
                   "uname": item.uname, "gname": item.gname, "link": item.linkname}
            if item.isfile():
                with archive.extractfile(item) as member, tempfile.TemporaryFile(dir=scratch) as content:
                    value = original.copy_member(member, content, item.size, MAX_MEMBER)
                    row["sha256"] = value["sha256"]
                    content.seek(0)
                    prefix = content.read(4)
                    if controls and name == "control":
                        require(item.size <= MIB, "Control document exceeds bound")
                        content.seek(0); retained[name] = content.read(MIB + 1)
                    if not controls and prefix == b"\x7fELF":
                        row["elf"] = elf_metadata(content, scratch)
            else:
                require(item.size == 0, "Non-file member contains data")
            timestamp = {"path": name, "mtime": item.mtime}
            budget.reserve(row); budget.reserve(timestamp)
            rows.append(row); timestamps.append(timestamp)
        require(getattr(archive, "_recovery_zero_end", False), "TAR end marker missing")
        original.zero_tail(archive.fileobj)
    rows.sort(key=lambda x: x["path"]); timestamps.sort(key=lambda x: x["path"])
    return {"entries": rows, "content_metadata_sha256": digest_json(rows),
            "timestamps_sha256": digest_json(timestamps)}, retained


def inspect_deb(source, name, scratch):
    with run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, scratch, 8 * MIB) as stream:
        controls, documents = tar_metadata(stream, scratch, controls=True)
    require("control" in documents, "DEB control document missing")
    document = field_document(documents["control"])
    expected_name, expected_version, expected_arch = name[:-4].rsplit("_", 2)
    identity_matches = ((document.get("Package"), document.get("Version"), document.get("Architecture")) ==
                        (expected_name, expected_version, expected_arch))
    require(expected_arch in ("amd64", "all"), "Fixed DEB roster is outside AMD64 scope")
    with run_upstream("/usr/bin/dpkg-deb", ["--fsys-tarfile"], source, scratch, MAX_STREAM) as stream:
        payload, _ = tar_metadata(stream, scratch)
    return {"kind": "deb", "configured_identity_matches": identity_matches, "control": {k: document.get(k) for k in CONTROL_FIELDS if k in document},
            "control_files": controls, "payload": payload,
            "semantic_identity": digest_json({"control": controls["entries"], "payload": payload["entries"]}),
            "package_scripts_executed": False, "installed": False}


def inspect_wheel(source, name, scratch):
    budget = MetadataBudget(4 * MIB)
    source.seek(0)
    with zipfile.ZipFile(source) as archive:
        original.zip_inventory(archive, MAX_STREAM, MAX_MEMBER)
        rows, documents, timestamps, hashes = [], {}, [], {}
        for item in archive.infolist():
            if item.is_dir():
                continue
            path = original.clean_name(item.filename)
            require(item.file_size <= MAX_MEMBER, "Wheel member exceeds bound")
            with archive.open(item) as member, tempfile.TemporaryFile(dir=scratch) as content:
                value = original.copy_member(member, content, item.file_size, MAX_MEMBER)
                row = {"path": path, "bytes": value["bytes"], "sha256": value["sha256"],
                       "mode": item.external_attr >> 16, "ownership": "not_stored_in_wheel"}
                hashes[path] = value
                if path.endswith((".dist-info/METADATA", ".dist-info/WHEEL", ".dist-info/RECORD")):
                    require(item.file_size <= MIB, "Wheel metadata exceeds bound")
                    key = path.rsplit("/", 1)[-1]
                    require(key not in documents, "Multiple wheel metadata directories")
                    content.seek(0); documents[key] = content.read(MIB + 1)
                content.seek(0)
                if content.read(4) == b"\x7fELF":
                    row["elf"] = elf_metadata(content, scratch)
            timestamp = {"path": path, "datetime": item.date_time}
            budget.reserve(row); budget.reserve(timestamp)
            rows.append(row); timestamps.append(timestamp)
        require(set(documents) == {"METADATA", "WHEEL", "RECORD"}, "Wheel metadata missing")
        metadata = field_document(documents["METADATA"], ("Requires-Dist", "Classifier", "Project-URL", "Provides-Extra", "License-File", "Dynamic",
                                      "Platform", "Supported-Platform", "Requires-External", "Provides-Dist",
                                      "Obsoletes-Dist", "Requires", "Provides", "Obsoletes"))
        wheel = field_document(documents["WHEEL"], ("Tag",))
        parts = name[:-4].split("-"); require(len(parts) == 5, "Unexpected fixed wheel filename")
        normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
        identity_matches = (normalize(metadata.get("Name", "")) == normalize(parts[0]) and
                            metadata.get("Version") == parts[1] and
                            "-".join(parts[-3:]) in wheel.get_all("Tag", []))
        checked = set()
        for record in csv.reader(io.StringIO(documents["RECORD"].decode("utf-8", "strict"))):
            require(len(record) == 3, "Malformed wheel RECORD")
            path, encoded, size = record
            require(path in hashes and path not in checked, "Unexpected wheel RECORD member")
            checked.add(path)
            if not encoded:
                require(path.endswith(".dist-info/RECORD") and not size, "Unhashed wheel member")
            else:
                expected = "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(hashes[path]["sha256"])).rstrip(b"=").decode()
                require(encoded == expected and size == str(hashes[path]["bytes"]), "Wheel RECORD digest differs")
        require(checked == set(hashes), "Wheel RECORD is incomplete")
    rows.sort(key=lambda x: x["path"]); timestamps.sort(key=lambda x: x["path"])
    return {"kind": "wheel", "configured_identity_matches": identity_matches, "metadata": {"Name": metadata["Name"], "Version": metadata["Version"],
            "Requires-Python": metadata.get("Requires-Python"), "Requires-Dist": metadata.get_all("Requires-Dist", []),
            "Tags": wheel.get_all("Tag", []), "Root-Is-Purelib": wheel.get("Root-Is-Purelib")},
            "payload": {"entries": rows, "content_metadata_sha256": digest_json(rows),
                        "timestamps_sha256": digest_json(timestamps)},
            "semantic_identity": digest_json(rows), "installed": False}


def inspect_original(source, row, scratch):
    source.seek(0)
    require(os.fstat(source.fileno()).st_size == row["member_bytes"] and
            original.hash_range(source.fileno(), 0, row["member_bytes"]) == row["member_sha256"],
            "Original selected payload differs")
    name = row["required_name"]
    value = inspect_deb(source, name, scratch) if name.endswith(".deb") else inspect_wheel(source, name, scratch)
    return {"name": name, "bytes": row["member_bytes"], "sha256": row["member_sha256"], **value}


def inspect_path(path, row, scratch):
    with original.HeldFile(path) as held:
        with os.fdopen(os.dup(held.fd), "rb") as source:
            result = inspect_original(source, row, scratch)
        held.verify()
    return result


def inspect_cached(path, rows, scratch, observer=inspect_path, maximum_member=MAX_MEMBER):
    """Expose fixed original members to a read-only observer; never restore paths.

    The same operation can feed the existing Docker configuration inspector.
    Its callback receives an owned temporary regular path, the original row and
    scratch directory, and must return the row's required_name as `name`.
    """
    require(maximum_member in (MAX_MEMBER, 256 * MIB), "Unreviewed member bound")
    expected = {r["member_path"]: r for r in rows}
    require(len(expected) == len(rows), "Repeated selected member")
    first = rows[0]
    with original.HeldFile(path) as held:
        require(held.initial.st_size == first["cache_bytes"] and
                original.hash_range(held.fd, 0, held.initial.st_size) == first["cache_sha256"],
                "Original outer object differs")
        found, seen = [], set()
        with os.fdopen(os.dup(held.fd), "rb") as raw, gzip.GzipFile(fileobj=raw) as compressed:
            bounded = original.Meter(compressed, MAX_STREAM)
            with tarfile.open(fileobj=bounded, mode="r|", tarinfo=original.BoundedTarInfo) as archive:
                for item in archive:
                    name = original.tar_record(item, seen)["normalized_name"]
                    require(item.isfile() and 0 <= item.size <= maximum_member, "Unexpected cache member")
                    if name in expected:
                        row = expected[name]
                        require(item.size == row["member_bytes"], "Selected payload size differs")
                        with archive.extractfile(item) as member, tempfile.NamedTemporaryFile(dir=scratch, prefix="original-") as content:
                            copied = original.copy_member(member, content, item.size, maximum_member)
                            require(copied["sha256"] == row["member_sha256"], "Original inner member differs")
                            with original.HeldFile(Path(content.name)) as selected:
                                observed = observer(Path(content.name), row, scratch)
                                selected.verify()
                            require(observed["name"] == row["required_name"], "Observer renamed original member")
                            found.append(observed)
                require(getattr(archive, "_recovery_zero_end", False), "Cache TAR end marker missing")
                original.zero_tail(archive.fileobj)
            require(compressed.read(1) == b"", "Unexpected gzip tail")
        held.verify()
    require({v["name"] for v in found} == {r["required_name"] for r in rows}, "Selected member missing")
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache-artifact", type=Path, required=True)
    parser.add_argument("--recovered-artifact", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    def deadline(*_):
        raise TimeoutError("Fixed metadata observation deadline")
    previous = {number: signal.signal(number, deadline)
                for number in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
    signal.alarm(900)
    result = {"status": "HOST_BINARY_OBSERVATION_INCOMPLETE", "source_admission": False,
              "VS_assembly_admission": False, "package_generation": False, "package_installation": False,
              "package_scripts_executed": False, "results": []}
    active = "manifest"
    try:
        value = load_manifest(args.manifest)
        result["configuration"] = value["configuration"]
        result["equivalence_basis"] = "Control/payload contents and filesystem metadata, excluding tar/ZIP timestamps. Full original binary hashes remain separate; compression/AR wrapper differences are not classified."
        budget = MetadataBudget()
        def inspect_and_reserve(path, row, scratch):
            observed = inspect_path(path, row, scratch)
            budget.reserve(observed)
            return observed
        grouped = collections.defaultdict(list)
        for row in value["inputs"]:
            grouped[row["cache_name"]].append(row)
        for name, rows in sorted(grouped.items()):
            active = name
            result["results"].extend(inspect_cached(args.cache_artifact / "execution" / name, rows, args.scratch,
                                                       observer=inspect_and_reserve))
        for row in value["recovered"]:
            active = row["required_name"]
            with original.HeldFile(args.recovered_artifact / row["member_path"]) as held:
                with os.fdopen(os.dup(held.fd), "rb") as source:
                    observed = inspect_original(source, row, args.scratch)
                    budget.reserve(observed)
                    result["results"].append(observed)
                held.verify()
        require(len(result["results"]) == 141 and
                {v["name"] for v in result["results"]} == {r["name"] for r in value["required_names"]},
                "Incomplete configured roster observation")
        classes = collections.defaultdict(lambda: collections.defaultdict(list))
        for row in result["results"]:
            classes[row["name"]][row["semantic_identity"]].append(row["sha256"])
        result.update(status="FIXED_HOST_BINARY_METADATA_OBSERVED_NOT_ADMITTED", equivalence_classes=classes)
        code = 0
    except (OSError, ValueError, EOFError, tarfile.TarError, zipfile.BadZipFile,
            subprocess.SubprocessError, KeyError, TypeError) as error:
        site = traceback.extract_tb(error.__traceback__)[-1]
        result["failure"] = {"fixed_input": active, "error_type": type(error).__name__,
                             "site": {"file": Path(site.filename).name, "line": site.lineno, "function": site.name}}
        if isinstance(error, original.RecoveryError):
            result["failure"]["reason"] = str(error)[:256]
        code = 1
    finally:
        signal.alarm(0)
        for number, handler in previous.items():
            signal.signal(number, handler)
    encoded = (json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n").encode()
    require(len(encoded) <= MAX_REPORT, "Metadata report exceeds bound")
    with args.report.open("xb") as stream:
        stream.write(encoded)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
