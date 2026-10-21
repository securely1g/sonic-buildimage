#!/usr/bin/env python3
"""Exercise Debian package management in disposable native image containers.

The ordinary CLI runs on the Docker host. It downloads checksum-pinned Debian packages, including a command and its
library dependencies, and supplies a local APT repository to network-disabled
containers. --in-container is an internal entry point for the read-only mounted
copy of this script. It never creates Debian packages or changes a running image.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
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
    require(value.get("schema") == 2 and architecture in MACHINES, "Unsupported install lock or architecture")
    require(PACKAGE_NAME.fullmatch(value["package"]), "Invalid install package name")
    command = value["command"]
    require(isinstance(command, list) and command and
            re.fullmatch(r"/[a-zA-Z0-9_./+-]+", command[0]) and ".." not in Path(command[0]).parts and
            all(isinstance(argument, str) for argument in command), "Invalid install probe command")
    entries = value["packages"][architecture]
    names = [row["package"] for row in entries]
    require(entries and len(names) == len(set(names)) and value["package"] in names,
            "Install lock must contain each package once, including the requested command")
    require(set(value["required_dependencies"]) <= set(names) - {value["package"]},
            "Install dependencies are absent from the lock")
    for row in entries:
        require(PACKAGE_NAME.fullmatch(row["package"]) and isinstance(row["version"], str) and row["version"],
                "Invalid locked package identity")
        url = urllib.parse.urlsplit(row["url"])
        require(url.scheme == "https" and url.hostname in ("deb.debian.org", "snapshot.debian.org") and
                not url.username and not url.password and not url.query and not url.fragment,
                "Install package URL must use an official Debian archive over HTTPS")
        require(re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) and
                isinstance(row["bytes"], int) and 0 < row["bytes"] < 100 * 1024 * 1024,
                "Install package lacks valid integrity metadata")
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
        for field, expected in (("Package", row["package"]), ("Version", row["version"]), ("Architecture", architecture)):
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


def initial_configuration(commands, records):
    """Check package configuration before the first validation APT transaction.

    These are outputs of Debian maintainer scripts and triggers, not files we
    synthesize for the test. In particular, querying ldconfig does not create
    its cache, and querying alternatives does not register any command.
    """
    required = {"libc-bin", "ca-certificates", "vim-tiny", "less"}
    names = {name.split(":", 1)[0] for name in records}
    require(required <= names, "Initial-configuration packages are missing: " + repr(sorted(required - names)))
    pending = guest_command(["dpkg-query", "-W", "-f=${binary:Package}\t${Triggers-Pending}\t${Triggers-Awaited}\n"], commands)
    for line in pending.stdout.splitlines():
        fields = line.split("\t")
        require(len(fields) == 3 and not any(fields[1:]), "Package has unprocessed triggers: " + line)
    cache = Path("/etc/ld.so.cache")
    require(cache.is_file() and cache.stat().st_size > 0, "libc-bin did not generate the dynamic-loader cache")
    libraries = guest_command(["/sbin/ldconfig", "-p"], commands).stdout
    require("libc.so.6" in libraries, "Dynamic-loader cache does not contain libc")
    result = {"ldconfig_cache_sha256": sha(cache), "pending_triggers": []}

    configuration = Path("/etc/ca-certificates.conf")
    require(configuration.is_file(), "ca-certificates did not generate its configuration")
    enabled = [line for line in configuration.read_text().splitlines()
               if line and not line.startswith(("#", "!"))]
    require(enabled and all((Path("/usr/share/ca-certificates") / name).is_file() for name in enabled),
            "ca-certificates configuration does not select installed certificates")
    bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    require(bundle.is_file() and bundle.read_bytes().count(b"-----BEGIN CERTIFICATE-----") >= len(enabled),
            "ca-certificates did not generate the enabled certificate bundle")
    hashes = [path for path in Path("/etc/ssl/certs").iterdir() if re.fullmatch(r"[0-9a-f]{8}\.[0-9]+", path.name)]
    require(hashes and all(path.is_symlink() and path.is_file() for path in hashes),
            "ca-certificates did not generate valid OpenSSL certificate hash links")
    result["certificates"] = {"enabled": len(enabled), "hash_links": len(hashes), "bundle_sha256": sha(bundle)}

    result["alternatives"] = {}
    for group in ("editor", "ex", "rview", "vi", "view", "pager"):
        query = guest_command(["update-alternatives", "--query", group], commands).stdout
        values = dict(line.split(": ", 1) for line in query.splitlines() if ": " in line)
        require(values.get("Name") == group and values.get("Status") in ("auto", "manual"),
                "Package did not register its command alternative: " + group)
        selected = values.get("Value", "")
        require(selected.startswith("/") and Path(selected).is_file(), "Alternative has no installed provider: " + group)
        require(Path("/usr/bin", group).resolve() == Path(selected).resolve(),
                "Command differs from its alternatives record: " + group)
        result["alternatives"][group] = selected

    if "openssh-client" in names:
        group = guest_command(["getent", "group", "_ssh"], commands).stdout.strip().split(":")
        require(len(group) == 4 and group[0] == "_ssh", "OpenSSH did not create its _ssh system group")
        agent = Path("/usr/bin/ssh-agent").stat()
        require(agent.st_gid == int(group[2]) and stat.S_IMODE(agent.st_mode) == 0o2755,
                "OpenSSH did not configure ssh-agent's system group and setgid permission")
        result["openssh"] = {"group": "_ssh", "gid": agent.st_gid, "ssh_agent_mode": stat.S_IMODE(agent.st_mode)}
    return result


def source_file_inventory(receipts, root=Path("/")):
    """Protect source binaries independently of whether dpkg manages them."""
    expected = {}
    for receipt in receipts:
        for package in receipt.get("source_packages", []):
            for name in package.get("excluded_baseline_paths", []):
                require(isinstance(name, str) and name and not name.startswith("/") and
                        ".." not in Path(name).parts and str(Path(name)) == name,
                        "Invalid excluded baseline path")
                require(not os.path.lexists(root / name), "Excluded baseline file is installed: " + name)
            for entry in package.get("runtime_files", package.get("native_files", [])):
                name = entry["path"]
                require(not name.startswith("/") and ".." not in Path(name).parts,
                        "Invalid source payload path")
                path = root / name
                require(path.is_file() and not path.is_symlink(), "Source payload is not a regular file: " + name)
                metadata = {"sha256": sha(path), "mode": stat.S_IMODE(path.stat().st_mode),
                            "uid": path.stat().st_uid, "gid": path.stat().st_gid}
                require(all(metadata[key] == entry[key] for key in metadata),
                        "Source payload differs from its build receipt: " + name)
                require(name not in expected or expected[name] == metadata, "Conflicting source payload receipts")
                expected[name] = metadata
            for entry in package.get("runtime_links", []):
                name = entry["path"]
                require(not name.startswith("/") and ".." not in Path(name).parts,
                        "Invalid source symlink path")
                path = root / name
                require(path.is_symlink(), "Source symlink is missing: " + name)
                # Match archive inventory normalization: flattening may add ./,
                # while parent components must retain their path semantics.
                metadata = {"kind": "symlink", "linkname": str(PurePosixPath(os.readlink(path))),
                            "mode": stat.S_IMODE(path.lstat().st_mode),
                            "uid": path.lstat().st_uid, "gid": path.lstat().st_gid}
                require(all(metadata[key] == entry[key] for key in metadata),
                        "Source symlink differs from its build receipt: " + name)
                require(name not in expected or expected[name] == metadata, "Conflicting source symlink receipts")
                expected[name] = metadata
    return expected


def run_runtime_smoke(request, commands):
    if request.get("runtime_smoke"):
        guest_command(["/usr/bin/python3", "-B", "/probe/runtime_smoke.py", "/probe/runtime_validation.json"], commands)


def load_installed_receipts(names, root=Path("/")):
    receipts, identities = [], []
    for name in names:
        require(name.startswith("/usr/share/sonic/bazel/") and ".." not in Path(name).parts,
                "Invalid installed package-state receipt path")
        path = root / name.lstrip("/")
        require(path.is_file() and not path.is_symlink(), "Installed package-state receipt is missing: " + name)
        receipts.append(json.loads(path.read_text()))
        identities.append({"path": name, "sha256": sha(path)})
    return receipts, identities


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
        installed, result["installed_receipts"] = load_installed_receipts(request.get("installed_receipts", []))
        receipts.extend(installed)
        result["verified_info_files"] = verify_receipt_files(receipts)
        inherited_sources, result["installed_source_receipts"] = load_installed_receipts(
            request.get("installed_source_receipts", []))
        for receipt in inherited_sources:
            require(receipt.get("schema") == 1 and receipt.get("architecture") == request["architecture"] and
                    isinstance(receipt.get("source_packages"), list) and receipt["source_packages"],
                    "Invalid inherited source receipt")
        # Parent receipts describe an earlier dpkg database. Use only their
        # source inventories; the current image receipt above owns dpkg state.
        # Keep overlapping source paths so inconsistent proofs still fail.
        source_receipts = [*inherited_sources, *receipts]
        audit(commands)
        if request["mode"] == "initial-configuration":
            result["configuration"] = initial_configuration(commands, before)
        else:
            guest_command(apt_command(["update"]), commands)
        if request["mode"] == "install":
            lock = request["lock"]
            name = lock["package"]
            require(name not in before and not os.path.lexists(lock["command"][0]),
                    "Install probe package already exists in image")
            require(not any(package in before or any(key.startswith(package + ":") for key in before)
                            for package in lock["required_dependencies"]),
                    "Dependency probe must install a new library, not reuse an already installed one")
            protected = source_file_inventory(source_receipts)
            run_runtime_smoke(request, commands)
            guest_command(apt_command(["--no-install-recommends", "install", name]), commands)
            installed = installed_records(commands)
            # This is a new command and its dependencies. Existing Debian
            # packages and ordinary SONiC binary/wheel layers must stay intact.
            require(all(installed.get(key) == value for key, value in before.items()),
                    "Installing an additional package changed an existing package")
            added = {key: value for key, value in installed.items() if key not in before}
            locked = {row["package"]: row["version"] for row in lock["packages"][request["architecture"]]}
            require(added and all(locked.get(key.split(":", 1)[0]) == value["version"] for key, value in added.items()),
                    "APT installed packages outside the reviewed dependency set")
            names = {key.split(":", 1)[0] for key in added}
            require({name, *lock["required_dependencies"]} <= names,
                    "APT did not install the command and its new library dependency")
            guest_command(lock["command"], commands)
            info_inventory(installed)
            audit(commands)
            require(source_file_inventory(source_receipts) == protected, "APT changed a source payload")
            run_runtime_smoke(request, commands)
            result["install"] = {"requested_package": name, "added_packages": added,
                                 "preserved_existing_packages": len(before), "preserved_source_files": len(protected),
                                 "runtime_smoke": bool(request.get("runtime_smoke"))}
        else:
            require(request["mode"] == "initial-configuration", "Invalid probe mode")
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=str(error))
        traceback.print_exc()
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
    return result


def container_arguments(image_id, name, script, repository, sources, runtime_smoke=None, runtime_validation=None):
    # Package scripts need a writable root and Docker's ordinary chown/setuid
    # capabilities. This still has no host filesystem write mounts or network.
    arguments = ["docker", "run", "--rm", "--interactive", "--name", name, "--network=none",
            "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=2g", "--cpus=2",
            "--mount", "type=bind,src=" + str(script.resolve()) + ",dst=/probe/validate_dpkg_state.py,readonly",
            "--mount", "type=bind,src=" + str(repository.resolve()) + ",dst=/probe/packages,readonly",
            "--mount", "type=bind,src=" + str(sources.resolve()) + ",dst=/probe/sources.list,readonly"]
    if runtime_smoke:
        require(runtime_validation, "Runtime smoke script requires its image validation report")
        for source, target in ((runtime_smoke, "runtime_smoke.py"), (runtime_validation, "runtime_validation.json")):
            arguments.extend(["--mount", "type=bind,src=" + str(Path(source).resolve()) + ",dst=/probe/" + target + ",readonly"])
    return [*arguments, "--entrypoint=/usr/bin/python3", image_id, "-B", "/probe/validate_dpkg_state.py", "--in-container"]


def run_probe(image_id, request, repository, sources, output, label, runtime_smoke=None, runtime_validation=None):
    name = "sonic-dpkg-validation-" + uuid.uuid4().hex
    args = container_arguments(image_id, name, Path(__file__), repository, sources, runtime_smoke, runtime_validation)
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
             config_digest=None, receipt_paths=(), installed_receipts=(), installed_source_receipts=(),
             lock_path=None, runtime_smoke=None, runtime_validation=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"_generated": NOTICE, "schema": 1, "status": "running", "architecture": architecture,
              "host_machine": platform.machine(), "probes": {}}
    temporary_tag = None
    try:
        require(architecture in MACHINES and platform.machine() == MACHINES[architecture], "Validation requires the selected native host")
        require(bool(image) != bool(archive), "Choose exactly one existing image or archive")
        require(bool(runtime_smoke) == bool(runtime_validation), "Provide both the runtime smoke script and its validation report")
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
        common = {"architecture": architecture, "receipts": receipts, "installed_receipts": list(installed_receipts),
                  "installed_source_receipts": list(installed_source_receipts),
                  "lock": lock, "runtime_smoke": bool(runtime_smoke)}
        requests = [("initial-configuration", dict(common, mode="initial-configuration"), repository),
                    ("install", dict(common, mode="install"), repository)]
        # Run independent probes even when one fails, retaining all useful evidence.
        failures = []
        for label, request, selected_repository in requests:
            try:
                result = run_probe(inspected["Id"], request, selected_repository, sources, output, label, runtime_smoke, runtime_validation)
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
    parser.add_argument("--installed-receipt", action="append", default=[])
    parser.add_argument("--installed-source-receipt", action="append", default=[],
                        help="Inherited receipt used only to preserve source files, not historical dpkg state")
    parser.add_argument("--runtime-smoke", type=Path)
    parser.add_argument("--runtime-validation", type=Path)
    parser.add_argument("--lock", type=Path)
    args = parser.parse_args()
    validate(architecture=args.architecture, output=args.output, image=args.image,
             archive=args.archive, archive_sha256=args.archive_sha256, config_digest=args.config_digest,
             receipt_paths=args.package_receipt, installed_receipts=args.installed_receipt,
             installed_source_receipts=args.installed_source_receipt,
             lock_path=args.lock, runtime_smoke=args.runtime_smoke, runtime_validation=args.runtime_validation)


if __name__ == "__main__":
    main()
