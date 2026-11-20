"""Publish installer exports, configured OCI inputs and fixtures for CI consumers."""

import argparse
import errno
from functools import lru_cache
import gzip
import hashlib
import io
import json
import re
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.build import execution_root_from_bep
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci import oci_inventory

# Standalone parent probes also run on the original parents used by this build.
PARENTS = ("docker-base-trixie", "docker-swss-layer-trixie")


PARENT_IMAGES = {
    "docker-config-engine-trixie": "docker-base-trixie",
    "docker-swss-layer-trixie": "docker-config-engine-trixie",
    "docker-orchagent": "docker-config-engine-trixie",
    "docker-syncd-vs": "docker-config-engine-trixie",
}
COMMON_FILES = (
    "run/redis/sonic-db/database_config.json",
    "usr/lib/x86_64-linux-gnu/libswsscommon.so.0.0.0",
)


def family(archive):
    return archive.removesuffix(".gz").removesuffix("-dbg")


def validation_targets(archives, registered_debug, debug_enabled, validation_only_debug=()):
    from tools.bazel.ci import vs_probe_policy

    members = dict(archives)
    for name in validation_only_debug:
        runtime = family(name) + ".gz"
        if debug_enabled and runtime in archives:
            require(name.endswith("-dbg.gz"), "Expected a validation-only debug companion: " + name)
            members[name] = archives[runtime].removesuffix(".gz") + "-dbg.gz"
    for parent in PARENTS:
        name = parent + ".gz"
        members.setdefault(name, f"//dockers/{parent}:{name}")
    families = {}
    for name, archive_label in members.items():
        image = family(name)
        variant = "debug" if name.endswith("-dbg.gz") else "runtime"
        require(archive_label.endswith(".gz"), f"{name}: expected an explicit OCI archive target")
        entry = families.setdefault(image, {"members": {}})
        entry["members"][variant] = (name, archive_label, archive_label.removesuffix(".gz"))

    for image, entry in families.items():
        entry["supports_debug"] = image + "-dbg.gz" in registered_debug or image + "-dbg.gz" in validation_only_debug
        with_debug = debug_enabled and entry["supports_debug"]
        require(set(entry["members"]) == ({"runtime", "debug"} if with_debug else {"runtime"}),
                f"{image}: VS batch differs from debug selection")
        entry["sources"] = vs_probe_policy.preparation_targets(image, with_debug)
    return families


def extra_targets(families):
    # The archives, layouts and test fixtures share one Bazel configuration and
    # one build. Some fixtures are external targets with apparent repo names.
    labels = set()
    for entry in families.values():
        for _, archive, layout in entry["members"].values():
            labels.update((archive, layout))
        labels.update(entry["sources"].values())
    return sorted(labels)


def configured_outputs(bazel, options, labels, events):
    # Build private owner fixtures as top-level targets. A filegroup wrapper
    # in this repository would create a cross-package visibility violation.
    # cquery reports canonical repository names and preserves TreeArtifact roots.
    external = {label for label in labels if label.startswith("@") and not label.startswith("@@")}
    canonical = {label: label for label in labels}
    if external:
        repositories = json.loads(subprocess.check_output([bazel, "mod", "dump_repo_mapping", ""], text=True))
        for label in external:
            repository, target = label[1:].split("//", 1)
            canonical[label] = "@@" + repositories[repository] + "//" + target
    query = "set(" + " ".join(sorted(set(labels))) + ")"
    expression = 'str(target.label) + "\\t" + "\\t".join([f.path for f in target.files.to_list()])'
    result = subprocess.check_output(
        [bazel, "cquery", *options, query, "--output=starlark", "--starlark:expr=" + expression],
        text=True,
    )
    execution_root = execution_root_from_bep(events)
    outputs = {}
    for line in result.splitlines():
        fields = line.split("\t")
        label = fields[0]
        require(len(fields) == 2, f"{label}: expected one configured output file or OCI directory")
        output = fields[1]
        if label.lstrip("@").startswith("//"):
            label = label.lstrip("@")
        require(label not in outputs, "duplicate configured output: " + label)
        require(output and not Path(output).is_absolute() and ".." not in Path(output).parts,
                "unexpected Bazel output path: " + output)
        path = (execution_root / output).resolve(strict=True)
        outputs[label] = path
    return {key: outputs[label] for key, label in canonical.items()}


def copy_output(source, output):
    if output.exists() and source.samefile(output):
        return
    try:
        os.link(source.resolve(strict=True), output)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EACCES, errno.EPERM, errno.EMLINK):
            raise
        shutil.copyfile(source, output)


def layout_metadata(directory):
    def blob(descriptor):
        digest = descriptor["digest"]
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "invalid OCI blob path")
        return directory / "blobs" / "sha256" / digest[7:]

    def metadata(descriptor):
        data = blob(descriptor).read_bytes()
        require(len(data) == descriptor["size"] and "sha256:" + hashlib.sha256(data).hexdigest() == descriptor["digest"],
                "OCI metadata differs from its descriptor: " + descriptor["digest"])
        return json.loads(data)

    index = json.loads((directory / "index.json").read_bytes())
    manifest_descriptor, = index["manifests"]
    manifest = metadata(manifest_descriptor)
    return manifest, metadata(manifest["config"]), [blob(item) for item in manifest["layers"]]


def write_inventory(directory, variant, bundle, prefixes, retain=False):
    manifest, config, layers = layout_metadata(directory)
    digests = tuple(layer["digest"] for layer in manifest["layers"])
    # The installer emits many siblings with identical immutable parent layers.
    # Start with the longest already-indexed parent and apply only the suffix.
    prefix = max((key for key in prefixes if digests[:len(key)] == key), key=len, default=())
    files = prefixes.get(prefix, {}).copy()
    for layer in layers[len(prefix):]:
        oci_inventory.apply_layer(layer, files)
    if retain:
        prefixes[digests] = files
    report = {"config": config, "config_digest": manifest["config"]["digest"],
              "layers": manifest["layers"], "files": files}
    path = bundle / (variant + ".oci.json.gz")
    with path.open("wb") as raw, gzip.GzipFile(
            fileobj=raw, filename="", mode="wb", compresslevel=3, mtime=0) as stream:
        stream.write(json.dumps(report, sort_keys=True, separators=(",", ":")).encode())
    return path, manifest, config, {name: files[name] for name in COMMON_FILES if name in files}


@lru_cache(maxsize=4)
def redis_config(source_tar):
    """The private Redis probe uses just this file from the normalized parent TAR."""
    name = "run/redis/sonic-db/database_config.json"
    with tarfile.open(source_tar) as source:
        member, = [item for item in source if Path(item.name).as_posix() == name]
        require(member.isfile(), f"{source_tar}: expected regular {name}")
        return source.extractfile(member).read()


def stage_probe_fixtures(image, artifacts, bundle):
    from tools.bazel.ci.vs_standalone_probes import STANDALONE_IMAGES

    # Standalone checks use the owner records projected in *.owners.json.
    # P4RT also needs the original parent TAR for its separate SDK test.
    if image in STANDALONE_IMAGES:
        for name in ("base.source.tar", "config-engine.source.tar", "syncd.source.tar", "source-runtime.tar"):
            artifacts.pop(name, None)
    elif image != "docker-sonic-p4rt":
        for name in ("config-engine-source.tar", "config-engine.source.tar"):
            source = artifacts.pop(name, None)
            if source is None:
                continue
            fixture = bundle / "redis-config-fixture.tar"
            if not fixture.exists():
                data = redis_config(source)
                item = tarfile.TarInfo("run/redis/sonic-db/database_config.json")
                item.size = len(data)
                with tarfile.open(fixture, "w") as target:
                    target.addfile(item, io.BytesIO(data))
            artifacts[fixture.name] = fixture
    return artifacts


def write_bundles(args, families, paths, version):
    from tools.bazel.ci import vs_probe_policy

    destination = args.validation_directory
    destination.parent.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    with TemporaryDirectory(prefix=".vs-bundles-", dir=destination.parent) as work:
        staged = Path(work) / "containers"
        staged.mkdir()
        prefixes, references = {}, {}
        parents = ["docker-base-trixie", "docker-config-engine-trixie", "docker-swss-layer-trixie"]
        for image in [name for name in parents if name in families] + sorted(families.keys() - set(parents)):
            entry = families[image]
            bundle = staged / image
            bundle.mkdir()
            source_paths = {name: paths[label] for name, label in entry["sources"].items()}
            archive_specs = {}
            fixtures = {}
            if image in PARENT_IMAGES:
                require(PARENT_IMAGES[image] in references, f"{image}: missing configured VS parent {PARENT_IMAGES[image]}")
                fixture = bundle / "parent.json"
                fixture.write_text(json.dumps(references[PARENT_IMAGES[image]], sort_keys=True) + "\n")
                fixtures[fixture.name] = sha(fixture)
            for variant in sorted(entry["members"], key=lambda name: name == "debug"):
                archive, label, layout_label = entry["members"][variant]
                layout_path = paths[layout_label]
                parent = image in parents and variant == "runtime"
                inventory, manifest, config, common_files = write_inventory(
                    layout_path, variant, bundle, prefixes, retain=parent)
                if parent:
                    references[image] = {"image": image, "diff_ids": config["rootfs"]["diff_ids"],
                                         "files": common_files}
                # Use the actual target/ file when Make selected this member.
                # Auxiliary parents come from the same completed batch build.
                source = args.target_path / archive if archive in args.archive else paths[label]
                copy_output(source, bundle / archive)
                archive_specs[variant] = {
                    "file": archive, "sha256": sha(bundle / archive),
                    "config_digest": manifest["config"]["digest"],
                    "layers": manifest["layers"], "rootfs": config["rootfs"],
                }
                artifacts = vs_probe_policy.prepare(image, version, variant, layout_path, source_paths, bundle,
                                                   make_manifests=args.target_path / "bazel-manifests")
                fixtures_for_variant = stage_probe_fixtures(image, artifacts, bundle)
                for name, source in {**fixtures_for_variant, inventory.name: inventory}.items():
                    require(Path(name).name == name and name != "bundle.json" and name not in args.archive,
                            "invalid validation fixture name: " + name)
                    digest = sha(source)
                    if name in fixtures:
                        require(fixtures[name] == digest, f"{image}: inconsistent fixture {name}")
                    else:
                        copy_output(source, bundle / name)
                        fixtures[name] = digest
            (bundle / "bundle.json").write_text(json.dumps({
                "image": image, "revision": revision, "build_version": version, "architecture": args.architecture,
                "debug_enabled": args.validation_debug == "y", "supports_debug": entry["supports_debug"],
                "archives": archive_specs, "fixtures": fixtures,
            }, sort_keys=True) + "\n")
        (staged / "selection.json").write_text(json.dumps({
            "revision": revision, "build_version": version, "architecture": args.architecture,
            "debug_enabled": args.validation_debug == "y",
            "installer_archives": sorted(args.archive),
            "auxiliary_parents": list(PARENTS),
            "registered_debug": sorted(set(args.registered_debug)),
            "validation_only_debug": sorted(set(args.validation_only_debug)),
        }, sort_keys=True) + "\n")
        # A Make retry gets a complete replacement, including removal of any
        # debug bundles from an earlier attempt in this checkout.
        if destination.exists():
            destination.rename(Path(work) / "previous")
        staged.rename(destination)


def matrix(directory, debug_layer):
    # Use Make's selected archive list, including expected companion variants.
    # Losing a bundle or manifest member on disk must not shrink the job matrix.
    selection = json.loads((directory / "selection.json").read_bytes())
    expected = {family(name) for name in selection["installer_archives"]} | set(PARENTS)
    require(selection["debug_enabled"] == (debug_layer == "y"), "Make and workflow debug settings differ")
    require({p.name for p in directory.iterdir() if p.is_dir()} == expected,
            "validation bundle list differs from the VS build")
    output = []
    for image in sorted(expected):
        bundle = json.loads((directory / image / "bundle.json").read_bytes())
        variants = {"runtime"}
        if debug_layer == "y" and image + "-dbg.gz" in selection["registered_debug"] + selection["validation_only_debug"]:
            variants.add("debug")
        require(set(bundle["archives"]) == variants, f"{image}: missing selected archive during publication")
        output.append({"image": image})
    return {"include": output}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("matrix",))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--debug-layer", required=True, choices=("y", "n"))
    args = parser.parse_args()
    print(json.dumps(matrix(args.directory, args.debug_layer), separators=(",", ":")))
