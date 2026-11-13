"""Inventory one fixed original failed native upload and retain bounded checked public JSON.

The workflow must run the pure admission fixtures and verify the pinned local
module hashes before this module. API authentication is confined to api.github.com;
the artifact hosting GET carries no credentials. No subprocess or project build
is used. Original producer failure and all original payload bytes are preserved.
Payload/TAR/Docker/DEB bytes stay in the ZIP. Only original public JSON is copied
to numbered data files, with an explicit original member/path/hash map.
"""
import base64
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

from tools.bazel.ci import native_payload_recovery as recovery
from tools.bazel.ci import public_artifacts
from tools.bazel.ci import vs_cache_inspection as observation

INPUT_SHA256 = "09ad533fe596ca431dc1cabcffa27bfb6b804b0673e7fad5c8c12cf1859f9fce"
INPUTS = {
    "p4rt": ("native_payload_inventory_input.json", INPUT_SHA256),
    'gearbox': ('native_payload_inventory_gearbox_input.json', '665c8b74ed882793a2e892526bb0942cb254305729d2a519cca1e5f287e6dee2'),
    'syncd': ('native_payload_inventory_syncd_input.json', 'b27977dd2498a294f903281279538a1936b3ebd1eafd8a526bf56e1deab762ad'),
}

API = "https://api.github.com/repos/securely1g/sonic-buildimage"
CHUNK = 1024 * 1024
METADATA_LIMIT = 2 * CHUNK
INVENTORY_LIMIT = 8 * CHUNK
SPACE_RESERVE = 8 * 1024**3
# Original artifact 13803477455 has a 35,747,928-byte MODULE.bazel.lock.
# Retention caps are separate from the unchanged canonical receipt limits.
PUBLIC_MEMBER_BYTES = 64 * CHUNK
PUBLIC_TOTAL_BYTES = 64 * CHUNK
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
require = recovery.require


def strict(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite JSON')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deadline(signum, frame):
    raise TimeoutError("Original artifact inventory deadline")


@contextlib.contextmanager
def time_budget(seconds):
    started = time.monotonic()
    previous, interval = signal.getitimer(signal.ITIMER_REAL)
    require(interval == 0, "Unexpected repeating process timer")
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous) if previous else seconds)
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        remaining = previous - (time.monotonic() - started) if previous else 0
        signal.setitimer(signal.ITIMER_REAL, max(0, remaining))
        if previous and remaining <= 0 and not failed:
            raise TimeoutError("Original artifact inventory deadline")


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


@contextlib.contextmanager
def directory(path):
    """Hold and recheck every no-follow directory component, without listing it."""
    require(path.is_absolute() and str(path) == str(Path(os.path.normpath(path))) and
            len(path.parts) <= 12, "Invalid exact directory")
    descriptors, links = [], []
    try:
        current = os.open("/", DIRECTORY_FLAGS)
        descriptors.append(current)
        for part in path.parts[1:]:
            named = os.stat(part, dir_fd=current, follow_symlinks=False)
            require(stat.S_ISDIR(named.st_mode), "Non-directory ancestor")
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            descriptors.append(child)
            require(signature(named) == signature(os.fstat(child)), "Ancestor changed")
            links.append((current, part, signature(named)))
            current = child
        yield current
        for parent, name, expected in links:
            require(signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) == expected,
                    "Ancestor changed during observation")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def write_json(root, name, value, limit=METADATA_LIMIT):
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    write_bytes(root, name, raw, limit)


def write_bytes(root, name, raw, limit=METADATA_LIMIT):
    require(len(raw) <= limit, "Oversized bounded receipt")
    fd = os.open(name, FILE_FLAGS, 0o600, dir_fd=root)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def read_input(family="p4rt"):
    require(family in INPUTS, "Unexpected fixed native inspection family")
    name, expected_sha256 = INPUTS[family]
    path = Path(__file__).with_name(name)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= 65536,
                "Invalid fixed inspection input")
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())
    require(signature(before) == signature(after) and before.st_mtime_ns == after.st_mtime_ns and
            before.st_ctime_ns == after.st_ctime_ns and len(raw) == before.st_size and
            hashlib.sha256(raw).hexdigest() == expected_sha256, "Inspection input changed")
    value = strict(raw)
    request = base64.b64decode(value["original_request_base64"], validate=True)
    contract = base64.b64decode(value["original_contract_base64"], validate=True)
    require(hashlib.sha256(request).hexdigest() == value["original_request_sha256"] and
            hashlib.sha256(contract).hexdigest() == value["original_contract_sha256"],
            "Original request or contract bytes differ")
    return value, request, strict(contract)


def api_json(opener, token, suffix, root, name, calls, family="p4rt"):
    require(family in INPUTS, "Unexpected fixed native inspection family")
    case = recovery.CASES[family]
    require(suffix in ("/actions/runs/" + str(case["run"]),
                      "/actions/jobs/" + str(case["job"]),
                      "/actions/artifacts/" + str(case["artifact"])), "Unexpected metadata endpoint")
    request = urllib.request.Request(API + suffix,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    calls.append(name)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with time_budget(30), opener.open(request, timeout=30) as response:
        require(response.status == 200, "Metadata HTTP status differs")
        raw = response.read(METADATA_LIMIT + 1)
    require(len(raw) <= METADATA_LIMIT, "Oversized original metadata")
    value = strict(raw)
    write_bytes(root, name + "-response.data", raw)
    write_json(root, name + ".json", value)
    write_json(root, name + "-response.json", {"at": started, "HTTP_status": 200,
               "response_bytes": len(raw), "response_sha256": hashlib.sha256(raw).hexdigest(),
               "endpoint": API + suffix, "request_count": 1, "redirects_followed": 0})
    return value


def hosting_url(location):
    url = urllib.parse.urlsplit(location)
    require(url.scheme == "https" and not url.username and not url.password and
            not url.fragment and url.port in (None, 443) and url.hostname and
            any(url.hostname.endswith(suffix) for suffix in
                (".blob.core.windows.net", ".actions.githubusercontent.com", ".githubusercontent.com")),
            "Unexpected original artifact host")
    return url.hostname


def stream_exact(source, output, size, digest, expires):
    count, hashed = 0, hashlib.sha256()
    while True:
        require(time.monotonic() < expires, "Original ZIP download deadline")
        chunk = source.read(min(CHUNK, size - count + 1))
        if not chunk:
            break
        count += len(chunk)
        require(count <= size, "Original ZIP is larger than declared")
        output.write(chunk)
        hashed.update(chunk)
    require(count == size and hashed.hexdigest() == digest, "Original ZIP size or digest differs")
    return {"bytes": count, "sha256": hashed.hexdigest()}


def selected_public_json(archive, members, rows, expires, request=None):
    """The original publisher writes direct children of the uploaded public/ root.

    This maps exact ZIP-root paths, never basename matches at an unknown prefix.
    Its JSON and dependency safety validators are the actual observer/native
    interfaces bound by the workflow. Raw original bytes are never reserialized.
    """
    selected, total = {}, 0
    indexed = {row["name"]: row for row in rows}
    for member in sorted(members, key=lambda item: item.filename):
        if member.is_dir():
            continue
        name = member.filename
        require("/" not in name, "Original P4RT public upload layout differs")
        if not name.endswith(".json") and name != "MODULE.bazel.lock":
            continue
        total += member.file_size
        require(0 < member.file_size <= PUBLIC_MEMBER_BYTES and total <= PUBLIC_TOTAL_BYTES,
                "Original public JSON retention bound exceeded")
        require(time.monotonic() < expires, "Public JSON retention deadline")
        with archive.open(member) as source:
            raw = source.read(member.file_size + 1)
        require(len(raw) == member.file_size and
                hashlib.sha256(raw).hexdigest() == indexed[name]["sha256"],
                "Original public JSON differs from complete inventory")
        value = public_artifacts.load_json(raw.decode("utf-8"))
        if name in ("MODULE.bazel.lock", "module-graph.json"):
            kind = "lock" if name == "MODULE.bazel.lock" else "graph"
            public_artifacts.dependency_json(value, kind)
            validator = "dependency_json:" + kind
        elif name == "policy.json":
            require(request is not None, "Original policy requires its admitted request")
            recovery.worker.validate_public_evidence(value, name, request)
            validator = "worker.validate_public_evidence"
        else:
            public_artifacts.safe_json(value)
            validator = "safe_json"
        selected[name] = {"raw": raw, "sha256": indexed[name]["sha256"], "validator": validator}
    require(bool(selected), "Original public JSON is missing")
    return selected


def inventory(stream, artifact, expires, public=None, request=None):
    """Standard ZIP parsing, complete streamed CRC/hash checks, no member output."""
    rows, names, regular, total = [], set(), set(), 0
    with zipfile.ZipFile(stream) as archive:
        members = archive.infolist()
        require(0 < len(members) <= recovery.MAX_MEMBERS, "ZIP member count exceeded")
        require(sum(member.file_size for member in members) <= recovery.MAX_EXPANDED_BYTES,
                "ZIP expanded bound exceeded")
        for member in members:
            require(time.monotonic() < expires, "ZIP inventory deadline")
            name = recovery.member_name(member.filename, member.is_dir())
            require(member.filename == member.orig_filename and name not in names and
                    not member.flag_bits & 1, "Ambiguous or encrypted ZIP member")
            names.add(name)
            mode = stat.S_IFMT(member.external_attr >> 16)
            require(mode in ((0, stat.S_IFDIR) if member.is_dir() else (0, stat.S_IFREG)) and
                    member.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) and
                    0 <= member.file_size <= recovery.MAX_ZIP_BYTES,
                    "Unsupported ZIP member")
            if member.is_dir():
                require(member.file_size == 0, "Directory contains payload bytes")
            else:
                regular.add(name)
            count, crc, hashed = 0, 0, hashlib.sha256()
            with archive.open(member) as payload:
                while True:
                    require(time.monotonic() < expires, "ZIP inventory deadline")
                    chunk = payload.read(CHUNK)
                    if not chunk:
                        break
                    count += len(chunk)
                    total += len(chunk)
                    require(count <= member.file_size and total <= recovery.MAX_EXPANDED_BYTES,
                            "ZIP expanded stream exceeded bounds")
                    hashed.update(chunk)
                    crc = zlib.crc32(chunk, crc)
            require(count == member.file_size and crc == member.CRC, "ZIP member size or CRC differs")
            rows.append({"name": name, "type": "directory" if member.is_dir() else "regular",
                         "bytes": count, "sha256": None if member.is_dir() else hashed.hexdigest(),
                         "encrypted": False})
        if public is not None:
            require(type(public) is dict and not public, "Public evidence destination must be empty")
            public.update(selected_public_json(archive, members, rows, expires, request))
    for name in names:
        parts = name.split("/")
        require(all("/".join(parts[:index]) not in regular for index in range(1, len(parts))),
                "ZIP regular member shadows a directory")
    result = {"schema": 1, "artifact_id": artifact["id"], "zip_sha256": artifact["digest"][7:],
              "zip_bytes": artifact["size_in_bytes"], "central_directory_complete": True,
              "all_members_crc_verified": True, "members": rows}
    recovery.checked_inventory(result, artifact)
    return result


def public_review(root, request_bytes, contract, run, job, artifact, observed, public, retained, admission):
    """Review the same retained original bytes; never accept the failed producer."""
    result = {"schema": 1, "scope": "ORIGINAL_PUBLIC_SEMANTICS_INSPECTION_ONLY",
              "original_artifact": artifact["id"], "original_zip_sha256": artifact["digest"][7:],
              "original_conclusion": "failure", "canonical_native_acceptance": False,
              "fresh_build_or_runtime": False, "PR206_adoption": False}
    try:
        retained_by_name = {row["original_member"]: row for row in retained}
        require(len(retained_by_name) == len(retained) and set(retained_by_name) == set(public),
                "Retained public mapping differs")
        for name, entry in public.items():
            row = retained_by_name[name]
            require("/" not in name and row["bytes"] == len(entry["raw"]) and
                    row["sha256"] == hashlib.sha256(entry["raw"]).hexdigest(),
                    "Retained public byte binding differs")
        names = recovery.worker.public_json_names(admission["original_request"])
        selected = {name: entry["raw"] for name, entry in public.items() if name in names}
        logical_paths = {name: name for name in set(selected) | set(contract["outputs"])}
        require(all("/" not in name for name in logical_paths), "Original public layout differs")
        reviewed = recovery.review_public_upload(request_bytes, contract, run, job, artifact,
                                                 observed, selected, logical_paths)
        public_artifacts.safe_json(reviewed)
        result.update(status="PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED", review=reviewed)
    except (ValueError, KeyError, TypeError, IndexError, AssertionError) as error:
        result.update(status="PUBLIC_SEMANTICS_REJECTED_INSPECTION_ONLY", error_type=type(error).__name__)
    write_json(root, "public-semantic-review.json", result)
    return result


def collect(root, context, fixed, request_bytes, contract, calls, progress, family="p4rt"):
    require(family in INPUTS, "Unexpected fixed native inspection family")
    case = recovery.CASES[family]
    require(fixed["artifact_id"] == case["artifact"], "Fixed artifact belongs to another family")
    token = os.environ.pop("GH_TOKEN", "")
    require(token and len(token) <= 65536 and "\r" not in token and "\n" not in token,
            "Missing workflow artifact credential")
    opener = urllib.request.build_opener(NoRedirect)
    progress["stage"] = "original_run_metadata"
    run = api_json(opener, token, "/actions/runs/" + str(case["run"]), root, "original-run", calls, family)
    progress["stage"] = "original_job_metadata"
    job = api_json(opener, token, "/actions/jobs/" + str(case["job"]), root, "original-job", calls, family)
    progress["stage"] = "original_artifact_metadata"
    artifact = api_json(opener, token, "/actions/artifacts/" + str(case["artifact"]), root, "original-artifact", calls, family)
    progress["stage"] = "original_failed_upload_admission"
    admission = recovery.admit_original(request_bytes, contract, run, job, artifact)
    require(artifact["id"] == fixed["artifact_id"] and artifact["name"] == fixed["artifact_name"] and
            artifact["size_in_bytes"] == fixed["zip_bytes"] and
            artifact["digest"] == "sha256:" + fixed["zip_sha256"], "Logged original ZIP identity differs")
    require(artifact["archive_download_url"] == API + "/actions/artifacts/" + str(case["artifact"]) + "/zip",
            "Different original ZIP endpoint")
    free = os.fstatvfs(root)
    free_bytes = free.f_bavail * free.f_frsize
    require(free_bytes >= fixed["zip_bytes"] + SPACE_RESERVE, "Insufficient ephemeral download space")
    write_json(root, "admission.json", dict(admission, observed_free_bytes=free_bytes,
               additional_reserve_bytes=SPACE_RESERVE, physical_capacity_reserved=False))
    download_request = urllib.request.Request(artifact["archive_download_url"],
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    progress["stage"] = "original_artifact_redirect"
    calls.append("artifact_API_redirect")
    try:
        with time_budget(30), opener.open(download_request, timeout=30):
            raise ValueError("Original artifact API did not return its redirect")
    except urllib.error.HTTPError as error:
        try:
            require(error.code == 302, "Original artifact redirect status differs")
            location = error.headers["Location"]
        finally:
            error.close()
    del token, download_request
    host = hosting_url(location)
    fd = os.open("original.zip", FILE_FLAGS, 0o600, dir_fd=root)
    try:
        progress["stage"] = "original_zip_download"
        calls.append("original_hosting_GET")
        with time_budget(900), opener.open(urllib.request.Request(location), timeout=30) as response:
            require(response.status == 200, "Original artifact host status differs")
            with os.fdopen(os.dup(fd), "wb") as output:
                zipped = stream_exact(response, output, fixed["zip_bytes"], fixed["zip_sha256"],
                                      time.monotonic() + 900)
                output.flush()
                os.fsync(output.fileno())
        del location
        before = os.fstat(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        progress["stage"] = "original_zip_crc_and_hash_inventory"
        public = {}
        with time_budget(900), os.fdopen(os.dup(fd), "rb") as source:
            observed = inventory(source, artifact, time.monotonic() + 900, public,
                                 request=admission["original_request"])
        after = os.fstat(fd)
        named = os.stat("original.zip", dir_fd=root, follow_symlinks=False)
        require(signature(before) == signature(after) == signature(named) and
                before.st_size == after.st_size == named.st_size == fixed["zip_bytes"] and
                before.st_mtime_ns == after.st_mtime_ns == named.st_mtime_ns and
                before.st_ctime_ns == after.st_ctime_ns == named.st_ctime_ns,
                "Original ZIP binding changed during inventory")
    finally:
        os.close(fd)
    progress["stage"] = "bounded_inventory_receipt"
    write_json(root, "member-inventory.json", observed, INVENTORY_LIMIT)
    retained = []
    for index, (name, entry) in enumerate(sorted(public.items())):
        filename = f"public-evidence-{index:04d}.data"
        write_bytes(root, filename, entry["raw"], PUBLIC_MEMBER_BYTES)
        retained.append({"original_member": name, "retained_file": filename,
                         "bytes": len(entry["raw"]), "sha256": entry["sha256"],
                         "public_safety_validator": entry["validator"]})
    write_json(root, "public-evidence-map.json", {
        "schema": 1, "original_artifact": artifact["id"], "original_zip_sha256": zipped["sha256"],
        "layout": "direct_children_of_original_artifacts_public_upload_root",
        "members": retained, "original_bytes_preserved": True,
        "public_semantic_validation": False, "canonical_native_acceptance": False})
    progress["stage"] = "original_public_semantics_inspection_only"
    semantic = public_review(root, request_bytes, contract, run, job, artifact, observed,
                             public, retained, admission)
    return dict(context, schema=1, status="ORIGINAL_" + family.upper() + "_ZIP_INVENTORY_ONLY",
                original_artifact=artifact["id"], original_run=run["id"], original_job=job["id"],
                original_conclusion="failure", zip=zipped, hosting_hostname=host,
                member_count=len(observed["members"]), original_ZIP_preserved=True,
                original_member_paths_materialized=False, payload_bytes_retained_outside_ZIP=0,
                public_JSON_files_retained=len(retained),
                public_JSON_bytes_retained=sum(row["bytes"] for row in retained),
                scope="Bounded original public JSON retention in numbered data files; payloads remain in original ZIP",
                public_semantic_validation=semantic["status"] == "PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED",
                public_semantic_review=semantic["status"], canonical_native_acceptance=False,
                fresh_build_or_runtime=False, PR206_adoption=False, subprocesses=0,
                requires_successful_original_inspection_job=True)


def main():
    os.umask(0o077)
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    bounds = [1024**3] + [value for value in (soft, hard) if value != resource.RLIM_INFINITY]
    resource.setrlimit(resource.RLIMIT_AS, (min(bounds), min(bounds)))
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 1950)
    calls, result, code, progress = [], None, 1, {"stage": "credential_presence"}
    try:
        context = observation.identity(os.environ["RUNNER_NAME"], os.environ, observation.marker())
        family = os.environ.get("NATIVE_INVENTORY_IMAGE", "p4rt")
        fixed, request, contract = read_input(family)
        temporary = Path(os.environ["RUNNER_TEMP"])
        require(temporary.is_relative_to("/actions/_work") and
                not temporary.is_relative_to("/actions/_work/.source-image-cache"),
                "Unexpected ephemeral temporary directory")
        name = "native-original-" + family + "-" + str(context["run"]) + "-" + str(context["attempt"])
        with directory(temporary) as parent:
            os.mkdir(name, 0o700, dir_fd=parent)
            fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            try:
                initial = os.fstat(fd)
                try:
                    result = collect(fd, context, fixed, request, contract, calls, progress, family)
                    require(signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) == signature(initial),
                            "Owned output directory changed")
                    code = 0
                except Exception as error:
                    result = dict(context, schema=1, status="FAILED_NOT_QUALIFIED",
                                  error_type=type(error).__name__, stage=progress["stage"],
                                  canonical_native_acceptance=False,
                                  original_artifact=fixed["artifact_id"], original_conclusion="failure")
                    if isinstance(error, urllib.error.HTTPError):
                        if type(error.code) is int and 100 <= error.code <= 599:
                            result["HTTP_status"] = error.code
                        error.close()
                result["attempted_requests"] = list(calls)
                write_json(fd, "summary.json", result)
            finally:
                os.close(fd)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(result["status"] if result else "FAILED_BEFORE_OUTPUT")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
