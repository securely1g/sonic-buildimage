"""Fresh loaded-only checks of fixed original P4RT exports; never native acceptance.

Acquisition reuses native_payload_inventory.  Only the source-bound flat original
files are materialized.  Historical runtime reports stay outside the active
directory; any new runtime reports describe this verification job alone.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import native_payload_inventory as intake
from tools.bazel.ci import vs_runtime_check as runtime


INPUT_SHA256 = "dedb5f645ab0514d97d135ad1d17f66b0e242e784e4b932850d8bb9c8ee63962"
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
ORIGINAL_MEMBER_COUNT = 60
# Independently captured immutable ede072 API bodies, including original Git blob proofs.
ORIGINAL_INTERFACES = {'artifact_validation.py': 'a61840d3a8a9bd6307f55400a02ed1b9ead6b4c24786f74a479e0cebaf91d773', 'p4rt_handoff.py': 'fcbac48e6b901a9a49a3b174ba23174ae1902e5bf1ef4700971bf2f660272397', 'p4rt_image.py': '917e4e37513a775a2aa31fc319645dde2a0b7153998091a86f38998253d82fa7', 'p4rt_loaded.py': '54d5b5a11a3f6697c3f98a1b7b13b3c316ccd77eab6c2453202efea8919238c1', 'validate_runtime.py': 'b38453b8d06d53ce628f0ca8b304a8bbd6ec46e7fca183caf4d71ba4e6f532fe'}
REQUIRED_INTERFACES = {"artifact_validation.py", "p4rt_handoff.py", "p4rt_image.py",
                       "p4rt_loaded.py", "validate_runtime.py"}
LOADED_COMMAND_SECONDS = 2100
ROOT_COMMAND_SECONDS = runtime.ORIGINAL_P4RT_BODY_SECONDS + runtime.ROOT_CLEANUP_SECONDS + 30
require = intake.require


def stable_file(info):
    return intake.signature(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_fixed():
    path = Path(__file__).with_name("p4rt_original_loaded_input.json")
    with os.fdopen(os.open(path, READ_FLAGS), "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= 65536,
                "Invalid fixed loaded-input record")
        raw = stream.read(65537)
        require(stable_file(before) == stable_file(os.fstat(stream.fileno())) and
                len(raw) == before.st_size and hashlib.sha256(raw).hexdigest() == INPUT_SHA256,
                "Fixed loaded-input record differs")
    value = intake.strict(raw)
    require(value["materialized_members"] == 30 and value["declared_exports"] == 27 and
            value["canonical_acceptance"] is False and value["PR206_adoption"] is False,
            "Different loaded-only scope")
    return value


def selected_members(fixed):
    selected = {}
    forbidden = set(fixed["fresh_report_names"])
    for row in fixed["members_to_materialize"]:
        name = row["name"]
        require(type(name) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", name)
                and name not in selected and name not in forbidden,
                "Invalid or historical active member")
        require(type(row["bytes"]) is int and 0 <= row["bytes"] <= 8 * 1024**3 and
                type(row["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
                "Invalid original member identity")
        selected[name] = row
    require(len(selected) == fixed["materialized_members"] and
            sum(row["bytes"] for row in selected.values()) == fixed["materialized_total_bytes"],
            "Original materialization set differs")
    return selected


def original_zip_hash(source, expected, expires):
    digest, count = hashlib.sha256(), 0
    while True:
        require(time.monotonic() < expires, "Original ZIP authentication deadline")
        block = source.read(intake.CHUNK)
        if not block:
            break
        count += len(block)
        require(count <= expected["zip_bytes"], "Original ZIP exceeds exact size")
        digest.update(block)
    require(count == expected["zip_bytes"] and digest.hexdigest() == expected["zip_sha256"],
            "Original ZIP identity differs before materialization")
    source.seek(0)


def materialize(root, fixed, expires):
    """Copy only selected regular members, using held descriptors and exact hashes.

    The upstream collector already checked every member's CRC/hash.  Rehash the
    reopened whole ZIP, then keep that descriptor through selected CRC/hash reads
    and the final path-identity check; no intervening path-based ZIP reopen occurs.
    """
    selected = selected_members(fixed)
    expected = fixed["original_producer"]
    with os.fdopen(os.open("original.zip", READ_FLAGS, dir_fd=root), "rb") as source:
        before = os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size == expected["zip_bytes"],
                "Invalid original ZIP file")
        original_zip_hash(source, expected, expires)
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            require(len(infos) == ORIGINAL_MEMBER_COUNT, "Original member count differs")
            index = {}
            for info in infos:
                require(info.filename not in index and not info.is_dir() and
                        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", info.filename) and
                        not (info.flag_bits & 1) and
                        info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) and
                        stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG),
                        "Unexpected original ZIP member layout")
                index[info.filename] = info
            require(set(selected) <= set(index), "Selected original member missing")
            require(all(index[name].file_size == row["bytes"] for name, row in selected.items()),
                    "Selected original member size differs")
            free = os.fstatvfs(root)
            require(free.f_bavail * free.f_frsize >= fixed["materialized_total_bytes"] +
                    intake.SPACE_RESERVE, "Insufficient ephemeral materialization space")
            os.mkdir("loaded-inputs", 0o700, dir_fd=root)
            active = os.open("loaded-inputs", intake.DIRECTORY_FLAGS, dir_fd=root)
            try:
                active_identity = intake.signature(os.fstat(active))
                copied = []
                for name, row in sorted(selected.items()):
                    descriptor = os.open(name, intake.FILE_FLAGS, 0o600, dir_fd=active)
                    with os.fdopen(descriptor, "wb") as output, archive.open(index[name]) as member:
                        observed = intake.stream_exact(member, output, row["bytes"], row["sha256"], expires)
                        output.flush()
                        os.fsync(output.fileno())
                        held = os.fstat(output.fileno())
                        require(stat.S_ISREG(held.st_mode) and
                                stable_file(held) == stable_file(os.stat(name, dir_fd=active,
                                                                        follow_symlinks=False)),
                                "Materialized member path changed")
                    copied.append(dict(name=name, **observed))
                require(intake.signature(os.stat("loaded-inputs", dir_fd=root,
                                                  follow_symlinks=False)) == active_identity,
                        "Active directory changed")
            finally:
                os.close(active)
        require(stable_file(before) == stable_file(os.fstat(source.fileno())) ==
                stable_file(os.stat("original.zip", dir_fd=root, follow_symlinks=False)),
                "Original ZIP changed during materialization")
    return {"schema": 1, "status": "ORIGINAL_INPUTS_MATERIALIZED_NOT_ACCEPTED",
            "original_producer": expected, "members": copied,
            "original_member_count": ORIGINAL_MEMBER_COUNT,
            "materialized_bytes": sum(row["bytes"] for row in selected.values()),
            "historical_runtime_reports_copied": False, "fresh_runtime_validation": False,
            "canonical_native_acceptance": False, "PR206_adoption": False}


def read_record(path, limit=1024 * 1024, expected_sha256=None):
    with intake.directory(path.parent) as parent:
        with os.fdopen(os.open(path.name, READ_FLAGS, dir_fd=parent), "rb") as source:
            before = os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= limit and
                    before.st_uid == before.st_gid == 1000, "Invalid owned verification record")
            raw = source.read(limit + 1)
            require(len(raw) == before.st_size and stable_file(before) ==
                    stable_file(os.fstat(source.fileno())) ==
                    stable_file(os.stat(path.name, dir_fd=parent, follow_symlinks=False)),
                    "Verification record changed")
            require(expected_sha256 is None or hashlib.sha256(raw).hexdigest() == expected_sha256,
                    "Original validation report hash differs")
    return intake.strict(raw)


def original_sources(fixed):
    source = ROOT.with_name(ROOT.name + "-p4rt-original")
    owner = ROOT.with_name(ROOT.name + "-p4rt-owner")
    require(set(ORIGINAL_INTERFACES) == REQUIRED_INTERFACES,
            "Original source interface binding is incomplete")
    for name, expected in ORIGINAL_INTERFACES.items():
        runtime.no_links(source / "tools/bazel/ci" / name)
        runtime.checked_helper(source / "tools/bazel/ci" / name, expected)
    runtime.no_links(owner / "p4rt_app/runtime_package_test.py")
    runtime.checked_helper(owner / "p4rt_app/runtime_package_test.py",
                           fixed["original_source"]["owner_runtime_package_test_sha256"])
    # Original p4rt_loaded independently checks clean checkout Git identities
    # against validation.json before and after its installed runtime probes.
    return source, owner


def collection_path(row, temporary):
    temporary = Path(temporary)
    require(temporary.is_absolute() and str(temporary) == os.path.normpath(str(temporary)) and
            temporary.is_relative_to("/actions/_work") and
            not temporary.is_relative_to("/actions/_work/.source-image-cache"),
            "Unexpected run-attempt working directory")
    runtime.token(row["worker"], str(row["run"]), str(row["attempt"]))
    return temporary / ("native-original-p4rt-" + str(row["run"]) + "-" + str(row["attempt"]))


def require_collection(record, row, fixed):
    require(record["status"] == "ORIGINAL_P4RT_ZIP_INVENTORY_ONLY" and
            record["public_semantic_validation"] is True and
            record["public_semantic_review"] == "PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED" and
            record["canonical_native_acceptance"] is False and record["PR206_adoption"] is False,
            "Original public inventory is not consistently bound")
    for key in ("worker", "run", "attempt", "controller", "pool"):
        require(str(record[key]) == str(row[key]), "Collection belongs to another verification job")
    expected = fixed["original_producer"]
    require(record["original_artifact"] == expected["artifact_id"] and
            record["original_run"] == expected["run_id"] and
            record["original_job"] == expected["job_id"] and record["original_conclusion"] == "failure" and
            record["zip"] == {"bytes": expected["zip_bytes"], "sha256": expected["zip_sha256"]},
            "Original failed producer binding differs")


def materialize_checked(row, temporary):
    fixed = read_fixed()
    original_sources(fixed)
    directory = collection_path(row, temporary)
    require_collection(read_record(directory / "summary.json"), row, fixed)
    with intake.directory(directory) as root:
        result = materialize(root, fixed, time.monotonic() + 900)
        result["verification_job"] = row
        result["original_source"] = fixed["original_source"]
        result["fixed_input_sha256"] = INPUT_SHA256
        intake.write_json(root, "materialization.json", result)
    return result


def checked_materialization(row, temporary, fixed):
    directory = collection_path(row, temporary)
    require_collection(read_record(directory / "summary.json"), row, fixed)
    record = read_record(directory / "materialization.json")
    require(record["status"] == "ORIGINAL_INPUTS_MATERIALIZED_NOT_ACCEPTED" and
            record["verification_job"] == row and record["fixed_input_sha256"] == INPUT_SHA256 and
            record["original_producer"] == fixed["original_producer"] and
            record["original_source"] == fixed["original_source"] and
            record["canonical_native_acceptance"] is False, "Loaded input custody differs")
    expected = [{"name": r["name"], "bytes": r["bytes"], "sha256": r["sha256"]}
                for r in sorted(fixed["members_to_materialize"], key=lambda r: r["name"])]
    require(record["members"] == expected, "Materialized member identities differ")
    active = directory / "loaded-inputs"
    runtime.no_links(active)
    with intake.directory(active) as root:
        for name in fixed["fresh_report_names"]:
            require(not os.path.lexists(active / name), "Preserve existing runtime report")
        for member in expected:
            info = os.stat(member["name"], dir_fd=root, follow_symlinks=False)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 1000 and
                    info.st_size == member["bytes"], "Materialized input type or identity differs")
    for member in fixed["members_to_materialize"]:
        if member["role"] == "original_validation_report":
            read_record(active / member["name"], expected_sha256=member["sha256"])
    # The exact original validator hashes all 27 payload exports against the
    # authenticated validation.json before loading either Docker archive.
    return active


def loaded_error_reason(raw):
    """Project only authored original-validator reasons, never tool text or paths."""
    require(type(raw) is bytes and len(raw) <= runtime.CAP, "Oversized loaded diagnostic")
    text = raw.decode("utf-8", "replace")
    if "Traceback (most recent call last):\n" not in text:
        return None
    known = {
        "P4RT static validation did not pass": "original_static_report_not_passed",
        "Loaded checkout differs from the built source": "original_source_revision_mismatch",
        "The installed DWP checks must use the validated source owner": "original_owner_helper_mismatch",
        "Missing source-owned database configuration": "database_fixture_missing",
        "Loaded P4RT platform differs": "loaded_platform_mismatch",
        "Loaded layers differ from the validated OCI image": "loaded_layers_mismatch",
        "Missing loaded P4RT receipt": "loaded_runtime_receipt_missing",
        "Image source changed during loaded checks": "original_source_changed",
    }
    for line in reversed(text.splitlines()[-64:]):
        if len(line) > 8192:
            continue
        match = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}: (.*)", line)
        if not match:
            continue
        message = match.group(1)
        if message in known:
            return known[message]
        for image, reason in (("docker-sonic-p4rt", "installed_runtime_probe_failed"),
                              ("docker-sonic-p4rt-dbg", "installed_debug_probe_failed")):
            if message == image + " installed probe failed:":
                return reason
    return None


def loaded_failure_evidence(state, error):
    result = {"status": "failed", "stage": "original-p4rt-loaded",
              "failure": runtime.safe_failure(error), "canonical_native_acceptance": False}
    path = state / "original-p4rt-loaded.stderr"
    try:
        with os.fdopen(os.open(path, READ_FLAGS), "rb") as source:
            before = os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= runtime.CAP,
                    "Invalid owned loaded diagnostic")
            raw = source.read(runtime.CAP + 1)
            require(len(raw) == before.st_size and stable_file(before) ==
                    stable_file(os.fstat(source.fileno())) == stable_file(path.lstat()),
                    "Owned loaded diagnostic changed")
        result["stderr"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                            "authored_guard_reason": loaded_error_reason(raw)}
    except (OSError, ValueError):
        result["stderr"] = {"available_for_safe_projection": False}
    return result


def loaded_workload(docker, client_env, paths):
    """Execute only the original installed-image validator at the private endpoint."""
    fixed = read_fixed()
    row = paths["loaded_verification_job"]
    source, owner = original_sources(fixed)
    active = checked_materialization(row, paths["state"].parent, fixed)
    require(client_env["DOCKER_HOST"] == "unix://" + str(paths["exec"] / "docker.sock") and
            client_env["DOCKER_CONFIG"] == str(paths["state"] / "client"),
            "Loaded validator private endpoint differs")
    temporary = paths["state"] / "loaded-tmp"
    require(not os.path.lexists(temporary), "Preserve existing loaded fixture state")
    temporary.mkdir(mode=0o700)
    os.chown(temporary, 1000, 1000)
    environment = dict(client_env, PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                       TMPDIR=str(temporary),
                       GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
                       GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
                       GIT_CONFIG_KEY_0="protocol.allow", GIT_CONFIG_VALUE_0="never")
    try:
        runtime.bounded([sys.executable, "-B", str(source / "tools/bazel/ci/p4rt_loaded.py"),
                         "--directory", str(active), "--owner-source", str(owner)],
                        paths["state"], "original-p4rt-loaded", LOADED_COMMAND_SECONDS,
                        environment, worker_user=True)
    except BaseException as error:
        return loaded_failure_evidence(paths["state"], error)
    with os.scandir(temporary) as entries:
        require(next(entries, None) is None, "Loaded validator left an owned fixture directory")
    reports = []
    for name in fixed["fresh_report_names"]:
        value = read_record(active / name, 256 * 1024)
        intake.public_artifacts.safe_json(value)
        image = name.removesuffix(".runtime.json")
        archive = next(r for r in fixed["members_to_materialize"] if r["name"] == image + ".gz")
        require(value["status"] == "passed" and value["config_and_layers_verified"] is True and
                value["revision"] == fixed["original_source"]["built_revision"] and
                value["owner_revision"] == fixed["original_source"]["owner_revision"] and
                value["archive_sha256"] == archive["sha256"], "Fresh loaded result differs")
        reports.append({"file": name, "image": image, "result": value})
    return {"status": "passed", "scope": "fresh_loaded_runtime_of_original_payloads_only",
            "verification_job": row, "original_producer": fixed["original_producer"],
            "reports": reports, "historical_SDK_reconstructed": False,
            "Bazel_build_or_test": False, "DEB_generation": False,
            "canonical_native_acceptance": False, "PR206_adoption": False}


def run_loaded(row, temporary):
    fixed = read_fixed()
    original_sources(fixed)
    checked_materialization(row, temporary, fixed)
    paths = runtime.state_paths(row, temporary)
    runtime.no_links(paths["state"])
    require(not os.path.lexists(paths["state"]) and not os.path.lexists(paths["exec"]),
            "Preserve existing private verification state")
    observed = runtime.inspection.collect(row["worker"])
    runtime.reviewed_observation(observed)
    paths["state"].mkdir(mode=0o700)
    output = Path(temporary) / ("p4rt-original-loaded-" + row["run"] + "-" + row["attempt"] + ".json")
    require(not os.path.lexists(output), "Preserve existing loaded-only result")
    value = dict(schema=1, status="FAILED_NOT_ACCEPTED", verification_job=row,
                 scope="fresh_loaded_runtime_and_cleanup_of_original_failed_P4RT_payloads",
                 original_producer=fixed["original_producer"], original_source=fixed["original_source"],
                 native_controller=runtime.ORIGINAL_P4RT_NATIVE_CONTROLLER,
                 canonical_native_acceptance=False, PR206_adoption=False,
                 historical_SDK_reconstructed=False, Bazel_build_or_test=False, DEB_generation=False)
    def interrupted(signum, frame):
        raise runtime.ProbeFailure("Loaded-only verification interrupted")
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        data = runtime.bounded(["sudo", "-n", sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                                "root", "--worker", row["worker"], "--run", row["run"],
                                "--attempt", row["attempt"], "--controller", row["controller"],
                                "--temporary", str(temporary)], paths["state"], "private-p4rt-loaded",
                               ROOT_COMMAND_SECONDS, root_supervisor=True)
        private = intake.strict(data)
        value["private_docker"] = private
        require(private["status"] == "passed" and private["daemon_started"] is True and
                private["daemon_closed"] is True and private["native_stop"]["status"] == "passed" and
                private["native_stop"]["controller"] == runtime.ORIGINAL_P4RT_NATIVE_CONTROLLER and
                private["original_p4rt_loaded"]["status"] == "passed", "Loaded-only lifecycle did not pass")
        value["status"] = "ORIGINAL_P4RT_LOADED_AND_CLEANUP_PASSED_NOT_ACCEPTED"
    except BaseException as error:
        value["failure"] = runtime.safe_failure(error)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        with intake.directory(output.parent) as parent:
            intake.write_json(parent, output.name, value)
    return value


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "materialize", "validate", "root"))
    parser.add_argument("--worker", required=True)
    parser.add_argument("--run")
    parser.add_argument("--attempt")
    parser.add_argument("--controller")
    parser.add_argument("--temporary")
    args = parser.parse_args()
    if args.action == "root":
        require(args.run and args.attempt and args.temporary and
                re.fullmatch(r"[0-9a-f]{40}", args.controller or ""), "Incomplete loaded-only root scope")
        row = dict(worker=args.worker, run=args.run, attempt=args.attempt,
                   controller=args.controller, pool=runtime.inspection.POOL)
        paths = runtime.state_paths(row, args.temporary)
        paths.update(original_p4rt_loaded=True, loaded_verification_job=row)
        value = runtime.private_daemon(paths, p4rt_workload=loaded_workload)
        print(json.dumps(value, sort_keys=True))
        return 0  # The parent preserves the complete result and checks its status.
    require(os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 1000 and
            not any((args.run, args.attempt, args.controller, args.temporary)), "Unexpected worker invocation")
    row = runtime.identity(args.worker, os.environ)
    original_sources(read_fixed())
    if args.action == "preflight":
        runtime.reviewed_observation(runtime.inspection.collect(args.worker))
        print("ORIGINAL_P4RT_SOURCE_AND_RESOURCE_PREFLIGHT_ONLY")
        return 0
    if args.action == "materialize":
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        bounds = [1024**3] + [x for x in (soft, hard) if x != resource.RLIM_INFINITY]
        resource.setrlimit(resource.RLIMIT_AS, (min(bounds), min(bounds)))
        signal.signal(signal.SIGALRM, intake.deadline)
        signal.setitimer(signal.ITIMER_REAL, 900)
        try:
            materialize_checked(row, os.environ["RUNNER_TEMP"])
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        print("ORIGINAL_INPUTS_MATERIALIZED_NOT_ACCEPTED")
        return 0
    value = run_loaded(row, os.environ["RUNNER_TEMP"])
    print(value["status"])
    return 0 if value["status"] == "ORIGINAL_P4RT_LOADED_AND_CLEANUP_PASSED_NOT_ACCEPTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
