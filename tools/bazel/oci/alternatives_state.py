"""Initialize reviewed alternatives with Debian's real update-alternatives.

Only declared alternatives are registered in a temporary filesystem. Package
scripts are inspected, never executed. Upstream tar.bzl packages the resulting
metadata and links; placeholder executables never enter the output layer.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import tarfile
import tempfile

from tools.bazel.oci.oci_inventory import resolve_path
from tools.bazel.oci.oci_layout import validate_layout


ADMIN = "var/lib/dpkg/alternatives/"
LINKS = "etc/alternatives/"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and
            not any(ord(character) < 32 for character in value), "unsafe alternatives path: " + value)
    return str(path)


def image_inventory(layers):
    files, state = {}, {}
    for layer in layers:
        additions, controls, removed, opaque = {}, {}, [], []
        with tarfile.open(layer) as archive:
            for member in archive:
                name = safe(member.name)
                path = PurePosixPath(name)
                if path.name == ".wh..wh..opq":
                    opaque.append(str(path.parent))
                    continue
                if path.name.startswith(".wh."):
                    removed.append(str(path.parent / path.name[4:]))
                    continue
                additions[name] = {"kind": "directory" if member.isdir() else "symlink" if member.issym()
                                   else "hardlink" if member.islnk() else "file", "linkname": member.linkname}
                if name.startswith(ADMIN) and member.isfile():
                    require(member.size < 1024 * 1024, "oversized alternatives record")
                    controls[name] = archive.extractfile(member).read()
        for name in list(files):
            if any(name == item or name.startswith(item + "/") for item in removed) or any(
                    parent == "." or name.startswith(parent + "/") for parent in opaque):
                files.pop(name, None)
                state.pop(name, None)
        for name, item in additions.items():
            if item["kind"] != "directory":
                for descendant in list(files):
                    if descendant.startswith(name + "/"):
                        files.pop(descendant)
                        state.pop(descendant, None)
        files.update(additions)
        state.update(controls)
    return files, state


def tool_command(root):
    native = {
        "x86_64": ("x86_64-linux-gnu", "ld-linux-x86-64.so.2"),
        "aarch64": ("aarch64-linux-gnu", "ld-linux-aarch64.so.1"),
    }
    require(platform.machine() in native, "unsupported alternatives execution architecture")
    triplet, loader_name = native[platform.machine()]
    library = Path(root).resolve() / "usr/lib" / triplet
    loader = library / loader_name
    executable = Path(root).resolve() / "usr/bin/update-alternatives"
    require(loader.is_file() and executable.is_file() and (library / "libc.so.6").is_file(),
            "declared native dpkg/libc tool payloads are incomplete")
    return [str(loader), "--library-path", str(library), str(executable)]


def invoke(command, root, arguments):
    result = subprocess.run([*command, "--root", str(root), "--log", str(root / "alternatives.log"),
                             *arguments], capture_output=True, text=True, check=False,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"})
    require(result.returncode == 0, "update-alternatives failed: " + result.stderr)
    return result.stdout


def query_paths(query):
    """Read upstream's documented --query output, never its database format."""
    targets, links, alternatives = set(), {}, []
    header = True
    for line in query.splitlines():
        if line.startswith("Alternative: "):
            header = False
            targets.add(line.removeprefix("Alternative: "))
            alternatives.append(line.removeprefix("Alternative: "))
        elif line.startswith("Link: "):
            links[None] = line.removeprefix("Link: ")
        elif line.startswith(" "):
            name, path = line.split()
            if header:
                links[name] = path
            else:
                targets.add(path)
    return targets, links, alternatives


def build(base, inputs, command, output, mtree, receipt):
    require(inputs.get("architecture") in {"amd64", "arm64"}, "unsupported image architecture")
    layout = validate_layout(Path(base), "linux/" + inputs["architecture"])
    files, inherited = image_inventory(layout.layers)
    groups, package_records = {}, []
    for entry in inputs["packages"]:
        payload_files, _ = image_inventory([entry["payload"]])
        files.update(payload_files)
        with tarfile.open(entry["control"]) as controls:
            members = {safe(member.name): member for member in controls if member.isfile()}
            control = controls.extractfile(members["control"]).read().decode()
            require(re.search(r"^Package: " + re.escape(entry["package"]) + r"$", control, re.M),
                    "alternatives control belongs to another package")
            postinst = " ".join(controls.extractfile(members["postinst"]).read().decode().replace("\\\n", " ").split())
            prerm = " ".join(controls.extractfile(members["prerm"]).read().decode().replace("\\\n", " ").split())
        for group in entry["groups"]:
            require(set(group) == {"name", "link", "path", "priority"}, "unexpected alternatives declaration")
            name = group["name"]
            require(re.fullmatch(r"[a-zA-Z0-9+_.-]+", name) and name not in groups,
                    "invalid or duplicate alternatives group")
            require(isinstance(group["priority"], int), "alternative priority must be an integer")
            for key in ("link", "path"):
                require(group[key].startswith("/"), "alternative paths must be absolute")
                require(not any(character.isspace() for character in group[key]), "whitespace in alternatives declaration")
                safe(group[key][1:])
            expected = "--install {link} {name} {path} {priority}".format(**group)
            require(expected in postinst and "--remove {name} {path}".format(**group) in prerm,
                    "alternatives declaration differs from reviewed package scripts: " + name)
            target = resolve_path(group["path"][1:], payload_files)
            require(payload_files.get(target, {}).get("kind") == "file", "alternative provider missing from package payload")
            groups[name] = group
        package_records.append({"package": entry["package"], "control_sha256": sha(entry["control"]),
                                "payload_sha256": sha(entry["payload"]), "groups": entry["groups"]})
    require(groups, "no alternatives declared")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    require(not any(output.iterdir()), "alternatives output directory must be empty")
    generated, before_links = {}, {}
    with tempfile.TemporaryDirectory(prefix="sonic-alternatives-") as temporary:
        root = Path(temporary)
        (root / ADMIN).mkdir(parents=True)
        (root / LINKS).mkdir(parents=True)
        for name, data in inherited.items():
            require("/" not in name[len(ADMIN):], "nested alternatives record")
            (root / name).write_bytes(data)

        def placeholder(absolute):
            name = safe(absolute.removeprefix("/"))
            target = resolve_path(name, files)
            if files.get(target, {}).get("kind") != "file":
                return
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_symlink() and not path.exists():
                path.touch()

        def inherited_link(name):
            item = files.get(name, {})
            if item.get("kind") != "symlink":
                return
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(item["linkname"])
            before_links[name] = item["linkname"]

        # Even --query discards providers missing from its filesystem. Empty
        # placeholders preserve all actual image files while upstream reads its
        # own records; no database-format parser or executable payload is needed.
        # Preserve merged-usr aliases too: an inherited provider may be named
        # /bin/more while its archive stores usr/bin/more. All scratch aliases
        # are relative and stay inside the temporary root.
        for name, item in sorted(files.items()):
            if item["kind"] != "symlink":
                continue
            target = resolve_path(name, files)
            if not target or not (files.get(target, {}).get("kind") == "directory" or
                                  any(path.startswith(target + "/") for path in files)):
                continue
            location, destination = root / name, root / target
            destination.mkdir(parents=True, exist_ok=True)
            location.parent.mkdir(parents=True, exist_ok=True)
            require(not location.exists(), "conflicting directory alias in alternatives input")
            location.symlink_to(os.path.relpath(destination, location.parent))
        for name, item in files.items():
            if item["kind"] in {"file", "hardlink"}:
                placeholder("/" + name)
        queries = {}
        for name, group in groups.items():
            known_links = {name: group["link"]}
            if ADMIN + name in inherited:
                query = invoke(command, root, ["--query", name])
                targets, links, _ = query_paths(query)
                known_links.update({key or name: path for key, path in links.items()})
                for target in targets:
                    placeholder(target)
            for link_name, path in known_links.items():
                inherited_link(LINKS + link_name)
                inherited_link(safe(path[1:]))
            placeholder(group["path"])
            (root / safe(group["link"][1:])).parent.mkdir(parents=True, exist_ok=True)
            invoke(command, root, ["--install", group["link"], name, group["path"], str(group["priority"])])
            master = root / safe(group["link"][1:])
            require(master.is_symlink() and os.readlink(master) == "/" + LINKS + name,
                    "existing command prevents the declared alternative link: " + group["link"])
            require((root / LINKS / name).is_symlink(), "upstream tool did not create the selected alternative link")
            query = invoke(command, root, ["--query", name])
            queries[name] = query
            _, links, providers = query_paths(query)
            require(group["path"] in providers, "upstream tool did not register the declared provider")
            known_links.update({key or name: path for key, path in links.items()})
            generated[ADMIN + name] = ("file", (root / ADMIN / name).read_bytes())
            for link_name, path in known_links.items():
                for candidate in (LINKS + link_name, safe(path[1:])):
                    location = root / candidate
                    if location.is_symlink():
                        generated[candidate] = ("link", os.readlink(location))
                    elif candidate in before_links:
                        pure = PurePosixPath(candidate)
                        generated[str(pure.parent / (".wh." + pure.name))] = ("file", b"")
    lines = ["#mtree", "# AUTO-GENERATED. DO NOT EDIT MANUALLY. tools/bazel/oci/alternatives_state.py"]
    for path, (kind, value) in sorted(generated.items()):
        safe(path)
        if kind == "link":
            safe(value.removeprefix("/"))
            lines.append("./" + path + " type=link uid=0 gid=0 mode=0777 time=0 link=" + value)
        else:
            destination = output / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
            content = str(destination)
            escaped = "".join("\\%03o" % ord(c) if c.isspace() or c in "\\#=" else c for c in content)
            lines.append("./" + path + " type=file uid=0 gid=0 mode=0644 time=0 content=" + escaped)
    Path(mtree).write_text("\n".join(lines) + "\n")
    result = {"_generated": {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
                             "generator": "tools/bazel/oci/alternatives_state.py"},
              "schema": 1, "base_manifest_digest": layout.descriptor["digest"],
              "packages": package_records, "queries": queries,
              "maintainer_scripts_executed": False,
              "tool_sha256": sha(command[-1]),
              "files": {name: {"sha256": hashlib.sha256(value).hexdigest()} if kind == "file"
                        else {"link": value} for name, (kind, value) in sorted(generated.items())}}
    Path(receipt).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "inputs", "tool-root", "out-dir", "mtree", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    build(args.base, json.loads(args.inputs.read_text()), tool_command(args.tool_root),
          args.out_dir, args.mtree, args.receipt)


if __name__ == "__main__":
    main()
