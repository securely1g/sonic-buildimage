"""Keep only the Make symbols required by the unchanged base libsonicdbcli.

The original Common debug DEB also contains symbols for libraries now built by
Bazel. Those obsolete companions must never enter the image. This adapter keeps
the inherited library's exact companion and DWZ supplement, recording both the
original DEB/payload provenance and the newly filtered payload identity.
"""

import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


PACKAGE = "libswsscommon-dbgsym"
CONTRACT = Path(__file__).with_suffix(".json")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_contract(path=None):
    value = json.loads((Path(path) if path is not None else CONTRACT).read_bytes())
    require(value.get("schema") == 1 and value.get("package", {}).get("package") == PACKAGE and
            isinstance(value.get("files"), dict) and len(value["files"]) == 2,
            "invalid inherited base-symbol contract")
    return value


def descriptor(contract_path=None):
    path = Path(contract_path) if contract_path is not None else CONTRACT
    contract = read_contract(path)
    return {"schema": 1, "contract_sha256": sha(path),
            "runtime": copy.deepcopy(contract["runtime"]), "paths": sorted(contract["files"])}


def check_original(record, contract):
    for field, expected in contract["package"].items():
        require(record.get(field) == expected, "inherited base-symbol original package changed: " + field)


def check_record(record, *, contract_path=None):
    """Require exact original package provenance and the explicit two-file filter."""
    contract = read_contract(contract_path)
    check_original(record, contract)
    require(record.get("base_debug_symbols") == descriptor(contract_path),
            "inherited base-symbol filter differs from the reviewed contract")
    for field, expected in contract["original_payload"].items():
        require(record.get("original_payload_" + field) == expected,
                "inherited base-symbol original payload changed: " + field)
    require(isinstance(record.get("payload_sha256"), str) and
            re.fullmatch(r"[0-9a-f]{64}", record["payload_sha256"]) and
            type(record.get("payload_size")) is int and record["payload_size"] > 0 and
            record.get("payload_members") == len(contract["files"]),
            "invalid filtered inherited base-symbol payload")
    return record["base_debug_symbols"]


def check_member(archive, member, files):
    name = str(PurePosixPath(member.name))
    require(name in files and member.isfile() and member.sparse is None,
            "unexpected inherited base-symbol member: " + name)
    contents = archive.extractfile(member).read()
    actual = {"sha256": hashlib.sha256(contents).hexdigest(), "size": len(contents),
              "mode": member.mode, "uid": member.uid, "gid": member.gid}
    require(actual == {key: files[name][key] for key in actual},
            "inherited base-symbol bytes or metadata changed: " + name)
    return name, contents


def filter_payload(record, payload_path, *, contract_path=None):
    """Replace one extracted data tar with its two reviewed base-symbol files."""
    contract = read_contract(contract_path)
    check_original(record, contract)
    for field, expected in contract["original_payload"].items():
        require(record.get("payload_" + field) == expected,
                "inherited base-symbol original payload changed: " + field)
    require(sha(payload_path) == contract["original_payload"]["sha256"] and
            payload_path.stat().st_size == contract["original_payload"]["size"],
            "inherited base-symbol extracted payload changed")
    selected = {}
    with tarfile.open(payload_path, "r:") as archive:
        count = 0
        for member in archive:
            count += 1
            name = str(PurePosixPath(member.name))
            if name not in contract["files"]:
                continue
            require(name not in selected, "duplicate inherited base-symbol member: " + name)
            _, selected[name] = check_member(archive, member, contract["files"])
    require(count == contract["original_payload"]["members"] and set(selected) == set(contract["files"]),
            "inherited base-symbol input inventory changed")
    output = payload_path.with_name(payload_path.name + ".filtered")
    with tarfile.open(output, "w", format=tarfile.GNU_FORMAT) as archive:
        for name in sorted(selected):
            member = tarfile.TarInfo("./" + name)
            expected = contract["files"][name]
            member.mode, member.uid, member.gid, member.mtime = expected["mode"], expected["uid"], expected["gid"], 0
            member.size = len(selected[name])
            archive.addfile(member, io.BytesIO(selected[name]))
    output.replace(payload_path)
    record.update({"original_payload_" + field: value for field, value in contract["original_payload"].items()})
    record.update(payload_sha256=sha(payload_path), payload_size=payload_path.stat().st_size,
                  payload_members=len(selected), base_debug_symbols=descriptor(contract_path))
    check_record(record, contract_path=contract_path)


def validate_aggregate(manifest, payload_path, *, contract_path=None):
    """Check the filtered segment in an otherwise unchanged ordered Make tar."""
    filtered = [record for record in manifest["packages"]
                if record.get("package") == PACKAGE or "base_debug_symbols" in record]
    if not filtered:
        return []
    require(manifest.get("variant") == "debug" and len(filtered) == 1,
            "inherited base symbols belong only in the debug handoff")
    check_record(filtered[0], contract_path=contract_path)
    contract = read_contract(contract_path)
    with tarfile.open(payload_path, "r:") as archive:
        members = iter(archive)
        for record in manifest["packages"]:
            seen = set()
            for _ in range(record["payload_members"]):
                member = next(members, None)
                require(member is not None, "inherited base-symbol aggregate ended early")
                if record is filtered[0]:
                    name, _ = check_member(archive, member, contract["files"])
                    require(name not in seen, "duplicate inherited base-symbol member: " + name)
                    seen.add(name)
            if record is filtered[0]:
                require(seen == set(contract["files"]), "inherited base-symbol aggregate inventory changed")
        require(next(members, None) is None, "inherited base-symbol aggregate has unrecorded members")
    return [filtered[0]["base_debug_symbols"]]
