#!/usr/bin/env python3
"""Run container CI from an owner's declared tests, archives and package checks."""

import argparse
from dataclasses import dataclass, field, replace
from functools import partial
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from typing import Callable, Mapping
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import build, command_log, resolution
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.ci.bazel_commands import inspect_actions
from tools.bazel.gzip.source_archive import check_versions

OPTIONS = (
    "--jobs=4", "--local_resources=cpu=4", "--local_resources=memory=10000",
    "--lockfile_mode=update", "--noshow_progress", "--color=no", "--curses=no",
)
SOURCE_FILES = (
    ".bazelversion", ".bazelrc", "MODULE.bazel",
    "tools/bazel/ci/container.py", "tools/bazel/ci/build.py",
    "tools/bazel/ci/command_log.py", "tools/bazel/ci/bazel_commands.py",
    "tools/bazel/ci/artifact_validation.py", "tools/bazel/ci/resolution.py",
    "tools/bazel/gzip/source_archive.py",
)


@dataclass(frozen=True)
class Config:
    """Owner policy; the shared runner controls execution and evidence handling."""

    name: str
    scope: str
    tests: tuple[str, ...]
    archives: Mapping[str, str] = field(default_factory=dict)
    make_args: tuple[str, ...] = ()
    manifests: tuple[str, ...] = ()
    source_files: tuple[str, ...] = ()
    validate_archives: Callable | None = None
    # The source-job public artifact collector reads this private BEP file.
    # Contract-only jobs retain selected logs/XML and remove the raw events.
    retain_test_events: bool = False


def load_module(path):
    """Load owner files without ambiguous imports such as package_contract."""
    path = path.resolve()
    name = "container_ci_" + hashlib.sha256(str(path).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, "cannot load CI configuration: " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_config(path):
    return load_module(path).CONFIG


def configured_test_labels(targets, bazel, options, artifacts, receipt, execute):
    """Resolve apparent repository labels to the exact labels emitted by BEP."""
    labels = {}
    for index, target in enumerate(targets):
        canonical = target
        if target.startswith("@") and not target.startswith("@@"):
            matches = execute(
                [bazel, "cquery", *options, "--output=starlark",
                 "--starlark:expr=str(target.label)", target],
                artifacts, receipt, "test-label-" + str(index),
            ).splitlines()
            require(len(matches) == 1 and matches[0].startswith("@@") and "//" in matches[0],
                    "expected one canonical test label for " + target)
            canonical = matches[0]
        require(canonical not in labels, "duplicate configured test label: " + canonical)
        labels[canonical] = target
    return labels


def collect_test_outputs(events, artifacts, targets, labels=None):
    """Collect actual configured outputs, including Python-transition tests."""
    labels = labels if labels is not None else {target: target for target in targets}
    outputs = {}
    for line in events.read_text().splitlines():
        event = json.loads(line)
        target = labels.get(event.get("id", {}).get("testResult", {}).get("label"))
        if target not in targets:
            continue
        for output in event.get("testResult", {}).get("testActionOutput", []):
            filename = output.get("name")
            if filename not in ("test.log", "test.xml"):
                continue
            uri = urlparse(output.get("uri", ""))
            require(uri.scheme == "file" and not uri.netloc,
                    "test evidence is not a local file: " + target + "/" + filename)
            source = Path(unquote(uri.path))
            key = (target, filename)
            require(key not in outputs or outputs[key] == source,
                    "ambiguous configured test evidence: " + target + "/" + filename)
            outputs[key] = source
    names = [target.rsplit(":", 1)[1] for target in targets]
    require(len(names) == len(set(names)), "test evidence filenames must be unique")
    collected = []
    for target, name in zip(targets, names):
        for filename in ("test.log", "test.xml"):
            source = outputs.get((target, filename))
            require(source is not None and source.is_file() and source.stat().st_size > 0,
                    "missing required test evidence: " + target + "/" + filename)
            destination = artifacts / "tests" / name / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            collected.append(str(destination.relative_to(artifacts)))
    return collected


def run(config, *, workspace, artifacts, bazel, options):
    """Audit and execute one explicit container profile, retaining failure evidence."""
    require(not artifacts.exists() or not any(artifacts.iterdir()),
            "artifact directory must be empty: " + str(artifacts))
    artifacts.mkdir(parents=True, exist_ok=True)
    execute = partial(command_log.execute, cwd=workspace)
    receipt = {"status": "running", "commands": [], "scope": config.scope}
    started = time.monotonic()
    try:
        require(platform.machine() == "x86_64" and
                platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie",
                config.name + " CI requires native AMD64 Debian Trixie")
        receipt["platform"] = platform.platform()
        receipt["architecture"] = "amd64"
        receipt["revision"] = execute(["git", "rev-parse", "HEAD"], artifacts, receipt, "revision").strip()
        before = {name: sha(workspace / name) for name in config.source_files}
        version = execute([bazel, "--version"], artifacts, receipt, "bazel-version").strip()
        require(version == "bazel " + (workspace / ".bazelversion").read_text().strip(),
                "Bazel version does not match .bazelversion: " + version)
        receipt["bazel_version"] = version
        require(not (workspace / "MODULE.bazel.lock").exists(),
                "CI must start without a preexisting MODULE.bazel.lock")
        if config.make_args:
            execute(["make", "-f", "tools/bazel/prepare_manifests.mk", *config.make_args],
                    artifacts, receipt, "make-manifests")
        for name in config.manifests:
            require((workspace / "target/bazel-manifests" / name / "manifest.json").is_file(),
                    "missing prepared Make manifest: " + name)

        targets = [*config.tests, *config.archives.values()]
        require(bool(config.tests), "container CI must select explicit tests")
        actions = artifacts / "actions.raw.json"
        actions.touch(mode=0o600, exist_ok=False)
        try:
            execute([bazel, "aquery", *options, "deps(set(" + " ".join(targets) + "))",
                     "--output=jsonproto"], artifacts, receipt, "actions", output_path=actions)
            audit = inspect_actions(actions)
        finally:
            actions.unlink(missing_ok=True)
        audit["targets"] = targets
        (artifacts / "execution-gate-audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
        receipt["action_audit"] = audit
        require(not audit["deb_outputs"] and not audit["packaging_wrappers"],
                "container CI contains a DEB or packaging wrapper action")

        labels = configured_test_labels(config.tests, bazel, options, artifacts, receipt, execute)
        events = artifacts / "tests.raw.json"
        events.touch(mode=0o600, exist_ok=False)
        try:
            execute([bazel, "test", *options, "--nocache_test_results", "--test_output=errors",
                     "--build_event_json_file=" + str(events), *config.tests], artifacts, receipt, "tests")
            test_outputs = collect_test_outputs(events, artifacts, config.tests, labels)
        finally:
            if config.retain_test_events:
                events.replace(artifacts / "test-events.jsonl")
            else:
                events.unlink(missing_ok=True)
        receipt["tests"] = {target: "passed" for target in config.tests}
        if config.archives:
            paths = build.collect_archives(workspace, artifacts, receipt, config.archives,
                                           bazel=bazel, options=options)
            if config.validate_archives is not None:
                receipt["validation"] = config.validate_archives(
                    workspace, paths, artifacts, receipt, bazel=bazel, options=options)
        receipt["resolution"] = resolution.collect(workspace, artifacts, bazel=[bazel])
        check_versions(artifacts / "module-graph.json")
        require({name: sha(workspace / name) for name in config.source_files} == before,
                "container CI changed a checked source or package lock")
        report = {"schema": 1, "bazel_version": version, "targets": list(config.tests),
                  "test_outputs": test_outputs, "source_hashes": before,
                  "module_lock_sha256": sha(workspace / "MODULE.bazel.lock"),
                  "execution_gate_audit": "execution-gate-audit.json", "scope": config.scope}
        (artifacts / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        receipt["status"] = "passed"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        path = args.config.resolve()
        config = load_config(path)
        inputs = (*SOURCE_FILES, str(path.relative_to(ROOT)), *config.source_files)
        config = replace(config, source_files=tuple(dict.fromkeys(inputs)))
        run(config, workspace=ROOT, artifacts=args.artifacts.resolve(), bazel=args.bazel,
            options=[*OPTIONS, *args.bazel_arg])
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, "container CI failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
