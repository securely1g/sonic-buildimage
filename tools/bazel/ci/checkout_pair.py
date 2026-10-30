#!/usr/bin/env python3
"""Prepare and verify an exact same-repository PR merge for supplementary CI."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


REPOSITORY = "securely1g/sonic-buildimage"
REMOTE = "https://github.com/" + REPOSITORY + ".git"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
SCRIPTS = {
    "checkout_pair.py": "tools/bazel/ci/checkout_pair.py",
    "public_artifacts.py": "tools/bazel/ci/public_artifacts.py",
}


class PairError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise PairError(reason)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def load_json(value):
    def unique(items):
        result = {}
        for key, child in items:
            require(key not in result, "duplicate-json-key")
            result[key] = child
        return result
    def reject_constant(_value):
        raise PairError("invalid-json-number")
    return json.loads(value, object_pairs_hook=unique, parse_constant=reject_constant)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def git(directory, *arguments, input=None, check=True, protocols="https:file", timeout=180, extra_env=None):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_") and key not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0",
                       GIT_OPTIONAL_LOCKS="0", GIT_ALLOW_PROTOCOL=protocols, LC_ALL="C")
    environment.update(extra_env or {})
    command = ["git", "--no-replace-objects", "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
    if directory is not None:
        command += ["-c", "safe.directory=" + str(directory), "-C", str(directory)]
    result = subprocess.run(command + list(arguments), input=input, capture_output=True,
                            env=environment, timeout=timeout)
    if check:
        require(result.returncode == 0, "git-" + arguments[0] + "-failed")
    return result


def ref_name(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value),
            "invalid-ref")
    require(git(None, "check-ref-format", "refs/heads/" + value, check=False).returncode == 0,
            "invalid-ref")
    return value


def inputs(number, head, base_ref, base, merge=None, require_merge=False):
    require(isinstance(number, str) and re.fullmatch(r"[1-9][0-9]*", number), "invalid-pr-number")
    require(isinstance(head, str) and SHA.fullmatch(head), "invalid-head")
    require(isinstance(base, str) and SHA.fullmatch(base), "invalid-base")
    require(not require_merge or isinstance(merge, str) and SHA.fullmatch(merge), "invalid-merge")
    require(merge in (None, "") or isinstance(merge, str) and SHA.fullmatch(merge), "invalid-merge")
    return {"pr_number": int(number), "head_sha": head, "base_ref": ref_name(base_ref),
            "base_sha": base, "merge_sha": merge or None}


class GitHub:
    def __init__(self, token=None, cli=None):
        self.token = token
        self.cli = cli

    def get(self, path):
        endpoint = "repos/" + REPOSITORY + "/" + path
        if self.cli:
            result = subprocess.run([self.cli, "api", endpoint], capture_output=True, timeout=90)
            require(result.returncode == 0, "github-read-failed")
            data = result.stdout
        else:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "sonic-exact-pr-pair-ci"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            request = Request("https://api.github.com/" + endpoint, headers=headers)
            for attempt in range(3):
                try:
                    with urlopen(request, timeout=30) as response:
                        data = response.read(2 * 1024 * 1024 + 1)
                    break
                except (HTTPError, URLError) as error:
                    retry = not isinstance(error, HTTPError) or error.code >= 500
                    if not retry or attempt == 2:
                        raise PairError("github-read-failed") from None
                    time.sleep(attempt + 1)
        require(len(data) <= 2 * 1024 * 1024, "github-response-too-large")
        value = load_json(data)
        require(isinstance(value, dict), "invalid-github-response")
        return value


def remote_state(api, pair):
    pull = api.get("pulls/" + str(pair["pr_number"]))
    require(pull.get("state") == "open", "pr-not-open")
    for role in ("head", "base"):
        repository = pull.get(role, {}).get("repo") or {}
        require(repository.get("full_name") == REPOSITORY and
                repository.get("owner", {}).get("login") == "securely1g", "wrong-pr-owner")
    require(pull["head"].get("sha") == pair["head_sha"], "head-moved")
    require(pull["base"].get("ref") == pair["base_ref"], "base-ref-moved")
    head_ref = ref_name(pull["head"].get("ref"))
    for role, name, expected in (("head", head_ref, pair["head_sha"]),
                                 ("base", pair["base_ref"], pair["base_sha"])):
        value = api.get("git/ref/heads/" + quote(name, safe=""))
        require(value.get("ref") == "refs/heads/" + name and
                value.get("object", {}).get("type") == "commit", "invalid-" + role + "-ref")
        require(value["object"].get("sha") == expected, role + "-moved")
    return {"observed_at": now(), "head_ref": head_ref, "head_sha": pair["head_sha"],
            "base_ref": pair["base_ref"], "base_sha": pair["base_sha"],
            "rest_base_sha": pull["base"].get("sha")}


def commit_details(repository, revision):
    value = git(repository, "cat-file", "commit", revision).stdout
    header = value.split(b"\n\n", 1)[0].decode("ascii")
    fields = header.splitlines()
    trees = [line.split(" ", 1)[1] for line in fields if line.startswith("tree ")]
    parents = [line.split(" ", 1)[1] for line in fields if line.startswith("parent ")]
    require(len(trees) == 1 and SHA.fullmatch(trees[0]) and all(SHA.fullmatch(x) for x in parents),
            "invalid-commit")
    return {"tree_sha": trees[0], "parents": parents}


def merge_message(pair, head_ref):
    return ("Validate " + REPOSITORY + " PR " + str(pair["pr_number"]) + "\n\n" +
            "Head: " + head_ref + "@" + pair["head_sha"] + "\n" +
            "Base: " + pair["base_ref"] + "@" + pair["base_sha"] + "\n").encode()


def resolve(pair, directory, api, remote=REMOTE):
    before = remote_state(api, pair)
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    git(None, "init", "--bare", "--template=", str(directory))
    refspecs = [
        "+refs/heads/" + pair["base_ref"] + ":refs/pair/base",
        "+refs/heads/" + before["head_ref"] + ":refs/pair/head",
        "+refs/pull/" + str(pair["pr_number"]) + "/head:refs/pair/pull-head",
    ]
    options = []
    if getattr(api, "cli", None):
        options = ["-c", "credential.helper=", "-c",
                   "credential.helper=!" + shlex.quote(api.cli) + " auth git-credential"]
    git(directory, *options, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--no-auto-gc", remote,
        *refspecs, timeout=600)
    require(git(directory, "rev-parse", "--is-shallow-repository").stdout.strip() == b"false",
            "incomplete-history")
    for name, expected in (("base", pair["base_sha"]), ("head", pair["head_sha"]),
                           ("pull-head", pair["head_sha"])):
        require(git(directory, "rev-parse", "refs/pair/" + name).stdout.decode().strip() == expected,
                name + "-moved")
    git(directory, "update-ref", "HEAD", pair["base_sha"])
    require(git(directory, "rev-parse", "HEAD").stdout.decode().strip() == pair["base_sha"],
            "wrong-merge-base-checkout")
    bases = git(directory, "merge-base", "--all", pair["base_sha"], pair["head_sha"]).stdout.decode().splitlines()
    require(bool(bases) and all(SHA.fullmatch(value) for value in bases), "no-merge-base")
    merged = git(directory, "merge-tree", "--write-tree", pair["base_sha"], pair["head_sha"], check=False)
    require(merged.returncode == 0, "merge-conflict")
    tree = merged.stdout.decode("ascii").splitlines()[0]
    require(SHA.fullmatch(tree), "invalid-merge-tree")
    identity_env = {"GIT_AUTHOR_NAME": "SONiC CI", "GIT_AUTHOR_EMAIL": "securely1g@gmail.com",
                    "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_NAME": "SONiC CI",
                    "GIT_COMMITTER_EMAIL": "securely1g@gmail.com", "GIT_COMMITTER_DATE": "@0 +0000"}
    merge = git(directory, "commit-tree", tree, "-p", pair["base_sha"], "-p", pair["head_sha"],
                input=merge_message(pair, before["head_ref"]), extra_env=identity_env).stdout.decode().strip()
    require(SHA.fullmatch(merge), "invalid-merge")
    details = commit_details(directory, merge)
    require(details == {"tree_sha": tree, "parents": [pair["base_sha"], pair["head_sha"]]},
            "wrong-merge-identity")
    require(pair["merge_sha"] is None or merge == pair["merge_sha"], "merge-differs-from-plan")
    git(directory, "update-ref", "refs/pair/merge", merge)
    after = remote_state(api, pair)
    require(after["head_ref"] == before["head_ref"], "head-ref-moved")
    return {"merge_sha": merge, "tree_sha": tree, "parents": details["parents"],
            "head_ref": before["head_ref"], "merge_bases": bases,
            "remote_before": before, "remote_after": after,
            "git_version": git(None, "--version").stdout.decode().strip()}


def real_directory(path):
    path = Path(path).absolute()
    require(path.is_dir() and not path.is_symlink() and path.resolve() == path, "unsafe-directory")
    return path


def write_json(path, value, mode=0o444):
    path = Path(path)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


def stage_validator(workspace, runner_temp, revision, output):
    workspace = real_directory(workspace)
    require(isinstance(revision, str) and SHA.fullmatch(revision), "invalid-validator-revision")
    require(git(workspace, "rev-parse", "--show-toplevel").stdout.decode().strip() == str(workspace),
            "wrong-workspace")
    require(git(workspace, "rev-parse", "HEAD").stdout.decode().strip() == revision, "validator-moved")
    require(not git(workspace, "status", "--porcelain=v1", "--untracked-files=all").stdout,
            "validator-workspace-dirty")
    directory = Path(tempfile.mkdtemp(prefix="sonic-exact-pair-", dir=real_directory(runner_temp)))
    hashes = {}
    for name, relative in SCRIPTS.items():
        source = workspace / relative
        require(source.is_file() and not source.is_symlink(), "validator-file-missing")
        value = git(workspace, "show", revision + ":" + relative).stdout
        require(source.read_bytes() == value, "validator-file-modified")
        (directory / name).write_bytes(value)
        (directory / name).chmod(0o500)
        hashes[name] = digest(value)
    receipt = directory / "checkout.json"
    output = Path(output)
    require(output.is_file() and not output.is_symlink(), "invalid-github-output")
    with output.open("a") as stream:
        stream.write("artifact_collector=" + str(directory / "public_artifacts.py") + "\n")
        stream.write("receipt=" + str(receipt) + "\n")
        stream.write("validator_revision=" + revision + "\n")
    return directory, receipt, hashes


def submodule_status(workspace):
    result = {}
    for line in git(workspace, "submodule", "status", "--recursive", protocols="https").stdout.decode().splitlines():
        require(line.startswith(" "), "submodule-checkout-mismatch")
        fields = line[1:].split(" ", 2)
        require(len(fields) >= 2 and SHA.fullmatch(fields[0]), "invalid-submodule-status")
        path = fields[1]
        parts = PurePosixPath(path).parts
        require(re.fullmatch(r"[A-Za-z0-9._/-]+", path) and not path.startswith("/") and
                str(PurePosixPath(path)) == path and all(part not in (".", "..", ".git") for part in parts),
                "invalid-submodule-path")
        require(len(fields) == 2 or fields[2].startswith("(") and fields[2].endswith(")"),
                "invalid-submodule-status")
        require(path not in result, "duplicate-submodule")
        result[path] = fields[0]
    return result


def submodules(workspace):
    git(workspace, "submodule", "sync", "--recursive", protocols="https", timeout=600)
    git(workspace, "submodule", "update", "--init", "--recursive", "--force", "--checkout",
        protocols="https", timeout=1800)
    return submodule_status(workspace)


def prepare(pair, workspace, runner_temp, revision, output, api, remote=REMOTE, with_submodules=False):
    require(pair["merge_sha"] is not None, "missing-planned-merge")
    workspace = real_directory(workspace)
    directory, receipt_path, hashes = stage_validator(workspace, runner_temp, revision, output)
    resolved = resolve(pair, directory / "source.git", api, remote)
    require(git(workspace, "rev-parse", "HEAD").stdout.decode().strip() == revision and
            not git(workspace, "status", "--porcelain=v1", "--untracked-files=all").stdout,
            "validator-workspace-moved")
    configured = git(workspace, "config", "--local", "--name-only", "--get-regexp",
                     r"^(filter\.|include\.|includeif\.)", check=False)
    require(configured.returncode in (0, 1) and not configured.stdout, "unsafe-local-git-config")
    git(workspace, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--no-auto-gc",
        str(directory / "source.git"), "refs/pair/merge:refs/sonic-ci/exact-pair-" + resolved["merge_sha"], protocols="file")
    git(workspace, "checkout", "--detach", "--force", resolved["merge_sha"], protocols="file")
    require(git(workspace, "rev-parse", "HEAD").stdout.decode().strip() == resolved["merge_sha"] and
            commit_details(workspace, "HEAD") == {"tree_sha": resolved["tree_sha"], "parents": resolved["parents"]},
            "source-checkout-mismatch")
    modules = submodules(workspace) if with_submodules else {}
    final = remote_state(api, pair)
    require(final["head_ref"] == resolved["head_ref"], "head-ref-moved")
    identity = {"repository": REPOSITORY, "pr_number": pair["pr_number"], "head_repository": REPOSITORY,
                "head_ref": resolved["head_ref"], "head_sha": pair["head_sha"],
                "base_ref": pair["base_ref"], "base_sha": pair["base_sha"],
                "merge_sha": resolved["merge_sha"], "tree_sha": resolved["tree_sha"],
                "parents": resolved["parents"], "validator_revision": revision, "validator_files": hashes}
    receipt = {"schema": 1, "status": "verified", "identity": identity,
               "identity_sha256": canonical(identity), "verified_at": now(),
               "git_version": resolved["git_version"], "submodules_verified_at_checkout": with_submodules,
               "submodules_at_checkout": modules,
               "remote_observations": [resolved["remote_before"], resolved["remote_after"], final]}
    write_json(receipt_path, receipt)
    with Path(output).open("a") as stream:
        stream.write("receipt_sha256=" + digest(receipt_path.read_bytes()) + "\n")
        stream.write("verified=true\n")
    return receipt


def validate_identity(identity, pair, revision):
    keys = {"repository", "pr_number", "head_repository", "head_ref", "head_sha", "base_ref",
            "base_sha", "merge_sha", "tree_sha", "parents", "validator_revision", "validator_files"}
    require(isinstance(identity, dict) and set(identity) == keys, "invalid-receipt-identity")
    require(identity["repository"] == REPOSITORY and identity["head_repository"] == REPOSITORY,
            "wrong-receipt-owner")
    require(all(identity[key] == pair[key] for key in ("pr_number", "head_sha", "base_ref", "base_sha", "merge_sha")),
            "receipt-input-mismatch")
    ref_name(identity["head_ref"])
    require(isinstance(identity["tree_sha"], str) and SHA.fullmatch(identity["tree_sha"]) and
            identity["parents"] == [pair["base_sha"], pair["head_sha"]],
            "receipt-merge-mismatch")
    require(identity["validator_revision"] == revision and isinstance(revision, str) and SHA.fullmatch(revision),
            "receipt-validator-mismatch")
    require(isinstance(identity["validator_files"], dict) and set(identity["validator_files"]) == set(SCRIPTS) and
            all(isinstance(value, str) and DIGEST.fullmatch(value) for value in identity["validator_files"].values()),
            "invalid-validator-files")


def verify_receipt(path, workspace, pair, revision, expected_digest):
    path = Path(path).absolute()
    require(path.is_file() and not path.is_symlink() and path.resolve() == path and path.stat().st_size <= 128 * 1024,
            "missing-checkout-receipt")
    raw = path.read_bytes()
    require(isinstance(expected_digest, str) and DIGEST.fullmatch(expected_digest) and
            digest(raw) == expected_digest, "checkout-receipt-digest-mismatch")
    value = load_json(raw)
    keys = {"schema", "status", "identity", "identity_sha256", "verified_at", "git_version",
            "submodules_verified_at_checkout", "submodules_at_checkout", "remote_observations"}
    require(isinstance(value, dict) and set(value) == keys and value["schema"] == 1 and value["status"] == "verified",
            "invalid-checkout-receipt")
    identity = value["identity"]
    validate_identity(identity, pair, revision)
    require(value["identity_sha256"] == canonical(identity), "receipt-identity-digest-mismatch")
    require(isinstance(value["verified_at"], str) and isinstance(value["git_version"], str) and
            isinstance(value["submodules_verified_at_checkout"], bool) and isinstance(value["submodules_at_checkout"], dict) and
            isinstance(value["remote_observations"], list) and len(value["remote_observations"]) == 3,
            "invalid-receipt-observations")
    observation_keys = {"observed_at", "head_ref", "head_sha", "base_ref", "base_sha", "rest_base_sha"}
    for observation in value["remote_observations"]:
        require(isinstance(observation, dict) and set(observation) == observation_keys and
                all(observation[key] == identity[key] for key in ("head_ref", "head_sha", "base_ref", "base_sha")) and
                isinstance(observation["observed_at"], str) and isinstance(observation["rest_base_sha"], str) and
                SHA.fullmatch(observation["rest_base_sha"]), "receipt-observation-mismatch")
    for path, sha in value["submodules_at_checkout"].items():
        require(isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9._/-]+", path) and
                not path.startswith("/") and str(PurePosixPath(path)) == path and
                all(part not in (".", "..", ".git") for part in PurePosixPath(path).parts) and
                isinstance(sha, str) and SHA.fullmatch(sha), "invalid-receipt-submodules")
    require(value["submodules_verified_at_checkout"] or not value["submodules_at_checkout"],
            "unexpected-receipt-submodules")
    workspace = real_directory(workspace)
    require(git(workspace, "rev-parse", "HEAD").stdout.decode().strip() == identity["merge_sha"] and
            commit_details(workspace, "HEAD") == {"tree_sha": identity["tree_sha"], "parents": identity["parents"]},
            "checkout-receipt-mismatch")
    for name, relative in SCRIPTS.items():
        staged = Path(__file__).resolve().with_name(name)
        require(staged.is_file() and not staged.is_symlink() and digest(staged.read_bytes()) == identity["validator_files"][name],
                "validator-file-mismatch")
        require(digest(git(workspace, "show", revision + ":" + relative).stdout) == identity["validator_files"][name],
                "validator-revision-file-mismatch")
    return {"schema": 1, "status": "verified", "identity": identity,
            "identity_sha256": value["identity_sha256"], "receipt_sha256": digest(raw),
            "verified_at": value["verified_at"],
            "submodules_verified_at_checkout": value["submodules_verified_at_checkout"],
            "submodule_count_at_checkout": len(value["submodules_at_checkout"]),
            "submodules_sha256_at_checkout": canonical(value["submodules_at_checkout"])}


def verify_projection(value, identity):
    keys = {"schema", "status", "identity", "identity_sha256", "receipt_sha256", "verified_at",
            "submodules_verified_at_checkout", "submodule_count_at_checkout", "submodules_sha256_at_checkout"}
    require(isinstance(value, dict) and set(value) == keys and value["schema"] == 1 and value["status"] == "verified",
            "invalid-checkout-projection")
    require(value["identity"] == identity and value["identity_sha256"] == canonical(identity),
            "checkout-projection-mismatch")
    require(all(isinstance(value[key], str) and DIGEST.fullmatch(value[key])
                for key in ("receipt_sha256", "submodules_sha256_at_checkout")), "invalid-projection-digest")
    require(isinstance(value["verified_at"], str) and isinstance(value["submodules_verified_at_checkout"], bool) and
            isinstance(value["submodule_count_at_checkout"], int) and not isinstance(value["submodule_count_at_checkout"], bool) and
            value["submodule_count_at_checkout"] >= 0, "invalid-checkout-projection")
    require(value["submodules_verified_at_checkout"] or
            value["submodule_count_at_checkout"] == 0 and value["submodules_sha256_at_checkout"] == canonical({}),
            "invalid-checkout-projection")
    return value


def arguments(parser, merge=False):
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-base-ref", required=True)
    parser.add_argument("--expected-base", required=True)
    parser.add_argument("--expected-merge", required=merge)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Verify refs and calculate the deterministic local merge without checking it out")
    arguments(plan)
    plan.add_argument("--temporary-directory", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--github-cli")
    checkout = commands.add_parser("prepare", help="Stage the validator and check out the planned merge")
    arguments(checkout, merge=True)
    checkout.add_argument("--workspace", type=Path, required=True)
    checkout.add_argument("--runner-temp", type=Path, required=True)
    checkout.add_argument("--validator-revision", required=True)
    checkout.add_argument("--github-output", type=Path, required=True)
    checkout.add_argument("--submodules", action="store_true")
    args = parser.parse_args()
    try:
        pair = inputs(args.pr_number, args.expected_head, args.expected_base_ref, args.expected_base,
                      args.expected_merge, require_merge=args.command == "prepare")
        api = GitHub(os.environ.get("GH_TOKEN"), getattr(args, "github_cli", None))
        if args.command == "plan":
            with tempfile.TemporaryDirectory(prefix="sonic-pair-plan-", dir=real_directory(args.temporary_directory)) as directory:
                result = resolve(pair, Path(directory) / "source.git", api)
            value = {"schema": 1, "status": "planned", "repository": REPOSITORY, "inputs": pair,
                     "result": result, "planner_script_sha256": digest(Path(__file__).read_bytes()),
                     "qualification": "Local merge planning only; no source checkout, build or source-PR check was executed."}
            write_json(args.output, value, mode=0o644)
            print(json.dumps({"status": "planned", "merge_sha": result["merge_sha"], "tree_sha": result["tree_sha"]}))
        else:
            result = prepare(pair, args.workspace, args.runner_temp, args.validator_revision,
                             args.github_output, api, with_submodules=args.submodules)
            print(json.dumps({"status": "verified", "identity_sha256": result["identity_sha256"],
                              "merge_sha": result["identity"]["merge_sha"]}))
    except PairError as error:
        parser.exit(1, "exact PR pair preparation failed: " + str(error) + "\n")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        parser.exit(1, "exact PR pair preparation failed\n")


if __name__ == "__main__":
    main()
