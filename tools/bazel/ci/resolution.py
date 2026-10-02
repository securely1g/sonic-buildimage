"""Retain generated Bazel resolution evidence without changing source inputs."""

import hashlib
import shutil
import subprocess


def retain(workspace, directory, graph):
    """Archive the tested workspace's lock and graph; reject tracked changes."""
    def git(*args):
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(workspace),
                                        "-C", str(workspace), *args], text=True)

    if git("ls-files", "--", "MODULE.bazel.lock").strip():
        raise ValueError("MODULE.bazel.lock must be generated, not tracked")
    git("check-ignore", "--quiet", "MODULE.bazel.lock")
    git("diff", "--exit-code", "HEAD", "--")
    lock = workspace / "MODULE.bazel.lock"
    if not lock.is_file() or not lock.stat().st_size:
        raise ValueError("Bazel did not generate MODULE.bazel.lock")
    shutil.copyfile(lock, directory / "MODULE.bazel.lock")
    (directory / "module-graph.txt").write_text(graph)
    files = {}
    for name in ("MODULE.bazel.lock", "module-graph.txt"):
        path = directory / name
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files[name] = {"sha256": digest, "bytes": path.stat().st_size}
    return {"lockfile_mode": "update", "tracked_files_unchanged": True,
            "generated_evidence": files}
