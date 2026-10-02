#!/usr/bin/env python3
"""Build the pinned Bazel kernel and verify its handoff to native Make."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse


MANIFEST = "kernel-packages.json"
PROVENANCE = "kernel-provenance.json"
TARGET = "@sonic_linux_kernel//:kernel_packages"
INPUTS = Path("target/bazel-kernel-inputs")
REGISTRY_PREFIX = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
CI_REGISTRY = REGISTRY_PREFIX + "codex/sonic-linux-kernel"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def endpoint(value):
    """Keep credentials out of command lines, logs, and retained evidence."""
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme in {"http", "https", "grpc", "grpcs"} and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and not re.search(r"[\s\x00-\x1f]", value),
            "kernel cache needs an HTTP(S) or gRPC(S) endpoint without URL credentials or query parameters")
    return value


def contract(workspace):
    """Read only literal version assignments, never execute Make source here."""
    contents = (workspace / "rules/linux-kernel.mk").read_text()
    values = {}
    for name in ("KERNEL_VERSION", "KERNEL_ABISUFFIX", "KERNEL_SUBVERSION", "KERNEL_FEATURESET"):
        matches = re.findall(r"^" + name + r"\s*=\s*([A-Za-z0-9.+~-]+)\s*$", contents, re.MULTILINE)
        require(len(matches) == 1, "kernel version assignment must be literal: " + name)
        values[name] = matches[0]
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
    return {"schema": 1, "architecture": "amd64", "platform": "vs",
            "kernel_version": version, "kernel_abi": kernel,
            "package_version": release, "signing": "unsigned"}, {
        package + "_" + release + "_" + arch + ".deb": {
            "package": package, "version": release, "architecture": arch}
        for package, arch in packages.items()}


def regular(path):
    require(path.is_file() and not path.is_symlink(), "expected regular kernel output: " + str(path))
    require(path.stat().st_size > 0, "empty kernel output: " + str(path))
    return path


def source_identity(workspace):
    source = workspace / "src/sonic-linux-kernel"
    helper = regular(source / "tools/bazel/kernel_action.py")
    identity = subprocess.check_output(
        [sys.executable, str(helper), "--source-tree-sha256", str(source)], text=True, timeout=60).strip()
    require(re.fullmatch(r"[0-9a-f]{64}", identity), "invalid kernel checkout source digest")
    return identity


def verify(bundle, workspace, *, check_debs=True):
    bundle = bundle.resolve(strict=True)
    manifest = json.loads(regular(bundle / MANIFEST).read_text())
    expected, packages = contract(workspace)
    require(all(manifest.get(key) == value for key, value in expected.items()),
            "Bazel kernel does not match the native AMD64 VS package contract")
    require(re.fullmatch(r"[0-9a-f]{64}", manifest.get("source_tree_sha256", "")),
            "kernel manifest has no declared source tree digest")
    require(manifest["source_tree_sha256"] == source_identity(workspace),
            "kernel action source inputs differ from the pinned kernel checkout")
    tools = manifest.get("build_tools", {})
    require(tools.get("schema_version") == 1 and tools.get("kind") == "debian-build-tools"
            and tools.get("architecture") == "amd64"
            and re.fullmatch(r"[0-9a-f]{64}", tools.get("identity_sha256", ""))
            and isinstance(tools.get("packages"), dict) and tools["packages"],
            "kernel build tools must have a declared runtime identity")
    archives = manifest.get("source_archives")
    require(isinstance(archives, list) and archives and all(
        isinstance(item, dict) and isinstance(item.get("name"), str)
        and re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) for item in archives),
        "kernel manifest omitted its source archive identities")
    entries = manifest.get("packages")
    require(isinstance(entries, list) and len(entries) == len(packages)
            and all(isinstance(item, dict) for item in entries),
            "kernel manifest must contain exactly four packages")
    require({item.get("name") for item in entries} == set(packages),
            "kernel package set differs from the native prerequisites")
    for item in entries:
        name = item["name"]
        require(all(item.get(key) == value for key, value in packages[name].items()),
                "kernel package metadata differs: " + name)
        require(type(item.get("size")) is int and item["size"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")),
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
    return manifest


def copy_bundle(source, destination, workspace):
    manifest = verify(source, workspace)
    destination.mkdir(parents=True, exist_ok=False)
    for name in [MANIFEST, PROVENANCE, *(item["name"] for item in manifest["packages"])]:
        regular(source / name)
        with (source / name).open("rb") as incoming, (destination / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    require(verify(destination, workspace) == manifest, "kernel bundle changed during staging")
    require(sha256(source / PROVENANCE) == sha256(destination / PROVENANCE),
            "kernel provenance changed during staging")
    return manifest


def verify_provenance(bundle, workspace, source_commit):
    manifest = verify(bundle, workspace)
    provenance = json.loads(regular(bundle / PROVENANCE).read_text())
    require(provenance.get("schema") == 1 and provenance.get("source_commit") == source_commit
            and provenance.get("manifest_sha256") == sha256(bundle / MANIFEST)
            and provenance.get("target") == TARGET
            and re.fullmatch(r"[0-9a-f]{40}", provenance.get("kernel_gitlink", "")),
            "kernel bundle provenance differs from this source invocation")
    recorded = subprocess.check_output([
        "git", "-c", "safe.directory=" + str(workspace), "-C", str(workspace),
        "ls-tree", "HEAD", "--", "src/sonic-linux-kernel"], text=True, timeout=30).strip()
    require(recorded == "160000 commit " + provenance["kernel_gitlink"] + "\tsrc/sonic-linux-kernel",
            "kernel provenance gitlink differs from the native source revision")
    return {"manifest": manifest, "provenance": provenance}


def stage_outputs(workdir, bundle, workspace, source):
    """Accept only the kernel launcher's declared outputs below its output root."""
    names = {}
    workdir = workdir.resolve(strict=True)
    for line in regular(workdir / "output-paths.txt").read_text().splitlines():
        path = Path(line)
        require(path.is_absolute() and path.is_relative_to("/work") and ".." not in path.parts,
                "kernel output does not belong to the launcher's /work output root")
        candidate = workdir / path.relative_to("/work")
        resolved = candidate.resolve(strict=True)
        require(resolved.is_relative_to(workdir), "kernel output escapes its build output root")
        regular(resolved)
        require(path.name not in names, "duplicate kernel output: " + path.name)
        names[path.name] = resolved
    expected = set(contract(workspace)[1]) | {MANIFEST}
    require(set(names) == expected, "kernel target returned an unexpected output set")
    bundle.mkdir(parents=True, exist_ok=False)
    for name, path in names.items():
        with path.open("rb") as incoming, (bundle / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    manifest = verify(bundle, workspace)
    provenance = {"schema": 1, "source_commit": source["source_commit"],
                  "kernel_gitlink": source["components"]["src/sonic-linux-kernel"]["gitlink"],
                  "manifest_sha256": sha256(bundle / MANIFEST), "target": TARGET}
    (bundle / PROVENANCE).write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return {"manifest": manifest, "provenance": provenance}


def configure_registry(workspace, ci_registry):
    """Select one SONiC endpoint in the disposable kernel workspace."""
    rc = workspace / ".bazelrc"
    contents = rc.read_text()
    matches = list(re.finditer(r"^common --registry=(" + re.escape(REGISTRY_PREFIX) + r"\S+)$",
                              contents, re.MULTILINE))
    require(len(matches) == 1 and re.fullmatch(re.escape(REGISTRY_PREFIX) + r"[0-9a-f]{40}", matches[0][1]),
            "kernel workspace must select one immutable SONiC registry snapshot")
    registry = CI_REGISTRY if ci_registry else matches[0][1]
    if ci_registry:
        start, end = matches[0].span(1)
        rc.write_text(contents[:start] + registry + contents[end:])
    return registry


def build(workspace, state, artifacts, source, invocation, remote_cache, upload, execute, receipt, disk_cache=None,
          *, ca_bundle=None, java_trust_store=None, ci_registry=False):
    if remote_cache:
        remote_cache = endpoint(remote_cache)
    workdir = state / ("kernel-" + invocation)
    bundle = state / ("kernel-packages-" + invocation)
    # Bazel writes a resolution lock even when no source action runs. Keep that
    # generated state out of the independent pristine image source checkout.
    kernel_workspace = state / ("kernel-source-" + invocation)
    shutil.copytree(workspace / "tools/bazel/kernel", kernel_workspace)
    registry = configure_registry(kernel_workspace, ci_registry)
    command = [sys.executable, str(workspace / "src/sonic-linux-kernel/tools/bazel/build.py"),
               "--workspace", str(kernel_workspace), "--work-dir", str(workdir),
               "--repository-cache", str(state / "repository-cache"), "--target", TARGET]
    if remote_cache:
        command += ["--remote-cache", remote_cache]
    if not upload:
        command.append("--remote-cache-read-only")
    if disk_cache:
        cache = Path(disk_cache).resolve()
        require(cache.is_relative_to(state) and cache != state, "kernel disk cache must be a dedicated directory in state")
        command += ["--disk-cache", str(cache)]
    execution_trust = {}
    for option, path in (("ca-bundle", ca_bundle), ("java-trust-store", java_trust_store)):
        if path:
            path = regular(Path(path).resolve(strict=True))
            command += ["--" + option, str(path)]
            execution_trust[option.replace("-", "_") + "_sha256"] = sha256(path)
    try:
        execute(command, workspace, artifacts, receipt, "kernel-build")
    finally:
        evidence_dir = artifacts / "kernel"
        evidence_dir.mkdir(exist_ok=False)
        for name in ("build.log", "invocation.json", "execution.json", "profile.json.gz", "bep.json", "output-paths.txt"):
            path = workdir / name
            if path.is_file():
                regular(path)
                shutil.copyfile(path, evidence_dir / name)
        lock = kernel_workspace / "MODULE.bazel.lock"
        if lock.is_file():
            regular(lock)
            shutil.copyfile(lock, evidence_dir / lock.name)
    evidence = stage_outputs(workdir, bundle, workspace, source)
    evidence.update(bundle=str(bundle), workdir=str(workdir), remote_cache=remote_cache,
                    upload_local_results=upload, disk_cache=str(disk_cache) if disk_cache else None,
                    execution_trust=execution_trust, registry=registry)
    receipt["kernel"] = evidence
    (artifacts / "kernel-receipt.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--workspace", default=Path.cwd(), type=Path)
    args = parser.parse_args()
    verify(args.bundle, args.workspace)


if __name__ == "__main__":
    main()
