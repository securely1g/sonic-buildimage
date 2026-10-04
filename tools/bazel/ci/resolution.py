"""Retain generated Bazel resolution evidence without changing source inputs."""

import hashlib
import json
import re
import shutil
import subprocess


def inspect_graph(stdout, stderr, returncode):
    """Validate the full module graph, allowing two unused Bazel 8 extensions."""
    graph = json.loads(stdout)
    if graph.get("key") != "<root>" or not graph.get("root") or not graph.get("dependencies"):
        raise ValueError("Missing resolved module graph")
    expanded, referenced = set(), set()

    def visit(node):
        key = node["key"]
        referenced.add(key)
        if not node.get("unexpanded", False):
            expanded.add(key)
        for field in ("dependencies", "indirectDependencies", "cycles"):
            for dependency in node.get(field, []):
                visit(dependency)

    visit(graph)
    if referenced != expanded:
        raise ValueError("Incomplete resolved module graph")
    result = {"returncode": returncode, "module_count": len(expanded),
              "module_graph_complete": True, "extension_inspection_complete": returncode == 0}
    if returncode == 0:
        return result

    # Bazel 8.5.1's mod inspector evaluates every extension, including these
    # unused extensions after the required Linux build/tests already succeeded.
    # Keep their diagnostics; reject every other failure and incomplete graph.
    errors = [line for line in stderr.splitlines() if line.startswith(("ERROR:", "Error:"))]
    allowed = [
        r"ERROR: Traceback \(most recent call last\):",
        r"Error: 'struct' value has no field or method 'AppleDynamicFramework'",
        r"ERROR: @@bazel_tools//tools/cpp:cc_configure\.bzl does not export a module extension called cc_configure_extension, yet its use is requested at https://bcr\.bazel\.build/modules/abseil-cpp/20240722\.0/MODULE\.bazel:23:29",
        r"ERROR: Error loading '@@rules_apple\+//apple:apple\.bzl' for module extensions, requested by https://bcr\.bazel\.build/modules/rules_apple/3\.5\.1/MODULE\.bazel:27:48: at [^\n]+/rules_apple\+/apple/apple\.bzl:27:5: initialization of module 'apple/internal/apple_xcframework_import\.bzl' failed: at [^\n]+/rules_apple\+/apple/apple\.bzl:27:5: initialization of module 'apple/internal/apple_xcframework_import\.bzl' failed",
        r"ERROR: Results may be incomplete as 2 extensions failed\.",
    ]
    if (returncode != 2 or len(errors) != len(allowed)
            or not all(sum(bool(re.fullmatch(pattern, error)) for error in errors) == 1 for pattern in allowed)
            or not {"abseil-cpp@20240722.0", "rules_apple@3.5.1"} <= expanded):
        raise ValueError("Unexpected module graph inspection failure")
    result["unused_extension_failures"] = ["abseil-cpp@20240722.0:cc_configure_extension",
                                           "rules_apple@3.5.1:apple"]
    return result


def collect(workspace, directory, *, bazel=("bazel",), options=()):
    """Retain module provenance without changing the tested extension lock."""
    lock = workspace / "MODULE.bazel.lock"
    tested_lock = lock.read_bytes()
    command = [*bazel, "mod", "graph", "--output=json", "--extension_info=hidden",
               "--lockfile_mode=off", "--color=no", "--curses=no", *options]
    result = subprocess.run(command, cwd=workspace, text=True, capture_output=True)
    (directory / "module-graph.json").write_text(result.stdout)
    (directory / "module-graph.stderr").write_text(result.stderr)
    (directory / "module-graph-inspection.json").write_text(
        json.dumps({"argv": command, "returncode": result.returncode}, indent=2) + "\n")
    inspection = inspect_graph(result.stdout, result.stderr, result.returncode)
    inspection["argv"] = command
    (directory / "module-graph-inspection.json").write_text(json.dumps(inspection, indent=2) + "\n")
    if lock.read_bytes() != tested_lock:
        raise ValueError("Graph inspection changed the tested extension lock")
    retained = retain(workspace, directory, result.stdout, graph_filename="module-graph.json")
    retained["inspection"] = inspection
    return retained


def retain(workspace, directory, graph, graph_filename="module-graph.txt"):
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
    (directory / graph_filename).write_text(graph)
    files = {}
    for name in ("MODULE.bazel.lock", graph_filename):
        path = directory / name
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files[name] = {"sha256": digest, "bytes": path.stat().st_size}
    return {"lockfile_mode": "update", "tracked_files_unchanged": True,
            "generated_evidence": files}
