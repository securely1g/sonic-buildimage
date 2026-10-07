#!/usr/bin/env python3
"""Refresh syncd's checked APT data/control lock using existing Distroless targets.

This preparation builds only the reviewed data and control targets. It audits
all selected action outputs and commands before executing them, and refuses a
selection that can create Debian packages or invoke a Make packaging wrapper.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

OWNER = Path(__file__).absolute().parent
ROOT = OWNER.parents[2]
sys.path.insert(0, str(OWNER))
import apt_lock

EXTENSION = "@@rules_distroless+//apt:extensions.bzl%apt"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments, log_path, *, output_path=None):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output = output_path.open("w") if output_path else subprocess.PIPE
    try:
        with log_path.open("w") as log:
            log.write("$ " + shlex.join(arguments) + "\n")
            process = subprocess.Popen(arguments, cwd=ROOT, stdout=output,
                                       stderr=subprocess.PIPE if output_path else subprocess.STDOUT,
                                       text=True, bufsize=1)
            stream = process.stderr if output_path else process.stdout
            for line in stream:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            result = process.wait()
            require(result == 0, "command failed with exit " + str(result) + ": " + str(log_path))
    finally:
        if output_path:
            output.close()


def capture(arguments, log_path):
    output_path = log_path.with_suffix(log_path.suffix + ".stdout")
    run(arguments, log_path, output_path=output_path)
    return output_path.read_text().strip()


def resolution():
    value = json.loads((ROOT / "MODULE.bazel.lock").read_bytes())
    data = value["moduleExtensions"][EXTENSION]["general"]
    specs = data["generatedRepoSpecs"]
    contents = [specs[name]["attributes"]["lock_content"] for name in apt_lock.SET_NAMES.values()]
    require(len(set(contents)) == 1, "runtime and debug APT hubs have different resolutions")
    return json.loads(contents[0]), specs


def resolved_version(output_base):
    path = output_base / "external/rules_distroless+/MODULE.bazel"
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "module":
            fields = {item.arg: ast.literal_eval(item.value) for item in node.value.keywords if item.arg in ("name", "version")}
            require(fields.get("name") == "rules_distroless", "unexpected resolved module source")
            return fields.get("version")
    raise ValueError("resolved rules_distroless module identity is absent")


def inspect_actions(path):
    value = json.loads(path.read_bytes())
    fragments = {item["id"]: item for item in value.get("pathFragments", [])}
    memo = {}
    def fragment(identifier):
        if identifier not in memo:
            item = fragments[identifier]
            memo[identifier] = ((fragment(item["parentId"]) + "/") if item.get("parentId") else "") + item["label"]
        return memo[identifier]
    artifacts = {item["id"]: fragment(item["pathFragmentId"]) for item in value.get("artifacts", [])}
    outputs, suspicious = [], []
    for action in value.get("actions", []):
        outputs.extend(artifacts[identifier] for identifier in action.get("outputIds", []))
        command = " ".join(action.get("arguments", []))
        wrapper = re.search(r"(?:^|[/\s])(?:make|gmake|dpkg-buildpackage)(?:\s|$)", command)
        deb_build = "dpkg-deb" in command and re.search(r"(?:--build|(?:^|\s)-b(?:\s|$))", command)
        if wrapper or deb_build:
            suspicious.append({"mnemonic": action.get("mnemonic"), "arguments_sha256": hashlib.sha256(command.encode()).hexdigest()})
    deb_outputs = sorted(path for path in outputs if path.endswith(".deb"))
    result = {"schema": 1, "action_count": len(value.get("actions", [])), "output_count": len(outputs),
              "deb_outputs": deb_outputs, "packaging_wrappers": suspicious}
    return result


def publish(path, data):
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def refresh(args):
    artifacts = args.artifacts.absolute()
    artifacts.mkdir(parents=True, exist_ok=False)
    prefix = [args.bazel] + args.bazel_startup_arg
    def bazel(operation, *rest):
        return prefix + [operation] + args.bazel_arg + list(rest)
    version = capture([args.bazel, "--version"], artifacts / "bazel-version.log")
    require(version == "bazel " + apt_lock.BAZEL_VERSION, "unexpected Bazel version: " + version)
    module_sha = sha(ROOT / "MODULE.bazel")
    hubs = "set(" + " ".join("@" + name + "//:packages" for name in apt_lock.SET_NAMES.values()) + ")"
    run(bazel("cquery", hubs, "--output=label"), artifacts / "hubs-query.log")
    output_base = Path(capture(bazel("info", "output_base"), artifacts / "output-base.log"))
    require(resolved_version(output_base) == apt_lock.DISTROLESS_VERSION, "unexpected rules_distroless version")
    resolved, specs = resolution()
    roots = {}
    packages = {}
    for variant in apt_lock.SET_NAMES:
        roots[variant], selected = apt_lock.resolved_closure(resolved, variant)
        packages.update(selected)
    labels = {}
    for key, package in packages.items():
        repository = apt_lock.sanitize(key)
        spec = specs.get(repository, {})
        attributes = spec.get("attributes", {})
        urls = [uri.rstrip("/") + "/" + package["filename"] for uri in resolved["sources"][package["suite"]]["uris"]]
        require(spec.get("repoRuleId", "").endswith("%deb_import") or spec.get("ruleClassName", "").endswith("%deb_import"),
                "unexpected APT repository rule for " + key)
        require(attributes.get("target_name") == repository and attributes.get("package_name") == package["name"] and
                attributes.get("sha256") == package["sha256"] and attributes.get("urls") == urls and
                attributes.get("mergedusr") is False, "resolved APT repository differs from its lock: " + key)
        base = "@@" + args.apt_repo_prefix + repository + "//:"
        labels[key] = {"payload": base + "data", "control": base + "control"}
    targets = sorted(label for pair in labels.values() for label in pair.values())
    (artifacts / "targets.txt").write_text("\n".join(targets) + "\n")
    expression = "set(" + " ".join(targets) + ")"
    actions = artifacts / "actions.json"
    run(bazel("aquery", "deps(" + expression + ")", "--output=jsonproto"), artifacts / "actions.log", output_path=actions)
    audit = inspect_actions(actions)
    (artifacts / "execution-gate-audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    require(not audit["deb_outputs"] and not audit["packaging_wrappers"], "APT preparation action graph can create DEBs or invoke a packaging wrapper")
    run(bazel("build", *targets), artifacts / "build.log")
    files_path = artifacts / "files.txt"
    query_expression = '"@@" + target.label.repo_name + "//:" + target.label.name + "\\t" + "\\t".join([f.path for f in target.files.to_list()])'
    run(bazel("cquery", expression, "--output=starlark", "--starlark:expr=" + query_expression),
        artifacts / "files.log", output_path=files_path)
    files = {}
    for line in files_path.read_text().splitlines():
        fields = line.split("\t")
        require(len(fields) == 2 and fields[0] not in files, "APT target must provide one unique file: " + line)
        files[fields[0]] = fields[1]
    require(set(files) == set(targets), "APT cquery file set differs from the selected targets")
    output_link = ROOT / "bazel-out"
    require(output_link.is_symlink(), "Bazel did not publish its standard output link")
    output_directory = output_link.resolve()
    require(output_directory.name == "bazel-out" and output_directory.is_relative_to(output_base),
            "Bazel output link does not belong to the inspected output base")
    execution_root = output_directory.parent
    locked_packages = {}
    for key, package in sorted(packages.items()):
        item = {name: package[name] for name in ("name", "version", "architecture", "suite", "filename", "sha256", "size")}
        item["depends_on"] = sorted(package["depends_on"])
        for kind, label in labels[key].items():
            relative = Path(files[label])
            require(not relative.is_absolute() and ".." not in relative.parts and relative.parts[0] in ("external", "bazel-out"),
                    "unexpected APT content path: " + str(relative))
            path = (output_base if relative.parts[0] == "external" else execution_root) / relative
            require(path.is_file(), "missing built APT content file: " + str(path))
            item[kind + "_sha256"] = sha(path)
            item[kind + "_size"] = path.stat().st_size
        locked_packages[key] = item
    current, _ = resolution()
    for variant in apt_lock.SET_NAMES:
        current_roots, current_packages = apt_lock.resolved_closure(current, variant)
        require(current_roots == roots[variant] and all(current_packages[key] == packages[key] for key in current_packages),
                "APT resolution changed while its content was built")
    require(sha(ROOT / "MODULE.bazel") == module_sha, "MODULE.bazel changed during APT preparation")
    suites = {package["suite"] for package in locked_packages.values()}
    lock = {"schema": 1, "bazel_version": apt_lock.BAZEL_VERSION,
            "rules_distroless_version": apt_lock.DISTROLESS_VERSION, "roots": roots,
            "sources": {suite: resolved["sources"][suite] for suite in sorted(suites)},
            "packages": locked_packages}
    lock_bytes = (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode()
    labels_bytes = apt_lock.render(lock).encode()
    (artifacts / "apt.lock.json").write_bytes(lock_bytes)
    (artifacts / "apt_packages.bzl").write_bytes(labels_bytes)
    if args.update:
        publish(OWNER / "apt.lock.json", lock_bytes)
        publish(OWNER / "apt_packages.bzl", labels_bytes)
    else:
        require((OWNER / "apt.lock.json").read_bytes() == lock_bytes and
                (OWNER / "apt_packages.bzl").read_bytes() == labels_bytes,
                "checked syncd APT content changed; review the candidate and rerun with --update")
    receipt = {"schema": 1, "bazel_version": version, "rules_distroless_version": apt_lock.DISTROLESS_VERSION,
               "module_sha256": module_sha, "module_lock_sha256": sha(ROOT / "MODULE.bazel.lock"),
               "apt_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(), "package_count": len(locked_packages),
               "root_counts": {variant: len(value) for variant, value in roots.items()}, "updated": args.update,
               "execution_gate_audit": "execution-gate-audit.json"}
    (artifacts / "refresh.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--bazel-startup-arg", action="append", default=[])
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--apt-repo-prefix", default="rules_distroless++apt+")
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(refresh(args), indent=2, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, "syncd APT refresh failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
