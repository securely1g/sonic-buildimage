#!/usr/bin/env python3
"""Select one reviewed registry branch for Draft CI without editing tracked RCs."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    args = parser.parse_args()
    canonical = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main"
    prefix = canonical.removesuffix("main")
    if not args.registry.startswith(prefix + "codex/") or "\n" in args.registry:
        parser.error("select an explicit reviewed SONiC registry branch")
    original = Path(".bazelrc").read_text()
    if original.count(canonical) != 1:
        parser.error("expected one maintained SONiC registry in .bazelrc")
    bazel = shutil.which("bazel")
    if not bazel:
        parser.error("install Bazel before selecting the Draft registry")
    directory = Path(os.environ["RUNNER_TEMP"]) / "sonic-draft-registry"
    directory.mkdir(parents=True, exist_ok=True)
    effective = directory / "effective.bazelrc"
    effective.write_text(original.replace(canonical, args.registry))
    wrapper = directory / "bazel"
    # Bazel's standalone version probe rejects injected startup options.
    wrapper.write_text("#!/bin/sh\n"
                       + 'if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n'
                       + "  exec " + shlex.quote(bazel) + ' "$@"\nfi\n'
                       + "exec " + shlex.quote(bazel)
                       + " --noworkspace_rc --bazelrc=" + shlex.quote(str(effective))
                       + ' "$@"\n')
    wrapper.chmod(0o755)
    with Path(os.environ["GITHUB_PATH"]).open("a") as stream:
        stream.write(str(directory) + "\n")
    (directory / "selection.json").write_text(json.dumps({
        "registry": args.registry,
        "prerequisite": "https://github.com/securely1g/sonic-bazel-registry/pull/80",
        "missing_version": "0.0.16-0c1d31528ad50c6505117a6df7409ccebee863cf",
        "original_bazel": bazel,
    }, indent=2) + "\n")
    print("Draft validation registry:", args.registry)


if __name__ == "__main__":
    main()
