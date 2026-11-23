"""Select VS validations using a successful master run and the current producer's outputs."""

import argparse
from http.client import HTTPException
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import Request, urlopen
import zipfile

from tools.bazel.ci import validation_profile, vs_component_artifacts, vs_probe_policy
from tools.bazel.ci.vs_host_base_snapshot import github_api
from tools.bazel.ci.vs_validation_bundles import BASE, CONFIG, PARENT_IMAGES, SWSS

WORKFLOW = ".github/workflows/bazel-oci.yml"
GROUPS = ("archive", "package_amd64", "kernel_packages_amd64", "kernel_arm64",
          "frr_python_amd64", "yang_models", "native_sysmgr_amd64", "sysmgr_sources", "checks")
PARENTS = {**PARENT_IMAGES, "docker-nat": SWSS, "docker-sonic-bmp": CONFIG}
SOURCES = {"src/sonic-eventd": "eventd", "src/libteam": "libteam", "src/sonic-frr": "frr_native",
           "src/libraries/sonic-fib": "fib", "src/sonic-supervisord-utilities-rs": "supervisor_rust",
           "src/sonic-sysmgr": "sysmgr"}
# These component jobs are embedded in package or reusable workflow jobs.
OWNER_GROUPS = {"host": ("package_amd64",), "swss": ("package_amd64",),
                "sysmgr": ("native_sysmgr_amd64",)}
# Anything without known ownership (including toolchains, locks, shared helpers,
# new Make files and changed workflow policies) schedules the complete graph.
DOCS = {"README.md", "README.buildsystem.md", "README.arm64_build_on_amd64.md", "README.sbom.md",
        "tools/bazel/ci/README.md", ".github/workflows/README.md"}


def full(reason):
    return {"full": True, "baseline": None, "reason": reason, "images": [], "components": [],
            "groups": dict.fromkeys(GROUPS, True)}


def descendants(images):
    affected = set(images)
    for child in PARENTS:
        parent = child
        while parent in PARENTS:
            parent = PARENTS[parent]
            if parent in affected:
                affected.add(child)
                break
    return affected


def select(paths):
    images, owners, groups = set(), set(), set()
    modules = vs_component_artifacts.components()
    known_images = set(PARENTS) | {BASE}
    for path in paths:
        if path in DOCS or (path.startswith("doc/") and path.endswith(".md")):
            continue
        match = re.fullmatch(r"tools/bazel/ci/components/([a-z_]+)\.py", path)
        if match and match[1] in modules:
            # A validator edit does not change the installed container. Changes to
            # its declared producer targets will also be visible in the profile.
            owners.add(match[1])
            if match[1] in {"swss", "host"}:
                groups.add("checks")
            continue
        if path in {"src/libteam/verify_runtime.py", "src/sonic-frr/verify_runtime.py",
                    "src/libraries/sonic-fib/verify_runtime.py", "src/sonic-eventd/bazel/verify_packages.py"}:
            if path == "src/libteam/verify_runtime.py":
                groups.add("checks")  # libteam_sdk_timestamp_test imports this checker.
            owners.add(next(owner for root, owner in SOURCES.items() if path.startswith(root + "/")))
            if path.startswith("src/sonic-eventd/"):
                owners.add("host")
            continue
        owner = next((owner for root, owner in SOURCES.items() if path == root or path.startswith(root + "/")), None)
        if owner:
            groups.add("checks")
            owners.add(owner)
            images.update(descendants(modules[owner].IMAGES))
            if owner == "libteam":
                groups.add("package_amd64")
            if owner == "eventd":
                owners.add("host")
            if owner == "sysmgr":
                groups.add("sysmgr_sources")
            continue
        if path == "sonic-linux-kernel" or path.startswith(("sonic-linux-kernel/", "tools/bazel/kernel/")):
            groups.update(("checks", "kernel_packages_amd64", "kernel_arm64"))
            continue
        if path.startswith(("src/sonic-frr-mgmt-framework/", "src/sonic-bgpcfgd/")):
            images.add("docker-fpm-frr")
            groups.update(("checks", "frr_python_amd64"))
            continue
        if path.startswith(("src/sonic-yang-models/", "src/sonic-yang-mgmt/")):
            images.update(descendants({CONFIG}))
            groups.update(("checks", "archive", "package_amd64", "yang_models", "frr_python_amd64"))
            continue
        match = re.match(r"(?:dockers|platform/vs)/(docker-[^/]+)(?:/|$)", path)
        if match and match[1] in known_images:
            image = match[1]
            images.add(image)
            if not path.endswith("/bazel/runtime_probe.py"):
                images.update(descendants({image}))
                owners.update(name for name, module in modules.items() if image in module.IMAGES)
                # The package jobs exercise Docker metadata and SWSS startup generation.
                groups.update(("checks", "package_amd64", "archive"))
            elif image in {"docker-sonic-mgmt-framework", "docker-dash-engine", "docker-gbsyncd-vs"}:
                groups.add("checks")  # These runtime probes also have direct Python regression tests.
            continue
        match = re.fullmatch(r"tools/bazel/ci/([a-z_]+)_loaded\.py", path)
        if match and match[1] in vs_probe_policy.FAMILIES.values():
            groups.add("checks")  # The regression suite directly imports loaded-image drivers.
            images.update(image for image, family in vs_probe_policy.FAMILIES.items() if family == match[1])
            continue
        return full(f"Shared or unclassified input: {path}")
    for owner in owners:
        groups.update(OWNER_GROUPS.get(owner, ()))
    return {"full": False, "baseline": None, "images": sorted(images), "components": sorted(owners),
            "groups": {name: name in groups for name in GROUPS}, "reason": "Changed inputs and tests"}


def refine(plan, previous, current):
    if plan["full"]:
        return plan
    plan = {**plan, "groups": dict(plan["groups"])}
    if previous["revision"] != plan["baseline"] or current["revision"] != os.environ["GITHUB_SHA"]:
        return full("Validation profiles do not match the compared revisions")
    for field in ("schema", "architecture", "debug"):
        if current[field] != previous[field]:
            return full("VS configuration changed: " + field)
    # A newly introduced job/configuration must establish a successful baseline.
    if any(current[key].keys() != previous[key].keys() for key in ("images", "components", "groups")):
        return full("VS selected a different set of validation inputs")
    images, owners = set(plan["images"]), set(plan["components"])
    changed_images = {name for name, value in current["images"].items() if previous["images"][name] != value}
    images.update(descendants(changed_images))
    modules = vs_component_artifacts.components()
    for owner, value in current["components"].items():
        if previous["components"][owner] != value:
            owners.add(owner)
            images.update(descendants(modules[owner].IMAGES))
            if owner == "libteam":
                plan["groups"]["package_amd64"] = True
            for group in OWNER_GROUPS.get(owner, ()):
                plan["groups"][group] = True
    for name, value in current["groups"].items():
        if previous["groups"][name] != value:
            plan["groups"][name] = True
    return {**plan, "images": sorted(images), "components": sorted(owners)}


def successful_master_runs(api, workflow_filename, repository, number, ancestor):
    """Only earlier, green pushes from this repo and workflow on the tested ancestry."""
    path = ".github/workflows/" + workflow_filename
    workflow = api("workflows/" + workflow_filename)
    if workflow["path"] != path:
        return
    runs = api(f"workflows/{workflow['id']}/runs?event=push&branch=master&status=success&per_page=20")
    for run in runs["workflow_runs"]:
        if not (run["workflow_id"] == workflow["id"] and run["path"] == path and
                run["event"] == "push" and run["head_branch"] == "master" and
                run["repository"]["full_name"] == run["head_repository"]["full_name"] == repository and
                run["status"] == "completed" and run["conclusion"] == "success" and
                run["run_number"] < number and re.fullmatch("[0-9a-f]{40}", run["head_sha"]) and
                ancestor(run["head_sha"])):
            continue
        yield run


def baseline(api, repository, number, ancestor):
    for run in successful_master_runs(api, "bazel-oci.yml", repository, number, ancestor):
        # The trusted run must have actually passed the producer and acceptance
        # gate. A previous partial run is a valid baseline by induction: this
        # gate only accepts skips authorized against its own successful baseline.
        job_data = api(f"runs/{run['id']}/jobs?per_page=100")
        passed = {job["name"] for job in job_data["jobs"] if job["conclusion"] == "success"}
        if not {"VS installer in SONiC slave (AMD64)", "Container archive (AMD64)"} <= passed:
            continue
        name = "validation-profile-v1-" + str(run["run_attempt"])
        artifacts = api(f"runs/{run['id']}/artifacts?name={name}&per_page=5")
        usable = [item for item in artifacts["artifacts"] if item["name"] == name and not item["expired"]
                  and item["size_in_bytes"] < 128 * 1024]
        if len(usable) == 1:
            return run["head_sha"], usable[0]["id"]
        # Old policy, expired evidence or incomplete retry: validate afresh.
        return None
    return None


def download(artifact_id):
    if type(artifact_id) is not int or artifact_id <= 0:
        raise ValueError("Invalid profile artifact ID")
    url = (os.environ["GITHUB_API_URL"] + "/repos/" + os.environ["GITHUB_REPOSITORY"] +
           f"/actions/artifacts/{artifact_id}/zip")
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "sonic-validation"})
    # The Actions API redirects to object storage. Send the bearer token only
    # on the first request, never on a redirected request.
    request.add_unredirected_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    limit = 128 * 1024
    with urlopen(request, timeout=20) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Oversize validation profile")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info, = archive.infolist()
        if info.filename != "validation-profile.json" or info.file_size > limit:
            raise ValueError("Invalid validation profile artifact")
        profile = json.loads(archive.read(info))
    if profile["schema"] != 1 or profile["architecture"] != "amd64" or profile["debug"] is not False:
        raise ValueError("Incompatible master validation profile")
    for field in ("images", "components", "groups"):
        if not profile[field] or not all(re.fullmatch("[a-f0-9]{64}", item) for item in profile[field].values()):
            raise ValueError("Incomplete validation profile: " + field)
    return profile


def changes(base, revision):
    # A move or deletion affects its previous owner as well as its new owner.
    data = subprocess.check_output(["git", "diff", "--ignore-submodules=none", "--no-renames", "--name-only", "-z", base, revision, "--"])
    return [os.fsdecode(name) for name in data.split(b"\0") if name]


def plan_run(current_profile=None):
    if os.environ["GITHUB_EVENT_NAME"] not in {"push", "pull_request"}:
        return full("Standalone or explicitly requested run")
    try:
        if "GITHUB_TOKEN" not in os.environ:
            os.environ["GITHUB_TOKEN"] = os.environ["GH_TOKEN"]
        revision = os.environ["GITHUB_SHA"]
        if current_profile is not None:
            early = json.loads(os.environ["PLAN"])
            if early["full"]:
                return early
            return refine(early, download(early["profile_artifact_id"]), current_profile)
        def ancestor(sha):
            return subprocess.run(["git", "merge-base", "--is-ancestor", sha, revision], stderr=subprocess.DEVNULL).returncode == 0
        found = baseline(github_api, os.environ["GITHUB_REPOSITORY"], int(os.environ["GITHUB_RUN_NUMBER"]), ancestor)
        if not found:
            return full("No successful compatible VS baseline")
        base, artifact_id = found
        if download(artifact_id)["revision"] != base:
            return full("Previous VS profile does not match its workflow revision")
        return {**select(changes(base, revision)), "baseline": base, "profile_artifact_id": artifact_id}
    except (OSError, HTTPException, RuntimeError, KeyError, TypeError, ValueError, AttributeError, zipfile.BadZipFile,
            subprocess.CalledProcessError) as error:
        print(f"Cannot establish comparable CI evidence ({type(error).__name__}); validating all.", file=sys.stderr)
        return full("Previous successful validation evidence unavailable")


def filter_matrix(plan, matrix, key):
    selected = plan["images" if key == "image" else "components"]
    return {"include": [row for row in matrix["include"] if plan["full"] or row[key] in selected]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--profile", type=Path)
    profile = commands.add_parser("profile")
    profile.add_argument("--directory", required=True, type=Path)
    profile.add_argument("--output", required=True, type=Path)
    filt = commands.add_parser("filter")
    filt.add_argument("--key", required=True, choices=("image", "component"))
    args = parser.parse_args()
    if args.command == "profile":
        result = validation_profile.create(args.directory)
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n")
        return
    if args.command == "filter":
        result = filter_matrix(json.loads(os.environ["PLAN"]), json.loads(os.environ["MATRIX"]), args.key)
    else:
        result = plan_run(validation_profile.read(args.profile) if args.profile else None)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
