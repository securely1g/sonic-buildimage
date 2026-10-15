"""Write dpkg ownership and control metadata for declared OCI package payloads.

Archive assembly belongs to tar.bzl. This program only reads package archives
and writes metadata files and an mtree specification. It does not run package
installation scripts: the receipt states that boundary explicitly.
"""

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

from debian.deb822 import Deb822

from tools.bazel.oci.normalize_layer import DIRECTORY_ALIASES, normalized_path
from tools.bazel.oci.oci_layout import validate_layout


INFO = "var/lib/dpkg/info/"
TRIGGERS = "var/lib/dpkg/triggers/"
CONTROL_FILES = {"preinst", "postinst", "prerm", "postrm", "config", "templates",
                 "triggers", "shlibs", "symbols"}
SCRIPT_FILES = {"preinst", "postinst", "prerm", "postrm", "config"}
PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*\Z")


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data, usedforsecurity=algorithm != "md5").hexdigest()


def md5_stream(stream):
    # MD5 is the dpkg compatibility format, not an integrity/security decision.
    return hashlib.file_digest(stream, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and
            not any(ord(character) < 32 for character in value), "unsafe package path: " + value)
    return str(path)


def paragraphs(data):
    return [dict(item) for item in Deb822.iter_paragraphs(data.splitlines())]


def records(data):
    result = {}
    for fields in paragraphs(data.decode() if isinstance(data, bytes) else data):
        name = fields["Package"]
        require(PACKAGE.fullmatch(name), "invalid package identity: " + name)
        require(name not in result, "multiple architectures of one package need explicit support: " + name)
        result[name] = fields
    return result


def info_name(fields):
    name = fields["Package"]
    if fields.get("Multi-Arch") == "same":
        name += ":" + fields["Architecture"]
    return name


def read_base(layers):
    """Read metadata and member types, respecting OCI deletion semantics."""
    metadata, members = {}, {}
    for layer in layers:
        additions, files, removed, opaque = {}, {}, set(), set()
        with tarfile.open(layer) as archive:
            for member in archive:
                name = safe_path(member.name)
                path = PurePosixPath(name)
                if path.name == ".wh..wh..opq":
                    opaque.add(str(path.parent))
                elif path.name.startswith(".wh."):
                    removed.add(str(path.parent / path.name[4:]))
                else:
                    additions[name] = "directory" if member.isdir() else "file"
                    if name.startswith((INFO, TRIGGERS)) or name == "var/lib/dpkg/status":
                        require(member.isfile() or member.isdir(), "unexpected dpkg metadata link: " + name)
                        if member.isfile():
                            require(member.size <= 32 * 1024 * 1024, "oversized dpkg metadata: " + name)
                            files[name] = (archive.extractfile(member).read(), member.mode)
        for name in list(members):
            if any(name == item or name.startswith(item + "/") for item in removed) or any(
                    parent == "." or name.startswith(parent + "/") for parent in opaque):
                members.pop(name, None)
                metadata.pop(name, None)
        members.update(additions)
        metadata.update(files)
    require("var/lib/dpkg/status" in metadata, "base lacks dpkg status")
    require(not metadata.get(TRIGGERS + "Unincorp", (b"", 0))[0].strip(),
            "base has pending unincorporated triggers")
    return metadata, members


def read_control(path):
    result = {}
    with tarfile.open(path) as archive:
        for member in archive:
            name = safe_path(member.name)
            if name == "." and member.isdir():
                continue
            require("/" not in name and member.isfile(), "invalid Debian control member: " + name)
            require(name not in result and member.size <= 32 * 1024 * 1024,
                    "duplicate or oversized Debian control member: " + name)
            result[name] = (archive.extractfile(member).read(), member.mode)
    require("control" in result, "package archive lacks control fields")
    require(not set(result).difference(CONTROL_FILES | {"control", "conffiles", "md5sums"}),
            "unreviewed package control member: " + ", ".join(sorted(set(result).difference(CONTROL_FILES | {"control", "conffiles", "md5sums"}))))
    return result


def source_control(entry, architecture):
    """Read reviewed text controls, verifying original bytes before substitution."""
    if "control" in entry:
        return read_control(entry["control"]), {"control_sha256": sha(entry["control"])}
    manifest_path = Path(entry["control_manifest"])
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("schema") == 1, "unsupported source control manifest")
    record = manifest["packages"][entry["package"]]
    result, substitutions = {}, {}
    for name, item in record["control_files"].items():
        require(name == "control" or name == "conffiles" or name in CONTROL_FILES,
                "unexpected source control file: " + name)
        path = manifest_path.parent / safe_path(item["path"])
        data = path.read_bytes()
        require(digest(data) == item["sha256"], "source control file differs from reviewed bytes: " + str(path))
        for replacement in record.get("architecture_substitutions", {}).get(name, []):
            before, after = replacement["from"], replacement["to"].format(architecture=architecture)
            require(before and before.encode() in data, "source control substitution has no match: " + name)
            data = data.replace(before.encode(), after.encode())
            substitutions[name] = {"original_sha256": item["sha256"], "installed_sha256": digest(data)}
        result[name] = (data, item["mode"])
    require("control" in result, "source control manifest lacks a control paragraph")
    return result, {"manifest_sha256": sha(manifest_path), "reference": record.get("reference"),
                    "control_version": record["version"],
                    "source_version": record.get("source_version", record["version"]),
                    "control_field_substitutions": record.get("control_field_substitutions", {}),
                    "architecture_substitutions": substitutions}


def payload_files(path, *, excludes=(), includes=()):
    """Read the installed payload without extracting it onto the build host."""
    paths, sums, types = set(), {}, {}
    with tarfile.open(path) as archive:
        for member in archive:
            name = safe_path(member.name)
            require(not PurePosixPath(name).name.startswith(".wh."), "package payload contains OCI whiteout")
            require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                    "unsupported package member: " + name)
            excluded = any(fnmatch.fnmatchcase(name, pattern.lstrip("/")) for pattern in excludes)
            included = any(fnmatch.fnmatchcase(name, pattern.lstrip("/")) for pattern in includes)
            if excluded and not included:
                continue
            paths.add(name)
            types[name] = "directory" if member.isdir() else "file"
            sums.pop(name, None)
            if member.isfile() or member.islnk():
                with archive.extractfile(member) as stream:
                    sums[name] = md5_stream(stream)
    for name in tuple(paths):
        paths.update(str(parent) for parent in PurePosixPath(name).parents)
    paths.add(".")
    return paths, sums, types


def installed_layers(layers):
    """Resolve the ordered, already-normalized payload layers including whiteouts."""
    entries = {}
    for layer in layers:
        additions, removed, opaque = {}, set(), set()
        with tarfile.open(layer) as archive:
            for member in archive:
                name = safe_path(member.name)
                path = PurePosixPath(name)
                if path.name == ".wh..wh..opq":
                    opaque.add(str(path.parent))
                elif path.name.startswith(".wh."):
                    removed.add(str(path.parent / path.name[4:]))
                else:
                    require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                            "unsupported installed payload member: " + name)
                    value = {"kind": "directory" if member.isdir() else "symlink" if member.issym() else "file"}
                    if member.isfile() or member.islnk():
                        with archive.extractfile(member) as stream:
                            value["md5"] = md5_stream(stream)
                        value["size"] = member.size
                    additions[name] = value
        for name in list(entries):
            if any(name == item or name.startswith(item + "/") for item in removed) or any(
                    parent == "." or name.startswith(parent + "/") for parent in opaque):
                entries.pop(name, None)
        parent_paths = {str(parent) for name in entries for parent in PurePosixPath(name).parents}
        for name, value in additions.items():
            # Most files are new leaves. Scanning every existing entry for
            # every leaf makes a development-package layer quadratic in size.
            # Only a path with children can hide earlier descendants.
            if value["kind"] != "directory" and name in parent_paths:
                for descendant in list(entries):
                    if descendant.startswith(name + "/"):
                        entries.pop(descendant)
            entries[name] = value
            parent_paths.update(str(parent) for parent in PurePosixPath(name).parents)
    for name in tuple(entries):
        for parent in PurePosixPath(name).parents:
            key = str(parent)
            require(entries.get(key, {"kind": "directory"})["kind"] == "directory",
                    "installed payload crosses a non-directory: " + name)
            entries.setdefault(key, {"kind": "directory"})
    return entries


def parse_conffiles(data):
    paths, removed = [], []
    for line in data.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        flag = line.startswith("remove-on-upgrade ")
        if flag:
            line = line[len("remove-on-upgrade "):].strip()
        require(line.startswith("/") and not any(character.isspace() for character in line),
                "conffile flags or whitespace require explicit lifecycle support: " + line)
        safe_path(line[1:])
        require(line not in paths + removed, "duplicate conffile declaration: " + line)
        (removed if flag else paths).append(line)
    return paths, removed


def register_triggers(metadata, packages):
    """Preserve and update dpkg's file/explicit trigger interest registrations.

    This mirrors the documented dpkg triggers database format, not trigger
    processing. Activation directives are retained for later dpkg transactions;
    installation-time side effects remain explicitly outside this generator.
    """
    owners = {item["info_name"] for item in packages}
    old_owners = owners | {owner for item in packages for owner in item["old_info_names"]}
    file_interests = []
    for line in metadata.get(TRIGGERS + "File", (b"", 0))[0].decode().splitlines():
        fields = line.split()
        require(len(fields) == 2, "invalid inherited file trigger registration")
        if fields[1].removesuffix("/noawait") not in old_owners:
            file_interests.append(tuple(fields))
    explicit = {}
    for name, (data, _) in metadata.items():
        if name.startswith(TRIGGERS) and name[len(TRIGGERS):] not in {"File", "Lock", "Unincorp"}:
            trigger = name[len(TRIGGERS):]
            require("/" not in trigger, "unexpected nested trigger database file")
            explicit[trigger] = {line for line in data.decode().splitlines()
                                 if line.removesuffix("/noawait") not in old_owners}
    for item in packages:
        activated = []
        for line in item["controls"].get("triggers", (b"", 0))[0].decode().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            fields = line.split()
            require(len(fields) == 2, "invalid trigger directive for " + item["package"])
            operation, trigger = fields
            require(operation in {"interest", "interest-await", "interest-noawait", "activate",
                                  "activate-await", "activate-noawait"}, "unsupported trigger directive: " + operation)
            require(trigger and not any(ord(value) < 33 for value in trigger), "invalid trigger name")
            if operation.startswith("activate"):
                activated.append(trigger)
                continue
            owner = item["info_name"] + ("/noawait" if operation.endswith("-noawait") else "")
            if trigger.startswith("/"):
                safe_path(trigger[1:])
                file_interests.append((trigger, owner))
            else:
                require("/" not in trigger and trigger not in {"File", "Lock", "Unincorp", ".", ".."},
                        "invalid explicit trigger name")
                explicit.setdefault(trigger, set()).add(owner)
        item["installation_trigger_activations"] = sorted(set(activated))
    output = {TRIGGERS + "File": ("".join(path + " " + owner + "\n" for path, owner in sorted(set(file_interests))).encode(), 0o644)}
    if TRIGGERS + "Unincorp" not in metadata:
        # Its absence means "old dpkg without trigger support" to dpkg. This
        # generated interest database is already initialized and has no queue.
        output[TRIGGERS + "Unincorp"] = (b"", 0o644)
    for name, owners in sorted(explicit.items()):
        output[TRIGGERS + name] = ("".join(owner + "\n" for owner in sorted(owners)).encode(), 0o644)
    return output


def mtree_escape(value):
    return "".join("\\%03o" % ord(character) if character.isspace() or character in "\\#=" else character
                   for character in str(value))


def build(base, status_path, apt_receipt_path, inputs, output_dir, mtree_path, receipt_path):
    architecture = inputs["architecture"]
    require(architecture in {"amd64", "arm64"}, "unsupported dpkg state architecture")
    layout = validate_layout(Path(base), "linux/" + architecture)
    metadata, base_members = read_base(layout.layers)
    old_records = records(metadata["var/lib/dpkg/status"][0])
    final = records(Path(status_path).read_bytes())
    for name, fields in old_records.items():
        if name not in final:
            require(fields.get("Status", "").endswith("config-files"),
                    "final status silently drops an inherited package: " + name)
            final[name] = fields
    selection = json.loads(Path(apt_receipt_path).read_text())
    require(selection["base_manifest_digest"] == layout.descriptor["digest"], "APT selection uses another base")
    selected = {entry["package"]: entry for entry in selection["selected"]}
    require(len(selected) == len(selection["selected"]), "APT selection repeats a package")
    candidates = []
    seen_apt = set()
    for entry in inputs.get("apt", []):
        controls = read_control(entry["control"])
        control_fields = paragraphs(controls["control"][0].decode())
        require(len(control_fields) == 1, "ambiguous APT control paragraph")
        name = control_fields[0]["Package"]
        if name not in selected:
            continue
        current = selected[name]
        require(name not in seen_apt, "duplicate selected APT package input: " + name)
        require(sha(entry["control"]) == current["control_sha256"] and
                sha(entry["payload"]) == current["payload_sha256"], "selected APT bytes changed: " + name)
        seen_apt.add(name)
        candidates.append((dict(entry, package=name), controls, "apt", {}))
    require(seen_apt == set(selected), "missing selected APT package inputs")
    for kind in ("sources", "imported"):
        for entry in inputs.get(kind, []):
            entry = dict(entry)
            if kind == "imported" and "package" not in entry:
                entry["package"] = paragraphs(read_control(entry["control"])["control"][0].decode())[0]["Package"]
            if "control" not in entry and "control_manifest" not in entry:
                entry["control_manifest"] = inputs["source_controls"]
            controls, provenance = source_control(entry, architecture)
            candidates.append((entry, controls, kind, provenance))
    # The reviewed source package replaces the same selected APT package. Its
    # final ownership includes retained APT files as well as the source payload.
    # This preserves Debian copyright/config files that the source TAR omits.
    combined = {}
    for entry, controls, kind, provenance in candidates:
        name = entry["package"]
        previous = combined.get(name)
        if previous:
            require(previous[2] == "apt" and kind == "sources",
                    "ambiguous package metadata ownership: " + name)
            entry = dict(entry, lower_payloads=[previous[0]["payload"]])
            provenance = dict(provenance, replaced_selected_apt={
                "payload_sha256": sha(previous[0]["payload"]),
                "control_sha256": sha(previous[0]["control"]),
                "version": paragraphs(previous[1]["control"][0].decode())[0]["Version"],
            })
        combined[name] = (entry, controls, kind, provenance)
    candidates = list(combined.values())
    active_conffile_owners = {}
    for entry, controls, _, _ in candidates:
        for path in parse_conffiles(controls.get("conffiles", (b"", 0))[0])[0]:
            active_conffile_owners.setdefault(path, set()).add(entry["package"])
    extra_owned_paths = inputs.get("extra_owned_paths", {})
    require(not set(extra_owned_paths).difference(combined), "extra ownership names a package without payload controls")
    actual_entries = None
    if inputs.get("payload_layers"):
        actual_entries = installed_layers(inputs["payload_layers"])
    package_data, generated = [], {}
    names = set()
    for entry, controls, kind, provenance in candidates:
        name = entry["package"]
        require(PACKAGE.fullmatch(name) and name not in names, "ambiguous package metadata ownership: " + name)
        names.add(name)
        require(name in final and final[name].get("Status") == "install ok installed", "package missing final installed record: " + name)
        fields = final[name]
        control_fields = paragraphs(controls["control"][0].decode())
        require(len(control_fields) == 1 and control_fields[0].get("Package") == name and
                control_fields[0].get("Version") == provenance.get("control_version", fields["Version"]) and
                fields["Version"] == provenance.get("source_version", fields["Version"]),
                "package control identity mismatch: " + name)
        for key, substitutions in provenance.get("control_field_substitutions", {}).items():
            require(key in {"Depends", "Pre-Depends", "Conflicts", "Breaks", "Replaces", "Provides"},
                    "unsupported source control field substitution: " + key)
            for substitution in substitutions:
                before, after = substitution["from"], substitution["to"]
                require(before and before in control_fields[0].get(key, ""), "source control field substitution has no match")
                control_fields[0][key] = control_fields[0][key].replace(before, after)
        require(fields["Architecture"] in {architecture, "all"}, "package architecture mismatch: " + name)
        if kind in {"apt", "imported"}:
            require(control_fields[0]["Architecture"] == fields["Architecture"], "imported package architecture mismatch: " + name)
        for key in ("Depends", "Pre-Depends", "Conflicts", "Breaks", "Replaces", "Provides"):
            if fields.get(key) and control_fields[0].get(key):
                require("".join(fields[key].split()) == "".join(control_fields[0][key].split()),
                        "package relationship differs from reviewed control: " + name + " " + key)
        for key, value in control_fields[0].items():
            if key not in {"Status", "Architecture", "Version", "Installed-Size", "Conffiles"} and not fields.get(key):
                fields[key] = value
        paths, sums, types = set(), {}, {}
        for payload in entry.get("lower_payloads", []) + [entry["payload"]]:
            part_paths, part_sums, part_types = payload_files(payload,
                excludes=entry.get("exclude_patterns", inputs.get("exclude_patterns", ())),
                includes=entry.get("include_patterns", inputs.get("include_patterns", ())))
            paths.update(part_paths)
            for path in part_types:
                sums.pop(path, None)
            sums.update(part_sums)
            types.update(part_types)
        pristine_sums = dict(sums)
        aliases = entry.get("directory_aliases", inputs.get("normalization", {}).get("directory_aliases",
                            inputs.get("directory_aliases", DIRECTORY_ALIASES)))
        if actual_entries is not None:
            rewrites = {path: path if path in actual_entries else normalized_path(path, directory_aliases=aliases)
                        for path in paths}
            paths = {destination for destination in rewrites.values() if destination in actual_entries}
            sums = {rewrites[path]: actual_entries[rewrites[path]].get("md5", value)
                    for path, value in sums.items() if rewrites[path] in paths}
            types = {rewrites[path]: value for path, value in types.items() if rewrites[path] in paths}
            require(any(path != "." for path in paths), "package has no installed payload: " + name)
        for path in extra_owned_paths.get(name, []):
            path = safe_path(path.lstrip("/"))
            require(actual_entries is not None and path in actual_entries,
                    "extra owned path is absent from installed image layers: " + name + " " + path)
            paths.add(path)
            paths.update(str(parent) for parent in PurePosixPath(path).parents)
            types[path] = actual_entries[path]["kind"]
            if "md5" in actual_entries[path]:
                sums[path] = actual_entries[path]["md5"]
        conffiles, removed_conffiles = parse_conffiles(controls.get("conffiles", (b"", 0))[0])
        for path in removed_conffiles:
            aliases_for_path = {path[1:], normalized_path(path[1:], directory_aliases=aliases)}
            require(not aliases_for_path.intersection(pristine_sums),
                    "remove-on-upgrade conffile exists in package payload: " + name + " " + path)
            # A fresh installation needs no migration when the old file is
            # absent. Existing files require dpkg's upgrade/backup behavior;
            # this metadata-only action must not silently delete user config.
            # Debian sometimes transfers a conffile between packages: e.g.
            # binutils-common removes /etc/gprofng.rc while binutils adds it.
            # On a fresh install the new owner's declared file needs no old
            # conffile migration. An existing base file still fails closed.
            new_owner = active_conffile_owners.get(path, set()).difference({name})
            require(not aliases_for_path.intersection(base_members) and
                    (not aliases_for_path.intersection(actual_entries or {}) or bool(new_owner)),
                    "remove-on-upgrade conffile needs a real upgrade: " + name + " " + path)
        conffile_hashes = {}
        for path in conffiles:
            actual = path[1:]
            if actual not in pristine_sums:
                actual = normalized_path(actual, directory_aliases=aliases)
            require(actual in pristine_sums, "conffile is absent from package payload: " + name + " " + path)
            installed = actual if actual in paths else normalized_path(actual, directory_aliases=aliases)
            require(installed in paths, "conffile was removed by image filtering: " + name + " " + path)
            conffile_hashes[path] = pristine_sums[actual]
            sums.pop(installed, None)
            # dpkg compares conffile names with its ownership list literally.
            # Listing /run/x while declaring /var/run/x would remove a user's
            # configuration on `dpkg --remove`, despite the directory alias.
            if installed != path[1:]:
                paths.discard(installed)
                paths.add(path[1:])
                paths.update(str(parent) for parent in PurePosixPath(path[1:]).parents)
        fields.pop("Conffiles", None)
        conf_records = [" " + path + " " + conffile_hashes[path] for path in conffiles]
        # Preserve removed conffiles as obsolete, allowing dpkg purge to find
        # them later while upgrades retain a user's previous configuration.
        for line in old_records.get(name, {}).get("Conffiles", "").splitlines():
            values = line.split()
            if values and values[0] not in conffile_hashes:
                require(len(values) in {2, 3}, "invalid inherited conffile record")
                conf_records.append(" " + " ".join(values[:2]) + " obsolete")
                paths.add(values[0].lstrip("/"))
        if conf_records:
            fields["Conffiles"] = "\n" + "\n".join(conf_records)
        physical_paths = paths | {normalized_path(path, directory_aliases=aliases) for path in paths}
        sizes = {}
        for payload in entry.get("lower_payloads", []) + [entry["payload"]]:
            with tarfile.open(payload) as archive:
                for member in archive:
                    path = safe_path(member.name)
                    path = path if path in physical_paths else normalized_path(path, directory_aliases=aliases)
                    if path in physical_paths:
                        sizes[path] = (member.size + 1023) // 1024 if member.isfile() else 0
        fields["Installed-Size"] = str(sum(sizes.values()))
        if actual_entries is not None:
            fields["Installed-Size"] = str(sum((actual_entries.get(path, {}).get("size", 0) + 1023) // 1024
                                              for path in physical_paths))
        prefix = info_name(fields)
        generated[INFO + prefix + ".list"] = ("".join("/" + path + "\n" for path in sorted(paths)).encode(), 0o644)
        generated[INFO + prefix + ".md5sums"] = ("".join(value + "  " + path + "\n" for path, value in sorted(sums.items())).encode(), 0o644)
        if conffiles or removed_conffiles:
            generated[INFO + prefix + ".conffiles"] = controls["conffiles"]
        for filename in CONTROL_FILES.intersection(controls):
            content, mode = controls[filename]
            require(mode & 0o111 if filename in SCRIPT_FILES else True,
                    "maintainer script is not executable: " + name + "." + filename)
            generated[INFO + prefix + "." + filename] = (content, mode)
        previous = old_records.get(name)
        old_prefixes = [info_name(previous)] if previous else []
        package_data.append({"package": name, "info_name": prefix, "old_info_names": old_prefixes,
                             "controls": controls, "paths": paths, "types": types, "kind": kind,
                             "payload_sha256": sha(entry["payload"]), "provenance": provenance,
                             "conffiles": conffile_hashes, "version": fields["Version"]})
    for owner, declared_paths in extra_owned_paths.items():
        for path in declared_paths:
            path = safe_path(path.lstrip("/"))
            for item in package_data:
                require(item["package"] == owner or path not in item["paths"],
                        "extra owned path belongs to another package: " + path + " " + item["package"])
            owner_prefixes = {item["info_name"] for item in package_data if item["package"] == owner}
            owner_prefixes.update(prefix for item in package_data if item["package"] == owner for prefix in item["old_info_names"])
            for filename, (contents, _) in metadata.items():
                if filename.startswith(INFO) and filename.endswith(".list") and filename[len(INFO):-5] not in owner_prefixes:
                    require(path not in {line.lstrip("/") for line in contents.decode().splitlines()},
                            "extra owned path belongs to an inherited package: " + path)
    generated.update(register_triggers(metadata, package_data))
    # Keep unrelated metadata in inherited layers. Whiteouts remove only old
    # control files belonging to packages whose replacement inputs are present.
    removed = set()
    for item in package_data:
        prefixes = set(item["old_info_names"]) | {item["info_name"]}
        for path in metadata:
            if path.startswith(INFO) and any(path[len(INFO):].startswith(prefix + ".") for prefix in prefixes):
                if path not in generated:
                    removed.add(path)
    # Removing an old ownership record must not strand its obsolete payload.
    # Shared paths and conffiles remain owned/preserved, just as during upgrade.
    aliases = inputs.get("normalization", {}).get("directory_aliases", inputs.get("directory_aliases", DIRECTORY_ALIASES))
    all_new_paths = {path for item in package_data for path in item["paths"]}
    all_new_paths.update(normalized_path(path, directory_aliases=aliases) for path in tuple(all_new_paths))
    other_owned = set()
    replaced_prefixes = {prefix for item in package_data for prefix in item["old_info_names"]}
    for path, (contents, _) in metadata.items():
        if path.startswith(INFO) and path.endswith(".list") and path[len(INFO):-5] not in replaced_prefixes:
            other_owned.update(line.lstrip("/") for line in contents.decode().splitlines())
    other_owned.update(normalized_path(path, directory_aliases=aliases) for path in tuple(other_owned))
    for item in package_data:
        previous = old_records.get(item["package"], {})
        old_conffiles = {line.split()[0].lstrip("/") for line in previous.get("Conffiles", "").splitlines() if line.split()}
        old_conffiles.update(normalized_path(path, directory_aliases=aliases) for path in tuple(old_conffiles))
        for prefix in item["old_info_names"]:
            listing = metadata.get(INFO + prefix + ".list", (b"", 0))[0]
            for line in listing.decode().splitlines():
                path = safe_path(line.lstrip("/"))
                if path not in base_members:
                    path = normalized_path(path, directory_aliases=aliases)
                if path not in all_new_paths | other_owned | old_conffiles and base_members.get(path) == "file":
                    removed.add(path)
    for path in removed:
        name = PurePosixPath(path)
        generated[str(name.parent / (".wh." + name.name))] = (b"", 0o644)
    generated["var/lib/dpkg/status"] = (("\n\n".join(Deb822(fields).dump().strip() for _, fields in sorted(final.items())) + "\n\n").encode(), 0o644)
    receipt = {
        "_generated": {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.", "generator": "tools/bazel/oci/dpkg_state.py"},
        "schema": 1, "architecture": architecture, "base_manifest_digest": layout.descriptor["digest"],
        "status_input_sha256": sha(status_path), "apt_selection_sha256": sha(apt_receipt_path),
        "maintainer_scripts_executed": False, "installation_triggers_processed": False,
        "scope": "dpkg state for later package transactions; initial package configuration is validated separately",
        "info_files": {path: {"sha256": digest(content), "mode": mode}
                       for path, (content, mode) in sorted(generated.items())
                       if (path.startswith((INFO, TRIGGERS)) or path == "var/lib/dpkg/status")
                       and not PurePosixPath(path).name.startswith(".wh.")},
        "removed_paths": sorted(removed),
        "packages": [{key: value for key, value in item.items() if key not in {"controls", "paths", "types"}}
                     for item in package_data],
    }
    receipt_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    Path(receipt_path).write_bytes(receipt_bytes)
    if inputs.get("receipt_path"):
        generated[safe_path(inputs["receipt_path"].lstrip("/"))] = (receipt_bytes, 0o644)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    require(not any(output_dir.iterdir()), "dpkg metadata output directory must be empty")
    mtree = ["#mtree"]
    for path, (content, mode) in sorted(generated.items()):
        destination = output_dir / safe_path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        mtree.append("./" + mtree_escape(path) + " type=file uid=0 gid=0 mode=%04o time=0 content=" % mode + mtree_escape(destination))
    Path(mtree_path).write_text("\n".join(mtree) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "status", "apt-receipt", "inputs", "out-dir", "mtree", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    build(args.base, args.status, args.apt_receipt, json.loads(args.inputs.read_text()),
          args.out_dir, args.mtree, args.receipt)


if __name__ == "__main__":
    main()
