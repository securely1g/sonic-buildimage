#!/usr/bin/env python3
"""Run the original Teamd installed-image checks with private kernel/Redis state."""

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci import teamd_image as driver
from tools.bazel.ci import validate_runtime


def kernel_preflight(modules=Path("/sys/module")):
    """Inspect prerequisites without loading modules or changing the host."""
    required = ("team", "team_mode_loadbalance")
    missing = [name for name in required if not (modules / name).is_dir()]
    check.require(not missing, "Teamd worker requires preloaded kernel modules: " + ", ".join(missing))
    return {"status": "passed", "preloaded_modules": list(required), "read_only": True}


def probe_command(tag, container, directory, redis, *, debug):
    command = [
        "run", "--rm", "--name", container, "--network=none", "--cap-drop=ALL",
        "--cap-add=NET_ADMIN", "--cap-add=NET_RAW", "--security-opt=no-new-privileges",
        "--read-only", "--pids-limit=128", "--memory=4g", "--cpus=2",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m",
        "--tmpfs", "/run/teamd:rw,nosuid,nodev,noexec,size=16m",
        "--tmpfs", "/var/warmboot:rw,nosuid,nodev,noexec,size=16m",
        "--mount", "type=volume,dst=/run/redis",
    ]
    for source, destination in (
        (ROOT / "dockers/docker-teamd/bazel/runtime_probe.py", "/probe/runtime_probe.py"),
        (ROOT / "tools/bazel/oci/runtime_smoke.py", "/probe/runtime_smoke.py"),
        (redis, "/probe/redis"),
        (directory / "validation.json", "/probe/validation.json"),
    ):
        command += ["--mount", f"type=bind,src={source},dst={destination},readonly"]
    command += ["--entrypoint", "/usr/bin/python3", tag, "-B", "/probe/runtime_probe.py",
                "--validation", "/probe/validation.json"]
    if debug:
        command.append("--debug")
    return command


def checked_report(directory, identity):
    report = json.loads((directory / "validation.json").read_text())
    check.require(report["status"] == "passed" and report["architecture"] == "amd64",
                  "Teamd static validation did not pass for AMD64")
    check.require(report["revision"] == identity["revision"], "Teamd checker differs from built source")
    check.require(set(report["artifacts"]) == set(driver.TARGETS), "incomplete Teamd artifact inventory")
    for name, item in report["artifacts"].items():
        path = directory / name
        check.require(Path(name).name == name and path.is_file() and not path.is_symlink(),
                      "invalid retained Teamd artifact: " + name)
        check.require(item["target"] == driver.TARGETS[name], "Teamd artifact target changed: " + name)
        check.require(type(item["bytes"]) is int and path.stat().st_size == item["bytes"],
                      "Teamd artifact size changed: " + name)
        check.require(check.sha(path) == item["sha256"], "Teamd artifact changed: " + name)
    return report


def checked_probe(result, report, *, debug):
    native = report["native"] if debug else report["runtime_native"]
    check.require(result["status"] == "passed" and
                  result["source_elf_count"] == result["loader_checks"] == len(native),
                  "incomplete installed Teamd ELF/loader checks")
    check.require(result["debug_checks"] == (3 if debug else 0), "incomplete installed libteam GDB checks")
    check.require(result["teamd_control"] is True and result["tlm_teamd"] is True and
                  result["startup_template"] is True and result["teamsyncd_mtu"] == "1450" and
                  result["team_device"] == "PortChannel42" and
                  isinstance(result["teamnl_kernel_mode"], str) and result["teamnl_kernel_mode"],
                  "original Teamd control, startup or SWSS service checks did not complete")


def loaded(directory):
    check.require(platform.machine() == "x86_64", "Teamd loaded acceptance requires native AMD64")
    driver.clean_source(directory)
    identity = driver.source_identity()
    report = checked_report(directory, identity)
    modules = kernel_preflight()
    receipt = {"status": "running", "revision": identity["revision"], "architecture": "amd64",
               "kernel_preflight": modules, "images": {}, "artifacts": report["artifacts"]}
    tags, containers = [], []
    try:
        with tempfile.TemporaryDirectory(prefix="teamd-loaded-") as temporary:
            redis = Path(temporary) / "redis"
            redis.mkdir()
            # Preserve the original fixture's standard TAR extraction, including
            # Redis's declared multicall links. The complete TAR hash was checked.
            subprocess.run(["tar", "--no-same-owner", "-xf", str(directory / "test-redis.tar"),
                            "-C", str(redis)], check=True)
            for name in driver.IMAGES:
                expected = json.loads((directory / (name + ".validation.json")).read_text())
                check.require(expected["status"] == "passed", "Teamd image validation is incomplete")
                tag = "sonic-teamd-validation-" + uuid.uuid4().hex + ":latest"
                tags.append(tag)
                actual, config, _ = validate_runtime.load_archive(
                    directory / (name + ".gz"), tag, expected["config_digest"])
                validate_runtime.verify_loaded_image(actual, config, expected["config_digest"])
                check.require((actual["Os"], actual["Architecture"]) == ("linux", "amd64"),
                              "loaded Teamd platform changed")
                check.require(actual["RootFS"]["Layers"] == expected["rootfs"]["diff_ids"],
                              "loaded Teamd layers differ from static validation")
                container = "teamd-probe-" + uuid.uuid4().hex
                containers.append(container)
                debug = name.endswith("-dbg")
                text = validate_runtime.docker(*probe_command(tag, container, directory, redis, debug=debug),
                                               timeout=600)
                result = json.loads(text)
                checked_probe(result, report, debug=debug)
                result.update(variant=name, archive_sha256=report["artifacts"][name + ".gz"]["sha256"],
                              config_and_layers_verified=True)
                (directory / (name + ".runtime.json")).write_text(json.dumps(result, indent=2) + "\n")
                receipt["images"][name] = result
        driver.clean_source(directory)
        check.require(driver.source_identity() == identity, "Teamd source changed during loaded checks")
        receipt["status"] = "passed"
        return receipt
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        cleanup_failed = False
        commands = [["docker", "rm", "-f", name] for name in reversed(containers)]
        commands += [["docker", "image", "rm", tag] for tag in reversed(tags)]
        for command in commands:
            try:
                subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failed = True
        if cleanup_failed:
            receipt.update(status="failed", cleanup_error="private Docker cleanup did not complete")
        (directory / "loaded-validation.json").write_text(json.dumps(receipt, indent=2) + "\n")
        check.require(not cleanup_failed, "private Teamd Docker cleanup did not complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    loaded(args.artifacts.resolve())


if __name__ == "__main__":
    main()
