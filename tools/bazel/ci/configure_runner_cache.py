#!/usr/bin/env python3
"""Apply build resource limits while preserving the runner's Bazel configuration."""

import argparse
import json
from pathlib import Path


RESOURCE_BEGIN = "# BEGIN SONiC native CI Bazel resources"
RESOURCE_END = "# END SONiC native CI Bazel resources"
CGROUP_ROOT = Path("/sys/fs/cgroup")
RESOURCE_PROFILES = {
    (6, 20 * 1024**3): ("pilot", 12288, "20g"),
    (4, 10 * 1024**3): ("fleet", 7168, "10g"),
}


class ConfigurationFailure(Exception):
    def __init__(self, error_class):
        super().__init__(error_class)
        self.error_class = error_class


def read_resource_profile():
    """Select one supported worker profile from its finite cgroup v2 limits."""
    try:
        memory = int((CGROUP_ROOT / "memory.max").read_text(encoding="ascii").strip())
        quota, period = map(int, (CGROUP_ROOT / "cpu.max").read_text(encoding="ascii").split())
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationFailure("invalid_cgroup_limits") from None
    if memory <= 0 or quota <= 0 or period <= 0:
        raise ConfigurationFailure("invalid_cgroup_limits")
    if quota % period:
        raise ConfigurationFailure("unsupported_resource_profile")
    cpu_cores = quota // period
    selected = RESOURCE_PROFILES.get((cpu_cores, memory))
    if selected is None:
        raise ConfigurationFailure("unsupported_resource_profile")
    name, local_memory_mb, sonic_build_memory = selected
    return {"name": name, "cpu_cores": cpu_cores, "memory_limit_bytes": memory,
            "sonic_build_memory": sonic_build_memory, "jobs": 4,
            "local_memory_mb": local_memory_mb}


def read_runner_rc(rc):
    try:
        return rc.read_bytes()
    except FileNotFoundError:
        raise ConfigurationFailure("runner_rc_missing") from None
    except OSError:
        raise ConfigurationFailure("runner_rc_unreadable") from None


def set_resource_limits(rc, jobs, local_memory_mb):
    contents = read_runner_rc(rc)
    begin, end = RESOURCE_BEGIN.encode(), RESOURCE_END.encode()
    # Build options also apply to tests. The legacy memory flag works with both
    # sonic-pins' Bazel 6.4 and the newer image builds; 6.4 lacks local_resources.
    block = "\n".join([
        RESOURCE_BEGIN,
        f"build --jobs={jobs}",
        f"build --local_ram_resources={local_memory_mb}",
        RESOURCE_END,
    ]).encode()
    outside = contents
    if begin in contents or end in contents:
        if (contents.count(begin) != 1 or contents.count(end) != 1
                or contents.index(begin) > contents.index(end)):
            raise ConfigurationFailure("invalid_resource_block")
        start, finish = contents.index(begin), contents.index(end) + len(end)
        # The generated trailing newline belongs to this block, not the next
        # setting. Remove it too so repeated runs do not accumulate blank lines.
        if contents[finish:finish + 1] == b"\n":
            finish += 1
        outside = contents[:start] + contents[finish:]
    # Later rc options win: keep this job's resource limits after all existing
    # settings while preserving those settings in their original order.
    separator = b"" if not outside or outside.endswith(b"\n") else b"\n"
    updated = outside + separator + block + b"\n"
    # Do not normalize or inspect the manager-owned cache, authentication, or
    # trust settings. Everything outside this resource block stays byte-for-byte.
    if updated != contents:
        try:
            rc.write_bytes(updated)
        except OSError:
            raise ConfigurationFailure("runner_rc_write_failed") from None


def positive_integer(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def configure(rc, resource_limits=None):
    if resource_limits is None:
        read_runner_rc(rc)
    else:
        set_resource_limits(rc, **resource_limits)
    return {"status": "configured", "resource_limits": resource_limits or {}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rc", type=Path, default=Path.home() / ".bazelrc")
    parser.add_argument("--output", type=Path, default=Path("artifacts/vs/cache-configuration.json"))
    parser.add_argument("--jobs", type=positive_integer,
                        help="Job-scoped Bazel build/test concurrency (requires --local-memory-mb)")
    parser.add_argument("--local-memory-mb", type=positive_integer,
                        help="Job-scoped Bazel local action memory budget (requires --jobs)")
    parser.add_argument("--from-cgroup", action="store_true",
                        help="Select the supported worker resource profile from cgroup v2 limits")
    args = parser.parse_args(argv)
    if args.from_cgroup and (args.jobs is not None or args.local_memory_mb is not None):
        parser.error("--from-cgroup cannot be combined with --jobs or --local-memory-mb")
    if (args.jobs is None) != (args.local_memory_mb is None):
        parser.error("--jobs and --local-memory-mb must be supplied together")
    resource_limits = None if args.jobs is None else {
        "jobs": args.jobs, "local_memory_mb": args.local_memory_mb}
    try:
        profile = read_resource_profile() if args.from_cgroup else None
        if profile is not None:
            resource_limits = {key: profile[key] for key in ("jobs", "local_memory_mb")}
        receipt = configure(args.rc, resource_limits)
        if profile is not None:
            receipt["resource_profile"] = profile
    except ConfigurationFailure as error:
        receipt = {"status": "failure", "error_class": error.error_class}
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except OSError:
        receipt = {"status": "failure", "error_class": "receipt_write_failed"}
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "configured" else 1


if __name__ == "__main__":
    raise SystemExit(main())
