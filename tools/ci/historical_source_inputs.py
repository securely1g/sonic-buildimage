#!/usr/bin/env python3
"""Transport the original historical Salsa and P4 sources without recipe edits."""
import argparse
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from historical_vs_backfill import INPUTS, P4_INPUTS, git, require
import p4_source_inputs as p4

P4_MANIFEST = Path(__file__).with_name("historical-p4-source-inputs.json")
P4_URL = "http://download.opensuse.org/repositories/home:/p4lang/Debian_11/"
P4_DSC_CACHE = "https://packages.trafficmanager.net/packages/p4lang-p4c_1.2.4.2-3.dsc-a532d27270ada5967e3a49110b42116b"
GIT_SOURCES = {
    "monit": {"url": "https://salsa.debian.org/debian/monit.git",
              "selector": "refs/tags/debian/1%5.34.3-1", "commit": None},
    "rasdaemon": {"url": "https://salsa.debian.org/tai271828/rasdaemon.git",
                  "selector": "51a7f485f8b2e2ae43e613f19c5a387595174132",
                  "commit": "51a7f485f8b2e2ae43e613f19c5a387595174132"},
}
INITRAMFS_URL = "https://salsa.debian.org/kernel-team/initramfs-tools/-/archive/v0.142/initramfs-tools-v0.142.tar.gz"
INITRAMFS_SHA256 = "27a78cc25acc3ca3d9c78deca165bf001b09b260ce25a3f3756e47a0e7bc0554"
SOURCE_FILES = ("src/monit/Makefile", "rules/monit.mk", "src/rasdaemon/Makefile",
                "src/initramfs-tools/Makefile", "rules/initramfs-tools.mk",
                "src/p4lang/Makefile", "rules/p4lang.mk",
                "files/build/versions-public/default/versions-web")


def fingerprint(path):
    require(path.is_file() and not path.is_symlink(), "Source input must be a regular file: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def source_contract(workspace, source_sha):
    require(source_sha in INPUTS and source_sha not in P4_INPUTS, "Only the three older sources need this transport")
    require(git(workspace, "rev-parse", "HEAD") == source_sha, "Historical source HEAD differs")
    text = {name: (workspace / name).read_text() for name in SOURCE_FILES}
    require("MONIT_VERSION = 5.34.3-1" in text["rules/monit.mk"] and
            r"git reset --hard debian/1\%$(MONIT_VERSION)" in text["src/monit/Makefile"], "Monit selector changed")
    for name, spec in GIT_SOURCES.items():
        require("git clone " + spec["url"] in text[f"src/{name}/Makefile"], name + " Git origin changed")
    require("RASDAEMON_COMMIT = " + GIT_SOURCES["rasdaemon"]["commit"] in text["src/rasdaemon/Makefile"], "Rasdaemon pin changed")
    require("INITRAMFS_TOOLS_VERSION = 0.142" in text["rules/initramfs-tools.mk"] and
            "INITRAMFS_TOOLS_TARBALL_SHA256 = " + INITRAMFS_SHA256 in text["src/initramfs-tools/Makefile"] and
            INITRAMFS_URL.replace("0.142", "$(INITRAMFS_TOOLS_VERSION)") in text["src/initramfs-tools/Makefile"], "initramfs input changed")
    for package, version in p4.VERSIONS.items():
        key = package.replace("p4lang-", "P4LANG_").upper()
        upstream, revision = version.rsplit("-", 1)
        require(f"{key}_VERSION = {upstream}" in text["rules/p4lang.mk"] and
                f"{key}_VERSION_FULL = $({key}_VERSION)-{revision}" in text["rules/p4lang.mk"], "P4 version changed")
        require(f"dget -u {package}_$({key}_VERSION_FULL).dsc {P4_URL}{package}_$({key}_VERSION_FULL).dsc"
                in text["src/p4lang/Makefile"], "Historical P4 URL or dget options changed")
    return {name: fingerprint(workspace / name)["sha256"] for name in SOURCE_FILES}


def fetch(url, destination, expected_sha256):
    # All original URLs are fixed above; redirects retain HTTP/HTTPS only.
    subprocess.run(["curl", "--disable", "--fail", "--location", "--silent", "--show-error",
                    "--retry", "2", "--connect-timeout", "20", "--max-time", "300",
                    "--proto", "=http,https", "--proto-redir", "=http,https",
                    "--output", str(destination), url], check=True)
    actual = fingerprint(destination)
    require(actual["sha256"] == expected_sha256, "Downloaded source SHA256 differs: " + url)
    return {"url": url, **actual}


def git_identity(repository, spec):
    selected = git(repository, "rev-parse", spec["selector"])
    commit = git(repository, "rev-parse", spec["selector"] + "^{commit}")
    require(spec["commit"] is None or commit == spec["commit"], "Pinned Git commit differs")
    return {"selected_object": selected, "commit": commit,
            "tree": git(repository, "rev-parse", commit + "^{tree}")}


def create_bundle(repository, bundle, spec):
    selected = git_identity(repository, spec)
    subprocess.run(["git", "-C", str(repository), "fsck", "--full"], check=True)
    subprocess.run(["git", "-C", str(repository), "bundle", "create", str(bundle.resolve()), "--all"], check=True)
    return {**spec, **selected, **fingerprint(bundle)}


def verify_bundle(bundle, expected, spec, repository):
    require(expected["url"] == spec["url"] and expected["selector"] == spec["selector"], "Git origin or selector differs")
    require(fingerprint(bundle) == {key: expected[key] for key in ("sha256", "bytes")}, "Git bundle bytes differ")
    subprocess.run(["git", "clone", "--mirror", str(bundle.resolve()), str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "bundle", "verify", str(bundle.resolve())], check=True)
    subprocess.run(["git", "-C", str(repository), "fsck", "--full"], check=True)
    require(git_identity(repository, spec) == {key: expected[key] for key in ("selected_object", "commit", "tree")}, "Git bundle selector/commit/tree differs")


def p4_fetch_url(name):
    return P4_DSC_CACHE if name == "p4lang-p4c_1.2.4.2-3.dsc" else P4_URL + name


def historical_web_pins(workspace, directory):
    pins = dict(line.rsplit("==", 1) for line in
                (workspace / "files/build/versions-public/default/versions-web").read_text().splitlines()
                if line.startswith(P4_URL) and "==" in line)
    matched, unlisted = {}, []
    for path in sorted(directory.iterdir()):
        expected = pins.get(P4_URL + path.name)
        if expected:
            actual = p4.fingerprints(path)[0]["md5"]
            require(actual == expected, "Historical versions-web pin differs: " + path.name)
            matched[path.name] = expected
        else:
            unlisted.append(path.name)
    require(len(matched) == 6 and all(name.startswith("p4lang-pi_") for name in unlisted),
            "Historical P4 web pin inventory differs")
    return {"matched_md5": matched, "unlisted_selected_pi_files": unlisted}


def stage(workspace, source_sha, output):
    recipe_hashes = source_contract(workspace, source_sha)
    output.mkdir(parents=True, exist_ok=False)
    (output / "bundles").mkdir()
    manifest_path = P4_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    inventory = p4.manifest_contract(manifest)
    receipt = {"schema": 1, "source_sha": source_sha, "driver_sha": os.environ["GITHUB_SHA"],
               "run_id": os.environ["GITHUB_RUN_ID"], "recipe_sha256": recipe_hashes,
               "git": {}, "p4": {}, "p4_manifest_sha256": fingerprint(manifest_path)["sha256"]}
    def save():
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    receipt["status"] = "running"
    save()
    try:
        with tempfile.TemporaryDirectory(prefix="historical-git-") as temporary:
            for name, spec in GIT_SOURCES.items():
                repository = Path(temporary) / (name + ".git")
                subprocess.run(["git", "clone", "--mirror", spec["url"], str(repository)], check=True)
                bundle = output / "bundles" / (name + ".bundle")
                receipt["git"][name] = create_bundle(repository, bundle, spec)
                save()
        receipt["initramfs"] = fetch(INITRAMFS_URL, output / "initramfs-tools.tar.gz", INITRAMFS_SHA256)
        save()
        (output / "p4").mkdir()
        for name, pin in inventory.items():
            actual = fetch(p4_fetch_url(name), output / "p4" / name, pin["sha256"])
            receipt["p4"][name] = {**actual, "original_url": P4_URL + name}
            save()
        # Reuse the maintained verifier with the separate historical descriptor
        # and its original mismatch record; never edit either source descriptor.
        require(p4.run(manifest_path, output / "p4", output / "p4-verification.json") == 0, "P4 published-input verification failed")
        receipt["p4_verification"] = json.loads((output / "p4-verification.json").read_text())
        receipt["historical_web_pins"] = historical_web_pins(workspace, output / "p4")
        receipt["status"] = "verified"
    except BaseException as error:
        receipt.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        save()
    return receipt


def dget_arguments(arguments, directory):
    names = {f"{package}_{version}.dsc" for package, version in p4.VERSIONS.items()}
    if len(arguments) == 3 and arguments[0] == "-u" and arguments[1] in names and arguments[2] == P4_URL + arguments[1]:
        return [*arguments[:2], "file:" + str(directory / arguments[1])]
    return arguments


def write_dget_launcher(launcher, original_dget, directory):
    launcher.write_text("#!" + sys.executable + "\nimport os,sys\nfrom pathlib import Path\n"
                        + "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parent)) + ")\n"
                        + "from historical_source_inputs import dget_arguments\n"
                        + "program=" + repr(original_dget) + "\n"
                        + "os.execv(program, [program, *dget_arguments(sys.argv[1:], Path(" + repr(str(directory.resolve())) + "))])\n")
    launcher.chmod(0o755)


def smoke_dget(launcher, directory, runtime):
    names = ("p4lang-pi_0.1.3-2.dsc", "p4lang-pi_0.1.3.orig.tar.gz", "p4lang-pi_0.1.3-2.debian.tar.xz")
    before = {name: fingerprint(directory / name) for name in names}
    arguments = ["-u", names[0], P4_URL + names[0]]
    with tempfile.TemporaryDirectory(prefix="dget-smoke-", dir=runtime) as temporary:
        scratch = Path(temporary)
        result = subprocess.run([str(launcher), *arguments], cwd=scratch, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        print(result.stdout, end="", flush=True)
        require(result.returncode == 0 and (scratch / "p4lang-pi-0.1.3/debian/control").is_file(),
                "Native dget could not extract the verified local PI source")
        require({name: fingerprint(scratch / name) for name in names} == before,
                "dget fetched different PI input bytes")
        require({name: fingerprint(directory / name) for name in names} == before,
                "dget changed the staged PI inputs")
    return {"status": "passed", "original_arguments": arguments,
            "input_files_unchanged": True, "scratch_removed": True}


def install(workspace, source_sha, inputs, runtime, environment_file):
    recipe_hashes = source_contract(workspace, source_sha)
    receipt = json.loads((inputs / "receipt.json").read_text())
    require(receipt["schema"] == 1 and receipt["status"] == "verified" and receipt["source_sha"] == source_sha and
            receipt["driver_sha"] == os.environ["GITHUB_SHA"] and receipt["run_id"] == os.environ["GITHUB_RUN_ID"] and
            receipt["recipe_sha256"] == recipe_hashes, "Staged inputs belong to another source, driver or run")
    require(set(receipt["git"]) == set(GIT_SOURCES), "Git input inventory differs")
    manifest_path = P4_MANIFEST
    require(receipt["p4_manifest_sha256"] == fingerprint(manifest_path)["sha256"], "P4 manifest changed")
    inventory = p4.manifest_contract(json.loads(manifest_path.read_text()))
    require(set(receipt["p4"]) == set(inventory), "P4 inventory differs")
    for name, pin in inventory.items():
        require(receipt["p4"][name] == {"url": p4_fetch_url(name), "original_url": P4_URL + name, **pin}, "P4 URL or fingerprint differs")
        require(fingerprint(inputs / "p4" / name) == pin, "P4 bytes differ: " + name)
    require(historical_web_pins(workspace, inputs / "p4") == receipt["historical_web_pins"], "Historical web validation differs")
    require(receipt["initramfs"]["url"] == INITRAMFS_URL and
            receipt["initramfs"]["sha256"] == INITRAMFS_SHA256 and
            fingerprint(inputs / "initramfs-tools.tar.gz") == {key: receipt["initramfs"][key] for key in ("sha256", "bytes")}, "initramfs bytes differ")
    runtime.mkdir(parents=True, exist_ok=False)
    require(p4.run(manifest_path, inputs / "p4", runtime / "p4-verification.json") == 0, "Native P4 verification failed")
    assignments = {}
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    require(count >= 0, "Invalid inherited Git configuration count")
    for name, spec in GIT_SOURCES.items():
        expected = receipt["git"][name]
        bundle = inputs / "bundles" / (name + ".bundle")
        repository = runtime / (name + ".git")
        verify_bundle(bundle, expected, spec, repository)
        assignments[f"GIT_CONFIG_KEY_{count}"] = "url." + repository.resolve().as_uri() + ".insteadOf"
        assignments[f"GIT_CONFIG_VALUE_{count}"] = spec["url"]
        count += 1
    assignments[f"GIT_CONFIG_KEY_{count}"] = "protocol.file.allow"
    assignments[f"GIT_CONFIG_VALUE_{count}"] = "always"
    assignments["GIT_CONFIG_COUNT"] = str(count + 1)
    binary = runtime / "bin"
    binary.mkdir()
    original_dget = shutil.which("dget")
    require(original_dget is not None, "Native worker lacks dget")
    launcher = binary / "dget"
    write_dget_launcher(launcher, original_dget, inputs / "p4")
    dget_smoke = smoke_dget(launcher, (inputs / "p4").resolve(), runtime)
    (runtime / "http").mkdir()
    shutil.copyfile(inputs / "initramfs-tools.tar.gz", runtime / "http/initramfs-tools.tar.gz")
    assignments["BACKFILL_INITRAMFS_DIRECTORY"] = str((runtime / "http").resolve())
    with environment_file.open("x") as stream:
        for name, value in assignments.items():
            stream.write(f"export {name}={shlex.quote(value)}\n")
        stream.write("export PATH=" + shlex.quote(str(binary.resolve())) + ':"$PATH"\n')
    receipt.update(native_verified=True, dget_original=original_dget,
                   dget_launcher_sha256=fingerprint(launcher)["sha256"], dget_smoke=dget_smoke,
                   source_recipes_unchanged=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "install", "serve"))
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--source-sha", choices=INPUTS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--environment-file", type=Path)
    args = parser.parse_args()
    if args.command == "serve":
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=args.inputs)
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            args.output.write_text(str(server.server_port) + "\n")
            server.serve_forever()
    elif args.command == "stage":
        stage(args.workspace, args.source_sha, args.output)
    else:
        receipt = install(args.workspace, args.source_sha, args.inputs, args.runtime, args.environment_file)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
