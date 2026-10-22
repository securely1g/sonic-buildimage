#!/usr/bin/env python3
"""Keep native Make byproducts out of the source tree subsequently used by Bazel.

Only Git objects are copied. Each repository, including recursive submodules,
gets an independent object store and is checked out at the captured revision.
Auditing the native tree never resets, cleans, or reads working-file contents.
"""

import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import urllib.parse


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _git(directory, *arguments, safe_paths=(), config_file=None):
    # Do not inherit alternate object stores, global URL rewrites, templates,
    # hooks, credential helpers, fsmonitor commands, or optional index writes.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                       GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="file", LC_ALL="C")
    if config_file is not None:
        environment["GIT_CONFIG_GLOBAL"] = str(config_file)
    command = ["git", "-c", "safe.directory=" + str(directory)]
    for path in safe_paths:
        command.extend(["-c", "safe.directory=" + str(path)])
    command += ["-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", "-c", "protocol.allow=never",
               "-c", "protocol.file.allow=always", "-c", "protocol.https.allow=never",
               "-c", "protocol.http.allow=never", "-c", "protocol.ssh.allow=never",
               "-c", "protocol.git.allow=never", "-c", "protocol.ext.allow=never",
               "-C", str(directory), *arguments]
    result = subprocess.run(command, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    # Git errors can include URLs or working-file contents; keep them out of
    # provenance and exception messages.
    require(result.returncode == 0, "local Git operation failed: " + arguments[0])
    return result.stdout.decode("utf-8", errors="surrogateescape")


def _expected(source):
    require(isinstance(source, dict), "expected source must be an object")
    commit = source.get("source_commit")
    components = source.get("components")
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit),
            "invalid captured root commit")
    require(isinstance(components, dict), "expected components must be an object")
    result = {".": commit}
    for path, entry in components.items():
        require(isinstance(path, str) and bool(path) and "\0" not in path,
                "invalid captured component path")
        parts = PurePosixPath(path).parts
        require(parts and not path.startswith("/") and str(PurePosixPath(path)) == path and
                all(part not in (".", "..", ".git") for part in parts),
                "unsafe captured component path")
        require(isinstance(entry, dict) and isinstance(entry.get("commit"), str) and
                re.fullmatch(r"[0-9a-f]{40}", entry["commit"]) and
                entry.get("gitlink") == entry["commit"], "invalid captured component identity")
        result[path] = entry["commit"]
    return result


def _real_directory(path):
    path = Path(path).absolute()
    require(path.is_dir() and not path.is_symlink(), "source directory is missing or is a symlink")
    require(path.resolve() == path, "source directory has a symlink ancestor")
    return path


def _repository(workspace, path, independent=False):
    directory = workspace if path == "." else workspace / path
    directory = _real_directory(directory)
    dotgit = directory / ".git"
    require(dotgit.exists() and not dotgit.is_symlink(), "repository Git metadata is missing or linked")
    if independent:
        require(dotgit.is_dir(), "Bazel source repository must have independent Git metadata")
    require(_git(directory, "rev-parse", "--show-toplevel").strip() == str(directory),
            "component is not an initialized repository")
    return directory


def _gitlinks(directory, commit):
    result = {}
    for entry in _git(directory, "ls-tree", "-r", "-z", commit).split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, value = metadata.split()
        if mode == "160000":
            require(kind == "commit", "invalid recorded Git submodule")
            result[path] = value
    return result


def _check_graph(workspace, identities, independent=False):
    recorded = {".": identities["."]}
    directories = {}
    for path, commit in sorted(identities.items(), key=lambda item: (item[0].count("/"), item[0])):
        directory = _repository(workspace, path, independent)
        require(_git(directory, "rev-parse", "HEAD").strip() == commit,
                "repository HEAD differs from captured source: " + path)
        require(_git(directory, "cat-file", "-t", commit).strip() == "commit",
                "captured commit object is unavailable: " + path)
        directories[path] = directory
        for child, value in _gitlinks(directory, commit).items():
            full_path = child if path == "." else path + "/" + child
            require(full_path not in recorded, "duplicate captured gitlink")
            recorded[full_path] = value
    require(recorded == identities, "captured source does not match the complete recursive gitlink graph")
    return directories


def _metadata(directory, name):
    path = directory / name
    # Git filenames are relative; do not follow a native-created parent link
    # merely to obtain audit metadata about a removed file.
    parts = PurePosixPath(name).parts
    require(parts and not name.startswith("/") and ".." not in parts,
            "unsafe Git status path")
    parent = directory
    for part in parts[:-1]:
        parent /= part
        if parent.is_symlink():
            return {"kind": "obscured_by_symlink"}
    try:
        details = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    kind = ("file" if stat.S_ISREG(details.st_mode) else
            "directory" if stat.S_ISDIR(details.st_mode) else
            "symlink" if stat.S_ISLNK(details.st_mode) else "special")
    return {"kind": kind, "bytes": details.st_size, "mode": stat.S_IMODE(details.st_mode)}


def _changes(directory, ignored=True):
    arguments = ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                 "--ignore-submodules=none"]
    if ignored:
        arguments.append("--ignored=matching")
    entries = iter(_git(directory, *arguments).split("\0"))
    result = []
    for entry in entries:
        if not entry:
            continue
        require(len(entry) >= 4 and entry[2] == " ", "invalid Git status output")
        status, path = entry[:2], entry[3:]
        record = {"path": path, "index_status": status[0], "worktree_status": status[1]}
        record.update(_metadata(directory, path))
        if "R" in status or "C" in status:
            record["original_path"] = next(entries)
        result.append(record)
    return result


def _origin(directory):
    values = _git(directory, "config", "--local", "--get-regexp", r"^remote\.origin\.url$")
    lines = values.splitlines()
    require(len(lines) == 1, "source repository must have one public origin")
    value = lines[0].split(" ", 1)[1]
    parsed = urllib.parse.urlsplit(value)
    hostname = parsed.hostname or ""
    require(parsed.scheme == "https" and hostname and "." in hostname and
            not parsed.username and not parsed.password and not parsed.query and
            not parsed.fragment and parsed.port in (None, 443) and
            not hostname.endswith((".localhost", ".local", ".internal")) and
            parsed.path not in ("", "/"), "source origin must be a public credential-free HTTPS URL")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    require(address is None or address.is_global, "source origin must be public")
    return value


def _object_store(directory):
    metadata = directory / ".git"
    require(metadata.is_dir() and not metadata.is_symlink() and
            not (metadata / "commondir").exists() and not (metadata / "commondir").is_symlink(),
            "Git common directory must be independent")
    common = Path(_git(directory, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = directory / common
    require(common.resolve() == metadata, "Git common directory escapes independent metadata")
    objects = Path(_git(directory, "rev-parse", "--git-path", "objects").strip())
    if not objects.is_absolute():
        objects = directory / objects
    require(objects.is_dir() and not objects.is_symlink() and objects.resolve() == metadata / "objects",
            "Git object store is missing, linked, or outside independent metadata")
    for name in ("alternates", "http-alternates"):
        alternate = objects / "info" / name
        require(not alternate.exists() and not alternate.is_symlink(), "Git alternate object stores are unsupported")
    count = 0
    for parent, directories, files in os.walk(objects, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            details = path.lstat()
            require(not stat.S_ISLNK(details.st_mode), "Git object store contains a symlink")
            require(stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode),
                    "Git object store contains a nonregular entry")
            if stat.S_ISREG(details.st_mode):
                require(details.st_nlink == 1, "Git object store shares hardlinked files")
                count += 1
    return count


def verify(workspace, expected_source):
    """Fail unless an independent source view still has exactly the clean inputs."""
    workspace = _real_directory(workspace)
    identities = _expected(expected_source)
    directories = _check_graph(workspace, identities, independent=True)
    for path, directory in directories.items():
        _object_store(directory)
        require(not _changes(directory), "Bazel source contains modified, untracked, or ignored files: " + path)
    return {"status": "passed", "source_commit": identities["."],
            "components": expected_source["components"], "clean": True}


def clone(workspace, destination, expected_source):
    """Create a new local object copy before native Make changes its checkout."""
    workspace = _real_directory(workspace)
    destination = Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(), "Bazel source destination must be fresh")
    parent = _real_directory(destination.parent)
    require(not destination.is_relative_to(workspace) and not workspace.is_relative_to(destination),
            "native and Bazel source directories must be disjoint")
    identities = _expected(expected_source)
    sources = _check_graph(workspace, identities)
    origins = {}
    for path, source in sources.items():
        require(not _changes(source, ignored=False), "native source is already modified: " + path)
        origins[path] = _origin(source)
        # A normal initialized submodule may use a .git file. Its objects must
        # still exist locally; local clones with alternates are not accepted.
        objects = Path(_git(source, "rev-parse", "--git-path", "objects").strip())
        if not objects.is_absolute():
            objects = source / objects
        for name in ("alternates", "http-alternates"):
            alternate = objects / "info" / name
            require(not alternate.exists() and not alternate.is_symlink(), "source uses Git alternate object stores")
    receipt = {"schema": 1, "status": "passed", "source_workspace": str(workspace),
               "bazel_workspace": str(destination), "source_commit": identities["."],
               "components": expected_source["components"], "repositories": {}}
    # An empty template prevents execution or copying of host Git templates.
    with tempfile.TemporaryDirectory(prefix="source-git-template-", dir=parent) as template:
        empty_template = Path(template) / "empty"
        empty_template.mkdir()
        config = Path(template) / "clone.gitconfig"
        for path, source in sources.items():
            target = destination if path == "." else destination / path
            if target.exists() or target.is_symlink():
                require(path != "." and target.is_dir() and not target.is_symlink() and not any(target.iterdir()),
                        "submodule destination is not an empty gitlink directory")
                target.rmdir()
            target.parent.mkdir(parents=True, exist_ok=True)
            _real_directory(target.parent)
            # Local clone checks ownership at the resolved Git directory, not
            # just its worktree. Its upload-pack subprocess does not preserve
            # every -c setting, so pass an invocation-only config file through
            # the environment. Never change the user's global Git config.
            source_git = _git(source, "rev-parse", "--absolute-git-dir").strip()
            config.write_text("")
            config.chmod(0o600)
            for safe in (source, source_git):
                _git(parent, "config", "--file", str(config), "--add", "safe.directory", str(safe))
            _git(parent, "clone", "--local", "--no-hardlinks", "--no-checkout",
                 "--no-recurse-submodules", "--template=" + str(empty_template), "--", str(source), str(target),
                 safe_paths=(source, source_git), config_file=config)
            _git(target, "remote", "set-url", "origin", origins[path])
            _git(target, "checkout", "--detach", "--force", identities[path])
            receipt["repositories"][path] = {
                "commit": identities[path], "origin": origins[path],
                "object_files": _object_store(target),
            }
    receipt["verification"] = verify(destination, expected_source)
    return receipt


def audit(workspace, expected_source):
    """Describe native source mutations without reading file contents or fixing them."""
    workspace = _real_directory(workspace)
    identities = _expected(expected_source)
    result = {"schema": 1, "source_workspace": str(workspace),
              "source_commit": identities["."], "components": expected_source["components"],
              "clean": True, "repositories": {}}
    for path, expected_commit in identities.items():
        record = {"expected_commit": expected_commit, "actual_commit": None,
                  "head_matches": False, "changes": [], "ignored": []}
        result["repositories"][path] = record
        try:
            directory = _repository(workspace, path)
            record["actual_commit"] = _git(directory, "rev-parse", "HEAD").strip()
            record["head_matches"] = record["actual_commit"] == expected_commit
            for change in _changes(directory):
                group = "ignored" if change["index_status"] == "!" else "changes"
                record[group].append(change)
        except (ValueError, OSError):
            record["error"] = "repository unavailable or cannot be audited"
        if not record["head_matches"] or record["changes"] or record["ignored"] or "error" in record:
            result["clean"] = False
    return result
