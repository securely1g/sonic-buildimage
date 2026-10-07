#!/usr/bin/env python3
"""Exercise the pinned P4 package import with fresh checkouts and output bases.

Only the private repository cache is shared. Make only emits an SBOM fragment
for an imported package. No DEB is generated, and the agent's existing build
outputs or caches are not changed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
TARGET = "//tools/bazel/p4:debs"
LOCK = Path("tools/bazel/p4/packages.lock.json")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command, cwd, log, *, success=True):
    started = time.monotonic()
    with log.open("w") as output:
        result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
    receipt = {"command": [str(part) for part in command], "exit_code": result.returncode,
               "seconds": round(time.monotonic() - started, 3)}
    if success != (result.returncode == 0):
        raise RuntimeError(f"unexpected exit {result.returncode}; see {log}")
    return receipt


def checkout(source, destination):
    """Export committed files only: no local outputs, locks, or Make cache."""
    destination.mkdir()
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=source,
                       stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as stream:
            stream.extractall(destination, filter="data")
    for forbidden in ("MODULE.bazel.lock", "target/debs", ".sonic-build-cache"):
        if (destination / forbidden).exists():
            raise ValueError(f"clean checkout unexpectedly contains {forbidden}")


def bazel_wrapper(bazel, output_base, path):
    # A new process and output base cannot reuse a previous external directory.
    command = [bazel, "--batch", "--output_base=" + str(output_base),
               "--output_user_root=" + str(output_base.with_name(output_base.name + "-user"))]
    path.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
    path.chmod(0o755)
    return str(path)


def package_details(path, package, architecture):
    fields = subprocess.check_output(
        ["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"], text=True)
    control = dict(line.split(": ", 1) for line in fields.splitlines())
    expected = {"Package": package["name"], "Version": package["version"],
                "Architecture": architecture}
    if control != expected:
        raise ValueError(f"incorrect package control for {path.name}: {control} != {expected}")
    if path.stat().st_size != package["size"] or sha256(path) != package["sha256"]:
        raise ValueError("incorrect package bytes: " + path.name)
    elfs = []
    with subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(path)],
                          stdout=subprocess.PIPE) as process:
        with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
            for member in stream:
                if not member.isfile():
                    continue
                with stream.extractfile(member) as contents:
                    header = contents.read(64)
                if header[:4] != b"\x7fELF":
                    continue
                if len(header) < 20 or header[4:6] != b"\x02\x01":
                    raise ValueError("expected little-endian ELF64: " + member.name)
                machine = struct.unpack_from("<H", header, 18)[0]
                if machine != 62:
                    raise ValueError("non-AMD64 ELF in " + path.name + ": " + member.name)
                elfs.append(member.name)
        if process.wait() != 0:
            raise ValueError("cannot read payload: " + path.name)
    return {"filename": path.name, "sha256": package["sha256"], "bytes": path.stat().st_size,
            "control": control, "elf_machine": "AMD64", "elf_files": elfs}


def assert_no_actions(path):
    graph = json.loads(path.read_text())
    actions = graph.get("actions", [])
    if actions:
        raise ValueError("import target registered build actions: " + repr(actions))
    return {"registered_actions": 0, "target": TARGET}


def verify_sbom(source, output, package, evidence):
    """Exercise the production Make recipe and SBOM emitter on an existing DEB."""
    macro = next(line for line in (source / "slave.mk").read_text().splitlines()
                 if line.startswith("sbom_emit_fragment = "))
    filename = package["filename"]
    artifact = output / filename
    makefile = evidence / "sbom.mk"
    makefile.write_text("\n".join([
        "SHELL := /bin/bash", ".ONESHELL:", ".SHELLFLAGS := -ec",
        "ENABLE_SBOM := y", "SBOM_STRICT := y",
        "CONFIGURED_PLATFORM := vs", "CONFIGURED_ARCH := amd64",
        "DEBS_PATH := " + str(output),
        "SONIC_BAZEL_P4_DEBS := " + filename,
        "SONIC_BAZEL_P4_CANDIDATES := " + filename,
        # Native metadata remains present: the imported recipe must not use it
        # to claim that the current checkout produced these historical bytes.
        filename + "_SRC_PATH := src/p4lang",
        macro, "include tools/bazel/p4/debs.mk", "",
    ]))
    # The package was already fetched and verified. Run its actual metadata
    # recipe while treating the import prerequisite as already complete.
    command = ["make", "--no-print-directory", "-B", "-o", "bazel-p4-import",
               "-f", str(makefile), str(artifact)]
    run(command, source, evidence / "sbom.log")
    fragment_path = Path(str(artifact) + ".cdx.json")
    fragment = json.loads(fragment_path.read_text())
    component = next(item for item in fragment["components"] if item["name"] == package["name"])
    if component.get("externalReferences") != [{"type": "distribution", "url": package["url"]}]:
        raise ValueError("P4 SBOM did not record the pinned package URL")
    if component.get("hashes") != [{"alg": "SHA-256", "content": package["sha256"]}]:
        raise ValueError("P4 SBOM did not record the imported package hash")
    properties = {item["name"]: item["value"] for item in component["properties"]}
    if "sonic:src_path" in properties or "sonic:submodule_commit" in properties or "pedigree" in component:
        raise ValueError("P4 SBOM incorrectly attributed imported bytes to the current checkout")
    shutil.copyfile(fragment_path, evidence / "p4-package.cdx.json")
    return {"filename": filename, "distribution_url": package["url"],
            "sha256": package["sha256"], "native_source_attribution": False}


def phase(args, work, name, cache, *, offline=False, success=True, change_lock=None):
    source = work / (name + "-checkout")
    checkout(ROOT, source)
    lock = json.loads((source / LOCK).read_text())
    if change_lock:
        change_lock(lock)
        (source / LOCK).write_text(json.dumps(lock, indent=2) + "\n")
    evidence = args.artifacts / name
    evidence.mkdir()
    wrapper = bazel_wrapper(args.bazel, work / (name + "-output"), work / (name + "-bazel"))
    output = work / (name + "-packages")
    command = [sys.executable, str(source / "tools/bazel/p4/stage.py"),
               "--output-directory", str(output), "--cache-directory", str(cache),
               "--bazel", wrapper]
    if offline:
        command.append("--bazel-arg=--repository_disable_download")
    receipt = run(command, source, evidence / "stage.log", success=success)
    receipt.update({"repository_downloads_disabled": offline, "fresh_checkout": True,
                    "fresh_output_base": True})
    if not success:
        failure = (evidence / "stage.log").read_text()
        package = lock["packages"][0]
        if not any(value in failure for value in (package["repository"], package["filename"])):
            raise ValueError(name + " failed for a reason unrelated to the selected P4 package")
        if name == "wrong-pin" and "checksum" not in failure.lower():
            raise ValueError("wrong-pin failure did not report a checksum mismatch")
        if output.exists() and list(output.glob("*.deb")):
            raise ValueError(name + " published packages despite the failed import")
        return receipt
    receipt["packages"] = [package_details(output / item["filename"], item,
                                          lock["architecture"]) for item in lock["packages"]]
    receipt["sbom"] = verify_sbom(source, output, lock["packages"][0], evidence)
    command = [wrapper, "aquery", "--output=jsonproto", "--lockfile_mode=update",
               "--repository_cache=" + str(cache / "repository_cache"),
               "--disk_cache=", "--remote_cache=", "--remote_executor=", TARGET]
    if offline:
        command.insert(-1, "--repository_disable_download")
    # Keep stdout parseable; Bazel progress goes to stderr.
    with (evidence / "actions.json").open("w") as stdout, (evidence / "aquery.log").open("w") as stderr:
        subprocess.run(command, cwd=source, stdout=stdout, stderr=stderr, check=True)
    receipt["action_audit"] = assert_no_actions(evidence / "actions.json")
    generated = source / "MODULE.bazel.lock"
    if not generated.is_file():
        raise ValueError("Bazel did not generate MODULE.bazel.lock")
    shutil.copyfile(generated, evidence / "MODULE.bazel.lock")
    return receipt


def cache_entry(cache, digest):
    entry = cache / "repository_cache/content_addressable/sha256" / digest / "file"
    if not entry.is_file() or sha256(entry) != digest:
        raise ValueError("package missing from Bazel's SHA256 repository cache: " + str(entry))
    return entry


def verify(args, work):
    lock = json.loads((ROOT / LOCK).read_text())
    if platform.machine() != "x86_64" or lock["architecture"] != "amd64":
        raise ValueError("this package lock and verifier support native AMD64 only")
    if subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip() != "amd64":
        raise ValueError("expected native AMD64 Debian userspace")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "."], cwd=ROOT, check=True)
    cache = work / "cache"
    if cache.exists():
        raise ValueError("cold cache already exists")
    result = {"bazel_version": subprocess.check_output([args.bazel, "version", "--gnu_format"], text=True).strip(),
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "revision_parents": subprocess.check_output(
                  ["git", "show", "-s", "--format=%P", "HEAD"], cwd=ROOT, text=True).split(),
              "github_sha": os.environ.get("GITHUB_SHA"),
              "architecture": platform.machine(), "target": TARGET,
              "package_count": len(lock["packages"]), "make_invoked": True,
              "make_scope": "SBOM emission only; package import prerequisite skipped",
              "native_package_producer_invoked": False,
              "phases": {}}
    result["phases"]["cold"] = phase(args, work, "cold", cache)
    result["phases"]["warm"] = phase(args, work, "warm", cache, offline=True)
    if result["phases"]["cold"]["packages"] != result["phases"]["warm"]["packages"]:
        raise ValueError("warm repository cache changed the package bytes")
    # Mutate only this verifier's disposable cache, then restore it immediately.
    package = lock["packages"][0]
    entry = cache_entry(cache, package["sha256"])
    saved = work / "original-package"
    entry.replace(saved)
    try:
        result["phases"]["missing"] = phase(args, work, "missing", cache,
                                                offline=True, success=False)
        entry.write_bytes(b"corrupted cached package: this is not a DEB\n")
        result["phases"]["tampered"] = phase(args, work, "tampered", cache,
                                                 offline=True, success=False)
    finally:
        entry.unlink(missing_ok=True)
        saved.replace(entry)
    def wrong_pin(current):
        digest = current["packages"][0]["sha256"]
        current["packages"][0]["sha256"] = ("1" if digest[0] != "1" else "2") + digest[1:]
    result["phases"]["wrong-pin"] = phase(args, work, "wrong-pin", cache,
                                              success=False, change_lock=wrong_pin)
    return result


def remove_work(directory):
    """Remove only temporary verifier data; Bazel may create read-only dirs."""
    for root, directories, _ in os.walk(directory, followlinks=False):
        Path(root).chmod(0o700)
        for name in directories:
            child = Path(root) / name
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--work-directory", type=Path, help="parent for disposable checkouts and cache")
    args = parser.parse_args()
    resolved = shutil.which(args.bazel)
    if not resolved:
        parser.error("Bazel executable not found: " + args.bazel)
    args.bazel = str(Path(resolved).resolve())
    args.artifacts = args.artifacts.resolve()
    args.artifacts.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="p4-import-check-", dir=args.work_directory))
    try:
        result = verify(args, work)
        (args.artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        remove_work(work)


if __name__ == "__main__":
    main()
