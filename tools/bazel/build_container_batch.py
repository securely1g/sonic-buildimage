"""Build selected Make container archives together with each owner's original Bazel settings."""

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.bazel import build_helpers
from tools.bazel.ci import time_vs_image


# Bazel applies this after rc/config expansion. Publishing only requires the
# top-level outputs. The policy keeps an existing "all" download mode intact.
EXPORT_POLICY = (
    '--invocation_policy=flag_policies { flag_name: "remote_download_outputs" '
    'disallow_values { disallowed_values: "minimal" new_value: "toplevel" } }'
)


def owner_settings(arguments, rc):
    """Expand the checked-in Syncd profile and translate explicit Starlark flags."""
    options = []
    for option in shlex.split(arguments):
        if option == "--config=syncd-dash":
            profile = []
            for line in rc.read_text().splitlines():
                words = shlex.split(line, comments=True)
                if words[:1] == ["build:syncd-dash"]:
                    profile.extend(words[1:])
            if not profile:
                raise ValueError("Missing build:syncd-dash in " + str(rc))
            options.extend(profile)
        else:
            options.append(option)
    settings = {}
    for option in options:
        key, equals, value = option.partition("=")
        if not (equals and key.startswith(("--//", "--@")) and "//" in key and ":" in key):
            raise ValueError("Unsupported per-container Bazel option: " + option)
        settings[key[2:]] = value
    return settings


def batch_package(workspace, archives):
    """Generate only forwarding targets; the configured dependency owns the archive."""
    groups = sorted({tuple(sorted(settings)) for _, settings in archives.values() if settings})
    rules = {group: "archive_config_" + str(i) for i, group in enumerate(groups)}
    definitions = ['load("//tools/bazel:container_batch.bzl", "configured_archive_rule")']
    for group, name in rules.items():
        definitions.append(f"{name} = configured_archive_rule({json.dumps(group)})")
    targets = []
    if rules:
        targets.append('load(":configs.bzl", ' + ", ".join(map(json.dumps, rules.values())) + ")")
    for archive, (label, settings) in sorted(archives.items()):
        if settings:
            rule = rules[tuple(sorted(settings))]
            targets.append(f'{rule}(name={json.dumps(archive)}, archive={json.dumps(label)}, '
                           f'settings={json.dumps(settings, sort_keys=True)}, tags=["manual"])')
        else:
            targets.append(f'filegroup(name={json.dumps(archive)}, srcs=[{json.dumps(label)}], tags=["manual"])')
    files = {"BUILD.bazel": "\n".join(targets) + "\n", "configs.bzl": "\n".join(definitions) + "\n"}
    key = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    parent = workspace / "target/bazel-container-batch"
    parent.mkdir(parents=True, exist_ok=True)
    package = parent / key
    if not package.exists():
        # Parallel Make processes may select the same set. Readers see a complete package.
        with tempfile.TemporaryDirectory(dir=parent) as staging:
            prepared = Path(staging) / key
            prepared.mkdir()
            for name, content in files.items():
                (prepared / name).write_text(content)
            try:
                prepared.rename(package)
            except OSError:
                if not package.is_dir():
                    raise
    return "//" + package.relative_to(workspace).as_posix()


def archive_outputs(events, labels):
    """Resolve only the successful top-level targets' single declared file."""
    outputs = {}
    with events.open() as stream:
        for line in stream:
            event = json.loads(line)
            target = event.get("id", {}).get("targetCompleted", {})
            label = target.get("label")
            if label not in labels or target.get("aspect"):
                continue
            completed = event["completed"]
            if not completed.get("success") or label in outputs:
                raise ValueError("Unsuccessful or duplicate Bazel target: " + label)
            files = [item for group in completed.get("outputGroup", []) if group["name"] == "default"
                     for item in group.get("inlineFiles", [])]
            if len(files) != 1:
                raise ValueError("Expected exactly one Bazel archive output for " + label)
            uri = urlsplit(files[0].get("uri", ""))
            path = Path(unquote(uri.path))
            if uri.scheme != "file" or uri.netloc or not path.is_absolute():
                raise ValueError("Expected a local Bazel archive URI for " + label)
            if not path.is_file() or not path.stat().st_size:
                raise ValueError("Bazel did not materialize a nonempty local archive for " + label)
            outputs[label] = path
    missing = labels - outputs.keys()
    if missing:
        raise ValueError("Missing Bazel archive outputs: " + ", ".join(sorted(missing)))
    return outputs


def build(args):
    workspace = Path.cwd()
    if not (len(args.archive) == len(args.label) == len(args.owner_args) == len(set(args.archive))):
        raise ValueError("Expected a unique archive, target label and option string for every batch member")
    archives = {}
    owners = defaultdict(list)
    debug_suffix = "-" + args.debug_image_mark + ".gz"
    for name, label, flags in zip(args.archive, args.label, args.owner_args):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.gz", name):
            raise ValueError("Invalid container archive name: " + name)
        if not label.startswith(("//", "@")):
            raise ValueError("Expected an absolute Bazel target label: " + label)
        archives[name] = (label, owner_settings(flags, workspace / "tools/bazel/common.bazelrc"))
        owner = name.removesuffix(debug_suffix) + ".gz" if name.endswith(debug_suffix) else name
        owners[owner].append(name)
    package = batch_package(workspace, archives)
    labels = {name: package + ":" + name for name in archives}
    settings = os.environ
    cache_directory = settings.get("BAZEL_CONTAINER_CACHE_DIR", settings.get("BAZEL_SWSS_CACHE_DIR"))
    global_args = settings.get("BAZEL_CONTAINER_ARGS", settings.get("BAZEL_SWSS_ARGS", ""))
    options = build_helpers.cache_options(cache_directory) + shlex.split(global_args)
    options.extend(build_helpers.remote_cache_options())
    platform = {"amd64": "x86_64_trixie", "arm64": "aarch64_trixie"}[args.architecture]
    platform = "@sonic_build_infra//platforms:" + platform
    options.extend(["--platforms=" + platform, "--host_platform=" + platform])
    directory = settings.get("SONIC_VS_CONTAINER_BATCH_TIMING_DIR")
    build_timing = Path(directory) if directory else None
    export_timings = {}
    for owner, names in owners.items():
        timing = None
        if build_timing:
            timing = time_vs_image.start_recipe(workspace, owner, [], [], ["export-" + n for n in names])
        export_timings.update(dict.fromkeys(names, timing))
    with tempfile.TemporaryDirectory(prefix="sonic-container-batch-") as events_dir:
        events = Path(events_dir) / "events.json"
        command = [
            settings.get("BAZEL", "bazel"), EXPORT_POLICY, "build", *options,
            "--build_event_json_file=" + str(events),
            "--nobuild_event_json_file_path_conversion",
            "--experimental_build_event_output_group_mode=default=inline_only",
            *labels.values(),
        ]
        time_vs_image.run_phase(build_timing, "build", command)
        outputs = archive_outputs(events, set(labels.values()))
        for name, label in labels.items():
            with time_vs_image.time_phase(export_timings[name], "export-" + name):
                build_helpers.export_archive(outputs[label], args.target_path / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-path", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--debug-image-mark", default="dbg")
    for option in ("--archive", "--label", "--owner-args"):
        parser.add_argument(option, action="append", required=True)
    args = parser.parse_args()
    try:
        build(args)
    except subprocess.CalledProcessError as error:
        return time_vs_image.exit_status(error.returncode)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
