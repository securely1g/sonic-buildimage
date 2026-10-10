#!/usr/bin/env python3
"""Build and verify the bounded AMD64 Trixie VS kernel bundle for Make."""

import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import resolution

MANIFEST = "kernel-packages.json"
PROVENANCE = "kernel-provenance.json"
TARGET = "@sonic_linux_kernel//:kernel_packages"
INPUTS = Path("target/bazel-kernel-inputs")
KERNEL_PATH = Path("src/sonic-linux-kernel")
REGISTRY_PREFIX = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
DEFAULT_REGISTRY = REGISTRY_PREFIX + "main"
# This temporary endpoint contains the unlanded tools registration in #39.
DRAFT_REGISTRY = REGISTRY_PREFIX + "codex/kernel-build-tools-current"
WATCHED = [
    ".gitmodules", "Makefile.work", "rules/linux-kernel.mk", "rules/linux-kernel.dep",
    "tools/bazel/ci/kernel.py", "tools/bazel/kernel", str(KERNEL_PATH),
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0,
            "expected nonempty regular kernel output: " + str(path))
    return path


def git(workspace, *args):
    return subprocess.check_output(
        ["git", "-c", "safe.directory=" + str(workspace), "-C", str(workspace), *args],
        text=True, timeout=60).strip()


def endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme in {"http", "https", "grpc", "grpcs"} and parsed.hostname
            and parsed.username is None and parsed.password is None and not parsed.query
            and not parsed.fragment and not re.search(r"[\s\x00-\x1f]", value),
            "kernel cache needs an HTTP(S) or gRPC(S) endpoint without URL credentials or query parameters")
    return value


def module_declarations(path):
    """Read literal module declarations without executing Starlark."""
    modules = {}
    root = None
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Name):
            continue
        name = call.func.id
        require(not name.endswith("_override"), "kernel consumer template must not contain module overrides")
        if name not in {"module", "bazel_dep"}:
            continue
        values = {item.arg: ast.literal_eval(item.value) for item in call.keywords}
        if name == "module":
            require(root is None, "duplicate module declaration")
            root = values
        else:
            require(values.get("name") not in modules, "duplicate kernel dependency declaration")
            modules[values["name"]] = values
    return root, modules


def consumer_modules(workspace, source):
    root, modules = module_declarations(workspace / "tools/bazel/kernel/MODULE.bazel")
    source_root, source_modules = module_declarations(source / "MODULE.bazel")
    require(root == {"name": "sonic-kernel-cache-consumer"}, "unexpected kernel consumer module")
    require(set(modules) == {"sonic-linux-kernel", "sonic-build-infra"},
            "kernel consumer must declare only the kernel and shared tools modules")
    require(source_root.get("name") == "sonic-linux-kernel"
            and modules["sonic-linux-kernel"] == {
                "name": "sonic-linux-kernel", "version": source_root.get("version"),
                "repo_name": "sonic_linux_kernel"},
            "local kernel consumer version differs from its source module")
    infra = modules["sonic-build-infra"]
    require(infra == source_modules.get("sonic-build-infra")
            and infra.get("repo_name") == "sonic_build_infra"
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{40}", infra.get("version", "")),
            "kernel consumer tools version differs from its source module")
    return {name: value["version"] for name, value in modules.items()}


def launcher_contract(workspace, source):
    names = {"WORKER_IMAGE", "BAZEL_URL", "BAZEL_SHA256"}
    values = {}
    for node in ast.parse((source / "tools/bazel/build.py").read_text()).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    version = (workspace / "tools/bazel/kernel/.bazelversion").read_text().strip()
    require(version == (source / ".bazelversion").read_text().strip()
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version),
            "kernel consumer Bazel version differs from its source launcher")
    require(set(values) == names
            and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64}", values["WORKER_IMAGE"])
            and values["BAZEL_URL"] == "https://releases.bazel.build/" + version + "/release/bazel-" + version + "-linux-x86_64"
            and re.fullmatch(r"[0-9a-f]{64}", values["BAZEL_SHA256"]),
            "kernel launcher must pin its worker and Bazel executable")
    return {"worker": values["WORKER_IMAGE"], "bazel_version": version, "bazel_sha256": values["BAZEL_SHA256"]}


def contract(workspace):
    """Derive the native package contract from literal Make assignments."""
    contents = (workspace / "rules/linux-kernel.mk").read_text()
    values = {}
    for name in ("KERNEL_VERSION", "KERNEL_ABISUFFIX", "KERNEL_SUBVERSION", "KERNEL_FEATURESET"):
        matches = re.findall(r"^" + name + r"\s*=\s*([A-Za-z0-9.+~-]+)\s*$", contents, re.MULTILINE)
        require(len(matches) == 1, "kernel version assignment must be literal: " + name)
        values[name] = matches[0]
    require(values == {"KERNEL_VERSION": "6.12.41", "KERNEL_ABISUFFIX": "+deb13",
                       "KERNEL_SUBVERSION": "1", "KERNEL_FEATURESET": "sonic"},
            "Bazel kernel bundle supports only the 6.12.41-1 +deb13 sonic contract")
    version = values["KERNEL_VERSION"]
    release = version + "-" + values["KERNEL_SUBVERSION"]
    abi = version + values["KERNEL_ABISUFFIX"]
    feature = values["KERNEL_FEATURESET"]
    kernel = abi + "-" + feature + "-amd64"
    packages = {
        "linux-headers-" + abi + "-common-" + feature: "all",
        "linux-kbuild-" + abi: "amd64",
        "linux-image-" + kernel + "-unsigned": "amd64",
        "linux-headers-" + kernel: "amd64",
    }
    expected = {"schema": 1, "architecture": "amd64", "platform": "vs",
                "kernel_version": version, "kernel_abi": kernel,
                "package_version": release, "signing": "unsigned"}
    return expected, {
        package + "_" + release + "_" + architecture + ".deb": {
            "package": package, "version": release, "architecture": architecture}
        for package, architecture in packages.items()
    }


def source_archives(source, expected):
    """Read the source owner's three literal archive pins without executing it."""
    tree = ast.parse((source / "tools/bazel/sources.bzl").read_text())
    declarations = [node.value for node in tree.body if isinstance(node, ast.Assign)
                    and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "KERNEL_SOURCES"]
    require(len(declarations) == 1 and isinstance(declarations[0], ast.Dict),
            "kernel source must declare a literal KERNEL_SOURCES map")
    result = []
    for value in declarations[0].values:
        require(isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "struct" and not value.args,
                "kernel source archive declarations must be literal structs")
        fields = {item.arg: ast.literal_eval(item.value) for item in value.keywords}
        require(set(fields) == {"name", "sha256"} and isinstance(fields["name"], str)
                and re.fullmatch(r"[0-9a-f]{64}", fields.get("sha256", "")),
                "invalid kernel source archive declaration")
        result.append(fields)
    version = expected["kernel_version"]
    release = expected["package_version"]
    require({item["name"] for item in result} == {
        "linux_" + release + ".dsc", "linux_" + version + ".orig.tar.xz",
        "linux_" + release + ".debian.tar.xz"} and len(result) == 3,
        "kernel source archive set differs from the native package version")
    return sorted(result, key=lambda item: item["name"])


def source_state(workspace):
    workspace = workspace.resolve(strict=True)
    source = workspace / KERNEL_PATH
    source_commit = git(workspace, "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", source_commit), "invalid buildimage source commit")
    recorded = git(workspace, "ls-tree", "HEAD", "--", str(KERNEL_PATH))
    match = re.fullmatch(r"160000 commit ([0-9a-f]{40})\tsrc/sonic-linux-kernel", recorded)
    require(match is not None, "buildimage must pin the kernel as a gitlink")
    kernel_gitlink = match.group(1)
    require(git(source, "rev-parse", "HEAD") == kernel_gitlink,
            "kernel checkout differs from the buildimage gitlink")
    require(not git(workspace, "diff", "--name-only", "HEAD", "--", *WATCHED),
            "kernel handoff inputs must match the buildimage source commit")
    require(not git(source, "status", "--porcelain=v1", "--untracked-files=all"),
            "kernel checkout must have no tracked or untracked changes")
    selected = ["Makefile", "manage-config", "config.local", "patches-debian", "patches-sonic"]
    tracked = set(git(source, "ls-files", "--", *selected).splitlines())
    actual = {"Makefile", "manage-config"}
    for name in selected[2:]:
        actual.update(str(path.relative_to(source)) for path in (source / name).rglob("*") if path.is_file())
    require(actual == tracked and all(not (source / name).is_symlink() for name in actual),
            "kernel source inventory differs from its tracked gitlink inputs")
    helper = regular(source / "tools/bazel/kernel_action.py")
    identity = subprocess.check_output(
        [sys.executable, "-B", str(helper), "--source-tree-sha256", str(source)], text=True, timeout=60).strip()
    require(re.fullmatch(r"[0-9a-f]{64}", identity), "invalid kernel checkout source digest")
    expected, _ = contract(workspace)
    return {"source_commit": source_commit, "kernel_gitlink": kernel_gitlink,
            "source_tree_sha256": identity, "source_archives": source_archives(source, expected),
            "modules": consumer_modules(workspace, source), "launcher": launcher_contract(workspace, source)}


def verify_tools(tools):
    inputs = tools.get("input_sha256") if isinstance(tools, dict) else None
    require(isinstance(tools, dict) and tools.get("schema_version") == 1
            and tools.get("kind") == "debian-build-tools" and tools.get("architecture") == "amd64"
            and isinstance(inputs, list) and inputs
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in inputs)
            and inputs == sorted(inputs) and isinstance(tools.get("packages"), dict) and tools["packages"]
            and all(isinstance(name, str) and isinstance(version, str) and name and version
                    for name, version in tools["packages"].items()),
            "kernel build tools must have a declared AMD64 runtime identity")
    expected = hashlib.sha256(json.dumps({"architecture": "amd64", "inputs": inputs},
                                         sort_keys=True).encode()).hexdigest()
    require(tools.get("identity_sha256") == expected, "kernel build-tool identity differs from its input hashes")


def verify(bundle, workspace, *, source=None, check_debs=True, with_provenance=True):
    bundle = bundle.resolve(strict=True)
    source = source or source_state(workspace)
    expected, packages = contract(workspace)
    names = set(packages) | {MANIFEST} | ({PROVENANCE} if with_provenance else set())
    require({path.name for path in bundle.iterdir()} == names, "kernel bundle has missing or unexpected files")
    manifest = json.loads(regular(bundle / MANIFEST).read_text())
    require(all(manifest.get(key) == value for key, value in expected.items()),
            "Bazel kernel does not match the native AMD64 VS package contract")
    require(manifest.get("source_tree_sha256") == source["source_tree_sha256"],
            "kernel action source inputs differ from the pinned kernel checkout")
    files = manifest.get("source_files")
    require(isinstance(files, list) and files and all(
        isinstance(item, dict) and set(item) == {"name", "mode", "sha256"}
        and isinstance(item["name"], str) and item["name"] and item["mode"] in {0o644, 0o755}
        and isinstance(item["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        for item in files), "kernel manifest omitted its source inventory")
    require(files == sorted(files, key=lambda item: item["name"])
            and len({item["name"] for item in files}) == len(files)
            and hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() == source["source_tree_sha256"],
            "kernel manifest source inventory differs from its source digest")
    require(manifest.get("source_archives") == source["source_archives"],
            "kernel source archives differ from the pinned kernel checkout")
    verify_tools(manifest.get("build_tools"))
    entries = manifest.get("packages")
    require(isinstance(entries, list) and len(entries) == len(packages)
            and all(isinstance(item, dict) for item in entries)
            and {item.get("name") for item in entries} == set(packages),
            "kernel manifest must contain exactly the four native packages")
    for item in entries:
        name = item["name"]
        require(all(item.get(key) == value for key, value in packages[name].items()),
                "kernel package metadata differs: " + name)
        require(type(item.get("size")) is int and item["size"] > 0
                and isinstance(item.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]),
                "invalid kernel package digest or size: " + name)
        path = regular(bundle / name)
        require(path.stat().st_size == item["size"] and sha256(path) == item["sha256"],
                "kernel package failed size/SHA256 verification: " + name)
        if check_debs:
            output = subprocess.check_output(
                ["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"],
                text=True, timeout=30)
            fields = dict(line.split(": ", 1) for line in output.splitlines())
            require(fields == {key.title(): value for key, value in packages[name].items()},
                    "kernel DEB control metadata differs: " + name)
    result = {"manifest": manifest}
    if with_provenance:
        provenance = json.loads(regular(bundle / PROVENANCE).read_text())
        require(provenance.get("schema") == 2 and provenance.get("source_commit") == source["source_commit"]
                and provenance.get("kernel_gitlink") == source["kernel_gitlink"]
                and provenance.get("modules") == source["modules"]
                and provenance.get("launcher") == source["launcher"]
                and provenance.get("manifest_sha256") == sha256(bundle / MANIFEST)
                and provenance.get("target") == TARGET,
                "kernel bundle provenance differs from this source invocation")
        selected = provenance.get("resolution", {})
        require(selected.get("kernel_source_kind") == "local_override"
                and selected.get("kernel_source_commit") == source["kernel_gitlink"]
                and selected.get("modules") == source["modules"]
                and selected.get("registry") in {DEFAULT_REGISTRY, DRAFT_REGISTRY}
                and selected.get("infra_source_commit") == source["modules"]["sonic-build-infra"].rsplit("-", 1)[1]
                and all(isinstance(selected.get(name), str) and re.fullmatch(r"[0-9a-f]{64}", selected[name])
                        for name in ("graph_sha256", "lock_sha256", "infra_source_json_sha256"))
                and selected.get("graph_inspection", {}).get("module_graph_complete") is True,
                "kernel bundle has no verified local-source resolution provenance")
        result["provenance"] = provenance
    return result


def configure_registry(workspace, draft):
    rc = workspace / ".bazelrc"
    contents = rc.read_text()
    matches = list(re.finditer(r"^common --registry=(" + re.escape(REGISTRY_PREFIX) + r"\S+)$",
                              contents, re.MULTILINE))
    require(len(matches) == 1 and matches[0][1] == DEFAULT_REGISTRY,
            "kernel workspace must select one SONiC registry endpoint on main")
    selected = DRAFT_REGISTRY if draft else DEFAULT_REGISTRY
    if draft:
        start, end = matches[0].span(1)
        rc.write_text(contents[:start] + selected + contents[end:])
    return selected


def inspect_resolution(workdir, consumer, source, registry, *, fetch=None):
    graph_path = regular(workdir / "module-graph.json")
    stderr = (workdir / "module-graph.stderr").read_text()
    status = int(regular(workdir / "module-graph.exit-code").read_text())
    graph_text = graph_path.read_text()
    inspection = resolution.inspect_graph(graph_text, stderr, status)
    keys = set()

    def visit(node):
        keys.add(node["key"])
        for field in ("dependencies", "indirectDependencies", "cycles"):
            for child in node.get(field, []):
                visit(child)

    visit(json.loads(graph_text))
    kernels = {key for key in keys if key.startswith("sonic-linux-kernel@")}
    require(len(kernels) == 1 and kernels <= {"sonic-linux-kernel@_", "sonic-linux-kernel@" + source["modules"]["sonic-linux-kernel"]},
            "resolved graph does not contain exactly the local kernel source module")
    infra_version = source["modules"]["sonic-build-infra"]
    require({key for key in keys if key.startswith("sonic-build-infra@")} == {"sonic-build-infra@" + infra_version},
            "resolved graph selected a different kernel tools module")
    lock = regular(consumer / "MODULE.bazel.lock")
    hashes = json.loads(lock.read_text()).get("registryFileHashes", {})
    require(not any("/modules/sonic-linux-kernel/" in name for name in hashes),
            "local kernel source must not be reported as a published registry module")
    url = registry + "/modules/sonic-build-infra/" + infra_version + "/source.json"
    require(isinstance(hashes.get(url), str) and re.fullmatch(r"[0-9a-f]{64}", hashes[url]),
            "generated lock omitted the selected kernel tools source metadata")
    if fetch is None:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(1024 * 1024 + 1)
    else:
        data = fetch(url)
    require(len(data) <= 1024 * 1024 and hashlib.sha256(data).hexdigest() == hashes[url],
            "kernel tools source metadata differs from the tested resolution lock")
    metadata = json.loads(data)
    infra_commit = infra_version.rsplit("-", 1)[1]
    require(metadata.get("url") == "https://github.com/securely1g/sonic-build-infra/archive/" + infra_commit + ".tar.gz"
            and metadata.get("strip_prefix") == "sonic-build-infra-" + infra_commit,
            "kernel tools registry source differs from its module version")
    integrity = metadata.get("integrity", "")
    require(isinstance(integrity, str) and integrity.startswith("sha256-")
            and len(base64.b64decode(integrity[7:], validate=True)) == 32,
            "kernel tools registry source has no SHA256 integrity")
    (workdir / "infra-source.json").write_bytes(data)
    return {"kernel_source_kind": "local_override", "kernel_source_commit": source["kernel_gitlink"],
            "modules": source["modules"], "registry": registry, "infra_source_commit": infra_commit,
            "graph_sha256": sha256(graph_path), "lock_sha256": sha256(lock),
            "infra_source_json_sha256": hashlib.sha256(data).hexdigest(), "graph_inspection": inspection}


def stage_outputs(workdir, bundle, workspace, source, selected):
    require(source_state(workspace) == source, "kernel handoff source changed during the build")
    workdir = workdir.resolve(strict=True)
    names = {}
    for line in regular(workdir / "output-paths.txt").read_text().splitlines():
        path = Path(line)
        require(path.is_absolute() and path.is_relative_to("/work") and ".." not in path.parts,
                "kernel output does not belong to the launcher's /work root")
        candidate = (workdir / path.relative_to("/work")).resolve(strict=True)
        require(candidate.is_relative_to(workdir), "kernel output escapes its build output root")
        regular(candidate)
        require(path.name not in names, "duplicate kernel output: " + path.name)
        names[path.name] = candidate
    expected = set(contract(workspace)[1]) | {MANIFEST}
    require(set(names) == expected, "kernel target returned an unexpected output set")
    bundle.mkdir(parents=True, exist_ok=False)
    for name, path in names.items():
        with path.open("rb") as incoming, (bundle / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    verify(bundle, workspace, source=source, with_provenance=False)
    provenance = {"schema": 2, "source_commit": source["source_commit"], "kernel_gitlink": source["kernel_gitlink"],
                  "modules": source["modules"], "launcher": source["launcher"],
                  "manifest_sha256": sha256(bundle / MANIFEST),
                  "target": TARGET, "resolution": selected}
    (bundle / PROVENANCE).write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    result = verify(bundle, workspace, source=source)
    require(source_state(workspace) == source, "kernel handoff source changed during staging")
    return result


def copy_bundle(source_bundle, destination, workspace):
    source = source_state(workspace)
    result = verify(source_bundle, workspace, source=source)
    destination.mkdir(parents=True, exist_ok=False)
    for name in [MANIFEST, PROVENANCE, *(item["name"] for item in result["manifest"]["packages"])]:
        with regular(source_bundle / name).open("rb") as incoming, (destination / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    require(source_state(workspace) == source and verify(destination, workspace, source=source) == result,
            "kernel bundle or source changed during staging")
    return result


def plan(workspace, state, remote_cache, upload, draft, *, ca_bundle=None, java_trust_store=None, distdir=None):
    workspace = workspace.resolve(strict=True)
    state = state.resolve()
    require(state != workspace and state not in workspace.parents and not state.exists(),
            "kernel state must be a new dedicated directory that does not contain the workspace")
    make_inputs = (workspace / INPUTS).resolve()
    require(state != make_inputs and state not in make_inputs.parents and make_inputs not in state.parents,
            "kernel state and Make input directory must not overlap")
    bundle = state / "bundle"
    source = source_state(workspace)
    source_directory = None
    if distdir:
        directory = Path(distdir).resolve(strict=True)
        require(directory.is_dir(), "kernel source archive directory must exist")
        require(state != directory and state not in directory.parents and directory not in state.parents,
                "kernel state and source archive directory must not overlap")
        expected_archives = {item["name"]: item["sha256"] for item in source["source_archives"]}
        require({path.name for path in directory.iterdir()} == set(expected_archives),
                "kernel source archive directory must contain exactly the pinned archives")
        files = []
        for name, expected_sha256 in sorted(expected_archives.items()):
            path = regular(directory / name)
            require(sha256(path) == expected_sha256, "kernel source archive SHA256 differs: " + name)
            files.append({"name": name, "sha256": expected_sha256, "size": path.stat().st_size})
        source_directory = {"path": str(directory), "files": files}
    registry = DRAFT_REGISTRY if draft else DEFAULT_REGISTRY
    if remote_cache:
        remote_cache = endpoint(remote_cache)
    require(not upload or remote_cache, "remote upload requires an explicit cache endpoint")
    command = [sys.executable, "-B", str(workspace / KERNEL_PATH / "tools/bazel/build.py"),
               "--workspace", str(state / "consumer"), "--work-dir", str(state / "build"),
               "--repository-cache", str(state / "repository-cache"),
               "--module-override", "sonic-linux-kernel=" + str(workspace / KERNEL_PATH), "--target", TARGET]
    if remote_cache:
        command += ["--remote-cache", remote_cache]
    if not upload:
        command.append("--remote-cache-read-only")
    if source_directory:
        command += ["--distdir", source_directory["path"]]
    trust = {}
    for option, path in (("ca-bundle", ca_bundle), ("java-trust-store", java_trust_store)):
        if path:
            path = regular(Path(path).resolve(strict=True))
            if option == "ca-bundle":
                require("PRIVATE KEY" not in path.read_text(), "CA bundle must not contain a private key")
            command += ["--" + option, str(path)]
            trust[option.replace("-", "_") + "_sha256"] = sha256(path)
    return {"schema": 1, "source": source, "registry": registry, "target": TARGET,
            "source_kind": "local_override", "state": str(state), "bundle": str(bundle),
            "remote_cache": remote_cache, "upload_local_results": upload, "disk_cache": None,
            "execution_trust": trust, "distdir": source_directory,
            "worker_limits": {"cpus": 4, "memory_bytes": 12 * 1024 ** 3, "bazel_jobs": 1, "kbuild_jobs": 4},
            "argv": command}


def build(workspace, state, remote_cache, upload, draft, *, ca_bundle=None, java_trust_store=None, distdir=None, execute=None, fetch=None):
    prepared = plan(workspace, state, remote_cache, upload, draft,
                    ca_bundle=ca_bundle, java_trust_store=java_trust_store, distdir=distdir)
    workspace = workspace.resolve(strict=True)
    state = Path(prepared["state"])
    state.mkdir(parents=True, exist_ok=False)
    consumer = state / "consumer"
    shutil.copytree(workspace / "tools/bazel/kernel", consumer)
    require(configure_registry(consumer, draft) == prepared["registry"], "kernel registry selection changed")
    (state / "plan.json").write_text(json.dumps(prepared, indent=2, sort_keys=True) + "\n")
    if execute is None:
        subprocess.run(prepared["argv"], cwd=workspace, check=True)
    else:
        execute(prepared["argv"], workspace)
    invocation = json.loads(regular(state / "build/invocation.json").read_text())
    require(invocation.get("schema") == 1 and invocation.get("target") == TARGET
            and invocation.get("worker") == prepared["source"]["launcher"]["worker"]
            and invocation.get("remote_cache") == prepared["remote_cache"]
            and invocation.get("remote_cache_read_only") is (not prepared["upload_local_results"])
            and invocation.get("disk_cache") is None,
            "kernel launcher invocation differs from the prepared source build")
    selected = inspect_resolution(state / "build", consumer, prepared["source"], prepared["registry"], fetch=fetch)
    result = stage_outputs(state / "build", Path(prepared["bundle"]), workspace, prepared["source"], selected)
    receipt = {"schema": 1, "plan": prepared, "result": result}
    (state / "kernel-receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "build", "verify", "stage"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--remote-cache")
    parser.add_argument("--upload-local-results", action="store_true")
    parser.add_argument("--draft-registry", action="store_true")
    parser.add_argument("--ca-bundle", type=Path)
    parser.add_argument("--java-trust-store", type=Path)
    parser.add_argument("--distdir", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    if args.command in {"plan", "build"}:
        if args.state_dir is None or args.bundle is not None:
            parser.error("plan/build require --state-dir and write its bundle subdirectory")
        function = plan if args.command == "plan" else build
        result = function(workspace, args.state_dir, args.remote_cache, args.upload_local_results, args.draft_registry,
                          ca_bundle=args.ca_bundle, java_trust_store=args.java_trust_store, distdir=args.distdir)
    else:
        if args.state_dir or args.remote_cache or args.upload_local_results or args.draft_registry or args.ca_bundle or args.java_trust_store or args.distdir:
            parser.error("verify/stage do not accept build options")
        bundle = (args.bundle or workspace / INPUTS).resolve(strict=True)
        if args.command == "verify":
            result = verify(bundle, workspace)
        else:
            if args.bundle is None:
                parser.error("stage requires --bundle naming a verified source bundle")
            result = copy_bundle(bundle, workspace / INPUTS, workspace)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
