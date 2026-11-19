"""Shared cache settings and atomic archive publication for Make/Bazel builds."""

import argparse
import filecmp
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.bazel.cache import credentials


EXPORT_POLICY = (
    '--invocation_policy=flag_policies { flag_name: "remote_download_outputs" '
    'disallow_values { disallowed_values: "minimal" new_value: "toplevel" } }'
)


def build_labels(workspace, archives):
    """Give external and shorthand producers stable top-level BEP identities."""
    labels = archives.copy()
    aliases = {name: label for name, label in archives.items()
               if not (label.startswith(("//", "@@")) and ":" in label)}
    if not aliases:
        return labels
    contents = "".join(
        f'filegroup(name={json.dumps(name)}, srcs=[{json.dumps(label)}], tags=["manual"])\n'
        for name, label in sorted(aliases.items())
    )
    package = Path("target/bazel-container-batch") / hashlib.sha256(contents.encode()).hexdigest()
    directory = workspace / package
    directory.mkdir(parents=True, exist_ok=True)
    definition = directory / "BUILD.bazel"
    if not definition.is_file():
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as temporary:
            temporary.write(contents)
        Path(temporary.name).replace(definition)
    for name in aliases:
        labels[name] = f"//{package}:{name}"
    return labels


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


def build_export(command, outputs, labels, directory=None):
    from tools.bazel.ci import time_vs_image

    if command[:1] == ["--"]:
        command = command[1:]
    if not command or len(outputs) != len(labels) or len({p.name for p in outputs}) != len(outputs):
        raise ValueError("Expected a Bazel executable and one target label per unique archive")
    producers = build_labels(Path.cwd(), dict(zip((p.name for p in outputs), labels)))
    with tempfile.TemporaryDirectory(prefix="sonic-container-export-") as temporary:
        events = Path(temporary) / "events.json"
        arguments = [
            command[0], EXPORT_POLICY, "build", *command[1:],
            "--build_event_json_file=" + str(events),
            "--nobuild_event_json_file_path_conversion",
            "--experimental_build_event_output_group_mode=default=inline_only",
            *dict.fromkeys(producers.values()),
        ]
        time_vs_image.run_phase(directory, "build", arguments)
        sources = archive_outputs(events, set(producers.values()))
        for output in outputs:
            with time_vs_image.time_phase(directory, "export-" + output.name):
                export_archive(sources[producers[output.name]], output)


def cache_options(cache_directory):
    """Share content-addressed caches, keeping each Bazel output base private."""
    if not cache_directory:
        return []
    root = Path(cache_directory).expanduser().resolve()
    options = []
    for name, flag in (("repository_cache", "repository_cache"), ("disk_cache", "disk_cache")):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        options.append(f"--{flag}={path}")
    return options


def remote_cache_options():
    """Read the shared cache without exposing authentication in Bazel options."""
    if not os.environ.get("BAZEL_REMOTE_AUTH"):
        endpoint = os.environ.get("SONIC_BAZEL_REMOTE_CACHE")
        if not endpoint:
            return []
        upload = "true" if os.environ.get("SONIC_BAZEL_REMOTE_UPLOAD") == "y" else "false"
        return [f"--remote_cache={endpoint}", f"--remote_upload_local_results={upload}"]
    credentials.authorization()
    helper = Path(__file__).resolve().parent / "cache/credentials.py"
    return [
        "--remote_cache=" + credentials.ENDPOINT + "/",
        "--credential_helper=" + credentials.HOST + "=" + str(helper),
        "--remote_executor=",
        "--remote_accept_cached=true",
        "--remote_upload_local_results=false",
    ]


def export_archive(source, destination):
    """Leave the previous image intact if copying a new archive fails."""
    if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".",
                                     delete=False) as output:
        temporary = Path(output.name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    options = commands.add_parser("options", help="Print one Bazel option per line")
    options.add_argument("--cache-directory")
    options.add_argument("--arguments", default="", help="Additional shell-quoted Bazel options")
    export = commands.add_parser("export", help="Publish the single archive reported by cquery")
    export.add_argument("--query-output", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--execution-root", type=Path)
    build = commands.add_parser("build-export", help="Build and publish archives from BEP without cquery")
    build.add_argument("--output", type=Path, action="append", required=True)
    build.add_argument("--label", action="append", required=True)
    build.add_argument("--timing-directory", type=Path)
    build.add_argument("bazel_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "options":
        arguments = cache_options(args.cache_directory) + shlex.split(args.arguments)
        try:
            # Apply the shared cache policy after caller options so Make image
            # builds cannot accidentally upload results or enable remote execution.
            arguments += remote_cache_options()
        except ValueError:
            parser.exit(2, credentials.ERROR + "\n")
        if any(not value or any(char in value for char in "\r\n\0") for value in arguments):
            parser.error("Bazel options must be nonempty single-line arguments")
        for value in arguments:
            print(value)
    elif args.command == "build-export":
        try:
            build_export(args.bazel_command, args.output, args.label, args.timing_directory)
        except subprocess.CalledProcessError as error:
            parser.exit(error.returncode if error.returncode >= 0 else 128 - error.returncode)
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.exit(1, str(error) + "\n")
    else:
        files = args.query_output.splitlines()
        if len(files) != 1:
            parser.error("expected exactly one Bazel archive output")
        source = Path(files[0])
        if args.execution_root is not None:
            if not args.execution_root.is_absolute() or source.is_absolute() or ".." in source.parts:
                parser.error("expected a Bazel exec path relative to the producing execution root")
            source = args.execution_root / source
        if not source.is_file() or not source.stat().st_size:
            parser.error("Bazel did not produce a nonempty archive")
        export_archive(source, args.output)


if __name__ == "__main__":
    main()
