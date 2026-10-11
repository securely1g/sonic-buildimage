"""Check source-built VS/DASH Syncd packages and their detached symbols."""

import argparse
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile

from tools.bazel.ci.artifact_validation import command, debug_archives, payload, require, sha


def validate(syncd_runtime, syncd_debug, vs_runtime, vs_debug):
    syncd = payload(syncd_runtime)
    required = {"usr/bin/syncd", "usr/bin/syncd_dash", "usr/bin/syncd_request_shutdown",
                "usr/bin/saidump", "usr/bin/saiplayer", "usr/bin/saisdkdump",
                "usr/bin/saidiscovery", "usr/bin/saiasiccmp", "etc/init.d/syncd"}
    require(required <= syncd.keys(), "VS/DASH package is missing a required executable or service file")
    for name in required:
        require(syncd[name]["mode"] == 0o755, "required executable mode differs: " + name)
    symbols = debug_archives(syncd_runtime, syncd_debug, expected_machine=62)
    pairs = {pair["path"]: pair for pair in symbols["debug_pairs"]}
    require({"usr/bin/syncd", "usr/bin/syncd_dash"} <= pairs.keys(),
            "both Syncd variants must have matching split symbols")
    require(pairs["usr/bin/syncd"]["build_id"] != pairs["usr/bin/syncd_dash"]["build_id"],
            "VS and DASH executables unexpectedly have the same identity")
    vs = debug_archives(vs_runtime, vs_debug, expected_machine=62)
    require(any(pair["path"].endswith("/libsaivs.so.0.0.0") for pair in vs["debug_pairs"]),
            "VS SAI lacks matching split symbols")

    dependencies = {}
    source_lines = {}
    with tempfile.TemporaryDirectory(prefix="syncd-dash-packages-") as temporary:
        installed = Path(temporary)
        for path in (syncd_runtime, syncd_debug):
            with tarfile.open(path) as archive:
                archive.extractall(installed, filter="data")
        with tarfile.open(syncd_runtime) as archive:
            members = {member.name.lstrip("./"): member for member in archive}
            for name, expected in (("syncd", "libsaivs.so.0"), ("syncd_dash", "libsai.so")):
                path = Path(temporary) / name
                path.write_bytes(archive.extractfile(members["usr/bin/" + name]).read())
                dynamic = command(["readelf", "-d", str(path)])
                needed = re.findall(r"\(NEEDED\).*\[(.*?)\]", dynamic)
                require(expected in needed, name + " does not link its intended SAI implementation")
                require(all("/" not in item for item in needed), name + " embeds a build-directory dependency")
                dependencies[name] = needed
                # GDB must discover the installed detached symbols itself;
                # passing a symbol-file directly would bypass this contract.
                lines = command(["gdb", "--batch", "--nx", "-ex",
                                 "set debug-file-directory " + str(installed / "usr/lib/debug"),
                                 "-ex", "file " + str(installed / "usr/bin" / name),
                                 "-ex", "info line syncd_main"],
                                env=dict(os.environ, DEBUGINFOD_URLS="", LC_ALL="C"))
                match = re.search(r'Line (\d+) of "([^"]*syncd_main\.cpp)"', lines)
                require(match is not None, name + " installed symbols lack source-line lookup: " + lines)
                source_lines[name] = {"line": int(match[1]), "source": match[2]}
    return {"syncd_symbols": symbols, "vs_symbols": vs, "needed": dependencies,
            "installed_source_lines": source_lines,
            "sha256": {name: sha(path) for name, path in {
                "syncd_runtime": syncd_runtime, "syncd_debug": syncd_debug,
                "vs_runtime": vs_runtime, "vs_debug": vs_debug,
            }.items()},
            "remaining_checks": ["Redis-backed Syncd startup and forwarding"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("syncd-runtime", "syncd-debug", "vs-runtime", "vs-debug"):
        parser.add_argument("--" + name, required=True, type=Path)
    result = validate(**vars(parser.parse_args()))
    result["_generated"] = {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
                            "generator": "tools/bazel/p4/sdk/packages_test.py"}
    output = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", ".")) / "validation.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("Verified source-built VS/DASH Syncd, VS SAI and their detached symbols.")


if __name__ == "__main__":
    main()
