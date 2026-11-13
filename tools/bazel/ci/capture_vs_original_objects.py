"""Capture fixed original byte streams once; never restore or admit cache objects."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from tools.bazel.ci import recover_vs_original_inputs as recovery

ROOT = Path("/dpkg_cache")
SELECTION_SHA256 = "49bac47857f971e66db7cba347019d37fc7a150b26f339dd5b3a025d43073510"
MAX_OBJECT = 128 * 1024 * 1024
ORIGINAL_BYTES = 276022500

# The default retains the already published106-object host-input capture.
# Each additional scope is an immutable, independently observed byte roster.
SCOPES = {
    "host": {"sha256": SELECTION_SHA256, "manifest_bytes": 29014, "objects": 106,
             "original_bytes": ORIGINAL_BYTES, "max_object": MAX_OBJECT, "ambiguous_names": 47},
    "legacy": {"sha256": "629d690259c61e9f441fdbca486e83b3e9ebb8489807127759b91b39ed2c2742", "manifest_bytes": 9874,
               "objects": 34, "original_bytes": 4644482017,
               "max_object": 256 * 1024 * 1024, "ambiguous_names": 4},
}


def selection(path, scope="host"):
    policy = SCOPES[scope]
    with recovery.HeldFile(path) as held:
        recovery.require(held.initial.st_size == policy["manifest_bytes"], "Unexpected original identity manifest size")
        raw = os.pread(held.fd, policy["manifest_bytes"] + 1, 0)
        held.verify()
    recovery.require(hashlib.sha256(raw).hexdigest() == policy["sha256"],
                     "Original identity manifest changed")
    value = json.loads(raw)
    recovery.require(value["root"] == str(ROOT) and len(value["objects"]) == policy["objects"],
                     "Unexpected original object scope")
    names = set()
    for item in value["objects"]:
        recovery.require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]*-[0-9a-f]{23}-[0-9a-f]{23}\.tgz", item["name"])
                         and item["name"] not in names, "Unsafe or duplicate original name")
        names.add(item["name"])
        recovery.require(type(item["bytes"]) is int and 0 < item["bytes"] <= policy["max_object"]
                         and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]), "Invalid original byte identity")
    recovery.require(sum(item["bytes"] for item in value["objects"]) == policy["original_bytes"],
                     "Original object byte budget changed")
    return value


class OriginalReadError(Exception):
    """Only an input open/read/verification failure; never a staging write."""
    def __init__(self, error):
        self.error_type = type(error).__name__
        self.missing = isinstance(error, FileNotFoundError)
        super().__init__(self.error_type)


@contextlib.contextmanager
def original_source(path):
    try:
        held = recovery.HeldFile(path)
    except (OSError, recovery.RecoveryError) as error:
        raise OriginalReadError(error) from None
    try:
        yield held
        try:
            held.verify()
        except (OSError, recovery.RecoveryError) as error:
            raise OriginalReadError(error) from None
    finally:
        held.close()


class OriginalReader:
    def __init__(self, stream):
        self.stream = stream

    def read(self, size):
        try:
            return self.stream.read(size)
        except OSError as error:
            raise OriginalReadError(error) from None


def capture_one(item, output, temporary, max_object=MAX_OBJECT):
    result = dict(name=item["name"], bytes=item["bytes"], sha256=item["sha256"])
    # The temporary stream is outside the uploaded output directory. A missing,
    # changed or unreadable source never creates a published object file.
    with tempfile.TemporaryFile(dir=temporary) as staged:
        try:
            with original_source(ROOT / item["name"]) as held:
                if held.initial.st_size != item["bytes"]:
                    return dict(result, status="original_size_mismatch")
                with os.fdopen(os.dup(held.fd), "rb") as stream:
                    copied = recovery.copy_member(OriginalReader(stream), staged, item["bytes"], max_object)
                if copied["sha256"] != item["sha256"]:
                    return dict(result, status="original_hash_mismatch")
        except OriginalReadError as error:
            return dict(result, status="original_missing" if error.missing else "original_not_safely_read",
                        error_type=error.error_type)
        except recovery.RecoveryError as error:
            return dict(result, status="original_not_safely_read", error_type=type(error).__name__)
        staged.seek(0)
        # Staging/final write, flush and fsync errors are fatal. Only explicit
        # input exceptions above can produce a partial availability report.
        with output.open(item["name"]) as target:
            copied = recovery.copy_member(staged, target, item["bytes"], max_object)
            recovery.require(copied["sha256"] == item["sha256"], "Staged original bytes changed")
    return dict(result, status="ORIGINAL_BYTES_CAPTURED_NOT_ADMITTED",
                path="execution/" + item["name"])


def capture(value, parent, temporary, identity, scope="host"):
    policy = SCOPES[scope]
    parent, temporary = Path(parent), Path(temporary)
    recovery.require(parent != temporary and parent not in temporary.parents and
                     temporary not in parent.parents, "Temporary streams must be outside uploaded outputs")
    with recovery.output_parent(parent) as parent_fd, recovery.output_parent(temporary):
        storage = os.fstatvfs(parent_fd)
        recovery.require(storage.f_bavail * storage.f_frsize >= policy["original_bytes"] * 2 + policy["max_object"],
                         "Insufficient temporary capture space")
        with contextlib.closing(recovery.Outputs(parent, parent_fd)) as output:
            rows = [capture_one(item, output, temporary, policy["max_object"]) for item in value["objects"]]
            captured = [row for row in rows if row["status"] == "ORIGINAL_BYTES_CAPTURED_NOT_ADMITTED"]
            result = dict(schema=1, status="ORIGINAL_OBJECT_CAPTURE_NOT_ADMISSION",
                          capture_job=identity, original_observation=value["original_observation"],
                          selection_sha256=policy["sha256"], objects=rows,
                          expected_objects=len(rows), captured_objects=len(captured),
                          captured_bytes=sum(row["bytes"] for row in captured),
                          missing_or_mismatched_objects=len(rows)-len(captured),
                          complete_original_byte_capture=len(captured)==len(rows),
                          ambiguous_required_names=policy["ambiguous_names"], source_key_compatibility="not_established",
                          package_generation=False, cache_writes=False, cache_restore=False,
                          cache_key_generation=False, payloads_extracted=False,
                          current_source_or_full_vs_admission=False)
            output.record("capture-lineage.json", result)
            output.verify()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--scope", choices=tuple(SCOPES), default="host")
    parser.add_argument("--output-parent", required=True, type=Path)
    parser.add_argument("--temporary-parent", required=True, type=Path)
    args = parser.parse_args()
    recovery.require(os.environ.get("GITHUB_REPOSITORY") == "securely1g/sonic-buildimage",
                     "Unexpected capture repository")
    run, attempt, revision, worker = (os.environ[key] for key in
        ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "RUNNER_NAME"))
    recovery.require(re.fullmatch(r"[1-9][0-9]*", run) and re.fullmatch(r"[1-9][0-9]*", attempt)
                     and re.fullmatch(r"[0-9a-f]{40}", revision)
                     and re.fullmatch(r"ks-[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?", worker),
                     "Unexpected capture job identity")
    value = selection(args.selection, args.scope)
    result = capture(value, args.output_parent, args.temporary_parent,
                     dict(repository=os.environ["GITHUB_REPOSITORY"], run=int(run),
                          attempt=int(attempt), revision=revision, worker=worker,
                          pool="sonic-slave-container-k3s"), args.scope)
    print(json.dumps({key: result[key] for key in
        ("status", "expected_objects", "captured_objects", "missing_or_mismatched_objects",
         "complete_original_byte_capture", "current_source_or_full_vs_admission")}, sort_keys=True))


if __name__ == "__main__":
    main()
