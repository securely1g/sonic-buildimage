#!/usr/bin/env python3
"""One opt-in diagnostic continuation of the recorded failed native attempt.

This never produces a passing normal native receipt or enters normal Bazel
package/installer phases. Native Make can invoke its existing audited TAR
bridge. This accepts no arbitrary workspace, source delta, or build command.
Inspect writes only its requested job evidence, never the inspected build area.
"""

import argparse
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import time
from types import SimpleNamespace
import uuid

MANIFEST = json.loads(Path(__file__).with_suffix(".json").read_text())
SUPPORT = Path(os.environ.get("SONIC_RECOVERY_SUPPORT", str(Path(__file__).parent))).absolute()


def load_support(name):
    """Load only the reviewed PR2 helper bytes, never script-directory shadows."""
    path = SUPPORT / (name + ".py")
    if (SUPPORT.resolve(strict=True) != SUPPORT or path.resolve(strict=True) != path or
            hashlib.sha256(path.read_bytes()).hexdigest() != MANIFEST["support_sha256"][path.name]):
        raise ValueError("pinned PR2 helper differs: " + name)
    spec = importlib.util.spec_from_file_location("recovery_support_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_area = load_support("build_area")
native_build = load_support("native_build")
source_workspace = load_support("source_workspace")


CONFIG = ("SONIC_CONFIG_MAKE_JOBS = 2\n"
          "export MONIT_SOURCE_METHOD = debian\n"
          "export RASDAEMON_SOURCE_METHOD = debian\n"
          "export P4LANG_PI_SOURCE_METHOD = github\n")
OLD_HELPER = "bazel/python_test.py"
NEW_HELPER = "bazel/python_test_runner.py"
REFERENCES = {"bazel/BUILD.bazel": 1, "misc/BUILD.bazel": 2}
OLD_INNER = "fsroot.docker.trixie"
require = native_build.require
git = source_workspace._git


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def job_evidence(path, value):
    # Keep the initial reservation readable if cancellation interrupts an update.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with temporary.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def real(path, directory=False):
    require(path.resolve(strict=True) == path and not path.is_symlink(),
            "retained path is linked: " + str(path))
    require(path.is_dir() if directory else path.is_file(), "unexpected retained path type: " + str(path))
    return path


def paths(manifest):
    root = Path(manifest["build_root"])
    workspace, state = root / "checkout", root / "state"
    return root, workspace, state, state / ("native-" + manifest["invocation"])


def cache_inventory(path, *, entry_limit=10000, seconds=3, depth_limit=32, child_limit=100):
    """Read metadata only, following neither symlinks nor other filesystems."""
    result = {"path": str(path), "scope": "metadata only; no symlinks followed; one filesystem",
              "limits": {"entries": entry_limit, "seconds": seconds, "depth": depth_limit,
                         "reported_immediate_children": child_limit},
              "entries_observed": 0, "files": 0, "directories": 0, "symlinks": 0, "other": 0,
              "regular_file_bytes": 0, "regular_file_allocated_bytes": 0,
              "immediate_children_observed": 0, "immediate_children": [],
              "owner_counts": {}, "mode_counts": {}, "host_unwritable_entries": 0,
              "errors": [], "complete": True}
    deadline = time.monotonic() + seconds
    seen_files = set()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def limited(reason):
        result["complete"] = False
        if reason not in result.setdefault("limitations", []):
            result["limitations"].append(reason)

    def error(relative, exception):
        limited("metadata errors")
        if len(result["errors"]) < 16:
            result["errors"].append({"path": relative, "error": type(exception).__name__})

    def walk(descriptor, relative, depth, device):
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if result["entries_observed"] >= entry_limit:
                    limited("entry limit")
                    return
                if time.monotonic() >= deadline:
                    limited("time limit")
                    return
                result["entries_observed"] += 1
                name = relative + "/" + entry.name if relative else entry.name
                if not relative:
                    result["immediate_children_observed"] += 1
                try:
                    details = entry.stat(follow_symlinks=False)
                    mode = details.st_mode
                    kind = ("directories" if stat.S_ISDIR(mode) else "files" if stat.S_ISREG(mode)
                            else "symlinks" if stat.S_ISLNK(mode) else "other")
                    result[kind] += 1
                    owner, permissions = f"{details.st_uid}:{details.st_gid}", oct(stat.S_IMODE(mode))
                    result["owner_counts"][owner] = result["owner_counts"].get(owner, 0) + 1
                    result["mode_counts"][permissions] = result["mode_counts"].get(permissions, 0) + 1
                    access = os.access(entry.name, os.W_OK | (os.X_OK if kind == "directories" else 0),
                                       dir_fd=descriptor, follow_symlinks=False)
                    result["host_unwritable_entries"] += not access
                    if not relative and len(result["immediate_children"]) < child_limit:
                        result["immediate_children"].append({
                            "name": entry.name, "kind": kind, "uid": details.st_uid,
                            "gid": details.st_gid, "mode": permissions,
                            "host_writable_and_searchable_if_directory": access})
                    identity = (details.st_dev, details.st_ino)
                    if kind == "files" and identity not in seen_files:
                        seen_files.add(identity)
                        result["regular_file_bytes"] += details.st_size
                        result["regular_file_allocated_bytes"] += details.st_blocks * 512
                    if kind == "directories":
                        if details.st_dev != device:
                            limited("other filesystem")
                        elif depth >= depth_limit:
                            limited("depth limit")
                        else:
                            child = os.open(entry.name, flags, dir_fd=descriptor)
                            try:
                                current = os.fstat(child)
                                require((current.st_dev, current.st_ino) == identity,
                                        "directory changed during inspection")
                                walk(child, name, depth + 1, device)
                            finally:
                                os.close(child)
                except (OSError, ValueError) as exception:
                    error(name, exception)
    try:
        real(path, directory=True)
        descriptor = os.open(path, flags)
        try:
            walk(descriptor, "", 0, os.fstat(descriptor).st_dev)
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as exception:
        error(".", exception)
    result["immediate_children"].sort(key=lambda item: item["name"])
    result["immediate_child_names_complete"] = (
        result["complete"] and result["immediate_children_observed"] <= child_limit)
    return result


def inspect(manifest):
    """Gather independent path results without lockfiles, Docker, or Make."""
    root, workspace, state, native = paths(manifest)
    names = (root.parent, root, workspace, state, native, workspace / "target", native / "docker-data",
             native / "native-cache", native / "native-cache/bazel", native / "native-cache/vcache",
             state / ("bazel-source-" + manifest["invocation"]), state / "native-diagnostic",
             Path("/data/sonic-runner/cache/sonic-buildimage/vs-installer/state/repository-cache"),
             Path("/data/sonic-runner/cache/sonic-buildimage/vs-installer/state/package-cache"))
    result = {"schema": 1, "diagnostic_only": True, "mode": "inspect",
              "observed_at_unix": time.time(), "prior_run": manifest["run_id"],
              "executing_uid": os.getuid(), "executing_gid": os.getgid(), "paths": []}
    for path in names:
        record = {"path": str(path)}
        try:
            real(path, directory=True)
            details = path.stat()
            record.update(uid=details.st_uid, gid=details.st_gid,
                          mode=oct(stat.S_IMODE(details.st_mode)), device=details.st_dev)
            record["writable_and_searchable"] = os.access(path, os.W_OK | os.X_OK)
            record["available_bytes"] = shutil.disk_usage(path).free
            mount = subprocess.run(["findmnt", "--json", "--output", "SOURCE,FSTYPE,TARGET,OPTIONS",
                                    "--target", str(path)], check=True, capture_output=True,
                                   text=True, timeout=10)
            record["mount"] = json.loads(mount.stdout)["filesystems"]
            record["fresh_300_gib_gate"] = record["available_bytes"] >= build_area.MIN_FREE
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError) as error:
            record["error"] = type(error).__name__ + ": " + str(error)
        result["paths"].append(record)
    result["native_cache_inventory"] = [
        cache_inventory(path) for path in
        (native / "native-cache", native / "native-cache/bazel", native / "native-cache/vcache")]
    # Hash only known public build evidence, not arbitrary host files.
    result["evidence"] = {}
    for name, expected in manifest["evidence"].items():
        try:
            path = real(workspace / "artifacts" / name)
            actual = digest(path)
            result["evidence"][name] = {"sha256": actual, "matches": actual == expected}
        except (OSError, ValueError, RuntimeError) as error:
            result["evidence"][name] = {"error": type(error).__name__ + ": " + str(error)}
    return result


@contextmanager
def exclusive(root):
    # Lock the existing directory inode; no lockfile can alias another path.
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = root.stat()
        locked = os.fstat(descriptor)
        require((current.st_dev, current.st_ino) == (locked.st_dev, locked.st_ino),
                "retained build root was replaced")
        yield
    finally:
        os.close(descriptor)


def audit_support(manifest):
    """Keep only the proven untracked old Docker tree outside source status."""
    _, workspace, _, _ = paths(manifest)
    real(workspace / OLD_INNER, directory=True)
    for revision in (manifest["source_commit"], "HEAD"):
        require(not git(workspace, "ls-tree", "-r", revision, "--", OLD_INNER).strip(),
                "old Docker subtree contains captured or current tracked entries")
    require(not git(workspace, "ls-files", "--stage", "--", OLD_INNER).strip(),
            "old Docker subtree contains staged entries")
    # A separate pinned module instance retains the original status parser and
    # metadata checks without globally changing normal source_workspace helpers.
    support = load_support("source_workspace")
    original_git = support._git
    def scoped_git(directory, *arguments, **kwargs):
        if directory == workspace and arguments[0] == "status":
            arguments += ("--", ".", ":(top,exclude,literal)" + OLD_INNER)
        return original_git(directory, *arguments, **kwargs)
    support._git = scoped_git
    return support


def evidence(manifest):
    root, workspace, state, native = paths(manifest)
    for name, expected in manifest["evidence"].items():
        require(digest(real(workspace / "artifacts" / name)) == expected,
                "prior evidence changed: " + name)
    source = json.loads((workspace / "artifacts/image/image-receipt.json").read_text())
    previous = json.loads((workspace / "artifacts/image/native-receipt.json").read_text())
    require(source["source_commit"] == manifest["source_commit"] and source["status"] == "failed",
            "unexpected previous image source/status")
    require(source["components"]["src/sonic-dash-api"]["commit"] == manifest["dash_commit"],
            "recorded DASH source differs")
    require(previous["status"] == "failed" and previous["worker_removed"] is True and
            all(previous[key] == manifest[key] for key in ("source_commit", "invocation", "worker_image")),
            "unexpected previous native attempt")
    spec = real(state / ("worker-" + manifest["invocation"]) / "execution-environment.json")
    worker = json.loads(spec.read_text())
    require(worker["worker_image"] == manifest["worker_image"] and
            worker["platform"] == "linux/amd64" and worker["distribution"] == "trixie",
            "retained worker specification changed")
    require(real(workspace / "rules/config.user").read_text() == CONFIG,
            "native configuration changed")
    require(real(workspace / ".platform").read_text().strip() == "vs" and
            real(workspace / ".arch").read_text().strip() == "amd64", "native platform changed")
    require(git(workspace, "show", "-s", "--format=%ct", manifest["source_commit"]).strip() ==
            manifest["source_date_epoch"], "original epoch changed")
    directories = source_workspace._check_graph(workspace, source_workspace._expected(source))
    old = json.loads((workspace / "artifacts/image/native-source-audit.json").read_text())
    support = audit_support(manifest)
    modifications = {}
    for name, directory in directories.items():
        # Ignore uploaded evidence itself; it was finalized after the old audit.
        def relevant(changes):
            return [item for item in changes if not (name == "." and
                    (item["path"].startswith("artifacts/") or
                     item["path"] == OLD_INNER or item["path"].startswith(OLD_INNER + "/")))]
        current = relevant(support._changes(directory, ignored=False))
        require(current == relevant(old["repositories"][name]["changes"]),
                "retained source status/metadata changed: " + name)
        # The old audit lacks content hashes. Capture today's modified files
        # honestly, without claiming their bytes were proven by the old audit.
        modifications[name] = {item["path"]: digest(real(directory / item["path"]))
                               for item in current if item["kind"] == "file" and
                               item["index_status"] != "?"}
    return source, modifications


def audit_exclusion(manifest):
    _, workspace, _, _ = paths(manifest)
    old = json.loads((workspace / "artifacts/image/native-source-audit.json").read_text())
    entries = [item for item in old["repositories"]["."]["changes"]
               if item["path"] == OLD_INNER or item["path"].startswith(OLD_INNER + "/")]
    require(all(item["index_status"] == item["worktree_status"] == "?" for item in entries),
            "prior Docker subtree audit contains source changes")
    return {"path": OLD_INNER, "prior_untracked_metadata_count": len(entries),
            "limit": "Preserved Docker data is excluded from source status; original checkout ownership "
                     "restoration could clear its setuid/setgid modes. No cache content validation claimed."}


def owner_overlay(checkout, commit, manifest):
    """Accept precisely the owner rename and its three BUILD references."""
    require(re.fullmatch(r"[0-9a-f]{40}", commit or ""), "an exact DASH owner commit is required")
    require(commit == manifest["reviewed_overlay_commit"], "DASH commit is not the reviewed overlay")
    real(checkout, directory=True)
    require(git(checkout, "rev-parse", "HEAD").strip() == commit, "DASH owner checkout differs")
    require(not source_workspace._changes(checkout), "DASH owner checkout must be clean")
    old = manifest["dash_commit"]
    require(git(checkout, "rev-parse", "HEAD^").strip() == old, "DASH fix must directly follow recorded owner")
    changed = set(git(checkout, "diff", "--name-only", "--no-renames", old, commit).splitlines())
    require(changed == {OLD_HELPER, NEW_HELPER, *REFERENCES}, "unexpected DASH source-owner delta")
    contents, hashes = {}, {}
    for name in [OLD_HELPER, *REFERENCES]:
        before = git(checkout, "show", old + ":" + name).encode()
        target = NEW_HELPER if name == OLD_HELPER else name
        after = git(checkout, "show", commit + ":" + target).encode()
        require(git(checkout, "ls-tree", old, name).split()[0] ==
                git(checkout, "ls-tree", commit, target).split()[0], "DASH file mode changed")
        if name == OLD_HELPER:
            require(after == before, "DASH launcher contents changed")
        else:
            require(before.count(b"python_test.py") == REFERENCES[name] and
                    after == before.replace(b"python_test.py", b"python_test_runner.py"),
                    "unexpected DASH BUILD change: " + name)
        contents[target] = after
        hashes[name] = {"new_path": target, "before": hashlib.sha256(before).hexdigest(),
                        "after": hashlib.sha256(after).hexdigest()}
    return contents, {"repository": "securely1g/sonic-dash-api", "old_commit": old,
                      "owner_commit": commit, "files": hashes}


def apply_overlay(workspace, contents, overlay):
    dash = workspace / "src/sonic-dash-api"
    for name, record in overlay["files"].items():
        path = real(dash / name)
        require(path.stat().st_nlink == 1 and digest(path) == record["before"],
                "DASH overlay input is shared or differs: " + name)
    require(not (dash / NEW_HELPER).exists() and not (dash / NEW_HELPER).is_symlink(),
            "DASH rename destination already exists")
    # Save original bytes in the diagnostic plan, not in the source checkout.
    (dash / OLD_HELPER).rename(dash / NEW_HELPER)
    for name in REFERENCES:
        (dash / name).write_bytes(contents[name])


def cache_selection(manifest):
    _, _, state, native = paths(manifest)
    fresh = state / "native-diagnostic/native-cache"
    return {"original": str(native / "native-cache"),
            "diagnostic": str(fresh),
            "method": "none", "method_override": "none",
            "original_cache_modified": False,
            "derived_bazel_cache": str(fresh / "bazel"),
            "derived_version_cache": str(fresh / "vcache"),
            "prior_empty_data_evidence": manifest["cache_inspection"],
            "cache_scope": "Fresh diagnostic caches; original derived caches contain no file data. "
                           "No prior Bazel result hits are available to reuse.",
            "generated_config_change": "The derived version-cache path changes "
                                       "src/sonic-build-hooks/buildinfo/config/buildinfo.config; "
                                       "the final source audit records that native-generated change."}


def derived_cache_inventories(manifest):
    _, _, _, native = paths(manifest)
    real(native / "native-cache", directory=True)
    result = []
    for name in ("bazel", "vcache"):
        path = native / "native-cache" / name
        try:
            path.lstat()
        except FileNotFoundError:
            result.append({"path": str(path), "missing": True})
            continue
        inventory = cache_inventory(path)
        require(inventory["complete"] and not any(
            inventory[key] for key in ("files", "symlinks", "other")),
            "old derived cache has data or an incomplete inventory: " + name)
        result.append(inventory)
    return result


def generated_config_snapshot(workspace):
    relative = "src/sonic-build-hooks/buildinfo/config/buildinfo.config"
    path = workspace / relative
    return {"path": relative, "sha256": digest(real(path)) if path.exists() or path.is_symlink() else None}


def recorded_command(manifest, inner_root):
    _, workspace, state, _ = paths(manifest)
    require(inner_root == state / "native-diagnostic/inner-docker-data",
            "unexpected diagnostic inner Docker root")
    log = real(workspace / "artifacts/image/native-build.log")
    require(digest(log) == manifest["evidence"]["image/native-build.log"], "native command log changed")
    records = [json.loads(line) for line in log.read_text().splitlines()
               if line.startswith('{"native_stage":')]
    require([item["native_stage"] for item in records] == ["init", "configure", "bazel-vs-native-inputs"],
            "unexpected original native stage sequence")
    argv = records[-1]["argv"]
    require(argv[:8] == ["runuser", "--preserve-environment", "--user", native_build.USER, "--",
                         "make", "-f", "Makefile.work"] and argv[-1] == "bazel-vs-native-inputs",
            "unexpected original native command")
    require("SOURCE_DATE_EPOCH=" + manifest["source_date_epoch"] in argv and
            not any(value.startswith("DOCKER_ROOT=") for value in argv), "native options changed")
    cache = cache_selection(manifest)
    for key, value in (("SONIC_DPKG_CACHE_SOURCE", cache["original"]),
                       ("SONIC_DPKG_CACHE_METHOD", "none"),
                       ("SONIC_DPKG_CACHE_METHOD_OVERRIDE", "none")):
        matches = [item for item in argv if re.match(re.escape(key) + r"(?:[^A-Za-z0-9_]|$)", item)]
        require(matches == [key + "=" + value], "recorded native cache option changed: " + key)
    require(not any(re.match(r"SONIC_(?:BAZEL_CACHE_SOURCE|VERSION_CACHE(?:_METHOD|_SOURCE)?)(?:[^A-Za-z0-9_]|$)",
                             item) for item in argv), "recorded derived cache options changed")
    old = "SONIC_DPKG_CACHE_SOURCE=" + cache["original"]
    changed = ["SONIC_DPKG_CACHE_SOURCE=" + cache["diagnostic"] if item == old else item
               for item in argv[:-1]]
    return changed + ["DOCKER_ROOT=" + str(inner_root), argv[-1]]


def docker_environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}
    env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    return env


def docker(argv, **kwargs):
    return subprocess.run(["docker", *argv], check=True, text=True, env=docker_environment(), **kwargs)


def owned(name, invocation, manifest, missing=False):
    result = subprocess.run(["docker", "--host=unix:///var/run/docker.sock", "inspect", name],
                            text=True, capture_output=True, timeout=30, env=docker_environment())
    if missing and result.returncode and ("No such" in result.stderr or "no such" in result.stderr):
        return None
    result.check_returncode()
    info = json.loads(result.stdout)[0]
    require(info["Config"].get("Labels", {}).get(native_build.LABEL) == invocation and
            info["Config"]["Image"] == manifest["worker_image"] and info["HostConfig"]["Privileged"] and
            info["HostConfig"]["CgroupnsMode"] == "private", "foreign diagnostic worker")
    root = manifest["build_root"]
    mounts = info["Mounts"]
    require(len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["Source"] == root and
            mounts[0]["Destination"] == root and mounts[0]["RW"], "diagnostic worker mounts changed")
    return info


def stop_owned(name, invocation, manifest):
    info = owned(name, invocation, manifest, missing=True)
    if info is None:
        return
    if info["State"]["Running"]:
        docker(["stop", "--time", "45", info["Id"]], timeout=60)
    require(not owned(info["Id"], invocation, manifest)["State"]["Running"], "diagnostic worker did not stop")
    docker(["rm", info["Id"]], timeout=30)


def job_identity():
    fields = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_REPOSITORY",
              "GITHUB_SHA", "GITHUB_WORKFLOW_REF")
    identity = {field: os.environ.get(field, "") for field in fields}
    require(all(identity.values()) and
            all(re.fullmatch(r"[1-9][0-9]*", identity[field])
                for field in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")) and
            re.fullmatch(r"[0-9a-f]{40}", identity["GITHUB_SHA"]) and
            identity["GITHUB_REPOSITORY"] == "securely1g/sonic-buildimage" and
            identity["GITHUB_WORKFLOW_REF"].startswith(
                "securely1g/sonic-buildimage/.github/workflows/runner-manager.yml@refs/"),
            "diagnostic resume/cleanup requires its GitHub job identity")
    return identity


def cleanup_job(output, cleanup_output, manifest):
    """Independent, bounded cleanup after a killed parent; never adopt another job."""
    identity = job_identity()
    require(not cleanup_output.exists() and not cleanup_output.is_symlink(),
            "cleanup evidence already exists")
    result = {"schema": 1, "diagnostic_only": True, "job_identity": identity}
    try:
        if not output.exists():
            result["skipped"] = "this job reserved no diagnostic invocation"
        else:
            record = json.loads(real(output).read_text())
            if record.get("status") == "diagnostic-rejected":
                require(record.get("job_identity") == identity, "rejected evidence belongs to another job")
                result["skipped"] = "this job reserved no diagnostic invocation"
            else:
                require(record.get("job_identity") == identity and record.get("manifest") == manifest and
                        record.get("diagnostic_only") is True and record.get("clean_checkout_proof") is False,
                        "diagnostic evidence belongs to another job or attempt")
                invocation = record["invocation"]
                require(re.fullmatch(r"[0-9a-f]{16}", invocation), "invalid diagnostic invocation")
                root, _, state, _ = paths(manifest)
                with exclusive(real(root, directory=True)):
                    plan = json.loads(real(state / "native-diagnostic/plan.json").read_text())
                    require(plan["job_identity"] == identity and plan["invocation"] == invocation and
                            plan["manifest"] == manifest, "retained diagnostic plan belongs to another job")
                    stop_owned("sonic-native-diagnostic-" + invocation, invocation, manifest)
                result.update(invocation=invocation, worker_removed=True)
    except Exception as error:
        result["error"] = str(error)
    cleanup_output.write_text(json.dumps(result, indent=2) + "\n")
    return 1 if "error" in result else 0


def validate_host(manifest):
    root, workspace, state, native = paths(manifest)
    owners = manifest["inspected_ownership"]
    runner = owners["runner"]
    require([os.getuid(), os.getgid()] == runner, "executing identity differs from inspected runner")
    for path in (root.parent, root, workspace, state, native, native / "docker-data", workspace / "target",
                 state / ("bazel-source-" + manifest["invocation"])):
        real(path, directory=True)
    require(root.parent.stat().st_uid == os.getuid() and stat.S_IMODE(root.parent.stat().st_mode) == 0o700,
            "retained attempt must have its private runner-owned parent")
    # Compare the observed mixed layout; never repair retained ownership.
    for expected, directories in (
            (runner, (root.parent, root, workspace, state, workspace / "target")),
            (owners["native"], (native, real(native / "native-cache", directory=True),
                               state / ("bazel-source-" + manifest["invocation"]))),
            (owners["docker"], (native / "docker-data",))):
        for path in directories:
            details = path.stat()
            require([details.st_uid, details.st_gid] == expected,
                    "retained owner differs from inspection: " + str(path))
    free = build_area.check_filesystem(root)
    require(not (state / "native-diagnostic").exists() and not (state / "native-diagnostic").is_symlink(),
            "this retained attempt already has a diagnostic operation")
    require(not docker(["ps", "-aq", "--no-trunc", "--filter", "volume=" + str(root)],
                       capture_output=True, timeout=30).stdout.strip(),
            "a container already uses this retained build root")
    info = json.loads(docker(["image", "inspect", manifest["worker_image"]],
                             capture_output=True, timeout=30).stdout)[0]
    require(info["Id"] == manifest["worker_image"] and info["Architecture"] == "amd64" and
            info["Os"] == "linux", "recorded immutable worker is unavailable")
    return free


def resume(manifest, checkout, commit, output, admission=None):
    identity = job_identity()
    root, workspace, state, native = paths(manifest)
    real(root, directory=True)
    with exclusive(root):
        free = validate_host(manifest)
        source, modified = evidence(manifest)
        contents, overlay = owner_overlay(checkout, commit, manifest)
        cache_before = derived_cache_inventories(manifest)
        invocation = uuid.uuid4().hex[:16]
        operation = state / "native-diagnostic"
        # Reject unexpected cache/Make options before reserving or changing sources.
        recorded_command(manifest, operation / "inner-docker-data")
        operation.mkdir(mode=0o755)  # One-shot, exclusive; never adopt old evidence.
        cache = Path(cache_selection(manifest)["diagnostic"])
        cache.mkdir(mode=0o700)
        for child in ("bazel", "vcache"):
            (cache / child).mkdir(mode=0o700)
        report = {"schema": 1, "diagnostic_only": True, "clean_checkout_proof": False,
                  "status": "diagnostic-running", "mode": "resume-native",
                  "job_identity": identity,
                  "manifest": manifest, "source": source, "accepted_overlay": overlay,
                  "modified_file_hashes_before": modified, "invocation": invocation,
                  "available_bytes_before_resume": free,
                  "admission": admission,
                  "execution_identity": execution_identity(manifest),
                  "native_cache": cache_selection(manifest),
                  "preserved_cache_inventory_before": cache_before,
                  "generated_config_before": generated_config_snapshot(workspace),
                  "source_audit_exclusion": audit_exclusion(manifest),
                  "source_audit_limit": "Prior native audit has metadata, not modified-file content hashes.",
                  "inner_docker_root": str(operation / "inner-docker-data")}
        plan = operation / "plan.json"
        plan.write_text(json.dumps(report, indent=2) + "\n")
        # Reserve this invocation outside the retained area before Docker create.
        # An independent always-run step can clean it after the parent is killed.
        job_evidence(output, report)
        for name in ("native_recovery.py", "native_recovery.json"):
            shutil.copyfile(Path(__file__).with_name(name), operation / name)
        for name in MANIFEST["support_sha256"]:
            shutil.copyfile(SUPPORT / name, operation / name)
        report["driver_hashes"] = {path.name: digest(path) for path in operation.glob("*.py")}
        report["support_commit"] = MANIFEST["support_commit"]
        (operation / "original-overlay-files").mkdir()
        for name in overlay["files"]:
            destination = operation / "original-overlay-files" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(workspace / "src/sonic-dash-api" / name, destination)
        name = "sonic-native-diagnostic-" + invocation
        process = None
        try:
            apply_overlay(workspace, contents, overlay)
            command = ["create", "--name", name, "--label", native_build.LABEL + "=" + invocation,
                       "--privileged", "--init", "--cgroupns=private", "--cpus=4", "--memory=16g",
                       "--memory-swap=16g", "--mount", "type=bind,src=" + str(root) + ",dst=" + str(root),
                       "--env", "SONIC_NATIVE_RECOVERY_INVOCATION=" + invocation,
                       "--workdir", str(workspace), manifest["worker_image"],
                       "python3", "-B", str(operation / "native_recovery.py"),
                       "--inside-worker", str(plan)]
            docker(command, capture_output=True, timeout=60)
            info = owned(name, invocation, manifest)
            with (operation / "native-resume.log").open("x") as log:
                process = subprocess.Popen(["docker", "--host=unix:///var/run/docker.sock", "start",
                                            "--attach", info["Id"]], stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True,
                                           env=docker_environment())
                require(process.wait() == 0, "diagnostic native worker failed; see retained native-resume.log")
            info = owned(name, invocation, manifest)
            require(not info["State"]["Running"] and info["State"]["ExitCode"] == 0,
                    "diagnostic worker did not complete")
            provenance = real(workspace / "target/bazel-native/provenance.json")
            require(json.loads(provenance.read_text())["source_commit"] == manifest["source_commit"],
                    "diagnostic native source identity changed")
            report.update(status="diagnostic-passed", native_provenance_sha256=digest(provenance))
        except (Exception, KeyboardInterrupt) as error:
            report.update(status="diagnostic-failed", error=str(error))
        finally:
            previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                stop_owned(name, invocation, manifest)
                if process:
                    process.wait(timeout=30)
                report["worker_removed"] = True
            except Exception as error:
                report.update(status="diagnostic-failed", cleanup_error=str(error))
            try:
                cache_after = derived_cache_inventories(manifest)
                report["preserved_cache_inventory_after"] = cache_after
                require(cache_after == cache_before, "old native cache metadata changed")
                report["generated_config_after"] = generated_config_snapshot(workspace)
                after = audit_support(manifest).audit(workspace, source)
                after["diagnostic_exclusion"] = report["source_audit_exclusion"]
                (operation / "source-audit-after.json").write_text(json.dumps(after, indent=2) + "\n")
            except Exception as error:
                report.update(status="diagnostic-failed", audit_error=str(error))
            try:
                report["available_bytes_after_resume"] = shutil.disk_usage(root).free
                report["retained_evidence"] = str(operation)
                encoded = json.dumps(report, indent=2) + "\n"
                (operation / "diagnostic-receipt.json").write_text(encoded)
                job_evidence(output, report)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 0 if report["status"] == "diagnostic-passed" else 1


def execution_identity(manifest):
    uid, gid = manifest["inspected_ownership"]["runner"]
    require(type(uid) is int and type(gid) is int and uid > 0 and gid > 0,
            "diagnostic worker must use a nonroot inspected runner identity")
    return {"account": native_build.USER, "uid": uid, "gid": gid,
            "previous_worker_uid": 1000, "previous_worker_gid": 1000,
            "retained_ownership_changed": False}


def worker_account(identity):
    """Create only the fresh container account; retain every host path owner."""
    name, uid, gid = identity["account"], identity["uid"], identity["gid"]
    try:
        require(grp.getgrgid(gid).gr_name == name, "unexpected diagnostic worker group")
    except KeyError:
        native_build.command(["groupadd", "--gid", str(gid), name])
    try:
        account = pwd.getpwuid(uid)
        require(account.pw_name == name and account.pw_gid == gid, "unexpected diagnostic worker account")
    except KeyError:
        native_build.command(["useradd", "--uid", str(uid), "--gid", str(gid), "--create-home",
                              "--shell", "/bin/bash", name])
    require(pwd.getpwnam(name).pw_uid == uid and pwd.getpwnam(name).pw_gid == gid,
            "diagnostic account name selects another identity")


def worker_cache_access(manifest, identity):
    # Host supplementary groups/ACL access do not transfer to the fresh account.
    # Test as that actual account before dockerd or Make can repair permissions.
    cache = real(Path(cache_selection(manifest)["diagnostic"]), directory=True)
    directories = [real(path, directory=True) for path in (cache, cache / "bazel", cache / "vcache")]
    native_build.command(["runuser", "--user", identity["account"], "--",
                          "sh", "-c", 'for path do test -w "$path" && test -x "$path" || exit 1; done',
                          "native-cache-permission-check", *map(str, directories)])


def inside(plan):
    record = json.loads(real(plan).read_text())
    invocation, manifest = record["invocation"], record["manifest"]
    require(os.geteuid() == 0 and Path("/.dockerenv").exists() and
            os.environ.get("SONIC_NATIVE_RECOVERY_INVOCATION") == invocation,
            "diagnostic bootstrap requires its owned worker")
    root, workspace, _, native = paths(manifest)
    operation = real(plan.parent, directory=True)
    require(operation == root / "state/native-diagnostic", "unexpected diagnostic plan location")
    # Fail before touching the old Docker state if identity/config changed.
    require(git(workspace, "rev-parse", "HEAD").strip() == manifest["source_commit"] and
            real(workspace / "rules/config.user").read_text() == CONFIG, "native identity/config changed")
    source_workspace._check_graph(workspace, source_workspace._expected(record["source"]))
    for item in record["accepted_overlay"]["files"].values():
        require(digest(real(workspace / "src/sonic-dash-api" / item["new_path"])) == item["after"],
                "accepted DASH overlay changed before execution")
    require(record["execution_identity"] == execution_identity(manifest),
            "diagnostic execution identity changed")
    require(record["native_cache"] == cache_selection(manifest), "diagnostic cache selection changed")
    worker_account(record["execution_identity"])
    worker_cache_access(manifest, record["execution_identity"])
    sudoers = Path("/etc/sudoers.d/sonic-native-ci")
    sudoers.write_text(native_build.USER + " ALL=(ALL) NOPASSWD:ALL\n")
    sudoers.chmod(0o440)
    (operation / "cgroup-delegation.json").write_text(json.dumps(native_build.prepare_cgroups()) + "\n")
    socket = Path("/run/sonic-native-recovery/docker.sock")
    socket.parent.mkdir(parents=True)
    env = native_build.native_environment(socket)
    env.update(GIT_CONFIG_SYSTEM=str(workspace / "tools/bazel/ci/fork-source.gitconfig"),
               GIT_CONFIG_NOSYSTEM="0", CARGO_NET_GIT_FETCH_WITH_CLI="true")
    trust, _ = native_build.execution_trust()
    require(trust == {"enabled": False}, "recorded worker trust changed")
    with (operation / "dockerd.log").open("x") as log:
        daemon = subprocess.Popen(["dockerd", "--host=unix://" + str(socket), "--group=" + native_build.USER,
                                   "--data-root=" + str(native / "docker-data"),
                                   "--exec-root=/run/sonic-native-recovery/exec",
                                   "--pidfile=/run/sonic-native-recovery/docker.pid",
                                   "--storage-driver=overlay2", "--exec-opt=native.cgroupdriver=cgroupfs"],
                                  stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(90):
                require(daemon.poll() is None, "retained private Docker daemon exited")
                if subprocess.run(["docker", "info"], env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=5).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise ValueError("retained private Docker daemon did not become ready")
            native_build.daemon_preflight(SimpleNamespace(
                state=operation, artifacts=operation, invocation=invocation), env)
            inner = operation / "inner-docker-data"
            require(not inner.exists() and not inner.is_symlink(), "inner Docker root must be new")
            native_build.command(recorded_command(manifest, inner), cwd=workspace, env=env)
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("inspect", "resume-native"))
    parser.add_argument("--owner-checkout", type=Path)
    parser.add_argument("--owner-commit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inside-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cleanup-job", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cleanup-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    def interrupted(signum, _frame):
        raise InterruptedError("received signal " + str(signum))
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    if args.inside_worker:
        inside(args.inside_worker)
        return 0
    if args.cleanup_job:
        require(args.cleanup_output is not None, "cleanup evidence output is required")
        return cleanup_job(args.cleanup_job, args.cleanup_output, MANIFEST)
    require(args.mode and args.output, "mode and a new job evidence output are required")
    require(not args.output.exists() and not args.output.is_symlink(), "job evidence already exists")
    real(args.output.absolute().parent, directory=True)
    manifest = MANIFEST
    root = Path(manifest["build_root"])
    require(not args.output.absolute().is_relative_to(root.parent),
            "job evidence must be outside the inspected retained area")
    if args.mode == "inspect":
        args.output.write_text(json.dumps(inspect(manifest), indent=2) + "\n")
        return 0
    require(args.owner_checkout is not None, "resume requires the reviewed source-owner checkout")
    admission = inspect(manifest)
    try:
        return resume(manifest, args.owner_checkout.absolute(),
                      args.owner_commit or manifest.get("reviewed_overlay_commit"), args.output, admission)
    except (Exception, KeyboardInterrupt) as error:
        if args.output.exists():
            # Preserve this job's reservation for the independent cleanup step.
            record = json.loads(real(args.output).read_text())
            require(record.get("job_identity") == job_identity(), "job evidence identity changed")
            record.update(status="diagnostic-failed", error=str(error))
        else:
            record = {"schema": 1, "status": "diagnostic-rejected", "job_identity": job_identity(),
                      "diagnostic_only": True, "admission": admission, "error": str(error)}
        job_evidence(args.output, record)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
