#!/usr/bin/env python3
"""Record declared native sources and verify the one supported Git transformation.

FRR's native recipe commits its SONiC patches and generated Debian changelog.
Those commits are build products, distinct from the recorded source gitlink.
Every other component must still be checked out at its recorded revision.
"""

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


FRR = "src/sonic-frr/frr"
PATCHES = "src/sonic-frr/patch/"
C_MODULE = "src/sonic-frr/dplane_fpm_sonic/dplane_fpm_sonic.c"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(directory, *arguments, environment=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_ALLOW_PROTOCOL="file", LC_ALL="C")
    if environment:
        env.update(environment)
    result = subprocess.run([
        "git", "-c", "safe.directory=" + str(directory), "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        "-C", str(directory), *arguments,
    ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    require(result.returncode == 0, "native source Git operation failed: " + arguments[0])
    return result.stdout


def text_git(directory, *arguments, **kwargs):
    return git(directory, *arguments, **kwargs).decode("utf-8", errors="surrogateescape").strip()


def safe_relative(name):
    path = PurePosixPath(name)
    require(bool(path.parts) and not path.is_absolute() and str(path) == name and
            "\0" not in name and all(part not in ("..", ".git") for part in path.parts),
            "invalid recorded component path")
    return name


def repository(source, name):
    directory = source if not name else source / safe_relative(name)
    require(directory.is_dir() and not directory.is_symlink() and directory.resolve() == directory,
            "native component is missing or linked: " + (name or "."))
    require((directory / ".git").exists() and not (directory / ".git").is_symlink(),
            "native component is uninitialized: " + (name or "."))
    require(text_git(directory, "rev-parse", "--show-toplevel") == str(directory),
            "native component is not a repository: " + (name or "."))
    return directory


def tree_links(directory, commit):
    result = {}
    for entry in git(directory, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, revision = metadata.split()
        if mode == b"160000":
            require(kind == b"commit", "invalid native gitlink")
            result[safe_relative(name.decode("utf-8"))] = revision.decode("ascii")
    return result


def check_component_index(directory, expected):
    actual = {}
    for entry in git(directory, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, revision, stage = metadata.split()
        require(stage == b"0", "native source index contains a conflict")
        if mode == b"160000":
            actual[name.decode("utf-8")] = revision.decode("ascii")
    require(actual == expected, "native source index has extra, missing, or changed gitlinks")


def recorded_file(source, commit, name):
    """Require the actual recipe input to equal its committed bytes."""
    path = source / name
    require(path.is_file() and not path.is_symlink() and path.resolve() == path,
            "native recipe input is missing or linked: " + name)
    entry = git(source, "ls-tree", "-z", commit, "--", name).split(b"\0")[0]
    require(entry and entry.split(b" ", 1)[0] in (b"100644", b"100755"),
            "native recipe input is not a recorded regular file: " + name)
    contents = git(source, "show", commit + ":" + name)
    require(path.read_bytes() == contents, "native recipe input differs from recorded source: " + name)
    return contents


def check_modules_file(directory, commit):
    entry = git(directory, "ls-tree", "-z", commit, "--", ".gitmodules")
    if entry:
        recorded_file(directory, commit, ".gitmodules")
    else:
        require(not (directory / ".gitmodules").exists() and not (directory / ".gitmodules").is_symlink(),
                "native source has an unrecorded .gitmodules file")


def frr_transformation(source, root_commit, directory, recorded, actual):
    inputs = {}

    def read(name):
        contents = recorded_file(source, root_commit, name)
        inputs[name] = hashlib.sha256(contents).hexdigest()
        return contents

    rules = read("rules/frr.mk").decode("utf-8")
    values = {}
    for key in ("FRR_VERSION", "FRR_SUBVERSION", "FRR_TAG"):
        matches = re.findall(r"^" + key + r"\s*([:+?]?=)\s*([^\n]+?)\s*$", rules, re.M)
        require(len(matches) == 1 and matches[0][0] == "=", "unsupported native " + key + " declaration")
        values[key] = matches[0][1]
    require(re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", values["FRR_VERSION"]) and
            re.fullmatch(r"[0-9]+", values["FRR_SUBVERSION"]) and
            values["FRR_TAG"] == "frr-$(FRR_VERSION)", "unsupported native FRR version or tag")
    tag = "frr-" + values["FRR_VERSION"]
    version = values["FRR_VERSION"] + "-sonic-" + values["FRR_SUBVERSION"]
    require(text_git(directory, "rev-parse", "refs/tags/" + tag + "^{commit}") == recorded,
            "FRR tag does not match the recorded source gitlink")
    require(text_git(directory, "rev-parse", "--abbrev-ref", "HEAD") == tag + "-patched",
            "native FRR is not on its declared patched branch")
    read("src/sonic-frr/Makefile")
    module = read(C_MODULE)
    series = read(PATCHES + "series").decode("utf-8")
    names = [line.strip() for line in series.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    require(names and len(names) == len(set(names)), "empty or duplicate FRR patch series")
    for name in names:
        require(PurePosixPath(name).name == name and re.fullmatch(r"[A-Za-z0-9_.+-]+\.patch", name),
                "unsupported FRR patch series entry")
        read(PATCHES + name)

    history = [line.split() for line in text_git(directory, "rev-list", "--reverse", "--parents",
                                               recorded + ".." + actual).splitlines()]
    require(len(history) == len(names) + 1, "FRR history does not have one commit per patch and one changelog commit")
    parent = recorded
    for entry in history:
        require(len(entry) == 2 and entry[1] == parent, "FRR transformation is not a linear chain from its recorded base")
        parent = entry[0]
    require(parent == actual, "FRR transformation does not end at native HEAD")

    # Writes go only to owned temporary metadata. The source object store is
    # read through an explicit alternate; no source index, refs, or objects change.
    objects = Path(text_git(directory, "rev-parse", "--git-path", "objects"))
    if not objects.is_absolute():
        objects = directory / objects
    objects = objects.resolve(strict=True)
    require(objects.is_dir() and objects.is_relative_to(source), "FRR object storage escapes the native checkout")
    patch_trees = []
    with tempfile.TemporaryDirectory(prefix="sonic-frr-provenance-") as temporary:
        scratch = Path(temporary).resolve()
        metadata = scratch / "proof.git"
        git(scratch, "init", "--bare", "--quiet", "--template=", str(metadata))
        environment = {"GIT_DIR": str(metadata), "GIT_INDEX_FILE": str(scratch / "proof.index"),
                       "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects)}
        git(scratch, "read-tree", recorded, environment=environment)
        for name, entry in zip(names, history):
            # Stage already validated committed bytes, avoiding a second read
            # from a potentially changing native patch path during reconstruction.
            patch = scratch / "input.patch"
            patch.write_bytes(git(source, "show", root_commit + ":" + PATCHES + name))
            git(scratch, "apply", "--cached", str(patch), environment=environment)
            expected_tree = text_git(scratch, "write-tree", environment=environment)
            actual_tree = text_git(directory, "rev-parse", entry[0] + "^{tree}")
            require(actual_tree == expected_tree, "native FRR patch tree differs: " + name)
            patch_trees.append({"patch": name, "commit": entry[0], "tree": actual_tree})

    final_parent = history[-1][1]
    require(git(directory, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                final_parent, actual) == b"debian/changelog\0",
            "native FRR final commit must change only debian/changelog")
    require(git(directory, "ls-tree", "-z", actual, "--", "debian/changelog").startswith(b"100644 blob "),
            "native FRR changelog must be a regular nonexecutable file")
    changelog = git(directory, "show", actual + ":debian/changelog")
    require(re.match(rb"frr \(" + re.escape(version.encode()) + rb"\) [^\r\n]+\r?\n", changelog),
            "native FRR changelog has the wrong package version")
    copied_module = directory / "zebra/dplane_fpm_sonic.c"
    require(copied_module.is_file() and not copied_module.is_symlink() and
            copied_module.resolve() == copied_module and copied_module.read_bytes() == module,
            "native FRR copied C module differs from the recorded recipe input")
    return {"kind": "frr-sonic-patches", "recorded_commit": recorded, "actual_commit": actual,
            "tag": tag, "version": version, "patch_count": len(names), "patch_trees": patch_trees,
            "changelog_sha256": hashlib.sha256(changelog).hexdigest(), "inputs": inputs,
            "copied_c_module": {"path": "zebra/dplane_fpm_sonic.c", "sha256": inputs[C_MODULE]}}


def source_identity(source):
    source = Path(source).absolute()
    root = repository(source, "")
    commit = text_git(root, "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "invalid native root commit")
    result = {"source_commit": commit, "source_branch": text_git(root, "rev-parse", "--abbrev-ref", "HEAD"),
              "source_submodules": {}, "native_transformations": {}}

    def visit(directory, recorded, prefix=""):
        children = tree_links(directory, recorded)
        check_component_index(directory, children)
        check_modules_file(directory, recorded)
        for name, revision in children.items():
            path = prefix + name
            require(path not in result["source_submodules"], "duplicate recorded native component")
            child = repository(source, path)
            actual = text_git(child, "rev-parse", "HEAD")
            result["source_submodules"][path] = revision
            if actual != revision:
                require(path == FRR, "unexpected native component HEAD change: " + path)
                result["native_transformations"][path] = frr_transformation(source, commit, child, revision, actual)
            # Recurse through the recorded parent, never a transformed tree.
            visit(child, revision, path + "/")

    visit(root, commit)
    return result
