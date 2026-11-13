"""Recover two fixed stored ZIP members through bounded original HTTP ranges.

The source-stable caller supplies the existing authenticated artifact API and
qualified original.copy_member. No package/image generation or ZIP fallback.
"""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

PLAN_SHA256 = "987d5cf4c93d921f733209bf701d71cf12dc72e0b30079375f254537bcc59190"
HEADER_LIMIT = 30
NAME_LIMIT = 512
EXTRA_LIMIT = 65535
MAX_SELECTED_MEMBER = 1024 ** 3
FIXED = {
    "docker-syncd-vs.gz": (14066258572, 679629216, "7fef31998236e57eded8c4e251f73bf8f744da48da70b05fbbd69df68681a46a"),
    "docker-gbsyncd-vs.gz": (14066969677, 702063555, "e247421fc5e8e4489c66b93dbfd6b538d181186e1e85256364ebd8dc5890dc05"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict(data):
    def pairs(items):
        result = {}
        for key, value in items:
            assert key not in result, 'duplicate JSON key'
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite JSON')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def local_header(raw, selected):
    assert type(raw) is bytes and len(raw) == HEADER_LIMIT
    values = struct.unpack('<4s5H3I2H', raw)
    signature, version, flags, method, _time, _date, crc, compressed, expanded, names, extras = values
    assert signature == b'PK\x03\x04'
    assert selected['method'] == method and method in (0, 8) and selected['flags'] == flags == 8
    assert 0 < selected['bytes'] <= MAX_SELECTED_MEMBER
    assert 0 < selected['compressed_bytes'] <= MAX_SELECTED_MEMBER
    assert method != 0 or selected['bytes'] == selected['compressed_bytes']
    assert version in (10, 20, 45) and 0 < names <= NAME_LIMIT and 0 <= extras <= EXTRA_LIMIT
    assert crc in (0, selected['crc32'])
    assert compressed in (0, selected['compressed_bytes'], 0xffffffff)
    assert expanded in (0, selected['bytes'], 0xffffffff)
    return {'name_bytes': names, 'extra_bytes': extras, 'flags': flags,
            'local_compressed': compressed, 'local_expanded': expanded}

def member_offset(raw, header, selected):
    assert len(raw) == header['name_bytes'] + header['extra_bytes']
    name_bytes = raw[:header['name_bytes']]
    name = name_bytes.decode('utf-8' if header['flags'] & 2048 else 'cp437')
    assert 0 < len(name) <= NAME_LIMIT and name.endswith('.gz')
    assert not name.startswith('/') and '\\' not in name and all(32 <= ord(c) < 127 for c in name)
    assert all(part not in ('', '.', '..') for part in name.split('/'))
    assert name == selected['name']
    assert hashlib.sha256(name.encode('utf-8')).hexdigest() == selected['name_sha256']
    extra = raw[header['name_bytes']:]
    fields = {}
    at = 0
    while at < len(extra):
        assert at + 4 <= len(extra)
        tag, count = struct.unpack_from('<HH', extra, at)
        at += 4
        assert at + count <= len(extra) and tag not in fields
        fields[tag] = extra[at:at + count]
        at += count
    if header['local_expanded'] == 0xffffffff or header['local_compressed'] == 0xffffffff:
        assert 1 in fields
        zipped = fields[1]
        at = 0
        for field, expected in (('local_expanded', selected['bytes']),
                                ('local_compressed', selected['compressed_bytes'])):
            if header[field] == 0xffffffff:
                assert at + 8 <= len(zipped)
                size, = struct.unpack_from('<Q', zipped, at)
                at += 8
                assert size in (0, expected)
    return selected['local_header_offset'] + HEADER_LIMIT + len(raw)

def load_plan(path):
    data = Path(path).read_bytes()
    require(hashlib.sha256(data).hexdigest() == PLAN_SHA256, "Fixed plan differs")
    value = strict(data)
    require(len(value["archives"]) == 2 and
            {row["name"] for row in value["archives"]} == set(FIXED), "Fixed archive set differs")
    for row in value["archives"]:
        require((row["artifact"]["id"], row["bytes"], row["sha256"]) == FIXED[row["name"]],
                "Fixed archive identity differs")
        require(row["method"] == 0 and row["flags"] == 8 and
                row["compressed_bytes"] == row["bytes"], "Only original stored members are selected")
    require(value["maximum_HTTP_requests_including_metadata"] == 10 and
            value["maximum_seconds"] == 1200 and
            value["maximum_hosting_body_bytes"] == 1381824925,
            "Fixed transport budget differs")
    require(not value["whole_ZIP_fallback"] and not value["source_or_image_admission"],
            "Transport scope differs")
    return value


def timeout(limits):
    remaining = limits["deadline"] - time.monotonic()
    require(math.isfinite(remaining), "Invalid transport deadline")
    if remaining <= 0:
        raise TimeoutError("Original archive transport deadline")
    require(limits["requests"] < limits["maximum_requests"], "Request budget exceeded")
    limits["requests"] += 1
    return min(30.0, remaining)


def validate_location(headers):
    values = headers.get_all("Location", [])
    require(len(values) == 1, "Artifact redirect is not unique")
    location = values[0]
    url = urllib.parse.urlsplit(location)
    require(url.scheme == "https" and not url.username and not url.password and
            not url.fragment and url.port in (None, 443), "Unsafe artifact redirect")
    require(url.hostname and any(url.hostname.endswith(suffix) for suffix in
            (".blob.core.windows.net", ".actions.githubusercontent.com", ".githubusercontent.com")),
            "Unexpected artifact hosting origin")
    return location


def artifact_location(opener, artifact, token, limits):
    require(isinstance(token, str) and 0 < len(token) <= 65536 and
            "\n" not in token and "\r" not in token, "Invalid API credential shape")
    endpoint = "https://api.github.com/repos/securely1g/sonic-buildimage/actions/artifacts/" + str(artifact["id"]) + "/zip"
    require(artifact["archive_download_url"] == endpoint, "Unexpected artifact API endpoint")
    request = urllib.request.Request(endpoint, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    try:
        with opener.open(request, timeout=timeout(limits)):
            raise ValueError("Artifact API did not return its expected redirect")
    except urllib.error.HTTPError as error:
        try:
            require(error.code == 302, "Artifact API rejected the fixed download")
            return validate_location(error.headers)
        finally:
            error.close()


def range_headers(response, start, end, total):
    require(type(start) is int and type(end) is int and 0 <= start <= end < total,
            "Invalid fixed byte range")
    count = end - start + 1
    require(count <= MAX_SELECTED_MEMBER, "Range exceeds selected archive bound")
    expected = "bytes " + str(start) + "-" + str(end) + "/" + str(total)
    require(response.status == 206 and
            response.headers.get_all("Content-Range", []) == [expected] and
            response.headers.get_all("Content-Length", []) == [str(count)] and
            response.headers.get_all("Content-Encoding", []) in ([], ["identity"]) and
            response.headers.get_all("Transfer-Encoding", []) == [],
            "Original byte-range headers differ")
    return count


def open_range(opener, location, start, end, total, limits):
    require(type(start) is int and type(end) is int and type(total) is int and
            0 <= start <= end < total, "Invalid fixed byte range")
    count = end - start + 1
    require(0 < count <= MAX_SELECTED_MEMBER and
            limits["hosting_declared"] + count <= limits["maximum_hosting_bytes"],
            "Hosting byte budget exceeded")
    seconds = timeout(limits)
    limits["hosting_declared"] += count
    try:
        response = opener.open(urllib.request.Request(location, headers={
            "Range": "bytes=" + str(start) + "-" + str(end), "Accept-Encoding": "identity"}), timeout=seconds)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError("Unexpected hosting response") from None
    try:
        range_headers(response, start, end, total)
    except BaseException:
        response.close()
        raise
    return response


def small_range(opener, location, start, count, total, limits):
    require(0 < count <= NAME_LIMIT + EXTRA_LIMIT, "Local header range exceeds its bound")
    with open_range(opener, location, start, start + count - 1, total, limits) as response:
        raw = response.read(count)
        require(len(raw) == count, "Local ZIP header is truncated")
        return raw


def recover_one(row, api, opener, token, destination, original, limits):
    expected = row["artifact"]
    require(row["method"] == 0 and row["flags"] == 8 and
            0 < row["bytes"] == row["compressed_bytes"] <= MAX_SELECTED_MEMBER,
            "Only bounded original stored members are supported")
    require(row["name"] in FIXED and Path(row["name"]).name == row["name"], "Unexpected output name")
    timeout(limits)  # The maintained API reader supplies its own bounded GET.
    actual = api("/actions/artifacts/" + str(expected["id"]))
    require(all(actual.get(key) == value for key, value in expected.items()), "Original artifact API identity differs")
    require(actual["expired"] is False and datetime.datetime.fromisoformat(
        actual["expires_at"].replace("Z", "+00:00")) > datetime.datetime.now(datetime.timezone.utc),
        "Original artifact expired")
    location = artifact_location(opener, expected, token, limits)
    total = expected["size_in_bytes"]
    raw = small_range(opener, location, row["local_header_offset"], HEADER_LIMIT, total, limits)
    header = local_header(raw, row)
    raw = small_range(opener, location, row["local_header_offset"] + HEADER_LIMIT,
                      header["name_bytes"] + header["extra_bytes"], total, limits)
    start = member_offset(raw, header, row)
    require(start + row["bytes"] <= total, "Selected member exceeds original ZIP bounds")
    output = destination / row["name"]
    with open_range(opener, location, start, start + row["bytes"] - 1, total, limits) as response:
        with output.open("xb") as target:
            copied = original.copy_member(response, target, row["bytes"], MAX_SELECTED_MEMBER)
    require(copied["bytes"] == row["bytes"] and copied["sha256"] == row["sha256"] and
            copied["CRC32"] == format(row["crc32"], "08x"), "Original archive digest or CRC differs")
    with original.HeldFile(output.absolute()) as held:
        require(held.initial.st_size == row["bytes"] and
                original.hash_range(held.fd, 0, row["bytes"]) == row["sha256"],
                "Recovered archive changed after copy")
        held.verify()
    return {"artifact_id": expected["id"], "artifact_digest": expected["digest"],
            "original_run": expected["workflow_run"]["id"], "member": row["name"],
            "bytes": row["bytes"], "sha256": row["sha256"], "CRC32": copied["CRC32"],
            "whole_ZIP_authenticated": False, "source_or_image_admission": False}


def recover_two(plan, api, token, destination, original, report):
    """The fixed caller hashes load_plan(), owners and its source before this call."""
    require(not destination.exists() and not destination.is_symlink(), "Recovery destination already exists")
    require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "An unrelated timer is already active")
    destination.mkdir(mode=0o700)
    limits = {"deadline": time.monotonic() + plan["maximum_seconds"], "requests": 0,
              "maximum_requests": plan["maximum_HTTP_requests_including_metadata"],
              "hosting_declared": 0, "maximum_hosting_bytes": plan["maximum_hosting_body_bytes"]}
    report.update(status="INCOMPLETE", archives=[], whole_ZIP_authenticated=False,
                  source_or_image_admission=False)
    def expired(_signum, _frame):
        raise TimeoutError("Original archive transport deadline")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, plan["maximum_seconds"])
    try:
        opener = urllib.request.build_opener(NoRedirect)
        for row in plan["archives"]:
            report["active_artifact"] = row["artifact"]["id"]
            report["archives"].append(recover_one(row, api, opener, token, destination, original, limits))
        report.update(status="TWO_ORIGINAL_ARCHIVES_VERIFIED", active_artifact=None)
    except Exception as error:
        report["failure_type"] = type(error).__name__
        raise RuntimeError("Fixed original archive recovery failed; inspect bounded report") from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        report["HTTP_requests"] = limits["requests"]
        report["hosting_declared_bytes"] = limits["hosting_declared"]
