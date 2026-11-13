#!/usr/bin/env python3
"""Observe fixed, existing legacy images without rebuilding or admitting them."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import observe_original_archive as archives
from tools.bazel.ci import observe_vs_host_binaries as binaries

MANIFEST_SHA256 = "9cb51e91ddf3726be7e565f14a1eeefafea30e4234ab4be9ffe1e47ee480210f"
require = binaries.require


def load_manifest(path):
    with binaries.original.HeldFile(path) as held:
        require(held.initial.st_size < 65536 and
                binaries.original.hash_range(held.fd, 0, held.initial.st_size) == MANIFEST_SHA256,
                "Fixed legacy observation roster differs")
        value = json.loads(os.pread(held.fd, held.initial.st_size, 0))
        held.verify()
    require(len(value["rows"]) == 34 and
            len({row["family"] for row in value["rows"]}) == 8,
            "Fixed legacy family count differs")
    return value


def observe_member(path, row, scratch):
    """Reuse the unchanged Docker inspector on the same held original bytes."""
    observations = [archives.observe(path, tag, row["entrypoint"],
                                     expected_sha256=row["member_sha256"])
                    for tag in row["expected_tags"]]
    return {"name": row["required_name"], "family": row["family"],
            "cache_sha256": row["cache_sha256"],
            "member_sha256": row["member_sha256"],
            "observations": observations,
            "source_admission": False, "VS_assembly_admission": False}


def failure_sites(error):
    """Bounded trusted source sites only; no exception text, values or paths."""
    allowed = {"observe_vs_legacy_configs.py", "observe_vs_host_binaries.py",
               "observe_original_archive.py", "recover_vs_original_inputs.py",
               "vs_archive_reuse.py", "artifact_validation.py"}
    result, frame, count = [], error.__traceback__, 0
    while frame is not None and count < 32:
        code = frame.tb_frame.f_code
        module = Path(code.co_filename).name
        if module in allowed:
            result.append({"module": module, "function": code.co_name[:80], "line": frame.tb_lineno})
        frame, count = frame.tb_next, count + 1
    return result[-8:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    def deadline(*_):
        raise TimeoutError("Legacy metadata observation deadline")

    previous = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(900)
    result = {"status": "LEGACY_CONFIGURATION_OBSERVATION_INCOMPLETE",
              "source_admission": False, "VS_assembly_admission": False,
              "package_or_image_generation": False, "cache_restore": False,
              "results": [], "errors": []}
    created = False
    active = "manifest"
    try:
        value = load_manifest(args.manifest)
        require(args.artifact.resolve() == args.artifact and args.artifact.is_dir(),
                "Original artifact directory must be canonical")
        args.scratch.mkdir(mode=0o700)
        created = True
        for row in value["rows"]:
            active = row["cache_name"]
            try:
                observed = binaries.inspect_cached(
                    args.artifact / row["cache_artifact_path"], [row], args.scratch,
                    observer=observe_member, maximum_member=256 * binaries.MIB)
            except TimeoutError:
                # The one-shot whole-observation deadline must remain terminal.
                raise
            except Exception as error:
                result["errors"].append({"name": row["required_name"], "cache_name": row["cache_name"],
                                         "cache_sha256": row["cache_sha256"],
                                         "member_sha256": row["member_sha256"],
                                         "error_type": type(error).__name__, "sites": failure_sites(error)})
                continue
            result["results"].extend(observed)
        result["artifact"] = value["artifact"]
        result["tag_contract"] = value["tag_contract"]
        if not result["errors"]:
            result["status"] = "LEGACY_CONFIGURATION_OBSERVED_NOT_ADMITTED"
        code = int(bool(result["errors"]))
    except Exception as error:
        result["failed_object"] = active
        result["error_type"] = type(error).__name__
        result["sites"] = failure_sites(error)
        code = 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if created:
            try:
                args.scratch.rmdir()
            except OSError as error:
                result["cleanup_error_type"] = type(error).__name__
                code = 1
        with args.report.open("x") as output:
            json.dump(result, output, sort_keys=True, indent=2)
            output.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
