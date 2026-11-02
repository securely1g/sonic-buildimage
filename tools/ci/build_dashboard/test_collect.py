"""Behavioral regression tests for metadata collection and timing semantics."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("collect", Path(__file__).with_name("collect.py"))
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
REPO = "securely1g/sonic-buildimage"
SHA = "a" * 40
OTHER_SHA = "b" * 40
START = "2026-10-10T00:00:00Z"
END = "2026-10-10T00:10:00Z"


def pr(number=1, branch="feature", sha=SHA, repo=REPO):
    return {"number": number, "title": "A public change", "state": "open", "draft": True,
            "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo}},
            "base": {"sha": OTHER_SHA}, "updated_at": END, "merged_at": None}


def run(attempt=1, run_id=10, status="completed", conclusion="success", **extra):
    data = {"id": run_id, "run_attempt": attempt, "head_branch": "feature", "head_sha": SHA,
            "head_repository": {"full_name": REPO}, "path": ".github/workflows/bazel-swss-oci.yml",
            "name": "Bazel SWSS OCI", "pull_requests": [{"number": 1}], "event": "pull_request",
            "status": status, "conclusion": conclusion, "run_started_at": START, "updated_at": END}
    data.update(extra)
    return data


def job(conclusion="success", status="completed"):
    return {"id": 99, "name": "Make VS with Bazel SWSS (AMD64)", "status": status,
            "conclusion": conclusion, "started_at": START, "completed_at": END,
            "runner_name": "private-machine-do-not-publish", "runner_id": 55,
            "steps": [{"number": 1, "name": "Build the VS image with Bazel SWSS archives",
                       "status": status, "conclusion": conclusion, "started_at": START, "completed_at": END}]}


class FakeAPI:
    def __init__(self, latest=None, pulls=None):
        self.latest = latest if latest is not None else [run()]
        self.pulls = pulls if pulls is not None else [pr()]
        self.calls = []
        self.attempts = {}
        self.jobs = {}
        self.linked = []
        self.fail_jobs = False
        self.logs = {}
        self.log_calls = []

    def job_log(self, repo, job_id):
        self.log_calls.append((repo, job_id))
        value = self.logs.get(job_id, collector.LogReadError("logs-expired"))
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, endpoint):
        self.calls.append(endpoint)
        if endpoint == f"repos/{REPO}":
            return {"full_name": REPO, "private": False, "default_branch": "master"}
        if endpoint in self.attempts:
            return self.attempts[endpoint]
        if endpoint.startswith(f"repos/{REPO}/actions/runs/"):
            run_id = int(endpoint.split("/")[-1])
            return next(item for item in self.latest if item["id"] == run_id)
        raise AssertionError(endpoint)

    def pages(self, endpoint, key=None):
        self.calls.append(endpoint)
        if "/pulls?state=" in endpoint:
            return self.pulls
        if "/workflows/" in endpoint:
            return self.latest if "bazel-swss-oci.yml" in endpoint else []
        if "/commits/" in endpoint:
            return self.linked
        if endpoint.endswith("/jobs"):
            if self.fail_jobs:
                raise collector.CollectionError("An API read failed")
            return self.jobs.get(endpoint, [job()])
        raise AssertionError(endpoint)


def empty():
    return {"schema_version": 1, "repository": REPO, "pull_requests": [], "runs": []}


def scope_manifest():
    return {
        "schema_version": 1, "collection_id": "1" * 32, "collection_sha256": "2" * 64, "repository": REPO,
        "pull_requests": [{"id": "PR_test", "number": 1, "state": "OPEN", "draft": True,
                           "head_repository": REPO, "head_ref": "feature", "head_sha": SHA,
                           "base_ref": "master", "base_sha": OTHER_SHA}],
        "runs": [{"run_id": 10, "attempt": 1, "pr_number": 1,
                  "workflow_path": ".github/workflows/bazel-swss-oci.yml"}],
    }


def scoped_pr_response(entry):
    base = {"id": "R_base", "databaseId": 100, "nameWithOwner": REPO}
    head = base if entry["head_repository"] == REPO else {
        "id": "R_fork", "databaseId": 101, "nameWithOwner": entry["head_repository"]}
    return {**base, "isPrivate": False, "pullRequest": {
        "id": entry["id"], "number": entry["number"], "title": "Fresh selected PR",
        "state": entry["state"], "isDraft": entry["draft"], "headRefOid": entry["head_sha"],
        "headRefName": entry["head_ref"], "baseRefName": entry["base_ref"],
        "headRef": {"target": {"oid": entry["head_sha"]}} if entry["state"] == "OPEN" else None,
        "baseRef": {"target": {"oid": entry["base_sha"]}}, "baseRepository": base, "headRepository": head,
        "updatedAt": END, "mergedAt": END if entry["state"] == "MERGED" else None,
    }}


def scoped_run_response(entry, selected_pr):
    head_id = 100 if selected_pr["head_repository"] == REPO else 101
    return run(attempt=entry["attempt"], run_id=entry["run_id"], head_branch=selected_pr["head_ref"],
               head_sha=selected_pr["head_sha"], path=entry["workflow_path"],
               repository={"id": 100, "full_name": REPO},
               head_repository={"id": head_id, "full_name": selected_pr["head_repository"]},
               pull_requests=[{"number": entry["pr_number"],
                               "head": {"sha": selected_pr["head_sha"], "ref": selected_pr["head_ref"], "repo": {"id": head_id}},
                               "base": {"sha": selected_pr["base_sha"], "ref": selected_pr["base_ref"], "repo": {"id": 100}}}])


class ScopedAPI:
    """Mock that rejects every endpoint outside the exact manifest selection."""
    def __init__(self, manifest):
        self.calls = []
        self.pr_responses, self.run_responses, self.jobs, self.job_pages = {}, {}, {}, {}
        pulls = {entry["number"]: entry for entry in manifest["pull_requests"]}
        for number, entry in pulls.items():
            value = scoped_pr_response(entry)
            self.pr_responses[number] = [copy.deepcopy(value), copy.deepcopy(value)]
        for entry in manifest["runs"]:
            value = scoped_run_response(entry, pulls[entry["pr_number"]])
            endpoint = f"repos/{REPO}/actions/runs/{entry['run_id']}"
            self.run_responses[endpoint] = [copy.deepcopy(value), copy.deepcopy(value)]
            selected_job = job()
            selected_job.update(run_id=entry["run_id"], run_attempt=entry["attempt"], head_sha=pulls[entry["pr_number"]]["head_sha"])
            self.jobs[endpoint + f"/attempts/{entry['attempt']}/jobs"] = [selected_job]

    def exact_pr(self, repo, number):
        self.calls.append(f"pr:{number}")
        if repo != REPO or number not in self.pr_responses:
            raise AssertionError((repo, number))
        values = self.pr_responses[number]
        return copy.deepcopy(values.pop(0) if len(values) > 1 else values[0])

    def get(self, endpoint):
        self.calls.append(endpoint)
        if endpoint in self.run_responses:
            values = self.run_responses[endpoint]
            return copy.deepcopy(values.pop(0) if len(values) > 1 else values[0])
        if endpoint in self.job_pages:
            value = self.job_pages[endpoint]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        for base, jobs in self.jobs.items():
            prefix = base + "?per_page=100&page="
            if endpoint.startswith(prefix) and endpoint[len(prefix):].isdigit():
                page = int(endpoint[len(prefix):])
                return {"total_count": len(jobs), "jobs": copy.deepcopy(jobs[(page - 1) * 100:page * 100])}
        raise AssertionError(endpoint)

    def pages(self, endpoint, key=None):
        self.calls.append(endpoint)
        raise AssertionError((endpoint, key))

    def job_log(self, repo, job_id):
        self.calls.append(f"log:{job_id}")
        if repo != REPO or job_id != 99:
            raise AssertionError((repo, job_id))
        raise collector.LogReadError("logs-expired")


def scoped_history():
    selected = collector.normalize_run(run(), "bazel-swss-oci.yml", REPO,
        [collector.normalize_job(job(), REPO, 10)], ([1], "github"))
    other_job = job()
    other_job["id"] = 199
    other_job = collector.normalize_job(other_job, REPO, 11)
    # A retained older classification must not be recalculated outside scope.
    other_job["kind"] = "other"
    other = collector.normalize_run(run(run_id=11, head_sha=OTHER_SHA, head_branch="other"),
        "bazel-swss-oci.yml", REPO, [other_job], ([2], "github"))
    return {"schema_version": 1, "repository": REPO, "generated_at": START,
            "pull_requests": [collector.normalize_pr(pr(), REPO), collector.normalize_pr(pr(2, "other", OTHER_SHA), REPO)],
            "runs": [selected, other], "comparisons": []}


class TimingTests(unittest.TestCase):
    def test_skipped_and_incomplete_are_not_zero(self):
        for conclusion, status in [("skipped", "completed"), (None, "queued"), (None, "completed")]:
            with self.subTest(conclusion=conclusion):
                normalized = collector.normalize_job(job(conclusion, status), REPO, 10)
                self.assertIsNone(normalized["duration_seconds"])
                self.assertIsNone(normalized["image_build_seconds"])
                self.assertIsNone(normalized["phases"]["build"])

    def test_invalid_and_reversed_timestamps(self):
        for start, end in [(END, START), (None, END), ("bad", END), ("0001-01-01T00:00:00Z", END),
                           ("2026-10-10T00:00:00", END)]:
            value = job()
            value.update(started_at=start, completed_at=end)
            self.assertIsNone(collector.seconds(value))
        value = job()
        value["completed_at"] = START
        self.assertEqual(collector.seconds(value), 0)

    def test_failed_build_elapsed_is_not_successful_build(self):
        normalized = collector.normalize_job(job("failure"), REPO, 10)
        self.assertEqual(normalized["duration_seconds"], 600)
        self.assertEqual(normalized["image_build_elapsed_seconds"], 600)
        self.assertIsNone(normalized["image_build_seconds"])
        self.assertEqual(normalized["phases"]["build"], 600)

    def test_only_explicit_image_jobs_and_steps_count(self):
        normalized = collector.normalize_job(job(), REPO, 10)
        self.assertEqual(normalized["image_build_seconds"], 600)
        value = job()
        value["name"] = "SWSS source layers (AMD64)"
        self.assertIsNone(collector.normalize_job(value, REPO, 10)["image_build_seconds"])
        value["name"] = "Bazel VS installer (AMD64)"
        value["steps"][0]["name"] = "Build and verify sonic-vs.bin with Bazel"
        self.assertEqual(collector.normalize_job(value, REPO, 10)["image_build_seconds"], 600)
        value["name"] = "Something containing VS"
        self.assertEqual(collector.job_kind(value["name"]), "other")

    def test_combined_make_image_step_counts(self):
        value = job()
        value["steps"][0]["name"] = "Build the VS image and both OCI container variants"
        self.assertEqual(collector.normalize_job(value, REPO, 10)["image_build_seconds"], 600)

    def test_fields_and_urls_are_allowlisted(self):
        value = job()
        value["html_url"] = "https://malicious.invalid"
        normalized = collector.normalize_job(value, REPO, 10)
        self.assertNotIn("runner", json.dumps(normalized))
        self.assertNotIn("private-machine", json.dumps(normalized))
        self.assertEqual(normalized["url"], f"https://github.com/{REPO}/actions/runs/10/job/99")


class AssociationTests(unittest.TestCase):
    def identify(self, value, pulls, linked=()):
        api = FakeAPI(pulls=pulls)
        api.linked = list(linked)
        return collector.Associations(api, REPO, "master", pulls).identify(value)

    def test_direct_association_keeps_historical_sha(self):
        api = FakeAPI(pulls=[pr(sha=OTHER_SHA)])
        result = collector.collect(api, REPO, empty())
        self.assertEqual(result["runs"][0]["source_sha"], SHA)
        self.assertEqual(result["pull_requests"][0]["head_sha"], OTHER_SHA)
        self.assertEqual(result["runs"][0]["pr_association"], "github")
        self.assertEqual(result["comparisons"], [])

    def test_unique_current_head_can_be_inferred(self):
        value = run(pull_requests=[])
        self.assertEqual(self.identify(value, [pr()]), ([1], "commit"))

    def test_branch_requires_commit_membership(self):
        value = run(pull_requests=[])
        current = pr(sha=OTHER_SHA)
        self.assertEqual(self.identify(value, [current]), ([], "none"))
        self.assertEqual(self.identify(value, [current], [current]), ([1], "branch"))

    def test_missing_historical_commit_keeps_run_and_explains_absent_link(self):
        api = FakeAPI(latest=[run(pull_requests=[])], pulls=[pr(sha=OTHER_SHA)])
        original = api.pages
        def pages(endpoint, key=None):
            if "/commits/" in endpoint:
                raise collector.CollectionError("Commit is missing", http_status=404)
            return original(endpoint, key)
        api.pages = pages
        result = collector.collect(api, REPO, empty())
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["pr_numbers"], [])
        self.assertIn("HTTP 404", result["runs"][0]["pr_association_note"])
        self.assertEqual(result["runs"][0]["jobs"][0]["image_build_seconds"], 600)

    def test_optional_inference_failure_is_retried_without_refetching_cached_jobs(self):
        api = FakeAPI(latest=[run(pull_requests=[])], pulls=[pr(sha=OTHER_SHA)])
        original = api.pages
        def pages(endpoint, key=None):
            if "/commits/" in endpoint:
                raise collector.CollectionError("Transport unavailable")
            return original(endpoint, key)
        api.pages = pages
        initial = collector.collect(api, REPO, empty())
        self.assertEqual(len(initial["runs"]), 1)
        self.assertIn("temporarily unavailable", initial["runs"][0]["pr_association_note"])
        retry = FakeAPI(latest=api.latest, pulls=api.pulls)
        retry.linked = api.pulls
        result = collector.collect(retry, REPO, initial)
        self.assertEqual(result["runs"][0]["pr_numbers"], [1])
        self.assertEqual(result["runs"][0]["pr_association"], "branch")
        self.assertIsNone(result["runs"][0]["pr_association_note"])
        self.assertFalse(any(endpoint.endswith("/jobs") for endpoint in retry.calls))

    def test_reused_branch_fork_or_master_are_not_inferred(self):
        value = run(pull_requests=[])
        self.assertEqual(self.identify(value, [pr(), pr(number=2)]), ([], "none"))
        self.assertEqual(self.identify(value, [pr(repo="someone/sonic-buildimage")]), ([], "none"))
        self.assertEqual(self.identify(run(pull_requests=[], head_branch="master"), [pr(branch="master")]), ([], "none"))
        self.assertEqual(self.identify(run(pull_requests=[], head_repository={"full_name": "someone/fork"}), [pr()]), ([], "none"))


class CollectionTests(unittest.TestCase):
    def test_new_attempt_preserves_prior_and_collects_all(self):
        api = FakeAPI(latest=[run(attempt=3)])
        api.attempts[f"repos/{REPO}/actions/runs/10/attempts/1"] = run(conclusion="failure")
        api.attempts[f"repos/{REPO}/actions/runs/10/attempts/2"] = run(attempt=2, conclusion="cancelled")
        result = collector.collect(api, REPO, empty())
        self.assertEqual([r["attempt"] for r in result["runs"]], [3, 2, 1])
        self.assertEqual([r["conclusion"] for r in result["runs"]], ["success", "cancelled", "failure"])
        self.assertEqual(sum(call.endswith("/jobs") for call in api.calls), 3)

    def test_terminal_cached_jobs_not_refetched_but_changed_run_is(self):
        initial = collector.collect(FakeAPI(), REPO, empty())
        cached = FakeAPI()
        result = collector.collect(cached, REPO, initial)
        self.assertEqual(result["runs"], initial["runs"])
        self.assertFalse(any(call.endswith("/jobs") for call in cached.calls))
        changed = FakeAPI(latest=[run(updated_at="2026-10-10T00:11:00Z")])
        collector.collect(changed, REPO, initial)
        self.assertTrue(any(call.endswith("/jobs") for call in changed.calls))

    def test_backfill_refetches_completed_attempts(self):
        initial = collector.collect(FakeAPI(), REPO, empty())
        api = FakeAPI(latest=[run(attempt=2)])
        api.attempts[f"repos/{REPO}/actions/runs/10/attempts/1"] = run()
        collector.collect(api, REPO, initial, backfill=True)
        self.assertEqual(sum(call.endswith("/jobs") for call in api.calls), 2)

    def test_nonterminal_always_refetches_and_expired_history_is_retained(self):
        api = FakeAPI(latest=[run(status="in_progress", conclusion=None)])
        initial = collector.collect(api, REPO, empty())
        repeated = FakeAPI(latest=api.latest)
        collector.collect(repeated, REPO, initial)
        self.assertTrue(any(call.endswith("/jobs") for call in repeated.calls))
        expired = collector.collect(FakeAPI(latest=[], pulls=[]), REPO, initial)
        self.assertEqual(expired["runs"], initial["runs"])
        self.assertEqual(expired["pull_requests"], initial["pull_requests"])

    def test_event_refetches_one_run_and_preserves_other_history(self):
        initial = collector.collect(FakeAPI(latest=[run(run_id=11)]), REPO, empty())
        api = FakeAPI()
        result = collector.collect(api, REPO, initial, run_id=10)
        self.assertEqual({r["run_id"] for r in result["runs"]}, {10, 11})
        self.assertFalse(any("/workflows/" in call for call in api.calls))

    def test_api_failure_never_returns_partial_success(self):
        api = FakeAPI()
        api.fail_jobs = True
        with self.assertRaises(collector.CollectionError):
            collector.collect(api, REPO, empty())

    def test_wrong_workflow_or_attempt_identity_fails(self):
        api = FakeAPI(latest=[run(path=".github/workflows/untrusted.yml")])
        with self.assertRaises(collector.CollectionError):
            collector.collect(api, REPO, empty(), run_id=10)
        api = FakeAPI(latest=[run(attempt=2)])
        api.attempts[f"repos/{REPO}/actions/runs/10/attempts/1"] = run(run_id=999)
        with self.assertRaises(collector.CollectionError):
            collector.collect(api, REPO, empty())

    def test_duplicate_jobs_fail_collection(self):
        api = FakeAPI()
        api.jobs[f"repos/{REPO}/actions/runs/10/attempts/1/jobs"] = [job(), job()]
        with self.assertRaises(collector.CollectionError):
            collector.collect(api, REPO, empty())


class ScopedCollectionTests(unittest.TestCase):
    def invoke_cli(self, api, mode_args, environment=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(json.dumps(scoped_history()))
            before = path.read_bytes()
            argv = ["collect.py", "--repo", REPO, "--history", str(path), "--output", str(path),
                    "--log-jobs", "0", *mode_args]
            with mock.patch.object(collector.sys, "argv", argv), mock.patch.object(collector, "GitHub", return_value=api) as factory, \
                    mock.patch.dict(os.environ, environment or {}, clear=True):
                code = collector.main()
            return code, before, path.read_bytes(), factory.call_count

    def test_exact_scope_preserves_unselected_history_and_freshness(self):
        manifest, history = scope_manifest(), scoped_history()
        before = copy.deepcopy(history)
        api = ScopedAPI(manifest)
        result = collector.collect_scoped(api, REPO, history, manifest, log_jobs=1)
        self.assertEqual(api.calls, [
            "pr:1", f"repos/{REPO}/actions/runs/10", f"repos/{REPO}/actions/runs/10/attempts/1/jobs?per_page=100&page=1",
            "log:99", "pr:1", f"repos/{REPO}/actions/runs/10",
        ])
        self.assertEqual(history, before)
        self.assertEqual(result["pull_requests"][1], before["pull_requests"][1])
        self.assertEqual(result["runs"][1], before["runs"][1])
        self.assertEqual(result["pull_requests"][0]["title"], "Fresh selected PR")
        self.assertEqual(result["pull_requests"][0]["base_sha"], OTHER_SHA)
        self.assertEqual(result["generated_at"], START)
        self.assertEqual(result["refresh"]["mode"], "scoped")
        self.assertEqual(result["refresh"]["collection_id"], manifest["collection_id"])
        self.assertEqual(result["refresh"]["collection_sha256"], manifest["collection_sha256"])
        self.assertEqual(result["refresh"]["pull_request_numbers"], [1])
        self.assertEqual(result["refresh"]["run_attempts"], [{"run_id": 10, "attempt": 1}])
        self.assertIsNotNone(collector.timestamp(result["refresh"]["completed_at"]))

    def test_invalid_manifest_fails_before_any_api_read(self):
        mutations = [
            lambda value: value.update(collection_id="invalid"),
            lambda value: value.update(collection_sha256="invalid"),
            lambda value: value.update(collection_id=int("1" * 32)),
            lambda value: value.update(collection_sha256=int("2" * 64)),
            lambda value: value["pull_requests"][0].update(head_sha=int("3" * 40)),
            lambda value: value["pull_requests"][0].update(base_sha=int("4" * 40)),
            lambda value: value.update(unexpected="data"),
            lambda value: value["pull_requests"].append(copy.deepcopy(value["pull_requests"][0])),
            lambda value: value["runs"].append(copy.deepcopy(value["runs"][0])),
            lambda value: value["runs"][0].update(pr_number=2),
            lambda value: value["runs"][0].update(workflow_path=".github/workflows/unselected.yml"),
        ]
        for mutate in mutations:
            manifest = scope_manifest()
            api = ScopedAPI(manifest)
            mutate(manifest)
            with self.assertRaises(collector.CollectionError):
                collector.collect_scoped(api, REPO, scoped_history(), manifest)
            self.assertEqual(api.calls, [])
        with self.assertRaises(collector.CollectionError):
            collector.parse_scope_manifest('{"schema_version":1,"schema_version":1}', REPO)

    def test_partial_refresh_requires_valid_retained_baseline(self):
        histories = [empty(), {**scoped_history(), "generated_at": "invalid"},
                     {**scoped_history(), "generated_at": "2999-01-01T00:00:00Z"},
                     {**scoped_history(), "schema_version": 2}, {**scoped_history(), "comparisons": [{"unknown": True}]}]
        duplicate = scoped_history()
        duplicate["pull_requests"].append(copy.deepcopy(duplicate["pull_requests"][0]))
        histories.append(duplicate)
        for history in histories:
            manifest = scope_manifest()
            api = ScopedAPI(manifest)
            with self.assertRaises(collector.CollectionError):
                collector.collect_scoped(api, REPO, history, manifest)
            self.assertEqual(api.calls, [])

    def test_opening_identity_failures_precede_job_reads(self):
        endpoint = f"repos/{REPO}/actions/runs/10"
        mutations = [
            lambda api: api.pr_responses[1][0]["pullRequest"].update(headRefOid=OTHER_SHA),
            lambda api: api.pr_responses[1][0]["pullRequest"].update(headRef=None),
            lambda api: api.pr_responses[1][0]["pullRequest"]["headRef"]["target"].update(oid=OTHER_SHA),
            lambda api: api.pr_responses[1][0]["pullRequest"]["baseRef"]["target"].update(oid=SHA),
            lambda api: api.run_responses[endpoint][0].update(run_attempt=2),
            lambda api: api.run_responses[endpoint][0].update(updated_at=None),
            lambda api: api.run_responses[endpoint][0].update(updated_at="invalid"),
            lambda api: api.run_responses[endpoint][0].update(path=".github/workflows/bazel-swss-oci.yml@refs/heads/other"),
            lambda api: api.run_responses[endpoint][0].update(pull_requests=[]),
            lambda api: api.run_responses[endpoint][0]["pull_requests"][0]["base"].update(sha=SHA),
            lambda api: api.run_responses[endpoint][0]["pull_requests"][0]["head"].update(sha=OTHER_SHA),
        ]
        for mutate in mutations:
            manifest = scope_manifest()
            api = ScopedAPI(manifest)
            mutate(api)
            with self.assertRaises(collector.CollectionError):
                collector.collect_scoped(api, REPO, scoped_history(), manifest, log_jobs=0)
            self.assertFalse(any("/jobs?" in call or call.startswith("log:") for call in api.calls))

    def test_all_selected_identities_are_checked_before_any_jobs(self):
        manifest = scope_manifest()
        second_pr = {**manifest["pull_requests"][0], "id": "PR_test2", "number": 2,
                     "head_ref": "other", "head_sha": "c" * 40}
        manifest["pull_requests"].append(second_pr)
        manifest["runs"].append({**manifest["runs"][0], "run_id": 11, "pr_number": 2})
        api = ScopedAPI(manifest)
        api.run_responses[f"repos/{REPO}/actions/runs/11"][0]["pull_requests"] = []
        with self.assertRaises(collector.CollectionError):
            collector.collect_scoped(api, REPO, scoped_history(), manifest, log_jobs=0)
        self.assertEqual(api.calls, ["pr:1", "pr:2", f"repos/{REPO}/actions/runs/10", f"repos/{REPO}/actions/runs/11"])

    def test_wrong_job_identity_fails_the_scoped_refresh(self):
        endpoint = f"repos/{REPO}/actions/runs/10/attempts/1/jobs"
        for change in ({"run_id": 11}, {"run_attempt": 2}, {"head_sha": OTHER_SHA}):
            manifest = scope_manifest()
            api = ScopedAPI(manifest)
            api.jobs[endpoint][0].update(change)
            with self.assertRaises(collector.CollectionError):
                collector.collect_scoped(api, REPO, scoped_history(), manifest, log_jobs=0)

    def test_completed_run_rejects_pending_jobs(self):
        manifest = scope_manifest()
        api = ScopedAPI(manifest)
        api.jobs[f"repos/{REPO}/actions/runs/10/attempts/1/jobs"][0].update(status="in_progress", conclusion=None)
        with self.assertRaises(collector.CollectionError):
            collector.collect_scoped(api, REPO, scoped_history(), manifest, log_jobs=0)

    def test_running_run_rejects_incoherent_job_outcomes(self):
        endpoint = f"repos/{REPO}/actions/runs/10"
        for change in ({"status": "in_progress", "conclusion": "success"},
                       {"status": "completed", "conclusion": None}, {"status": "completed", "conclusion": ""}):
            manifest = scope_manifest()
            api = ScopedAPI(manifest)
            for response in api.run_responses[endpoint]:
                response.update(status="in_progress", conclusion=None)
            api.jobs[endpoint + "/attempts/1/jobs"][0].update(change)
            with self.assertRaises(collector.CollectionError):
                collector.collect_scoped(api, REPO, scoped_history(), manifest, log_jobs=0)

    def test_incomplete_scoped_job_pages_leave_output_unchanged(self):
        endpoint = f"repos/{REPO}/actions/runs/10/attempts/1/jobs"
        for kind in ("missing_total", "short_page", "numeric_total"):
            manifest = scope_manifest()
            api = ScopedAPI(manifest)
            selected_job = api.jobs[endpoint][0]
            response = {"total_count": 2, "jobs": [selected_job]}
            if kind == "missing_total":
                del response["total_count"]
            elif kind == "numeric_total":
                response["total_count"] = "1"
            api.job_pages[endpoint + "?per_page=100&page=1"] = response
            code, before, after, _ = self.invoke_cli(api,
                ["--mode", "scoped", "--scope-manifest-env", "DASHBOARD_TEST_SCOPE"],
                {"DASHBOARD_TEST_SCOPE": json.dumps(manifest)})
            self.assertEqual(code, 1)
            self.assertEqual(after, before)

    def test_scoped_job_pages_require_a_stable_complete_total(self):
        endpoint = f"repos/{REPO}/actions/runs/10/attempts/1/jobs"
        manifest = scope_manifest()
        api = ScopedAPI(manifest)
        selected_job = api.jobs[endpoint][0]
        api.jobs[endpoint] = [{**selected_job, "id": 1000 + index} for index in range(101)]
        self.assertEqual(len(collector.scoped_jobs(api, REPO, 10, 1)), 101)
        self.assertEqual(api.calls, [endpoint + "?per_page=100&page=1", endpoint + "?per_page=100&page=2"])
        for response in ({"total_count": 102, "jobs": [api.jobs[endpoint][-1]]},
                         collector.CollectionError("A selected jobs page is unavailable")):
            retry = ScopedAPI(manifest)
            retry.jobs[endpoint] = copy.deepcopy(api.jobs[endpoint])
            retry.job_pages[endpoint + "?per_page=100&page=2"] = response
            with self.assertRaises(collector.CollectionError):
                collector.scoped_jobs(retry, REPO, 10, 1)

    def test_closing_identity_changes_leave_output_unchanged(self):
        endpoint = f"repos/{REPO}/actions/runs/10"

        def completes_during_job_read(api):
            api.run_responses[endpoint][0].update(status="in_progress", conclusion=None, updated_at=START)
            api.jobs[endpoint + "/attempts/1/jobs"][0].update(status="in_progress", conclusion=None)

        mutations = [
            lambda api: api.pr_responses[1][1]["pullRequest"].update(headRefOid=OTHER_SHA),
            lambda api: api.run_responses[endpoint][1].update(run_attempt=2),
            lambda api: api.run_responses[endpoint][1].update(pull_requests=[]),
            lambda api: api.run_responses[endpoint][1].update(updated_at="2026-10-10T00:11:00Z"),
            completes_during_job_read,
        ]
        for mutate in mutations:
            manifest = scope_manifest()
            api = ScopedAPI(manifest)
            mutate(api)
            code, before, after, _ = self.invoke_cli(api,
                ["--mode", "scoped", "--scope-manifest-env", "DASHBOARD_TEST_SCOPE"],
                {"DASHBOARD_TEST_SCOPE": json.dumps(manifest)})
            self.assertEqual(code, 1)
            self.assertEqual(after, before)

    def test_cli_modes_never_fall_back_from_missing_scoped_data(self):
        manifest = scope_manifest()
        invalid = [
            (["--mode", "scoped"], {}),
            (["--mode", "scoped", "--scope-manifest-env", "DASHBOARD_TEST_SCOPE"], {}),
            (["--mode", "scoped", "--scope-manifest-env", "DASHBOARD_TEST_SCOPE"], {"DASHBOARD_TEST_SCOPE": ""}),
            (["--mode", "full", "--scope-manifest-env", "DASHBOARD_TEST_SCOPE"], {"DASHBOARD_TEST_SCOPE": json.dumps(manifest)}),
            (["--scope-manifest-env", "DASHBOARD_TEST_SCOPE"], {"DASHBOARD_TEST_SCOPE": ""}),
        ]
        for args, environment in invalid:
            code, before, after, factory_calls = self.invoke_cli(None, args, environment)
            self.assertEqual(code, 1)
            self.assertEqual(after, before)
            self.assertEqual(factory_calls, 0)
        ordinary_api = FakeAPI()
        code, _, after, _ = self.invoke_cli(ordinary_api, [])
        self.assertEqual(code, 0)
        self.assertNotIn("refresh", json.loads(after))
        self.assertTrue(any("/pulls?state=" in call for call in ordinary_api.calls))
        scoped_api = ScopedAPI(manifest)
        code, _, after, _ = self.invoke_cli(scoped_api,
            ["--mode", "scoped", "--scope-manifest-env", "DASHBOARD_TEST_SCOPE"],
            {"DASHBOARD_TEST_SCOPE": json.dumps(manifest)})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(after)["refresh"]["mode"], "scoped")

    def test_ordinary_refresh_supersedes_scoped_freshness(self):
        manifest = scope_manifest()
        scoped = collector.collect_scoped(ScopedAPI(manifest), REPO, scoped_history(), manifest, log_jobs=0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            collector.write_atomic(path, scoped)
            self.assertEqual(collector.load_history(path, REPO), scoped)
            malformed = copy.deepcopy(scoped)
            malformed["refresh"]["run_attempts"][0]["attempt"] = 0
            collector.write_atomic(path, malformed)
            with self.assertRaises(collector.CollectionError):
                collector.load_history(path, REPO)
            for field, length in (("collection_id", 32), ("collection_sha256", 64), ("manifest_sha256", 64)):
                malformed = copy.deepcopy(scoped)
                malformed["refresh"][field] = int("1" * length)
                collector.write_atomic(path, malformed)
                with self.assertRaises(collector.CollectionError):
                    collector.load_history(path, REPO)
        ordinary = collector.collect(FakeAPI(), REPO, scoped, log_jobs=0)
        self.assertNotIn("refresh", ordinary)
        self.assertNotEqual(ordinary["generated_at"], START)


class IOTests(unittest.TestCase):
    def test_cli_loads_reviewed_sibling_with_safe_path_outside_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            # A same-name module in the caller's directory must not be loaded.
            Path(directory, "breakdown.py").write_text("raise RuntimeError('untrusted caller module')\n")
            result = subprocess.run(
                [sys.executable, "-B", str(Path(collector.__file__).resolve()), "--help"],
                cwd=directory, env=dict(os.environ, PYTHONSAFEPATH="1"),
                text=True, capture_output=True, timeout=10, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--log-jobs", result.stdout)

    def test_pagination_reads_beyond_first_page_for_lists_and_jobs(self):
        api = collector.GitHub()
        with mock.patch.object(api, "get", side_effect=[{"jobs": list(range(100))}, {"jobs": [100]}]) as get:
            self.assertEqual(len(api.pages("endpoint?filter=all", "jobs")), 101)
            self.assertTrue(get.call_args_list[1].args[0].endswith("&per_page=100&page=2"))
        with mock.patch.object(api, "get", side_effect=[list(range(100)), [100]]):
            self.assertEqual(len(api.pages("endpoint")), 101)

    def test_transient_retries_and_persistent_failure(self):
        bad = subprocess.CompletedProcess([], 1, "", "HTTP 503")
        good = subprocess.CompletedProcess([], 0, '{"ok": true}', "")
        with mock.patch.object(collector.subprocess, "run", side_effect=[bad, good]) as invoke, mock.patch.object(collector.time, "sleep"):
            self.assertEqual(collector.GitHub().get("endpoint"), {"ok": True})
            self.assertEqual(invoke.call_count, 2)
            self.assertIn("GET", invoke.call_args.args[0])
        with mock.patch.object(collector.subprocess, "run", return_value=bad), mock.patch.object(collector.time, "sleep"):
            with self.assertRaises(collector.CollectionError):
                collector.GitHub().get("endpoint")

    def test_network_errors_retry_but_authentication_errors_do_not(self):
        network = subprocess.CompletedProcess([], 1, "", "unexpected EOF")
        auth = subprocess.CompletedProcess([], 1, "", "HTTP 401")
        good = subprocess.CompletedProcess([], 0, "[]", "")
        with mock.patch.object(collector.subprocess, "run", side_effect=[network, good]) as invoke, mock.patch.object(collector.time, "sleep"):
            self.assertEqual(collector.GitHub().get("endpoint"), [])
            self.assertEqual(invoke.call_count, 2)
        with mock.patch.object(collector.subprocess, "run", return_value=auth) as invoke, mock.patch.object(collector.time, "sleep"):
            with self.assertRaises(collector.CollectionError):
                collector.GitHub().get("endpoint")
            self.assertEqual(invoke.call_count, 1)

    def test_exact_pr_query_passes_only_json_data_to_graphql(self):
        expected = scoped_pr_response(scope_manifest()["pull_requests"][0])
        response = subprocess.CompletedProcess([], 0, json.dumps({"data": {"repository": expected}}), "")
        with mock.patch.object(collector.subprocess, "run", return_value=response) as invoke:
            self.assertEqual(collector.GitHub().exact_pr(REPO, 1), expected)
        self.assertEqual(invoke.call_args.args[0], ["gh", "api", "graphql", "--method", "POST", "--input", "-"])
        request = json.loads(invoke.call_args.kwargs["input"])
        self.assertEqual(request["variables"], {"owner": "securely1g", "name": "sonic-buildimage", "number": 1})
        self.assertEqual(request["query"], collector.SCOPED_PR_QUERY)
        self.assertNotIn("shell", invoke.call_args.kwargs)

    def test_history_rejects_unknown_fields_and_missing_nested_records(self):
        history = collector.collect(FakeAPI(), REPO, empty())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            for mutation in ("private_field", "missing_jobs", "private_step"):
                value = copy.deepcopy(history)
                if mutation == "private_field":
                    value["runs"][0]["jobs"][0]["runner_name"] = "private-host"
                elif mutation == "missing_jobs":
                    del value["runs"][0]["jobs"]
                else:
                    value["runs"][0]["jobs"][0]["steps"][0]["environment"] = "private"
                collector.write_atomic(path, value)
                with self.assertRaises(collector.CollectionError):
                    collector.load_history(path, REPO)

    def test_atomic_history_replacement_and_invalid_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            collector.write_atomic(path, empty())
            self.assertEqual(collector.load_history(path, REPO), empty())
            with mock.patch.object(collector.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    collector.write_atomic(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), empty())
            self.assertEqual(list(Path(directory).glob(".history-*")), [])
            path.write_text('{"schema_version": 99}')
            with self.assertRaises(collector.CollectionError):
                collector.load_history(path, REPO)


if __name__ == "__main__":
    unittest.main()
