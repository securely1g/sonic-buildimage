"""Observe only known native-cache directory metadata; never prepare or use caches."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import time

from tools.bazel.ci import vs_cache_inspection as observation

OLD_WORKER = "ks-e6fb1976-715102e6996d-3-9700561e1db94bb3916a06a1a5209ad4"
CACHE_ROOT = "/actions/_work/.source-image-cache/source-images"
CHILDREN = ("repository", "disk", "bazelisk")
OUTPUT_LIMIT = 64 * 1024


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def inspect_directory(path, mounts, expires):
    """Hold every ancestor FD; inspect no entries and read no cache bytes."""
    parts = PurePosixPath(path)
    if not parts.is_absolute() or str(parts) != path or ".." in parts.parts or len(parts.parts) > 10:
        raise ValueError("Unexpected exact directory path")
    result = {"path": path, "status": "not_observed", "contents_read": False}
    descriptors, links = [], []
    try:
        directory = os.open("/", observation.DIRECTORY_FLAGS)
        descriptors.append(directory)
        for component in parts.parts[1:]:
            if time.monotonic() >= expires:
                raise TimeoutError("Metadata deadline")
            named = os.stat(component, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(named.st_mode):
                result["status"] = "symlink_rejected"
                break
            if not stat.S_ISDIR(named.st_mode):
                result["status"] = "non_directory"
                break
            child = os.open(component, observation.DIRECTORY_FLAGS, dir_fd=directory)
            descriptors.append(child)
            opened = os.fstat(child)
            if signature(named) != signature(opened):
                raise ValueError("Directory binding changed")
            links.append((directory, component, signature(opened)))
            directory = child
        else:
            before = os.fstat(directory)
            covering = observation.covering_mount(path, mounts)
            if sum(row["mountpoint"] == covering["mountpoint"] for row in mounts) != 1:
                raise ValueError("Ambiguous covering mount")
            data = observation.metadata(before)
            if data["device"] != covering["device"]:
                raise ValueError("Directory mount changed")
            after = os.fstat(directory)
            if observation.metadata(after) != data or after.st_ctime_ns != before.st_ctime_ns:
                raise ValueError("Directory metadata changed")
            result.update(status="DIRECTORY_METADATA_OBSERVED_CONTENTS_UNREAD", metadata=data,
                          ctime_ns=before.st_ctime_ns, mount=covering)
    except FileNotFoundError:
        result["status"] = "missing_on_this_registration_at_this_path"
    except PermissionError:
        result["status"] = "permission_denied"
    except (OSError, ValueError, TimeoutError) as error:
        result.update(status="not_fully_observed", error_type=type(error).__name__)
    finally:
        try:
            for parent, component, expected in links:
                if time.monotonic() >= expires:
                    raise TimeoutError("Metadata deadline")
                if signature(os.stat(component, dir_fd=parent, follow_symlinks=False)) != expected:
                    raise ValueError("Ancestor name binding changed")
        except (OSError, ValueError, TimeoutError) as error:
            result.update(status="not_fully_observed", error_type=type(error).__name__)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    return result


def selected_paths(worker):
    if worker == OLD_WORKER:
        raise ValueError("A different current registration is required")
    # identity() already checks this name against RUNNER_NAME and its exact K3s grammar.
    old, current = CACHE_ROOT + "/" + OLD_WORKER, CACHE_ROOT + "/" + worker
    return (("actions", "/actions"), ("work_root", "/actions/_work"),
            ("namespace", "/actions/_work/.source-image-cache"), ("source_images", CACHE_ROOT),
            ("old_worker_root", old), ("current_worker_root", current)) + tuple(
                (role + "_" + child, base + "/" + child)
                for role, base in (("old", old), ("current", current)) for child in CHILDREN)


def collect(context):
    paths = selected_paths(context["worker"])
    original_mounts = observation.read("/proc/self/mountinfo")
    mounts = observation.parse_mounts(original_mounts)
    expires = time.monotonic() + 20
    rows = {role: inspect_directory(path, mounts, expires) for role, path in paths}
    stable_mounts = observation.read("/proc/self/mountinfo") == original_mounts
    actions = rows["actions"]
    observed = "DIRECTORY_METADATA_OBSERVED_CONTENTS_UNREAD"
    approved_current_backing = None
    if stable_mounts and actions["status"] == observed:
        approved_current_backing = (actions["mount"]["mountpoint"] == "/actions" and
                                   actions["mount"]["filesystem"] in ("ext4", "xfs"))
    for row in rows.values():
        row["same_current_actions_backing"] = None
        if approved_current_backing and row["status"] == observed:
            row["same_current_actions_backing"] = (
                row["metadata"]["device"] == actions["metadata"]["device"] and
                row["mount"]["mountpoint"] == "/actions" and
                row["mount"]["mount_root_sha256"] == actions["mount"]["mount_root_sha256"])
    return dict(context, schema=1, status="EXACT_NATIVE_CACHE_METADATA_OBSERVATION",
                old_native={"run": 42851406956, "attempt": 1, "job": 129952774563,
                            "runner_id": 1537, "worker": OLD_WORKER,
                            "controller": "87b459c50a4ee8c90679a1b3756a5bb724c6d765",
                            "conclusion": "failure"},
                current_actions_mount_compatible=approved_current_backing,
                mount_snapshot_stable=stable_mounts, directories=rows,
                directory_listing=False, cache_contents_read=False, cache_writes=False,
                cache_reuse=False, missing_is_global_absence=False,
                historical_storage_generation_continuity="not_established",
                original_cache_content_provenance="not_established", cache_hits="not_observed",
                output_base_and_process_state_inspected=False,
                scope="Only six exact old/current cache leaves and six necessary directory anchors; metadata only")


def deadline(signum, frame):
    raise TimeoutError("Exact cache metadata observation deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    temporary = Path(os.environ["RUNNER_TEMP"])
    if (temporary.resolve() != temporary.absolute() or not temporary.is_relative_to("/actions/_work") or
            temporary.is_relative_to("/actions/_work/.source-image-cache") or args.output.parent != temporary or args.output.exists() or args.output.is_symlink()):
        parser.error("Require a fresh RUNNER_TEMP receipt")
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 30)
    code = 0
    try:
        result = collect(observation.identity(args.worker, os.environ, observation.marker()))
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        code = 1
        result = {"schema": 1, "status": "EXACT_NATIVE_CACHE_METADATA_OBSERVATION_FAILED",
                  "error_type": type(error).__name__, "cache_contents_read": False, "cache_writes": False}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    encoded = (json.dumps(result, sort_keys=True, indent=2) + "\n").encode()
    if len(encoded) > OUTPUT_LIMIT:
        raise ValueError("Oversized exact-path metadata receipt")
    with args.output.open("xb") as stream:
        stream.write(encoded)
    print(result["status"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
