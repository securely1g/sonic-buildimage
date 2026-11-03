#!/usr/bin/env python3
"""Stage the original LLDP source and preserve dget's signature/checksum checks."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCES = {
    "8bf48af011622657a73570a54d1a660b821bb30a",
    "13d750dee1750ee1c325e0fea9b7b4eaaf10cb14",
    "2dc503aaecfb0d3c317608aaeabc68a108e32b97",
    "c20b1d43e26c6eb2928191f041b78a60251ef9a0",
}
RECIPES = {
    "rules/lldpd.mk": "289a936f2291397b116f8a7e2081c3e4fbf7d86a9f6b24602e579189a49de44f",
    "src/lldpd/Makefile": "e8c358c7fef520f8b4edff7d88f449396a2927225b71223e722e497fa8730495",
}
DSC = "lldpd_1.0.16-1+deb12u1.dsc"
ORIGINAL_URL = "https://packages.trafficmanager.net/public/debian/pool/main/l/lldpd/" + DSC
DEBIAN_URL = "https://deb.debian.org/debian/pool/main/l/lldpd/"
PINS = {
    DSC: (2473, "1fd430c69c5fe708d0e31e1c2dd0dca880f84bdd5aa319e71402d2dd15983e4a"),
    "lldpd_1.0.16.orig.tar.gz": (1983821, "e3b391650c7ba67cea2fe84d67fdb4d7fc8aa1ec5cf86eb8bb984711df8465a9"),
    "lldpd_1.0.16.orig.tar.gz.asc": (833, "8a8b4e14f4d7993b69c204ad1fcfd176a48d9c9714a98e5dc01f9cccca125a14"),
    "lldpd_1.0.16-1+deb12u1.debian.tar.xz": (15168, "8386992f3755e3aef666ef9a9a5e33df95add994e595cffc535645511bcafec9"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(path):
    require(path.is_file() and not path.is_symlink(), "Expected regular source file: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def source_contract(workspace, source_sha, reviewed_runtime_source=False):
    require(source_sha in SOURCES or (reviewed_runtime_source and re.fullmatch(r"[0-9a-f]{40}", source_sha)),
            "Source is not an allowlisted historical revision or explicit reviewed runtime HEAD")
    actual = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
    require(actual == source_sha, "Historical source HEAD differs")
    for name, digest in RECIPES.items():
        require(fingerprint(workspace / name)["sha256"] == digest, "Original LLDP recipe/version changed: " + name)
    return dict(RECIPES)


def verify_inputs(directory):
    inventory = {}
    for name, (size, digest) in PINS.items():
        actual = fingerprint(directory / name)
        require(actual == {"bytes": size, "sha256": digest}, "LLDP source fingerprint differs: " + name)
        inventory[name] = {"url": DEBIAN_URL + name, **actual}
    # This verifies the signed DSC and every referenced member, including the
    # detached upstream signature. Do not use --nosigcheck or dget -u here.
    result = subprocess.run(["dscverify", "--no-conf", DSC], cwd=directory,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="", flush=True)
    require(result.returncode == 0, "LLDP DSC signature/checksum verification failed")
    return {"files": inventory, "signature_and_checksums_verified": True,
            "verification_output": result.stdout}


def stage(output):
    output.mkdir(parents=True, exist_ok=False)
    for name, (size, digest) in PINS.items():
        subprocess.run(["curl", "--disable", "--fail", "--location", "--silent", "--show-error",
                        "--retry", "2", "--connect-timeout", "20", "--max-time", "300",
                        "--proto", "=https", "--proto-redir", "=https",
                        "--output", str(output / name), DEBIAN_URL + name], check=True)
        require(fingerprint(output / name) == {"bytes": size, "sha256": digest},
                "Downloaded LLDP source differs: " + name)
    receipt = {"schema": 1, "status": "verified", "original_url": ORIGINAL_URL,
               "source_version": "1.0.16-1+deb12u1", "recipe_sha256": RECIPES,
               "producer_driver_sha": os.environ.get("GITHUB_SHA"),
               "producer_run_id": os.environ.get("GITHUB_RUN_ID"),
               "helper_sha256": fingerprint(Path(__file__))["sha256"], **verify_inputs(output)}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def dget_arguments(arguments, directory):
    # Preserve default signature/checksum verification and every other dget call.
    if arguments == [ORIGINAL_URL]:
        return ["file:" + str(directory.resolve() / DSC)]
    return arguments


def write_launcher(path, original_dget, directory, workspace, source_sha):
    # Self-contained: an already-running old job can use this private launcher
    # without changing the checked-out control helper or the source repository.
    path.write_text("#!" + sys.executable + "\nimport hashlib,os,pathlib,subprocess,sys\n"
                    + "arguments=sys.argv[1:]\n"
                    + "if arguments == [" + repr(ORIGINAL_URL) + "]:\n"
                    + "    root=pathlib.Path(" + repr(str(workspace.resolve())) + ")\n"
                    + "    if subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()!=" + repr(source_sha) + ": raise RuntimeError('Source HEAD changed')\n"
                    + "    for name,digest in " + repr(RECIPES) + ".items():\n"
                    + "        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest: raise RuntimeError('LLDP recipe changed')\n"
                    + "    arguments=[" + repr("file:" + str(directory.resolve() / DSC)) + "]\n"
                    + "program=" + repr(str(original_dget)) + "\n"
                    + "os.execv(program, [program, *arguments])\n")
    path.chmod(0o755)


def smoke_dget(launcher, inputs, runtime):
    before = {name: fingerprint(inputs / name) for name in PINS}
    with tempfile.TemporaryDirectory(prefix="lldp-dget-smoke-", dir=runtime) as temporary:
        scratch = Path(temporary)
        result = subprocess.run([str(launcher), ORIGINAL_URL], cwd=scratch,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        print(result.stdout, end="", flush=True)
        require(result.returncode == 0 and (scratch / "lldpd-1.0.16/debian/control").is_file(),
                "Original LLDP dget invocation failed signature verification or extraction")
        require({name: fingerprint(scratch / name) for name in PINS} == before,
                "LLDP dget extracted different input bytes")
        require({name: fingerprint(inputs / name) for name in PINS} == before,
                "LLDP dget changed staged inputs")
    return {"status": "passed", "arguments": [ORIGINAL_URL], "added_options": [],
            "input_files_unchanged": True, "scratch_removed": True}


def publish_launcher(candidate, destination, exclusive=False):
    # Write and chmod on the destination filesystem before exposing the path.
    with tempfile.NamedTemporaryFile(prefix=".lldp-dget-", dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(candidate.read_bytes())
    try:
        temporary.chmod(0o755)
        if exclusive:
            os.link(temporary, destination)
        else:
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    candidate.unlink()


def install(workspace, source_sha, inputs, runtime, environment_file=None,
            replace_launcher=None, expected_launcher_sha256=None,
            runtime_launcher=None, reviewed_runtime_source=False):
    recipes = source_contract(workspace, source_sha, reviewed_runtime_source)
    staged = json.loads((inputs / "receipt.json").read_text())
    require(staged["schema"] == 1 and staged["status"] == "verified" and
            staged["original_url"] == ORIGINAL_URL and staged["recipe_sha256"] == recipes,
            "LLDP staged-source receipt differs")
    verified = verify_inputs(inputs)
    require(staged["files"] == verified["files"], "LLDP staged-file provenance differs")
    runtime.mkdir(parents=True, exist_ok=False)
    binary = runtime / "bin"
    binary.mkdir()
    previous_sha = None
    require(not (replace_launcher and runtime_launcher), "Choose one runtime launcher mode")
    if runtime_launcher is not None:
        require(environment_file is None and reviewed_runtime_source, "Runtime PATH installation must be explicitly reviewed")
        require(runtime_launcher == Path("/usr/local/bin/dget") and not runtime_launcher.exists(),
                "Only create an absent worker-private /usr/local/bin/dget")
        original = Path("/usr/bin/dget")
        require(original.is_file(), "Original dget is missing")
        destination = runtime_launcher
    elif replace_launcher is not None:
        require(environment_file is None and expected_launcher_sha256,
                "Runtime replacement requires the exact previous launcher hash")
        require(source_sha in SOURCES - {"c20b1d43e26c6eb2928191f041b78a60251ef9a0"},
                "Only the older jobs have an existing private P4 launcher")
        previous_sha = fingerprint(replace_launcher)["sha256"]
        require(previous_sha == expected_launcher_sha256, "Existing private dget launcher changed")
        require(replace_launcher.parent.name == "bin" and
                replace_launcher.parent.parent.name == "historical-source-transport",
                "Only replace the job-private historical-source launcher")
        original = runtime / "dget-before-lldp"
        shutil.copyfile(replace_launcher, original)
        original.chmod(0o755)
        destination = replace_launcher
    else:
        original = shutil.which("dget")
        require(original is not None and environment_file is not None,
                "Native worker requires dget and a private environment output")
        destination = binary / "dget"
    candidate = binary / "dget.candidate"
    write_launcher(candidate, original, inputs, workspace, source_sha)
    smoke = smoke_dget(candidate, inputs, runtime)
    # Publish only after the real dget signature checks and extraction pass.
    # A failed smoke leaves an already-running build's original launcher intact.
    source_contract(workspace, source_sha, reviewed_runtime_source)
    if replace_launcher is not None:
        require(fingerprint(replace_launcher)["sha256"] == previous_sha,
                "Existing private dget launcher changed during validation")
    publish_launcher(candidate, destination, exclusive=runtime_launcher is not None)
    if environment_file is not None:
        import shlex
        with environment_file.open("x") as stream:
            stream.write("export PATH=" + shlex.quote(str(binary.resolve())) + ':"$PATH"\n')
    return {"schema": 1, "status": "verified", "source_sha": source_sha,
            "recipe_sha256": recipes, "source_recipes_unchanged": True,
            "original_url": ORIGINAL_URL, "local_url": "file:" + str(inputs.resolve() / DSC),
            "files": verified["files"], "signature_and_checksums_verified": True,
            "dget_smoke": smoke, "launcher": str(destination),
            "launcher_sha256": fingerprint(destination)["sha256"],
            "previous_launcher_sha256": previous_sha, "delegate": str(original),
            "producer_driver_sha": staged.get("producer_driver_sha"),
            "producer_run_id": staged.get("producer_run_id"),
            "installer_helper_sha256": fingerprint(Path(__file__))["sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "install"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--environment-file", type=Path)
    parser.add_argument("--replace-launcher", type=Path)
    parser.add_argument("--expected-launcher-sha256")
    parser.add_argument("--runtime-launcher", type=Path)
    parser.add_argument("--allow-reviewed-runtime-source", action="store_true")
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.output)
    else:
        require(all((args.workspace, args.source_sha, args.inputs, args.runtime)), "Missing native LLDP install inputs")
        receipt = install(args.workspace, args.source_sha, args.inputs, args.runtime,
                          args.environment_file, args.replace_launcher, args.expected_launcher_sha256,
                          args.runtime_launcher, args.allow_reviewed_runtime_source)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
