"""Select standalone native checks from the last master push that passed the whole matrix."""

import argparse
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from tools.bazel.ci import validation_changes
from tools.bazel.ci.source_workflows import matches
from tools.bazel.ci.vs_host_base_snapshot import github_api


# A successful selector-only run supplies no new evidence for native binaries.
NATIVE_JOBS = {
    "bazel-eventd.yml": ("Native event services (amd64)", "Native event services (arm64)"),
    "bazel-supervisor-rust.yml": ("Rust listener (AMD64)", "Rust listener (ARM64)"),
    "bazel-image-rules.yml": ("Image assembly contracts (amd64)", "Image assembly contracts (arm64)"),
    "runtime-image-validation.yml": ("Native validator tests (amd64)", "Native validator tests (arm64)"),
}


def validated_revision(api, workflow, repository, number, ancestor):
    for run in validation_changes.successful_master_runs(api, workflow, repository, number, ancestor):
        jobs = api(f"runs/{run['id']}/jobs?per_page=100")
        passed = {job["name"] for job in jobs["jobs"] if job["conclusion"] == "success"}
        if set(NATIVE_JOBS[workflow]) <= passed:
            return run["head_sha"]
    return None


def declared_input(paths, patterns):
    return any(matches(pattern, path) or (pattern.endswith("/**") and path == pattern[:-3])
               for path in paths for pattern in patterns)


def affected(workflow, paths, patterns):
    if declared_input(paths, patterns):
        return True
    plan = validation_changes.select(paths)
    # Shared build inputs, toolchains, test helpers, and unclassified changes
    # retain full coverage. The installed-image suite also reads Docker rules.
    return (plan["full"] or
            (workflow == "runtime-image-validation.yml" and bool(plan["images"])))


def select_workflow(workflow, patterns, env, event, api=github_api):
    kind = env["GITHUB_EVENT_NAME"]
    revision = env["GITHUB_SHA"]
    if kind == "workflow_dispatch":
        return True, "explicit source or cache test"
    pull = event.get("pull_request", {})
    repo = env["GITHUB_REPOSITORY"]
    if kind == "pull_request" and pull["head"]["repo"]["full_name"] != repo:
        # Preserve the existing fork path filters on a hosted runner before any
        # native runner is allocated. Fork builds can select different outputs.
        base = pull["base"]["sha"]
        if not re.fullmatch("[0-9a-f]{40}", base):
            raise ValueError("invalid pull request base")
        return declared_input(validation_changes.changes(base, revision), patterns), "fork PR paths"
    if not ((kind == "push" and env["GITHUB_REF"] == "refs/heads/master") or
            (kind == "pull_request" and pull["base"]["repo"]["full_name"] == repo
             and pull["base"]["ref"] == "master")):
        return True, "no comparable master configuration"

    def ancestor(sha):
        return subprocess.run(["git", "merge-base", "--is-ancestor", sha, revision],
                              stderr=subprocess.DEVNULL).returncode == 0

    try:
        baseline = validated_revision(api, workflow, repo, int(env["GITHUB_RUN_NUMBER"]), ancestor)
        if baseline:
            paths = validation_changes.changes(baseline, revision)
            return affected(workflow, paths, patterns), "compared with successful master run at " + baseline
    except (HTTPException, KeyError, OSError, RuntimeError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Could not verify prior native checks ({type(error).__name__}).", file=sys.stderr)
    return True, "no proven native validation baseline"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", choices=sorted(NATIVE_JOBS))
    args = parser.parse_args()
    patterns = os.environ["INPUT_PATHS"].split()
    if not patterns:
        raise ValueError("native workflow must declare its source and test inputs")
    selected, reason = select_workflow(args.workflow, patterns, os.environ,
                                       json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes()))
    print(f"{args.workflow}: {'run' if selected else 'skip'} ({reason})")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
        print(f"selected={str(selected).lower()}", file=out)


if __name__ == "__main__":
    main()
