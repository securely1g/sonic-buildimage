#!/usr/bin/env python3
"""Collect public build metadata without executing builds or reading their logs."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

WORKFLOWS = ("bazel-swss-oci.yml", "bazel-oci.yml", "bazel.yml")
PHASES = ("setup", "build", "validation", "upload", "other")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_JOBS = {
    "Make VS with Bazel SWSS (AMD64)": "make-vs",
    "Bazel VS installer (AMD64)": "bazel-vs",
}
IMAGE_STEPS = {
    "Build the VS image with Bazel SWSS archives",
    "Build the Bazel VS installer",
    "Build Bazel VS installer",
    "Build and verify sonic-vs.bin with Bazel",
}


class CollectionError(RuntimeError):
    """Incomplete collection: keep the previously published history."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.year < 2000:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def seconds(item):
    """Elapsed concluded work, not time claimed for skipped/incomplete work."""
    if item.get("status") != "completed" or item.get("conclusion") in (None, "skipped"):
        return None
    start, end = timestamp(item.get("started_at")), timestamp(item.get("completed_at"))
    if not start or not end:
        return None
    elapsed = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    return elapsed if elapsed >= 0 else None


def phase(name):
    lower = name.lower()
    if lower.startswith(("post ", "remove ", "complete job")):
        return "other"
    if "upload" in lower:
        return "upload"
    if lower.startswith(("verify", "test", "validate", "check")) and not any(
        word in lower for word in ("prerequisite", "capacity", "nested docker", "checkout")
    ):
        return "validation"
    if lower.startswith(("build ", "compile ")):
        return "build"
    if lower.startswith(("set up", "setup", "checkout", "prepare", "download", "configure", "install", "check")):
        return "setup"
    return "other"


def job_kind(name):
    if name in IMAGE_JOBS:
        return IMAGE_JOBS[name]
    if name.startswith(("Container archive (", "Syncd OCI contracts (")):
        return "container"
    if name.startswith(("SWSS source layers (", "Bazel SWSS packages (", "Bazel protobuf headers (")):
        return "source"
    if name.startswith("Bazel checks ("):
        return "checks"
    return "other"


def normalize_job(raw, repo, run_id):
    job_id = integer(raw.get("id"))
    if job_id is None:
        raise CollectionError("Job response is missing a positive integer ID")
    name = str(raw.get("name") or "Unnamed job")
    kind = job_kind(name)
    steps = []
    for raw_step in raw.get("steps", []):
        step_name = str(raw_step.get("name") or "Unnamed step")
        steps.append({
            "number": integer(raw_step.get("number")), "name": step_name,
            "status": raw_step.get("status"), "conclusion": raw_step.get("conclusion"),
            "started_at": timestamp(raw_step.get("started_at")),
            "completed_at": timestamp(raw_step.get("completed_at")),
            "duration_seconds": seconds(raw_step), "phase": phase(step_name),
        })
    phases = {}
    for category in PHASES:
        durations = [step["duration_seconds"] for step in steps
                     if step["phase"] == category and step["duration_seconds"] is not None]
        phases[category] = sum(durations) if durations else None
    image_steps = [step for step in steps if kind in ("make-vs", "bazel-vs") and step["name"] in IMAGE_STEPS]
    elapsed = [step["duration_seconds"] for step in image_steps if step["duration_seconds"] is not None]
    success = [step["duration_seconds"] for step in image_steps
               if step["conclusion"] == "success" and step["duration_seconds"] is not None]
    # A partial result from one of several image-build steps is not a full build.
    complete_image = bool(image_steps) and len(success) == len(image_steps)
    return {
        "job_id": job_id, "name": name, "kind": kind,
        "architecture": "amd64" if "(AMD64)" in name else "arm64" if "(ARM64)" in name else None,
        "status": raw.get("status"), "conclusion": raw.get("conclusion"),
        "started_at": timestamp(raw.get("started_at")), "completed_at": timestamp(raw.get("completed_at")),
        "duration_seconds": seconds(raw), "image_build_seconds": sum(success) if complete_image else None,
        "image_build_elapsed_seconds": sum(elapsed) if elapsed else None,
        "phases": phases, "steps": steps,
        "url": f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}",
    }


class GitHub:
    def __init__(self, executable="gh", retries=4):
        self.executable = executable
        self.retries = retries

    def get(self, endpoint):
        for attempt in range(self.retries):
            try:
                result = subprocess.run(
                    [self.executable, "api", "--method", "GET", endpoint],
                    text=True, capture_output=True,
                    timeout=30 if "/commits/" in endpoint and "/pulls" in endpoint else 90,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                if attempt + 1 == self.retries or isinstance(error, OSError):
                    raise CollectionError(f"GitHub request could not execute: {endpoint}") from error
                time.sleep(2 ** attempt)
                continue
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout)
                except ValueError as error:
                    raise CollectionError(f"GitHub returned invalid JSON: {endpoint}") from error
            http_error = re.search(r"HTTP (\d{3})", result.stderr)
            status = int(http_error.group(1)) if http_error else None
            # gh also reports transport failures (EOF, connection reset, timeout)
            # without an HTTP response. Retry those reads with the same bound.
            transient = status is None or status == 429 or status >= 500 or "rate limit" in result.stderr.lower()
            code = f"HTTP {status}" if status is not None else f"CLI exit {result.returncode}; no HTTP response"
            if not transient or attempt + 1 == self.retries:
                # Never put command output, tokens, or response bodies in public data.
                raise CollectionError(f"GitHub request failed ({code}): {endpoint}", http_status=status)
            print(f"Retrying metadata read after {code} (attempt {attempt + 1}/{self.retries}): {endpoint}", file=sys.stderr)
            time.sleep(min(30, 5 * 2 ** attempt))
        raise CollectionError(f"GitHub request failed: {endpoint}")

    def pages(self, endpoint, key=None):
        results = []
        separator = "&" if "?" in endpoint else "?"
        page = 1
        while True:
            response = self.get(f"{endpoint}{separator}per_page=100&page={page}")
            items = response.get(key) if key and isinstance(response, dict) else response
            if not isinstance(items, list):
                raise CollectionError(f"GitHub returned an invalid paginated response: {endpoint}")
            results.extend(items)
            if len(items) < 100:
                return results
            page += 1


def normalize_pr(raw, repo):
    number = integer(raw.get("number"))
    if not number:
        raise CollectionError("Pull-request response is missing a positive integer number")
    return {
        "number": number, "title": str(raw.get("title") or ""),
        "state": raw.get("state"), "draft": bool(raw.get("draft")),
        "url": f"https://github.com/{repo}/pull/{number}",
        "head_sha": raw.get("head", {}).get("sha"), "base_sha": raw.get("base", {}).get("sha"),
        "head_branch": raw.get("head", {}).get("ref"),
        "updated_at": timestamp(raw.get("updated_at")), "merged_at": timestamp(raw.get("merged_at")),
    }


class Associations:
    def __init__(self, api, repo, default_branch, pulls):
        self.api, self.repo, self.default_branch = api, repo, default_branch
        self.pulls = {pr["number"]: pr for pr in pulls}
        self.verified = {}
        self.notes = {}

    def same_repository(self, pr):
        return (pr.get("head", {}).get("repo") or {}).get("full_name", "").lower() == self.repo.lower()

    def identify(self, raw):
        direct = sorted({pr["number"] for pr in raw.get("pull_requests", [])
                         if integer(pr.get("number")) in self.pulls})
        if direct:
            return direct, "github"
        branch, sha = raw.get("head_branch"), raw.get("head_sha")
        source_repo = (raw.get("head_repository") or {}).get("full_name")
        if (not branch or branch == self.default_branch or not isinstance(sha, str)
                or not SHA_RE.fullmatch(sha) or (source_repo and source_repo.lower() != self.repo.lower())):
            return [], "none"
        candidates = [pr for pr in self.pulls.values()
                      if self.same_repository(pr) and pr.get("head", {}).get("ref") == branch]
        if len(candidates) != 1:
            return [], "none"
        pr = candidates[0]
        if pr.get("head", {}).get("sha") == sha:
            return [pr["number"]], "commit"
        if sha not in self.verified:
            try:
                linked = self.api.pages(f"repos/{self.repo}/commits/{sha}/pulls")
            except CollectionError as error:
                # PR inference is optional: never discard valid timing metadata
                # because an old commit lookup is unavailable. Main APIs remain
                # all-or-nothing. Transient inference failures retry next refresh.
                if error.http_status in (404, 422):
                    note = f"PR association unavailable: commit lookup returned HTTP {error.http_status}"
                else:
                    reason = f"HTTP {error.http_status}" if error.http_status else "transport or CLI error"
                    note = f"PR association temporarily unavailable: commit lookup failed ({reason})"
                self.notes[sha] = note
                print(f"{note}: {error}", file=sys.stderr)
                linked = []
            self.verified[sha] = {item.get("number") for item in linked
                                  if self.same_repository(item) and integer(item.get("number"))}
        if pr["number"] in self.verified[sha]:
            return [pr["number"]], "branch"
        return [], "none"


def normalize_run(raw, workflow, repo, jobs, association, association_note=None):
    run_id, attempt = integer(raw.get("id")), integer(raw.get("run_attempt"))
    if not run_id or not attempt:
        raise CollectionError("Run response is missing its ID or attempt")
    pr_numbers, method = association
    completed = [job["completed_at"] for job in jobs if job["completed_at"]]
    return {
        "run_id": run_id, "attempt": attempt,
        "workflow_name": str(raw.get("name") or workflow),
        "workflow_path": f".github/workflows/{workflow}",
        "source_sha": raw.get("head_sha"), "branch": raw.get("head_branch"),
        "event": raw.get("event"), "status": raw.get("status"), "conclusion": raw.get("conclusion"),
        "pr_numbers": pr_numbers, "pr_association": method, "pr_association_note": association_note,
        "started_at": timestamp(raw.get("run_started_at") or raw.get("created_at")),
        "completed_at": max(completed) if raw.get("status") == "completed" and completed else None,
        "updated_at": timestamp(raw.get("updated_at")),
        "url": f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}", "jobs": jobs,
    }


def load_history(path, repo):
    if path is None or not path.exists():
        return {"schema_version": 1, "repository": repo, "pull_requests": [], "runs": []}
    try:
        history = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CollectionError(f"Cannot read dashboard history: {path}") from error
    if (not isinstance(history, dict) or history.get("schema_version") != 1
            or history.get("repository") != repo or not isinstance(history.get("runs"), list)
            or not isinstance(history.get("pull_requests"), list)):
        raise CollectionError("Dashboard history schema or repository does not match")
    allowed = {
        "document": {"schema_version", "repository", "generated_at", "pull_requests", "runs", "comparisons"},
        "pr": {"number", "title", "state", "draft", "url", "head_sha", "base_sha", "head_branch", "updated_at", "merged_at"},
        "run": {"run_id", "attempt", "workflow_name", "workflow_path", "source_sha", "branch", "event", "status", "conclusion", "pr_numbers", "pr_association", "pr_association_note", "started_at", "completed_at", "updated_at", "url", "jobs"},
        "job": {"job_id", "name", "kind", "architecture", "status", "conclusion", "started_at", "completed_at", "duration_seconds", "image_build_seconds", "image_build_elapsed_seconds", "phases", "steps", "url"},
        "step": {"number", "name", "status", "conclusion", "started_at", "completed_at", "duration_seconds", "phase"},
    }
    def check_fields(value, kind):
        if not isinstance(value, dict) or set(value) - allowed[kind]:
            raise CollectionError(f"Dashboard history contains invalid {kind} metadata")

    check_fields(history, "document")
    for pr in history["pull_requests"]:
        check_fields(pr, "pr")
        if not integer(pr.get("number")):
            raise CollectionError("Dashboard history has an invalid PR number")
    keys = set()
    for run in history["runs"]:
        check_fields(run, "run")
        if not isinstance(run.get("jobs"), list) or not isinstance(run.get("pr_numbers"), list):
            raise CollectionError("Dashboard history is missing its job or PR list")
        for job in run["jobs"]:
            check_fields(job, "job")
            if (not integer(job.get("job_id")) or not isinstance(job.get("steps"), list)
                    or not isinstance(job.get("phases"), dict) or set(job["phases"]) - set(PHASES)):
                raise CollectionError("Dashboard history contains invalid job details")
            for step in job["steps"]:
                check_fields(step, "step")
        key = (integer(run.get("run_id")), integer(run.get("attempt")))
        if None in key or key in keys or run.get("workflow_path") not in {f".github/workflows/{w}" for w in WORKFLOWS}:
            raise CollectionError("Dashboard history has invalid or duplicate run attempts")
        keys.add(key)
    return history


def collect(api, repo, history, run_id=None, backfill=False, workers=4):
    repository = api.get(f"repos/{repo}")
    if repository.get("private") is not False or repository.get("full_name", "").lower() != repo.lower():
        raise CollectionError("Dashboard collection requires the requested public repository")
    pulls = api.pages(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc")
    associations = Associations(api, repo, repository.get("default_branch"), pulls)
    historical_prs = {pr["number"]: pr for pr in history["pull_requests"]}
    historical_prs.update({pr["number"]: normalize_pr(pr, repo) for pr in pulls})
    retained = {(run["run_id"], run["attempt"]): run for run in history["runs"]}
    latest = []
    if run_id and not backfill:
        raw = api.get(f"repos/{repo}/actions/runs/{run_id}")
        workflow = str(raw.get("path", "")).split("@", 1)[0].rsplit("/", 1)[-1]
        if workflow not in WORKFLOWS:
            raise CollectionError("Requested workflow run is not allowlisted")
        latest.append((workflow, raw))
    else:
        for workflow in WORKFLOWS:
            latest.extend((workflow, raw) for raw in api.pages(
                f"repos/{repo}/actions/workflows/{workflow}/runs", "workflow_runs"))
    pending = []
    for workflow, latest_raw in latest:
        current_id, current_attempt = integer(latest_raw.get("id")), integer(latest_raw.get("run_attempt"))
        if not current_id or not current_attempt:
            raise CollectionError("Run-list response is missing its ID or attempt")
        for attempt in range(1, current_attempt + 1):
            old = retained.get((current_id, attempt))
            if not backfill and old and old.get("status") == "completed" and old.get("conclusion") is not None:
                if attempt < current_attempt or (
                    old.get("updated_at") == timestamp(latest_raw.get("updated_at"))
                    and old.get("conclusion") == latest_raw.get("conclusion")
                    and latest_raw.get("status") == "completed"
                ):
                    if str(old.get("pr_association_note") or "").startswith("PR association temporarily unavailable:"):
                        retry_raw = {"head_branch": old.get("branch"), "head_sha": old.get("source_sha"),
                                     "head_repository": {"full_name": repo}, "pull_requests": []}
                        numbers, method = associations.identify(retry_raw)
                        retained[(current_id, attempt)] = dict(old, pr_numbers=numbers, pr_association=method,
                            pr_association_note=associations.notes.get(old.get("source_sha")))
                    continue
            raw = latest_raw if attempt == current_attempt else api.get(
                f"repos/{repo}/actions/runs/{current_id}/attempts/{attempt}")
            if raw.get("id") != current_id or raw.get("run_attempt") != attempt:
                raise CollectionError("Run-attempt response does not match the requested identity")
            association = associations.identify(raw)
            pending.append((workflow, raw, association, associations.notes.get(raw.get("head_sha"))))

    print(f"Fetching jobs for {len(pending)} new, changed, or explicitly refreshed run attempts", file=sys.stderr)

    def fetch(item):
        workflow, raw, association, association_note = item
        raw_jobs = api.pages(f"repos/{repo}/actions/runs/{raw['id']}/attempts/{raw['run_attempt']}/jobs", "jobs")
        jobs = [normalize_job(job, repo, raw["id"]) for job in raw_jobs]
        if len({job["job_id"] for job in jobs}) != len(jobs):
            raise CollectionError("Jobs endpoint returned duplicate IDs across pages")
        return normalize_run(raw, workflow, repo, jobs, association, association_note)

    # Only read-only job requests are concurrent. All must succeed before output.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for run in pool.map(fetch, pending):
            retained[(run["run_id"], run["attempt"])] = run
    return {
        "schema_version": 1, "repository": repo,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pull_requests": sorted(historical_prs.values(), key=lambda pr: pr["number"], reverse=True),
        "runs": sorted(retained.values(), key=lambda run: (run["run_id"], run["attempt"]), reverse=True),
        "comparisons": [],
    }


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".history-", suffix=".json", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="securely1g/sonic-buildimage")
    parser.add_argument("--github-cli", default="gh")
    parser.add_argument("--history", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--backfill", action="store_true", help="Re-read all available allowlisted run attempts, including completed history")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    args = parser.parse_args()
    if not REPOSITORY_RE.fullmatch(args.repo) or args.repo.split("/")[-1] in (".", ".."):
        parser.error("--repo must be an owner/repository name")
    if args.run_id is not None and args.run_id < 1:
        parser.error("--run-id must be positive")
    try:
        history = load_history(args.history if args.history is not None else args.output, args.repo)
        result = collect(GitHub(args.github_cli), args.repo, history, args.run_id, args.backfill, args.workers)
        write_atomic(args.output, result)
    except CollectionError as error:
        print(f"Collection failed; published history was not replaced: {error}", file=sys.stderr)
        return 1
    print(f"Collected {len(result['runs'])} run attempts and {len(result['pull_requests'])} PRs into {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
