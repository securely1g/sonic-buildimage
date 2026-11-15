"""Capture a separately labelled auxiliary installation; never execute SONiC commands."""
import argparse
import email.parser
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

FIXTURE_SHA256 = "d85273a6a17d467a1f7012221489cf357799cd34b16910cd5313b95b2fcbf2eb"
SOURCE_REVISION = "c02d93956919e4ea6886f347be4ac0ab6e3e36f0"
MAX_SCRIPT = 1024 * 1024
MAX_INPUT = 256 * 1024
MAX_FILE = 256 * 1024 * 1024
GROUPS = (
    ("sonic-python", ("yang", "common", "config"), "sonic-cfggen"),
    ("supervisor", ("common", "supervisor"), "supervisor-proc-exit-listener"),
    ("yang", ("yang",), "sonic-cfg-help"),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("non-finite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def snapshot(path, limit, *, executable=False):
    """Read a regular leaf without following symlinks, with stable fstat identity."""
    path = Path(path)
    require(path.is_absolute(), "file path must be absolute")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode), "expected regular file")
        require(0 <= before.st_size <= limit, "file outside bounded size")
        require(not executable or (before.st_size > 0 and before.st_mode & 0o111),
                "script is not executable or is empty")
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        current = path.lstat()
        identity = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_size,
                                row.st_mtime_ns, row.st_ctime_ns)
        require(identity(before) == identity(after) == identity(current),
                "file changed during capture")
        require(len(raw) == before.st_size, "incomplete file capture")
        return raw, {"path": str(path), "bytes": len(raw), "sha256": sha(raw),
                     "mode": stat.S_IMODE(before.st_mode),
                     "device": before.st_dev, "inode": before.st_ino}
    finally:
        os.close(fd)


def checked(row, *, limit=MAX_FILE):
    require(isinstance(row, dict) and
            {"path", "bytes", "sha256"} <= row.keys(), "missing file identity")
    require(type(row["bytes"]) is int and row["bytes"] >= 0, "invalid expected size")
    require(isinstance(row["sha256"], str) and
            re.fullmatch("[0-9a-f]{64}", row["sha256"]), "invalid expected SHA")
    path = Path(row["path"])
    require(path.is_absolute(), "input path must be absolute")
    # Runfiles may contain symlinks; both supplied and resolved names are retained.
    resolved = path.resolve(strict=True)
    raw, identity = snapshot(resolved, limit)
    require(len(raw) == row["bytes"] and sha(raw) == row["sha256"],
            "input file identity differs")
    identity["supplied_path"] = str(path)
    return raw, identity


def write_original(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)
    os.chmod(path, 0o600)
    require(Path(path).read_bytes() == raw, "copied original differs")


def write_json(path, value):
    write_original(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def installer_inventory(config):
    require(config.get("version") == "0.7.0", "unexpected installer version")
    root = Path(config["root"])
    require(root.is_absolute() and root.is_dir(), "invalid installer root")
    root = root.resolve(strict=True)
    files = config["files"]
    require(isinstance(files, list) and 1 <= len(files) <= 128,
            "invalid installer file count")
    inventory, identities, total = {}, [], 0
    for row in files:
        name = row["relative_name"]
        require(isinstance(name, str) and name and "\\" not in name,
                "invalid installer relative name")
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and str(relative) == name and
                all(part not in ("", ".", "..") for part in relative.parts) and
                relative.parts[0] in ("installer", "installer-0.7.0.dist-info"),
                "installer file outside fixed package")
        require(name not in inventory, "duplicate installer file")
        path = root.joinpath(*relative.parts).resolve(strict=True)
        require(path.is_relative_to(root), "installer file escapes root")
        raw, identity = checked(dict(path=str(path), bytes=row["bytes"],
                                     sha256=row["sha256"]), limit=8 * 1024 * 1024)
        total += len(raw)
        require(total <= 16 * 1024 * 1024, "installer manifest too large")
        inventory[name] = (raw, identity)
        identities.append(dict(identity, relative_name=name))
    for required in ("installer/__init__.py", "installer/destinations.py",
                     "installer/sources.py", "installer/_core.py",
                     "installer-0.7.0.dist-info/METADATA",
                     "installer-0.7.0.dist-info/RECORD"):
        require(required in inventory, "required installer file absent")
    metadata = email.parser.BytesParser().parsebytes(
        inventory["installer-0.7.0.dist-info/METADATA"][0])
    require(metadata.get_all("Name") == ["installer"] and
            metadata.get_all("Version") == ["0.7.0"],
            "installer metadata identity differs")
    # A package import may execute __init__ or another Python leaf. Seal the
    # complete bounded subtree before import, rejecting unlisted executable code.
    expected = {name for name in inventory if name.startswith("installer/")}
    observed = set()
    for path in (root / "installer").rglob("*"):
        if path.is_dir():
            require(not path.is_symlink(), "symlink installer directory")
            continue
        name = path.relative_to(root).as_posix()
        require(name in expected, "unsealed installer package file")
        require(path.resolve(strict=True).is_relative_to(root),
                "installer package file escapes root")
        observed.add(name)
        require(len(observed) <= 128, "installer package too large")
    require(observed == expected, "installer package manifest incomplete")
    return root, inventory, identities


def check_loaded_installer(root, inventory):
    names = []
    for name, module in tuple(sys.modules.items()):
        if name != "installer" and not name.startswith("installer."):
            continue
        require(module is not None and getattr(module, "__file__", None),
                "installer module lacks file identity")
        path = Path(module.__file__).resolve(strict=True)
        require(path.is_relative_to(root), "installer import came from another root")
        relative = path.relative_to(root).as_posix()
        require(relative in inventory, "installer import not sealed")
        raw, _ = snapshot(path, 8 * 1024 * 1024)
        require(sha(raw) == inventory[relative][1]["sha256"],
                "imported installer source changed")
        names.append(dict(module=name, relative_name=relative, sha256=sha(raw)))
    require({"installer", "installer.sources", "installer.destinations"} <=
            {row["module"] for row in names}, "missing actual installer imports")
    return sorted(names, key=lambda row: row["module"])


def capture_installations(fixture, wheels, excluded, output):
    records = []
    for group, roles, filename in GROUPS:
        archives = [Path(wheels[role]["resolved_path"]) for role in roles]
        with fixture.installed_wheels(archives, exclude_source_roots=excluded) as live:
            scripts = Path(live.scripts).resolve(strict=True)
            environment = Path(live.environment).resolve(strict=True)
            require(scripts.is_relative_to(environment), "scripts outside auxiliary venv")
            script = scripts / filename
            raw, identity = snapshot(script, MAX_SCRIPT, executable=True)
            first_line = raw.split(b"\n", 1)[0]
            if b"\n" in raw:
                first_line += b"\n"
            destination = output / group
            destination.mkdir(mode=0o700)
            write_original(destination / filename, raw)
            write_original(destination / "first-line.data", first_line)
            python = Path(live.python)
            require(python.parent.resolve(strict=True) == scripts and python.is_symlink(),
                    "auxiliary venv python is not the expected symlink")
            link = os.readlink(python)
            resolved = python.resolve(strict=True)
            interpreter_raw, interpreter_identity = snapshot(resolved, MAX_FILE)
            require(resolved == Path(sys.executable).resolve(strict=True),
                    "auxiliary venv resolved to a different interpreter")
            require(os.readlink(python) == link and python.resolve(strict=True) == resolved,
                    "venv interpreter changed during capture")
            records.append({
                "group": group, "wheel_roles": list(roles), "script": filename,
                "original_script": identity, "copied_relative_name": group + "/" + filename,
                "first_line_relative_name": group + "/first-line.data",
                "first_line_bytes": len(first_line), "first_line_sha256": sha(first_line),
                "first_line_includes_newline": first_line.endswith(b"\n"),
                "starts_with_shebang": first_line.startswith(b"#!"),
                "script_path_bytes": len(os.fsencode(script)),
                "venv_python_path": str(python),
                "venv_python_path_bytes": len(os.fsencode(python)),
                "venv_python_symlink_text": link,
                "venv_python_resolved": interpreter_identity,
                "fixture_context_was_live": True,
                "auxiliary_reproduction": True,
                "original_failed_execution_script": False,
                "script_executed_by_this_diagnostic": False,
            })
    return records


def public_summary(records):
    return {
        "schema": 1, "status": "captured",
        "kind": "auxiliary-installed-wheel-reproduction",
        "source_revision": SOURCE_REVISION,
        "original_test_evidence": False, "image_acceptance": False,
        "product_scripts_executed": False,
        "groups": [{key: row[key] for key in (
            "group", "script", "wheel_roles", "first_line_bytes", "first_line_sha256",
            "first_line_includes_newline", "starts_with_shebang", "script_path_bytes",
            "venv_python_path_bytes", "fixture_context_was_live",
            "auxiliary_reproduction", "original_failed_execution_script",
            "script_executed_by_this_diagnostic")} |
            {"script_bytes": row["original_script"]["bytes"],
             "script_sha256": row["original_script"]["sha256"],
             "interpreter_sha256": row["venv_python_resolved"]["sha256"]}
            for row in records],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--inputs-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw, _ = snapshot(args.inputs.absolute(), MAX_INPUT)
    require(sha(raw) == args.inputs_sha256, "auxiliary inputs changed")
    inputs = strict_json(raw)
    require(inputs["schema"] == 1 and inputs["source_revision"] == SOURCE_REVISION,
            "different auxiliary source domain")
    output = args.output
    require(output.is_absolute() and not output.exists() and not output.is_symlink(),
            "auxiliary output must be an absent absolute directory")
    output.mkdir(mode=0o700)
    sys.dont_write_bytecode = True
    try:
        require(inputs["fixture"]["sha256"] == FIXTURE_SHA256, "fixture owner changed")
        fixture_raw, fixture_identity = checked(inputs["fixture"], limit=64 * 1024)
        _, interpreter_identity = checked(inputs["interpreter"])
        require(Path(sys.executable).resolve(strict=True) ==
                Path(interpreter_identity["path"]), "wrong selected interpreter")
        require(sys.flags.isolated == 1, "selected interpreter must use -I")
        temporary = Path(inputs["test_tmpdir"])
        require(temporary.is_absolute() and temporary.is_dir() and
                not temporary.is_symlink() and not any(temporary.iterdir()) and
                os.environ.get("TEST_TMPDIR") == str(temporary),
                "TEST_TMPDIR must be the explicitly supplied empty private directory")
        excluded = [Path(path) for path in inputs["exclude_source_roots"]]
        require(excluded and len(excluded) <= 8 and
                all(path.is_absolute() and path.is_dir() for path in excluded),
                "invalid source exclusion roots")
        require(set(inputs["wheels"]) == {"yang", "common", "config", "supervisor"},
                "different fixed wheel group")
        wheels = {}
        for role, row in inputs["wheels"].items():
            _, identity = checked(row)
            require(re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl",
                                 Path(identity["supplied_path"]).name),
                    "unexpected wheel filename")
            wheels[role] = dict(identity, resolved_path=identity["path"])
        root, inventory, installer_files = installer_inventory(inputs["installer"])
        require(not any(name == "installer" or name.startswith("installer.")
                        for name in sys.modules), "installer was already imported")
        sys.path.insert(0, str(root))
        module_name = "_cohort_auxiliary_installed_wheel_fixture"
        require(module_name not in sys.modules, "fixture module already imported")
        spec = importlib.util.spec_from_file_location(module_name, fixture_identity["path"])
        fixture = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = fixture
        exec(compile(fixture_raw, fixture_identity["path"], "exec"), fixture.__dict__)
        require(Path(fixture.__file__).resolve(strict=True) ==
                Path(fixture_identity["path"]), "fixture import identity differs")
        loaded_before = check_loaded_installer(root, inventory)
        runtime_sys_path = list(sys.path)
        records = capture_installations(fixture, wheels, excluded, output)
        loaded_after = check_loaded_installer(root, inventory)
        for row in [inputs["fixture"], inputs["interpreter"], *inputs["wheels"].values()]:
            checked(row)
        installer_inventory(inputs["installer"])
        require(not any(temporary.iterdir()), "unchanged fixture cleanup incomplete")
        private = {
            "schema": 1, "status": "captured", "kind": "auxiliary-reproduction",
            "original_test_evidence": False, "image_acceptance": False,
            "inputs_sha256": sha(raw), "fixture": fixture_identity,
            "interpreter": interpreter_identity,
            "interpreter_runtime": {"executable": sys.executable, "version": sys.version,
                                    "implementation": sys.implementation.name,
                                    "isolated": sys.flags.isolated},
            "runtime_sys_path": runtime_sys_path,
            "installer_files": installer_files,
            "installer_modules_before": loaded_before, "installer_modules_after": loaded_after,
            "source_wheels": wheels, "test_tmpdir": str(temporary),
            "source_exclusion_roots": [str(path) for path in excluded],
            "records": records, "fixture_cleanup_complete": True,
            "limits": "Auxiliary installation paths/runtime may differ from original tests. "
                      "These are not original failed-script bytes or proof of that failure cause.",
        }
        write_json(output / "private.json", private)
        write_json(output / "summary.json", public_summary(records))
        return 0
    except Exception as error:
        write_json(output / "failure.private.json",
                   {"schema": 1, "status": "failed", "kind": "auxiliary-reproduction",
                    "error_type": type(error).__name__, "error": str(error),
                    "original_test_evidence": False, "image_acceptance": False})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

