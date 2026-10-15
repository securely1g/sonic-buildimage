#!/usr/bin/env python3
"""Exercise Debian package management in disposable native image containers.

The ordinary CLI runs on the Docker host. It downloads two checksum-pinned,
existing Debian packages and supplies a local APT repository to network-disabled
containers. --in-container is an internal entry point for the read-only mounted
copy of this script. It never creates Debian packages or changes a running image.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request
import uuid

MACHINES = {"amd64": "x86_64", "arm64": "aarch64"}
RESULT_PREFIX = "SONIC_DPKG_RESULT="
NOTICE = {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
          "generator": "tools/bazel/ci/validate_dpkg_state.py"}
PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::(?:amd64|arm64|all))?\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_lock(path, architecture):
    value = json.loads(Path(path).read_text())
    require(value.get("schema") == 1 and architecture in MACHINES, "Unsupported transaction lock or architecture")
    require(PACKAGE_NAME.fullmatch(value["package"]), "Invalid transaction package name")
    require(re.fullmatch(r"/[a-zA-Z0-9_./+-]+", value["executable"]) and
            ".." not in Path(value["executable"]).parts, "Invalid transaction executable")
    entries = value["packages"][architecture]
    require(len(entries) == 2 and [row["version"] for row in entries] == value["versions"] and
            len(set(value["versions"])) == 2, "Lock must select two different package versions in order")
    for name, case in value.get("upgrade_packages", {}).items():
        require(PACKAGE_NAME.fullmatch(name) and case.get("package") == name,
                "Invalid existing-package upgrade name")
        upgrades = case["packages"][architecture]
        require(len(upgrades) == 1 and [row["version"] for row in upgrades] == case["versions"],
                "Existing-package upgrade must have one locked target version")
        require(case["executable"].startswith("/usr/bin/") and ".." not in Path(case["executable"]).parts,
                "Invalid existing-package executable")
        entries = [*entries, *upgrades]
    for row in entries:
        url = urllib.parse.urlsplit(row["url"])
        require(url.scheme == "https" and url.hostname in ("deb.debian.org", "snapshot.debian.org") and
                not url.username and not url.password and not url.query and not url.fragment,
                "Transaction package URL must use an official Debian archive over HTTPS")
        require(re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) and
                isinstance(row["bytes"], int) and 0 < row["bytes"] < 100 * 1024 * 1024,
                "Transaction package lacks valid integrity metadata")
        require(re.fullmatch(r"[a-zA-Z0-9_.+~%-]+\.deb", Path(url.path).name), "Unsafe package filename")
    return value


def host_command(args, timeout=300):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    require(result.returncode == 0, "Command failed: " + repr(args[:5]) + "\n" + result.stdout[-4000:] + result.stderr[-4000:])
    return result.stdout


def prepare_repository(lock, architecture, directory):
    """Verify published packages and write a local index; never repack a DEB."""
    directory.mkdir(parents=True, exist_ok=True)
    paragraphs, downloaded = [], []
    for row in lock["packages"][architecture]:
        filename = Path(urllib.parse.urlsplit(row["url"]).path).name
        target = directory / filename
        if not target.exists():
            with urllib.request.urlopen(row["url"], timeout=60) as response:
                content = response.read(row["bytes"] + 1)
            require(len(content) == row["bytes"] and hashlib.sha256(content).hexdigest() == row["sha256"],
                    "Downloaded Debian package differs from its lock: " + filename)
            target.write_bytes(content)
        require(target.stat().st_size == row["bytes"] and sha(target) == row["sha256"],
                "Cached Debian package differs from its lock: " + filename)
        # dpkg-deb reads the original control data; its build mode is never used.
        for field, expected in (("Package", lock["package"]), ("Version", row["version"]), ("Architecture", architecture)):
            require(host_command(["dpkg-deb", "--field", str(target), field]).strip() == expected,
                    "Locked package has an unexpected " + field)
        control = host_command(["dpkg-deb", "--field", str(target)]).rstrip()
        paragraphs.append(control + "\nFilename: " + filename + "\nSize: " + str(row["bytes"]) +
                          "\nSHA256: " + row["sha256"])
        downloaded.append(dict(row, filename=filename))
    (directory / "Packages").write_text("\n\n".join(paragraphs) + "\n\n")
    return downloaded


def parse_installed(output):
    records = {}
    for line in output.splitlines():
        name, status, version, architecture = line.split("\t")
        # APT holds change the desired selection, not the installed state.
        # Include held packages so their ownership metadata is still checked.
        if status.split()[1:] != ["ok", "installed"]:
            continue
        require(PACKAGE_NAME.fullmatch(name) and name not in records, "Invalid or duplicate installed package name")
        records[name] = {"version": version, "architecture": architecture}
    require(records, "Image contains no installed package records")
    return records


def info_inventory(records, root=Path("/")):
    """Require ownership metadata for every configured package, including empty ones."""
    result = {}
    for name, record in sorted(records.items()):
        info = root / "var/lib/dpkg/info"
        path = info / (name + ".list")
        require(path.is_file() and not path.is_symlink(), "Installed package has no regular file-ownership list: " + name)
        lines = path.read_text().splitlines()
        require(all(line.startswith("/") and ".." not in Path(line).parts for line in lines),
                "Invalid owned path for package " + name)
        result[name] = dict(record, list_sha256=sha(path), owned_paths=len(lines))
    return result


def verify_receipt_files(receipts, root=Path("/")):
    """Bind installed control-file bytes to the package-state build receipt.

Receipts may be the package-state object or wrap it under ``dpkg_state``.
``info_files`` maps installed relative file paths to SHA256 strings (or objects
with ``sha256``). ``packages`` may also contain those maps per package.
"""
    expected = {}
    modes = {}
    removed = set()
    for receipt in receipts:
        state = receipt.get("dpkg_state", receipt)
        for name in state.get("removed_paths", []):
            require(isinstance(name, str) and name and not name.startswith("/") and
                    ".." not in Path(name).parts and str(Path(name)) == name,
                    "Invalid removed package-state path")
            removed.add(name)
        mappings = [state.get("info_files", {})]
        packages = state.get("packages", {})
        if isinstance(packages, dict):
            mappings.extend(row.get("info_files", {}) for row in packages.values() if isinstance(row, dict))
        elif isinstance(packages, list):
            mappings.extend(row.get("info_files", {}) for row in packages)
        for mapping in mappings:
            for name, entry in mapping.items():
                name = name.removeprefix("/")
                digest = entry["sha256"] if isinstance(entry, dict) else entry
                require(name.startswith("var/lib/dpkg/") and ".." not in Path(name).parts and
                        re.fullmatch(r"[0-9a-f]{64}", digest), "Invalid package-state receipt file")
                require(name not in expected or expected[name] == digest, "Conflicting package-state receipts: " + name)
                expected[name] = digest
                if isinstance(entry, dict) and "mode" in entry:
                    mode = entry["mode"]
                    require(isinstance(mode, int) and 0 <= mode <= 0o7777, "Invalid package-state file mode")
                    require(name not in modes or modes[name] == mode, "Conflicting package-state modes: " + name)
                    modes[name] = mode
    if receipts:
        require(expected, "Package receipts contain no dpkg state file hashes")
    require(not removed.intersection(expected), "Package-state receipt both removes and records a file")
    for name in sorted(removed):
        require(not os.path.lexists(root / name), "Obsolete package file remains installed: " + name)
    for name, digest in sorted(expected.items()):
        path = root / name
        require(path.is_file() and not path.is_symlink() and sha(path) == digest,
                "Installed package-state file differs from build receipt: " + name)
        require(name not in modes or stat.S_IMODE(path.stat().st_mode) == modes[name],
                "Installed package-state file mode differs from build receipt: " + name)
    return expected


def guest_command(args, commands, *, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=300,
                            env=dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C"))
    print("COMMAND " + json.dumps(args), flush=True)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    commands.append({"args": args, "returncode": result.returncode,
                     "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                     "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest()})
    require(not check or result.returncode == 0, "Package command failed: " + repr(args))
    return result


def installed_records(commands):
    return parse_installed(guest_command(["dpkg-query", "-W", "-f=${binary:Package}\t${Status}\t${Version}\t${Architecture}\n"], commands).stdout)


def apt_command(arguments):
    return ["apt-get", "-y", "-o", "Dir::Etc::sourcelist=/probe/sources.list",
            "-o", "Dir::Etc::sourceparts=-", "-o", "Dir::State::lists=/tmp/sonic-dpkg-lists",
            "-o", "Dir::Cache::archives=/tmp/sonic-dpkg-archives", "-o", "Acquire::Languages=none",
            "-o", "Dpkg::Use-Pty=0", *arguments]


def audit(commands):
    result = guest_command(["dpkg", "--audit"], commands)
    require(not result.stdout.strip() and not result.stderr.strip(), "dpkg reports incomplete or inconsistent package state")
    guest_command(apt_command(["check"]), commands)


def package_paths(name, commands):
    listing = guest_command(["dpkg-query", "-L", name], commands).stdout.splitlines()
    require(listing, "Package has no recorded owned paths: " + name)
    conffiles = guest_command(["dpkg-query", "-W", "-f=${Conffiles}", name], commands).stdout
    configurations = {}
    for line in conffiles.splitlines():
        values = line.split()
        if values:
            require(values[0].startswith("/") and ".." not in Path(values[0]).parts, "Invalid conffile path")
            if os.path.lexists(values[0]):
                configurations[values[0]] = sha(values[0])
    files = {path for path in listing if path.startswith("/") and path not in configurations and
             (Path(path).is_symlink() or Path(path).is_file())}
    require(files, "Package has no installed owned files: " + name)
    return files, configurations


def check_removed_alternatives(name, files, *, root=Path("/")):
    """Do not leave commands pointing to files deleted by a package removal."""
    removed = {str((root / path.lstrip("/")).resolve()) for path in files}
    directory = root / "etc/alternatives"
    for link in sorted(directory.iterdir()) if directory.is_dir() else []:
        if link.is_symlink():
            require(str(link.resolve()) not in removed,
                    "Package removal left an alternative pointing to removed files: " + str(link))
    shared_vim_groups = ("editor", "ex", "rview", "vi", "view")
    groups = shared_vim_groups if name == "vim-tiny" else \
        (*shared_vim_groups, "rvim", "vimdiff") if name == "vim" else ("pager",) if name == "less" else ()
    result = {}
    for group in groups:
        link = root / "usr/bin" / group
        alternative = directory / group
        require(not os.path.lexists(link) or link.exists(), "Package removal left a dangling command: " + str(link))
        require(not os.path.lexists(alternative) or alternative.exists(),
                "Package removal left a dangling alternative: " + str(alternative))
        result[group] = str(alternative.resolve()) if alternative.exists() else None
    if name == "less":
        more = root / "bin/more"
        require(more.is_file() and (directory / "pager").resolve() == more.resolve(),
                "Removing less did not restore the inherited more pager")
    if name == "vim":
        tiny = root / "usr/bin/vim.tiny"
        require(tiny.is_file() and all((directory / group).resolve() == tiny.resolve()
                                      for group in shared_vim_groups),
                "Removing full Vim did not restore the inherited vim-tiny alternatives")
        require(all(not os.path.lexists(directory / group) and not os.path.lexists(root / "usr/bin" / group)
                    for group in ("rvim", "vimdiff")),
                "Removing full Vim left a command without an inherited provider")
        command = root / "usr/bin/vim"
        require(command.is_file() and not command.is_symlink() and os.path.samefile(command, tiny),
                "Removing full Vim changed the vim-tiny-owned vim hard link")
    return result


def remove_and_check(name, commands):
    require(PACKAGE_NAME.fullmatch(name), "Invalid removal package")
    before = installed_records(commands)
    require(name in before, "Removal package is not installed: " + name)
    files, conffiles = package_paths(name, commands)
    # dpkg-query -S independently proves the recorded ownership used by remove.
    path = sorted(files)[0]
    owner = guest_command(["dpkg-query", "-S", path], commands).stdout
    require(any(line.startswith(name + ": ") for line in owner.splitlines()), "File owner differs from package being removed")
    guest_command(apt_command(["remove", name]), commands)
    after = installed_records(commands)
    require(name not in after, "apt remove left the package installed: " + name)
    require(set(before) - set(after) == {name}, "Removing the probe unexpectedly removed dependent packages")
    leftovers = sorted(path for path in files if os.path.lexists(path))
    require(not leftovers, "apt remove left owned files behind: " + repr(leftovers[:12]))
    require(all(Path(path).is_file() and sha(path) == digest for path, digest in conffiles.items()),
            "apt remove did not preserve configuration files")
    alternatives = check_removed_alternatives(name, files)
    audit(commands)
    remaining = guest_command(["dpkg-query", "-W", "-f=${db:Status-Status}", name], commands, check=False)
    require(remaining.returncode in (0, 1), "Could not query package state after removal")
    # Packages without conffiles/postrm disappear completely on remove. APT
    # cannot resolve a redundant purge in our intentionally minimal repository.
    needs_purge = remaining.returncode == 0 and remaining.stdout.strip() != "not-installed"
    if needs_purge:
        guest_command(apt_command(["purge", name]), commands)
    require(not any(os.path.lexists(path) for path in conffiles), "apt purge left configuration files behind")
    audit(commands)
    return {"package": name, "version": before[name]["version"], "removed_files": len(files),
            "preserved_then_purged_conffiles": sorted(conffiles), "remaining_alternatives": alternatives}


def run_guest(request):
    result = {"_generated": NOTICE, "schema": 1, "status": "running", "mode": request["mode"],
              "architecture": request["architecture"], "commands": []}
    commands = result["commands"]
    try:
        require(platform.machine() == MACHINES[request["architecture"]], "Container is not native to the requested architecture")
        require(guest_command(["dpkg", "--print-architecture"], commands).stdout.strip() == request["architecture"],
                "dpkg architecture differs from the requested architecture")
        for directory in ("/tmp/sonic-dpkg-lists/partial", "/tmp/sonic-dpkg-archives/partial"):
            Path(directory).mkdir(parents=True, exist_ok=True)
        before = installed_records(commands)
        result["installed"] = info_inventory(before)
        receipts = list(request.get("receipts", []))
        result["installed_receipts"] = []
        for name in request.get("installed_receipts", []):
            require(name.startswith("/usr/share/sonic/bazel/") and ".." not in Path(name).parts,
                    "Invalid installed package-state receipt path")
            path = Path(name)
            require(path.is_file() and not path.is_symlink(), "Installed package-state receipt is missing: " + name)
            receipts.append(json.loads(path.read_text()))
            result["installed_receipts"].append({"path": name, "sha256": sha(path)})
        result["verified_info_files"] = verify_receipt_files(receipts)
        audit(commands)
        guest_command(apt_command(["update"]), commands)
        if request["mode"] == "transactions":
            lock = request["lock"]
            name = lock["package"]
            require(name not in before and not os.path.lexists(lock["executable"]), "Transaction probe package already exists in image")
            old, new = lock["versions"]
            guest_command(["dpkg", "--compare-versions", old, "lt", new], commands)
            guest_command(apt_command(["install", name + "=" + old]), commands)
            installed = installed_records(commands)
            require(installed.get(name, {}).get("version") == old, "apt install selected the wrong version")
            require({key: value for key, value in installed.items() if key != name} == before,
                    "Installing probe changed another installed package")
            guest_command([lock["executable"]], commands)
            info_inventory(installed)
            audit(commands)
            # The only repository candidate is hello; this is a real version
            # change via apt-get upgrade, not a reinstall of identical bytes.
            guest_command(apt_command(["upgrade"]), commands)
            upgraded = installed_records(commands)
            require(upgraded.get(name, {}).get("version") == new, "apt upgrade did not install the newer version")
            require({key: value for key, value in upgraded.items() if key != name} == before,
                    "Upgrading probe changed another installed package")
            guest_command([lock["executable"]], commands)
            audit(commands)
            result["transaction"] = {"installed_version": old, "upgraded_version": new,
                                     "removal": remove_and_check(name, commands)}
        elif request["mode"] == "upgrade-imported":
            case = request["upgrade"]
            name = case["package"]
            require(name in before, "Upgrade package was not assembled into the OCI image")
            original = before[name]["version"]
            newer = case["versions"][0]
            guest_command(["dpkg", "--compare-versions", original, "lt", newer], commands)
            old_files, old_conffiles = package_paths(name, commands)
            guest_command(apt_command(["--only-upgrade", "install", name + "=" + newer]), commands)
            after = installed_records(commands)
            require(after.get(name, {}).get("version") == newer, "Existing OCI package did not upgrade")
            require({key: value for key, value in after.items() if key != name} ==
                    {key: value for key, value in before.items() if key != name},
                    "Existing-package upgrade changed another package")
            new_files, new_conffiles = package_paths(name, commands)
            stale = old_files - new_files - set(new_conffiles) - set(old_conffiles)
            require(not any(os.path.lexists(path) for path in stale), "Upgrade left obsolete owned files behind")
            guest_command([case["executable"], *case.get("arguments", [])], commands)
            info_inventory(after)
            audit(commands)
            result["upgrade"] = {"package": name, "assembled_version": original, "upgraded_version": newer,
                                 "obsolete_files_removed": len(stale)}
        else:
            require(request["mode"] == "remove-imported", "Invalid probe mode")
            result["removal"] = remove_and_check(request["package"], commands)
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=str(error))
        traceback.print_exc()
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
    return result


def container_arguments(image_id, name, script, repository, sources):
    # Package scripts need a writable root and Docker's ordinary chown/setuid
    # capabilities. This still has no host filesystem write mounts or network.
    return ["docker", "run", "--rm", "--interactive", "--name", name, "--network=none",
            "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=2g", "--cpus=2",
            "--mount", "type=bind,src=" + str(script.resolve()) + ",dst=/probe/validate_dpkg_state.py,readonly",
            "--mount", "type=bind,src=" + str(repository.resolve()) + ",dst=/probe/packages,readonly",
            "--mount", "type=bind,src=" + str(sources.resolve()) + ",dst=/probe/sources.list,readonly",
            "--entrypoint=/usr/bin/python3", image_id, "-B", "/probe/validate_dpkg_state.py", "--in-container"]


def run_probe(image_id, request, repository, sources, output, label):
    name = "sonic-dpkg-validation-" + uuid.uuid4().hex
    args = container_arguments(image_id, name, Path(__file__), repository, sources)
    try:
        try:
            process = subprocess.run(args, input=json.dumps(request), text=True, capture_output=True, timeout=1200)
        except subprocess.TimeoutExpired as error:
            for suffix, value in (("stdout", error.stdout), ("stderr", error.stderr)):
                (output / (label + "." + suffix + ".log")).write_bytes(value.encode() if isinstance(value, str) else value or b"")
            raise
        (output / (label + ".stdout.log")).write_text(process.stdout)
        (output / (label + ".stderr.log")).write_text(process.stderr)
        reports = [line[len(RESULT_PREFIX):] for line in process.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
        require(len(reports) == 1, "Container did not produce exactly one receipt: " + label)
        result = json.loads(reports[0])
        write_json(output / (label + ".json"), result)
        require(process.returncode == 0 and result["status"] == "passed", "Package-state probe failed: " + label + "; see retained logs")
        return result
    finally:
        # Restrict cleanup to this invocation's unpredictable name.
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True, timeout=30)


def validate(*, architecture, output, image=None, archive=None, archive_sha256=None,
             config_digest=None, receipt_paths=(), installed_receipts=(), remove_packages=(), upgrade_packages=(), lock_path=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"_generated": NOTICE, "schema": 1, "status": "running", "architecture": architecture,
              "host_machine": platform.machine(), "probes": {}}
    temporary_tag = None
    try:
        require(architecture in MACHINES and platform.machine() == MACHINES[architecture], "Validation requires the selected native host")
        require(bool(image) != bool(archive), "Choose exactly one existing image or archive")
        require(remove_packages and len(remove_packages) == len(set(remove_packages)),
                "Select at least one package already assembled into the image for a separate removal check")
        require(all(PACKAGE_NAME.fullmatch(name) for name in remove_packages), "Invalid removal package")
        if archive:
            archive = Path(archive).resolve()
            require(re.fullmatch(r"[0-9a-f]{64}", archive_sha256 or "") and sha(archive) == archive_sha256,
                    "Image archive differs from its expected SHA256")
            require(re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest or ""), "Archive validation requires the expected config digest")
            root = Path(__file__).resolve().parents[3]
            sys.path.insert(0, str(root))
            from tools.bazel.ci.validate_runtime import load_archive, verify_loaded_image
            temporary_tag = "sonic-dpkg-validation:" + uuid.uuid4().hex
            inspected, config, diagnostics = load_archive(archive, temporary_tag, config_digest)
            (output / "load.log").write_text(diagnostics)
            verify_loaded_image(inspected, config, config_digest)
            report["archive"] = {"sha256": archive_sha256, "bytes": archive.stat().st_size, "config_digest": config_digest}
        else:
            inspected = json.loads(host_command(["docker", "image", "inspect", image]))[0]
        require(inspected["Os"] == "linux" and inspected["Architecture"] == architecture, "Image platform differs from native validation")
        report["image"] = {key: inspected[key] for key in ("Id", "Architecture", "Os", "RootFS")}
        lock_path = Path(lock_path or Path(__file__).resolve().parents[1] / "tests/dpkg_transactions.lock.json")
        lock = read_lock(lock_path, architecture)
        report["lock_sha256"] = sha(lock_path)
        receipts = [json.loads(Path(path).read_text()) for path in receipt_paths]
        report["package_receipts"] = [{"name": Path(path).name, "sha256": sha(path)} for path in receipt_paths]
        repository = output / "packages"
        report["packages"] = prepare_repository(lock, architecture, repository)
        sources = output / "sources.list"
        sources.write_text("# AUTO-GENERATED. DO NOT EDIT. Local verified test packages only.\ndeb [trusted=yes] file:/probe/packages ./\n")
        upgrade_inputs = {}
        for name in upgrade_packages:
            require(PACKAGE_NAME.fullmatch(name) and name in lock.get("upgrade_packages", {}),
                    "Requested existing-package upgrade has no reviewed lock: " + name)
            case = lock["upgrade_packages"][name]
            upgrade_repository = output / ("packages-upgrade-" + name)
            downloaded = prepare_repository(case, architecture, upgrade_repository)
            upgrade_inputs[name] = (case, upgrade_repository)
            report.setdefault("upgrade_packages", {})[name] = downloaded
        common = {"architecture": architecture, "receipts": receipts, "installed_receipts": list(installed_receipts), "lock": lock}
        requests = [("transactions", dict(common, mode="transactions"), repository)]
        requests.extend(("remove-" + name.replace(":", "-"), dict(common, mode="remove-imported", package=name), repository)
                        for name in remove_packages)
        requests.extend(("upgrade-" + name, dict(common, mode="upgrade-imported", upgrade=case), selected_repository)
                        for name, (case, selected_repository) in upgrade_inputs.items())
        # Run independent probes even when one fails, retaining all useful evidence.
        failures = []
        for label, request, selected_repository in requests:
            try:
                result = run_probe(inspected["Id"], request, selected_repository, sources, output, label)
                report["probes"][label] = {"status": result["status"], "receipt_sha256": sha(output / (label + ".json"))}
            except Exception as error:
                failures.append(str(error))
                report["probes"][label] = {"status": "failed", "error": str(error)}
        require(not failures, "; ".join(failures))
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        write_json(output / "receipt.json", report)
        if temporary_tag:
            subprocess.run(["docker", "image", "rm", temporary_tag], capture_output=True, timeout=60)
    return report


def main():
    if sys.argv[1:] == ["--in-container"]:
        sys.exit(0 if run_guest(json.load(sys.stdin))["status"] == "passed" else 1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=MACHINES, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--image")
    selection.add_argument("--archive", type=Path)
    parser.add_argument("--archive-sha256")
    parser.add_argument("--config-digest")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--package-receipt", action="append", type=Path, default=[])
    parser.add_argument("--remove-package", action="append", default=[])
    parser.add_argument("--installed-receipt", action="append", default=[])
    parser.add_argument("--upgrade-package", action="append", default=[])
    parser.add_argument("--lock", type=Path)
    args = parser.parse_args()
    validate(architecture=args.architecture, output=args.output, image=args.image,
             archive=args.archive, archive_sha256=args.archive_sha256, config_digest=args.config_digest,
             receipt_paths=args.package_receipt, installed_receipts=args.installed_receipt,
             remove_packages=args.remove_package, upgrade_packages=args.upgrade_package, lock_path=args.lock)


if __name__ == "__main__":
    main()
