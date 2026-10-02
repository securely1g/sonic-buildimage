#!/usr/bin/env python3
"""Validate complete SWSS OCI archives with an explicitly selected Docker daemon.

Run this integration check against a private disposable daemon. It loads the
archives, creates isolated containers, and removes only containers it created.
Images remain available for the subsequent SONiC image handoff. No service is
started against a live Redis database: the startup control-flow probe stubs the
database, configuration generator, and supervisor and records that limitation.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import uuid
import zlib


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def path_name(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts, "unsafe archive path: " + value)
    return str(path)


def command(arguments, **kwargs):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=300, **kwargs)
    require(result.returncode == 0, "command failed: " + repr(arguments[:8]) + "\n" + result.stdout + result.stderr)
    return result.stdout


def metadata(member):
    kind = ("file" if member.isfile() else "directory" if member.isdir() else
            "symlink" if member.issym() else "hardlink" if member.islnk() else "other")
    result = {"kind": kind, "mode": member.mode, "uid": member.uid, "gid": member.gid}
    if kind in ("symlink", "hardlink"):
        # Layer flattening may add a harmless ./ prefix to link targets.
        # PurePosixPath removes dot components while retaining .. components.
        result["linkname"] = str(PurePosixPath(member.linkname))
    return result


def payload(path, require_root=True):
    result = {}
    with tarfile.open(path) as archive:
        for member in archive:
            name = path_name(member.name)
            require(name not in result, "duplicate package member: " + name)
            item = metadata(member)
            require(item["kind"] != "other", "unsupported package member: " + name)
            require(not require_root or (member.uid, member.gid) == (0, 0), "non-root package owner: " + name)
            if member.isfile():
                with archive.extractfile(member) as stream:
                    data = stream.read()
                item.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
                if data.startswith(b"\x7fELF"):
                    require(data[:6] == b"\x7fELF\x02\x01", "expected little-endian ELF64: " + name)
                    item["elf_machine"] = struct.unpack_from("<H", data, 18)[0]
            result[name] = item
    return result


# DASH and SWSS link this source-built shared library. Keep the matching runtime
# and symbols in the final layer instead of installing a second Debian build.
PROTOBUF_RUNTIME = "usr/lib/x86_64-linux-gnu/libprotobuf.so.32.0.12"


def source_protobuf_contract(combined, protobuf):
    require(protobuf.get(PROTOBUF_RUNTIME, {}).get("elf_machine") == 62,
            "missing source-built AMD64 protobuf runtime")
    for name, item in protobuf.items():
        if item["kind"] != "directory":
            require(combined.get(name) == dict(item, uid=0, gid=0),
                    "runtime layer changes source protobuf bytes or modes: " + name)
    require(combined.get("usr/lib/x86_64-linux-gnu/libprotobuf.so.32", {}).get("linkname") ==
            "libprotobuf.so.32.0.12", "incorrect source protobuf SONAME link")
    require({name for name, item in combined.items()
             if "/libprotobuf.so" in name and item["kind"] != "directory"} ==
            {PROTOBUF_RUNTIME, "usr/lib/x86_64-linux-gnu/libprotobuf.so.32"},
            "conflicting full protobuf runtime in declared layer")


def swss_contract(source, files):
    values = {}
    for node in ast.parse((source / "bazel/production_sources.bzl").read_text()).body:
        if isinstance(node, ast.Assign):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    programs = {item["install_path"] for item in values["SWSS_PROGRAMS"].values()}
    expected = {name: (0o755, None) for name in programs}
    for item in values["SWSS_AUTOMAKE_INSTALL"]:
        expected[item["install_path"]] = (item["mode"], source / item["source"])
    for line in (source / "debian/swss.install").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        origin, directory = line.split()
        name = str(PurePosixPath(directory) / PurePosixPath(origin).name)
        require(name not in expected, "duplicate SWSS install path: " + name)
        binary = origin == "target/release/countersyncd"
        expected[name] = (0o755 if directory == "usr/bin" else 0o644, None if binary else source / origin)
        if binary:
            programs.add(name)
    actual = {name for name, item in files.items() if item["kind"] != "directory"}
    require(actual == set(expected), "SWSS package differs from the source install contract")
    require(len(programs) == 30, "expected the complete 30-program SWSS configuration")
    for name, (mode, origin) in expected.items():
        require(files[name]["kind"] == "file" and files[name]["mode"] == mode, "SWSS type or mode: " + name)
        if origin is not None:
            require(files[name]["sha256"] == sha(origin), "SWSS installed data differs from source: " + name)
        else:
            require("elf_machine" in files[name], "SWSS executable is not ELF: " + name)
    return sorted(programs)


def archive_identity(path, expected_tag, manifest):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = [path_name(member.name) for member in members]
        require(len(names) == len(set(names)), "duplicate Docker archive members")
        indexed = dict(zip(names, members))

        def read_json(name):
            member = indexed[name]
            require(member.isfile() and member.size < 4 * 1024 * 1024, "invalid image JSON: " + name)
            return archive.extractfile(member).read()

        saved = json.loads(read_json("manifest.json"))
        require(isinstance(saved, list) and len(saved) == 1, "expected one Docker image")
        saved = saved[0]
        require(saved["RepoTags"] == [expected_tag], "incorrect Docker archive tag")
        config_bytes = read_json(path_name(saved["Config"]))
        config = json.loads(config_bytes)
        layers = saved["Layers"]
        require(layers and all(path_name(name) in indexed for name in layers), "missing Docker layers")
        require(len(layers) == len(config["rootfs"]["diff_ids"]), "config/layer count mismatch")
    require(config.get("os") == "linux" and config.get("architecture") in ("amd64", "arm64"), "unexpected image platform")
    runtime = config["config"]
    require(runtime.get("Entrypoint") == ["/usr/bin/docker-init.sh"], "SWSS entrypoint differs")
    labels = runtime.get("Labels", {})
    require(json.loads(labels["com.azure.sonic.manifest"]) == manifest, "native service manifest differs")
    require(not any(name.startswith("com.azure.sonic.manifest.") for name in labels), "conflicting flattened service labels")
    require("DEBIAN_FRONTEND=noninteractive" in runtime.get("Env", []), "missing build/runtime environment")
    return {"path": str(path), "sha256": sha(path), "size": path.stat().st_size,
            "image_id": "sha256:" + hashlib.sha256(config_bytes).hexdigest(),
            "tag": expected_tag, "architecture": config["architecture"], "config": config}


class Docker:
    def __init__(self, host):
        self.prefix = ["docker", "--host", host]
        self.session = uuid.uuid4().hex
        self.info = json.loads(self.run("info", "--format", "{{json .}}"))

    def run(self, *arguments):
        return command(self.prefix + list(arguments))

    def load(self, image):
        self.run("load", "--input", image["path"])
        actual = json.loads(self.run("image", "inspect", image["tag"]))[0]
        require(actual["Id"] == image["image_id"], "Docker loaded a different image")
        require(actual["RootFS"]["Layers"] == image["config"]["rootfs"]["diff_ids"], "Docker rootfs layer identity differs")

    def create(self, image, arguments=(), options=()):
        return self.run("create", "--network=none", "--cap-drop=ALL",
                        "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=2g",
                        "--cpus=2", "--label", "sonic.swss.validation=" + self.session,
                        *options, image["image_id"], *arguments).strip()

    def probe(self, image, arguments=(), options=()):
        container = self.create(image, arguments, options)
        try:
            output = self.run("start", "--attach", container)
            state = json.loads(self.run("inspect", "--format", "{{json .State}}", container))
            require(state["ExitCode"] == 0, "container probe failed: " + output)
            return output
        finally:
            self.run("rm", "--force", container)

    def export(self, image, expected, destination):
        # Debian's merged-/usr layout and /var/run alias change the path
        # emitted by Docker export. Resolve only parent directories, retaining
        # the final component so an expected symlink is still checked as one.
        locations = json.loads(self.probe(image, ("-c", r'''
import json, os, sys
print(json.dumps({name: os.path.normpath(os.path.join(
    os.path.realpath(os.path.dirname('/' + name)), os.path.basename(name)
)).lstrip('/') or '.' for name in sys.argv[1:]}))
''', *expected), ("--read-only", "--entrypoint=/usr/bin/python3")))
        exported_names = {}
        for name, location in locations.items():
            exported_names.setdefault(path_name(location), []).append(name)
        self.exported_locations = locations
        container = self.create(image, options=("--entrypoint=/bin/true",))
        actual = {}
        try:
            process = subprocess.Popen(self.prefix + ["export", container], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    for member in archive:
                        name = path_name(member.name)
                        if name not in exported_names:
                            continue
                        logical_names = exported_names[name]
                        require(not any(logical in actual for logical in logical_names),
                                "duplicate exported payload: " + name)
                        item = metadata(member)
                        if member.isfile():
                            target = destination / logical_names[0]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.extractfile(member) as stream, target.open("wb") as output:
                                shutil.copyfileobj(stream, output)
                            item.update(sha256=sha(target), size=target.stat().st_size)
                            for logical in logical_names[1:]:
                                alias = destination / logical
                                alias.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(target, alias)
                        for logical in logical_names:
                            actual[logical] = item
                process.stdout.close()
                errors = process.stderr.read().decode(errors="replace")
                require(process.wait() == 0, "Docker export failed: " + errors)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
            for name, record in expected.items():
                # Docker export represents the root implicitly.
                if name == "." and name not in actual:
                    continue
                required = {key: value for key, value in record.items() if key != "elf_machine"}
                require(actual.get(name) == required, "container payload differs: " + name + " " + repr(actual.get(name)))
            return actual
        finally:
            self.run("rm", "--force", container)


LOADER_PROBE = r'''
import importlib, importlib.metadata, json, os, subprocess, sys
records, errors, imports = [], [], []
for path in sys.argv[1:]:
    # Python supplies the DASH extension's interpreter symbols when importing it.
    # Check its shared-library closure here and exercise the import below.
    flags = [] if path == 'usr/lib/python3/dist-packages/dash_api/_utils.so' else ['-r']
    p = subprocess.run(['/usr/bin/ldd', *flags, '/' + path], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    passed = not (p.returncode or 'not found' in p.stdout or 'undefined symbol:' in p.stdout)
    if not passed:
        errors.append('loader: ' + path)
    records.append({'path': path, 'passed': passed, 'returncode': p.returncode, 'loader_output': p.stdout.strip()})
modules = ['swsscommon.swsscommon', 'swsscommon._swsscommon', 'sonic_py_common', 'jinja2', 'netifaces', 'pyroute2', 'scapy.all', 'google.protobuf', 'click', 'dash_api._utils', 'dash_api.utils', 'dash_api.appliance_pb2']
for name in modules:
    try:
        module = importlib.import_module(name)
        imports.append({'name': name, 'passed': True, 'file': getattr(module, '__file__', None)})
    except Exception as error:
        errors.append('import: ' + name)
        imports.append({'name': name, 'passed': False, 'error': str(error)})
try:
    version = importlib.metadata.version('pyroute2')
except importlib.metadata.PackageNotFoundError:
    version = None
if version != '0.5.14':
    errors.append('pyroute2 version: ' + str(version))
document = {'sip': {'ipv4': 16777482}, 'vm_vni': 4321, 'local_region_id': 100,
            'outbound_direction_lookup': 'dst_mac', 'trusted_vnis_list': [{'value': 100}]}
cli_roundtrip = {'input': document}
cli = ['/usr/bin/dash_api_utils', '-t', 'DASH_APPLIANCE_TABLE']
try:
    encoded = subprocess.run(cli + ['--to_proto'], input=json.dumps(document).encode(), check=True, capture_output=True).stdout
    decoded = subprocess.run(cli + ['--to_json'], input=encoded, check=True, capture_output=True).stdout
    cli_roundtrip.update(encoded_hex=encoded.hex(), decoded=json.loads(decoded))
    if cli_roundtrip['decoded'] != document:
        errors.append('DASH CLI round trip')
except Exception as error:
    errors.append('DASH CLI: ' + str(error))
syntax = subprocess.run(['/bin/bash', '-n', '/usr/bin/docker-init.sh'], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
if syntax.returncode:
    errors.append('entrypoint syntax: ' + syntax.stdout)
print(json.dumps({'passed': not errors, 'errors': errors, 'relocations': records, 'python_imports': imports, 'pyroute2_version': version, 'dash_cli': cli_roundtrip}))
'''


def elf_debug(extracted, expected, prebuilt):
    pairs, gaps = [], []
    for name, item in expected.items():
        if "elf_machine" not in item or name.startswith("usr/lib/debug/"):
            continue
        binary = extracted / name
        notes = command(["readelf", "-n", str(binary)])
        match = re.search(r"Build ID: ([0-9a-f]+)", notes)
        require(match is not None, "missing runtime ELF build ID: " + name)
        identifier = match[1]
        debug_name = "usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug"
        sections = command(["readelf", "-SW", str(binary)])
        require(".debug_info" not in sections, "unstripped runtime ELF: " + name)
        if name in prebuilt:
            require(debug_name not in expected, "prebuilt debug-gap declaration is stale: " + name)
            gaps.append({"path": name, "build_id": identifier, "reason": "producer supplies no matching debug artifact"})
            continue
        require(debug_name in expected, "missing embedded debug file: " + name)
        symbols = extracted / debug_name
        require("Build ID: " + identifier in command(["readelf", "-n", str(symbols)]), "debug build ID differs: " + name)
        require(".debug_info" in command(["readelf", "-SW", str(symbols)]), "debug file lacks DWARF: " + name)
        link = extracted / "debuglink"
        command(["objcopy", "--dump-section", ".gnu_debuglink=" + str(link), str(binary)])
        raw = link.read_bytes()
        end = raw.index(0)
        require(raw[:end].decode() == symbols.name, "debuglink filename differs: " + name)
        offset = (end + 4) & ~3
        require(len(raw) >= offset + 4 and struct.unpack_from("<I", raw, offset)[0] == zlib.crc32(symbols.read_bytes()),
                "debuglink CRC differs: " + name)
        pairs.append({"path": name, "build_id": identifier, "debug_path": debug_name})
    require(set(prebuilt) == {item["path"] for item in gaps}, "unmatched prebuilt gap declaration")
    return pairs, gaps


def gdb_probe(docker, image, pairs):
    results = []
    for program, symbol in (("orchagent", "main"), ("countersyncd", "countersyncd::main")):
        pair = next(item for item in pairs if item["path"] == "usr/bin/" + program)
        output = docker.probe(image, ("--batch", "-nx", "-nh", "-ex", "set debuginfod enabled off",
                                     "-ex", "set auto-load off", "-ex", "set debug-file-directory /usr/lib/debug",
                                     "-ex", "file /usr/bin/" + program, "-ex", "info line " + symbol,
                                     "-ex", "python print('SWSS_DEBUG_FILES=' + repr([f.filename for f in gdb.objfiles()]))"),
                              ("--read-only", "--tmpfs=/tmp", "--entrypoint=/usr/bin/gdb"))
        require("/" + pair["debug_path"] in output, "image GDB did not load packaged symbols: " + program)
        require(re.search(r'Line [1-9][0-9]* of "[^"]+"', output), "image GDB source lookup failed: " + output)
        results.append({"program": program, "output": output.strip()})
    return results


INIT_STUB = r'''#!/usr/bin/python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
if name == 'sonic-cfggen':
    args = sys.argv[1:]
    pathlib.Path('/tmp/swss-cfggen-args.json').write_text(json.dumps(args))
    for i, arg in enumerate(args):
        if arg == '-t' and ',' in args[i+1]:
            template, output = args[i+1].split(',', 1)
            assert pathlib.Path(template).is_file(), template
            target = pathlib.Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('# generated by isolated startup probe\n')
    print(os.environ.get('SWSS_PROBE_VLAN', ''))
elif name == 'sonic-db-cli':
    print(os.environ.get('SWSS_PROBE_SUBTYPE', '') if sys.argv[-1] == 'subtype' else os.environ.get('SWSS_PROBE_SWITCH', ''))
else:
    args = json.loads(pathlib.Path('/tmp/swss-cfggen-args.json').read_text())
    assert '-d' in args and '-y' in args and '/etc/sonic/constants.yml' in args
    assert '-a' in args and json.loads(args[args.index('-a')+1]) == {'ASIC_VENDOR': 'unknown'}
    expected = {'arp_update.conf': bool(os.environ.get('SWSS_PROBE_VLAN')) or os.environ.get('SWSS_PROBE_SWITCH') == 'chassis-packet',
                'ndppd.conf': bool(os.environ.get('SWSS_PROBE_VLAN')),
                'tunnel_packet_handler.conf': os.environ.get('SWSS_PROBE_SUBTYPE') == 'DualToR'}
    for name, present in expected.items():
        target = pathlib.Path('/etc/supervisor/conf.d')/name
        assert target.exists() == present, (name, present)
        if present:
            assert target.read_bytes() == (pathlib.Path('/usr/share/sonic/templates')/name).read_bytes()
    assert pathlib.Path('/usr/bin/wait_for_link.sh').stat().st_mode & 0o111
    print(json.dumps({'scenario': os.environ['SWSS_PROBE_SCENARIO'], 'copied_configuration': expected, 'cfggen_arguments': args}))
'''


def init_probe(docker, image, directory):
    stub_dir = directory / "startup-stubs"
    stub_dir.mkdir()
    stub_dir.chmod(0o755)
    for name in ("sonic-cfggen", "sonic-db-cli", "supervisord"):
        path = stub_dir / name
        path.write_text(INIT_STUB)
        path.chmod(0o755)
    results = []
    for name, vlan, subtype, switch in (("ordinary", "", "", ""), ("vlan", "Vlan1000", "", ""),
                                       ("dual-tor", "", "DualToR", ""), ("chassis-packet", "", "", "chassis-packet")):
        options = ("--mount", "type=bind,source=" + str(stub_dir) + ",target=/validation,readonly",
                   "--mount", "type=bind,source=" + str(stub_dir / "supervisord") + ",target=/usr/local/bin/supervisord,readonly",
                   "--env=PATH=/validation:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                   "--env=SWSS_PROBE_SCENARIO=" + name, "--env=SWSS_PROBE_VLAN=" + vlan,
                   "--env=SWSS_PROBE_SUBTYPE=" + subtype, "--env=SWSS_PROBE_SWITCH=" + switch,
                   "--env=ASIC_VENDOR=unknown", "--env=SWITCH_TYPE=" + switch)
        results.append(json.loads(docker.probe(image, options=options)))
    return {"scope": "actual entrypoint with cfggen, database and supervisor stubs; no live switch database", "scenarios": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-host", required=True, help="explicit private Docker daemon endpoint")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--debug-archive", type=Path)
    parser.add_argument("--swss-source", required=True, type=Path)
    parser.add_argument("--runtime-tar", required=True, type=Path)
    parser.add_argument("--rdeps-tar", required=True, type=Path, help="combined, normalized component runtime layer")
    parser.add_argument("--dependency-tar", action="append", default=[], type=Path)
    parser.add_argument("--config-tar", required=True, type=Path)
    parser.add_argument("--debug-tar", type=Path, help="complete image debug_symbols tar")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--debug-manifest", type=Path)
    parser.add_argument("--prebuilt-library", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    require(bool(args.debug_archive) == bool(args.debug_tar) == bool(args.debug_manifest), "supply all three debug inputs together")
    source_payload = payload(args.runtime_tar, require_root=False)
    programs = swss_contract(args.swss_source, source_payload)
    for archive in args.dependency_tar:
        for name, item in payload(archive, require_root=False).items():
            normalized = dict(item, uid=0, gid=0)
            require(name not in source_payload or normalized == dict(source_payload[name], uid=0, gid=0),
                    "conflicting declared payload: " + name)
            source_payload[name] = item
    expected = payload(args.rdeps_tar)
    for name, item in source_payload.items():
        if item["kind"] == "directory" and name not in expected:
            continue
        require(expected.get(name) == dict(item, uid=0, gid=0),
                "combined runtime layer changes component bytes or modes: " + name)
    for name, item in payload(args.config_tar).items():
        require(name not in expected or item == expected[name], "conflicting configuration payload: " + name)
        expected[name] = item
    # Linux symlink permissions are always 0777, independent of the mode field
    # serialized by the source tar writer. File/directory modes stay exact.
    expected = {name: dict(item, mode=0o777) if item["kind"] == "symlink" else item
                for name, item in expected.items()}
    normal = archive_identity(args.archive, "docker-orchagent:latest", json.loads(args.manifest.read_text()))
    machine = {"amd64": 62, "arm64": 183}[normal["architecture"]]
    elfs = sorted(name for name, item in expected.items() if "elf_machine" in item)
    require(all(expected[name]["elf_machine"] == machine for name in elfs), "container ELF target architecture differs")
    input_tars = [args.runtime_tar, args.rdeps_tar, *args.dependency_tar, args.config_tar]
    if args.debug_tar:
        input_tars.append(args.debug_tar)
    report = {"status": "running", "runtime_archive": normal, "programs": programs,
              "expected_payload": expected, "elf_paths": elfs,
              "package_inputs": [{"path": str(path.resolve()), "sha256": sha(path)} for path in input_tars],
              "swss_source_contract": {name: sha(args.swss_source / name)
                                       for name in ("bazel/production_sources.bzl", "debian/swss.install")}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        docker = Docker(args.docker_host)
        report["daemon"] = {key: docker.info.get(key) for key in ("ID", "Name", "ServerVersion", "Driver")}
        docker.load(normal)
        with tempfile.TemporaryDirectory(prefix="swss-container-check-", dir=args.output.parent) as temporary:
            directory = Path(temporary)
            docker.export(normal, expected, directory / "runtime")
            report["runtime_export_locations"] = docker.exported_locations
            report["runtime_loader"] = json.loads(docker.probe(normal, ("-c", LOADER_PROBE, *elfs),
                                                             ("--read-only", "--tmpfs=/tmp", "--entrypoint=/usr/bin/python3")))
            require(report["runtime_loader"]["passed"], "container loader/import failures: " +
                    "; ".join(report["runtime_loader"]["errors"]))
            report["startup"] = init_probe(docker, normal, directory)
            if args.debug_archive:
                debug = archive_identity(args.debug_archive, "docker-orchagent-dbg:latest", json.loads(args.debug_manifest.read_text()))
                require(debug["architecture"] == normal["architecture"], "debug image architecture differs")
                base_layers = normal["config"]["rootfs"]["diff_ids"]
                require(debug["config"]["rootfs"]["diff_ids"][:len(base_layers)] == base_layers, "debug image is not based on the tested runtime")
                symbols = payload(args.debug_tar)
                require(symbols and all(name == "." or item["kind"] == "directory" or re.fullmatch(r"usr/lib/debug/\.build-id/[0-9a-f]{2}/[0-9a-f]+\.debug", name)
                                       for name, item in symbols.items()), "unexpected debug payload")
                combined = dict(expected)
                for name, item in symbols.items():
                    require(name not in combined or item == combined[name], "debug layer changes runtime payload: " + name)
                    combined[name] = item
                docker.load(debug)
                docker.export(debug, combined, directory / "debug")
                report["debug_loader"] = json.loads(docker.probe(debug, ("-c", LOADER_PROBE, *elfs),
                                                               ("--read-only", "--tmpfs=/tmp", "--entrypoint=/usr/bin/python3")))
                require(report["debug_loader"]["passed"], "debug container loader/import failures: " +
                        "; ".join(report["debug_loader"]["errors"]))
                pairs, gaps = elf_debug(directory / "debug", combined, args.prebuilt_library)
                required_debug = set(programs) | {"usr/lib/libdashapi.so", "usr/lib/python3/dist-packages/dash_api/_utils.so", PROTOBUF_RUNTIME}
                require(required_debug <= {item["path"] for item in pairs}, "missing SWSS/DASH/protobuf debug coverage")
                report.update(debug_archive=debug, debug_pairs=pairs, prebuilt_debug_gaps=gaps,
                              gdb=gdb_probe(docker, debug, pairs))
            report["status"] = "passed" if args.debug_archive else "runtime-passed-debug-not-run"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "swss_programs": len(programs), "elfs": len(elfs), "report": str(args.output)}))


if __name__ == "__main__":
    main()
