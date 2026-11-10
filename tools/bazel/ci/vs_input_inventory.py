#!/usr/bin/env python3
"""Record configured Make inputs without building or acquiring any input."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import urlsplit


MAX_ROWS_BYTES = 4 * 1024 * 1024
MAX_ROWS = 30000
NAME = re.compile(r"[A-Za-z0-9_.+/@%=:~-]+\Z")
CATEGORIES = {
    "SONIC_COPY_DEBS": "existing-local-deb",
    "SONIC_ONLINE_DEBS": "declared-download-deb",
    "SONIC_MAKE_DEBS": "sonic-produced-deb",
    "SONIC_DPKG_DEBS": "sonic-produced-deb",
    "SONIC_DERIVED_DEBS": "sonic-produced-deb",
    "SONIC_EXTRA_DEBS": "sonic-produced-deb",
    "SONIC_BAZEL_KERNEL_DEBS": "original-kernel-deb",
    "SONIC_PYTHON_STDEB_DEBS": "sonic-produced-deb",
    "SONIC_PYTHON_WHEELS": "source-produced-wheel",
    "SONIC_COPY_FILES": "existing-local-file",
    "SONIC_ONLINE_FILES": "declared-download-file",
    "SONIC_MAKE_FILES": "sonic-produced-file",
    "SONIC_RFS_TARGETS": "filesystem-assembly-output",
}
KERNEL_SETTINGS = "CONFIGURED_ARCH CONFIGURED_PLATFORM BLDENV CROSS_BUILD_ENVIRON MULTIARCH_QEMU_ENVIRON KERNEL_VERSION KERNEL_ABISUFFIX KERNEL_SUBVERSION KERNEL_FEATURESET KVERSION ADDITIONAL_BUILD_PROFILES SECURE_UPGRADE_MODE SECURE_UPGRADE_KERNEL_CAFILE INCLUDE_EXTERNAL_PATCHES EXTERNAL_KERNEL_PATCH_URL EXTERNAL_KERNEL_PATCH_LOC".split()
DEPENDENCY_FIELDS = (
    "DEPENDS", "RDEPENDS", "DEBS_DEPENDS", "WHEEL_DEPENDS",
    "PYTHON_DEBS", "PYTHON_WHEELS", "FILES", "AFTER", "MAIN_DEB", "DEPENDENT_RFS",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def words(value: str) -> list[str]:
    result = value.split()
    require(all(NAME.fullmatch(word) for word in result), "Unsafe inventory token")
    return result


def parse_rows(raw: bytes) -> dict[tuple[str, str], str]:
    require(len(raw) <= MAX_ROWS_BYTES, "Inventory exceeds byte bound")
    lines = raw.decode("utf-8", "strict").splitlines()
    require(len(lines) <= MAX_ROWS, "Inventory exceeds row bound")
    rows = {}
    for line in lines:
        parts = line.split("\t")
        require(len(parts) == 3, "Invalid inventory row")
        kind, key, value = parts
        require(kind in {"format", "profile", "root", "include", "category", "node", "kernel_config"}, "Unknown row kind")
        require((kind, key) not in rows, "Duplicate inventory row")
        require(all(ord(c) >= 32 for c in key + value), "Control byte in inventory")
        rows[kind, key] = value
    require(rows.get(("format", "schema")) == "1", "Wrong inventory schema")
    for key, expected in {"architecture": "amd64", "platform": "vs", "distribution": "trixie"}.items():
        require(rows.get(("profile", key)) == expected, "Unexpected VS profile: " + key)
    require(all(("category", key) in rows for key in CATEGORIES), "Missing package categories")
    require({key for kind, key in rows if kind == "kernel_config"} == set(KERNEL_SETTINGS), "Missing or extra kernel settings")
    require(("root", "make_prerequisites") in rows, "Missing actual installer prerequisites")
    return rows


def safe_file(workspace: Path, relative: str) -> Path | None:
    path = PurePosixPath(relative)
    require(not path.is_absolute(), "Path escapes workspace")
    current = workspace
    for index, part in enumerate(path.parts):
        if part == "..":
            require(current != workspace, "Path escapes workspace")
            current = current.parent
            continue
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return None
        require(not stat.S_ISLNK(mode), "Symlink input is not admitted")
        if index < len(path.parts) - 1:
            require(stat.S_ISDIR(mode), "Inventory path component is not a directory")
    require(stat.S_ISREG(current.stat().st_mode), "Inventory input is not a regular file")
    return current


def inventory(rows: dict[tuple[str, str], str], workspace: Path) -> dict:
    categories: dict[str, list[str]] = {}
    for category in CATEGORIES:
        for node in words(rows["category", category]):
            categories.setdefault(node, []).append(category)
    nodes: dict[str, dict] = {}
    for (kind, key), value in rows.items():
        if kind != "node":
            continue
        node, field = key.split("|", 1)
        require(NAME.fullmatch(node) is not None, "Invalid package node")
        nodes.setdefault(node, {})[field] = value

    roots = {key: words(value) for (kind, key), value in rows.items() if kind == "root"}
    selected = set()
    unknown = set()
    edges = []
    queue = [token for values in roots.values() for token in values]
    while queue:
        token = queue.pop()
        node = token if token in categories else PurePosixPath(token).name
        if node.endswith("-install") and node[:-8] in categories:
            node = node[:-8]
        if node not in categories:
            unknown.add(token)
            continue
        if node in selected:
            continue
        selected.add(node)
        for field in DEPENDENCY_FIELDS:
            for dependency in words(nodes.get(node, {}).get(field, "")):
                edges.append({"from": node, "field": field, "to": dependency})
                queue.append(dependency)

    entries = []
    for node in sorted(selected):
        data = nodes.get(node, {})
        url = data.get("URL", "")
        # Do not export credentials, query strings or unreviewed download commands.
        origins = []
        for token in url.split():
            parsed = urlsplit(token)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                origins.append(parsed.scheme + "://" + parsed.hostname)
        entries.append({
            "name": node, "categories": categories[node],
            "kinds": sorted({CATEGORIES[c] for c in categories[node]}),
            "declared_url_sha256": hashlib.sha256(url.encode()).hexdigest() if url else None,
            "declared_origins": sorted(set(origins)),
            "origin_admission": "requires-existing-approved-transport" if url else "not-a-download-declaration",
            "availability": "not-inspected",
        })

    includes = []
    for relative in words(rows.get(("include", "files"), "")):
        path = safe_file(workspace, relative)
        require(path is not None, "A parsed Make source is absent")
        includes.append({"path": relative, "normalized_path": path.relative_to(workspace).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {
        "schema": 1,
        "status": "CONFIGURED_INPUT_INVENTORY_NOT_ACCEPTANCE",
        "profile": {key: value for (kind, key), value in rows.items() if kind == "profile"},
        "kernel_config": {key: {"value": value if not key.endswith("_URL") or not value else None, "value_sha256": hashlib.sha256(value.encode()).hexdigest()} for (kind, key), value in rows.items() if kind == "kernel_config"},
        "roots": roots, "selected_nodes": entries, "dependency_edges": edges,
        "unclassified_tokens": sorted(unknown), "parsed_make_sources": includes,
        "limits": [
            "No package, archive, wheel or rootfs was built or downloaded.",
            "Names and declared dependency edges do not establish artifact availability or compatibility.",
            "Runtime package-manager resolution and bootstrap inputs require separate authenticated evidence.",
            "A rootfs may be assembled from admitted existing packages; a prebuilt rootfs is not required by this inventory.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.workspace.is_symlink(), "Workspace may not be a symlink")
    root = args.workspace.resolve(strict=True)
    source = safe_file(root, args.rows.absolute().relative_to(root).as_posix())
    require(source is not None, "Rows file is absent")
    raw = source.read_bytes()
    document = inventory(parse_rows(raw), root)
    require(re.fullmatch(r"[0-9a-f]{40}", args.revision) is not None, "Invalid source revision")
    document["source_revision"] = args.revision
    document["original_rows_sha256"] = hashlib.sha256(raw).hexdigest()
    # Exclusive output, leaving all original inputs and existing files intact.
    require(args.output.parent.resolve(strict=True) == root / "target", "Unexpected inventory output directory")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
