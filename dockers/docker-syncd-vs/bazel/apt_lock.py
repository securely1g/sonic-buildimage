#!/usr/bin/env python3
"""Read the checked syncd APT content lock and maintain its Bazel label export."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re

OWNER = Path(__file__).absolute().parent
SET_NAMES = {"runtime": "syncd_vs_debian", "debug": "syncd_vs_debug_debian"}
BAZEL_VERSION = "8.5.1"
DISTROLESS_VERSION = "0.9.4-sonic.1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def package_identity(key, package, *, content_hashes):
    require(":amd64=" in key, "foreign architecture in syncd APT closure: " + key)
    require(isinstance(package, dict) and package.get("architecture") in ("amd64", "all") and
            package.get("name") and package.get("version"), "invalid locked APT package identity: " + key)
    require(key.endswith("=" + package["version"]), "locked APT package version differs: " + key)
    filename = PurePosixPath(package.get("filename", ""))
    require(str(filename) != "." and not filename.is_absolute() and ".." not in filename.parts,
            "invalid locked APT package filename: " + key)
    require(re.fullmatch(r"[0-9a-f]{64}", package.get("sha256", "")) is not None and
            isinstance(package.get("size"), int) and package["size"] > 0,
            "missing locked APT source integrity: " + key)
    require(isinstance(package.get("depends_on"), list) and all(isinstance(value, str) for value in package["depends_on"]),
            "invalid locked APT dependency list: " + key)
    if content_hashes:
        for kind in ("payload", "control"):
            require(re.fullmatch(r"[0-9a-f]{64}", package.get(kind + "_sha256", "")) is not None and
                    isinstance(package.get(kind + "_size"), int) and package[kind + "_size"] > 0,
                    "missing locked APT " + kind + " integrity: " + key)


def walk(packages, roots, *, content_hashes):
    require(isinstance(roots, list) and roots and len(roots) == len(set(roots)), "invalid syncd APT roots")
    pending = list(roots)
    result = {}
    while pending:
        key = pending.pop()
        if key in result:
            continue
        package = packages.get(key)
        package_identity(key, package, content_hashes=content_hashes)
        result[key] = package
        pending.extend(package["depends_on"])
    identities = {}
    fields = ["version", "architecture", "sha256", "size"]
    if content_hashes:
        fields += ["payload_sha256", "payload_size", "control_sha256", "control_size"]
    for package in result.values():
        identity = tuple(package[field] for field in fields)
        previous = identities.setdefault(package["name"], identity)
        require(previous == identity, "syncd APT closure selects different versions or content for one package")
    return {key: result[key] for key in sorted(result)}


def resolved_closure(lock, variant):
    require(lock.get("version") == 2, "unsupported resolved APT lock version")
    sets = lock.get("dependency_sets", {}).get(SET_NAMES[variant], {}).get("sets", {})
    require(set(sets) == {"amd64"} and sets["amd64"], "syncd APT resolution must contain one nonempty AMD64 set")
    roots = sorted(key + "=" + version for key, version in sets["amd64"].items())
    return roots, walk(lock.get("packages", {}), roots, content_hashes=False)


def closure(lock, variant):
    require(lock.get("schema") == 1 and lock.get("bazel_version") == BAZEL_VERSION and
            lock.get("rules_distroless_version") == DISTROLESS_VERSION, "unsupported syncd APT content lock")
    require(set(lock.get("roots", {})) == set(SET_NAMES), "syncd APT content lock needs runtime and debug roots")
    result = walk(lock.get("packages", {}), lock["roots"][variant], content_hashes=True)
    sources = lock.get("sources", {})
    for key, package in result.items():
        source = sources.get(package.get("suite"), {})
        uris = source.get("uris", [])
        require(uris and all(re.fullmatch(r"https://snapshot\.debian\.org/archive/(?:debian|debian-security)/[0-9]{8}T[0-9]{6}Z", uri)
                             for uri in uris), "locked APT package lacks a dated Debian source: " + key)
    return result


def render(lock):
    groups = {variant: closure(lock, variant) for variant in SET_NAMES}
    require(set(lock["packages"]) == set().union(*(set(group) for group in groups.values())),
            "syncd APT content lock contains unused packages")
    result = "# Generated from apt.lock.json by apt_lock.py. Do not edit this label list.\n"
    for variant, group in groups.items():
        result += "\nSYNCD_" + variant.upper() + "_APT_KEYS = [\n"
        result += "".join("    " + json.dumps(key) + ",\n" for key in group)
        result += "]\n"
    return result


def sanitize(key):
    # Exact rules_distroless 0.9.4 util.sanitize behavior. Refresh preparation
    # verifies each resulting repository's name, URL, and SHA against Bazel's
    # current generated repository specification before building any data tar.
    return key.removeprefix("/").replace("+", "-").replace(":", "-").replace("~", "_").replace("/", "_").replace("=", "_")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=OWNER / "apt.lock.json")
    parser.add_argument("--output", type=Path, default=OWNER / "apt_packages.bzl")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        expected = render(json.loads(args.lock.read_bytes()))
        if args.check:
            require(args.output.is_file() and args.output.read_text() == expected,
                    "syncd APT label export differs from the checked content lock; run apt_lock.py")
        else:
            args.output.write_text(expected)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(1, "syncd APT lock validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
