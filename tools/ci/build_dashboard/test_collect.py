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
            workflow = endpoint.split("/workflows/", 1)[1].split("/", 1)[0]
            return [item for item in self.latest
                    if item["path"].split("@", 1)[0].rsplit("/", 1)[-1] == workflow]
        if "/commits/" in endpoint:
            return self.linked
        if endpoint.endswith("/jobs"):
            if self.fail_jobs:
                raise collector.CollectionError("An API read failed")
            return self.jobs.get(endpoint, [job()])
        raise AssertionError(endpoint)


def empty():
    return {"schema_version": 1, "repository": REPO, "pull_requests": [], "runs": []}


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
        for name, step in [
            ("Bazel VS installer (AMD64)", "Build and verify sonic-vs.bin with Bazel"),
            ("Unprivileged sonic-vs.bin assembly (AMD64)",
             "Run Make inside an unprivileged SONiC builder and verify the installer"),
        ]:
            value["name"] = name
            value["steps"][0]["name"] = step
            normalized = collector.normalize_job(value, REPO, 10)
            self.assertEqual(normalized["kind"], "bazel-vs")
            self.assertEqual(normalized["image_build_seconds"], 600)
            self.assertEqual(normalized["phases"]["build"], 600)
        value["name"] = "Something containing VS"
        self.assertEqual(collector.job_kind(value["name"]), "other")

    def test_combined_make_image_step_counts(self):
        value = job()
        value["steps"][0]["name"] = "Build the VS image and both OCI container variants"
        self.assertEqual(collector.normalize_job(value, REPO, 10)["image_build_seconds"], 600)

    def test_pr64_short_make_step_preserves_observed_duration(self):
        # PR64 job 123845988910 reported this exact successful 1,738-second step.
        value = job()
        value.update(id=123845988910, started_at="2026-10-30T09:00:27Z",
                     completed_at="2026-10-30T10:28:48Z")
        value["steps"] = [{"number": 10, "name": "Build the VS image", "status": "completed",
                           "conclusion": "success", "started_at": "2026-10-30T09:04:56Z",
                           "completed_at": "2026-10-30T09:33:54Z"}]
        normalized = collector.normalize_job(value, REPO, 40988556063)
        self.assertEqual(normalized["image_build_seconds"], 1738)
        self.assertEqual(normalized["image_build_elapsed_seconds"], 1738)
        self.assertEqual(normalized["phases"]["build"], 1738)

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
    def test_cancelled_unprivileged_attempt_keeps_its_historical_revision(self):
        # Public PR76 fields for the two cancelled jobs in run 41033358115.
        historical_sha = "c2b124fb1b66d338ada38d1bcbb2abdb89b98831"
        current_sha = "4fec0d72939a226641a708293425b288871ce380"
        branch = "codex/bazel-vs-unprivileged-assembly"
        attempt = run(run_id=41033358115, conclusion="cancelled", head_branch=branch,
                      head_sha=historical_sha, path=".github/workflows/bazel-vs-unprivileged.yml",
                      name="Bazel VS unprivileged assembly", pull_requests=[{"number": 76}],
                      run_started_at="2026-10-30T09:25:46Z", updated_at="2026-10-30T10:20:53Z")
        current = pr(number=76, branch=branch, sha=current_sha)
        current["base"]["sha"] = "8fb9a93de16bfed545ae148d84943da14d5de2d2"
        api = FakeAPI(latest=[attempt], pulls=[current])
        api.jobs[f"repos/{REPO}/actions/runs/41033358115/attempts/1/jobs"] = [
            {"id": 123989169804, "name": "Native Make prerequisites (AMD64)",
             "status": "completed", "conclusion": "cancelled", "started_at": "2026-10-30T09:28:20Z",
             "completed_at": "2026-10-30T10:20:52Z", "steps": []},
            {"id": 124010400469, "name": "Unprivileged sonic-vs.bin assembly (AMD64)",
             "status": "completed", "conclusion": "cancelled", "started_at": "2026-10-30T10:20:52Z",
             "completed_at": "2026-10-30T10:20:52Z", "steps": []},
        ]
        result = collector.collect(api, REPO, empty(), workers=1, log_jobs=0)
        self.assertEqual(result["pull_requests"][0]["head_sha"], current_sha)
        self.assertEqual(len(result["runs"]), 1)
        saved = result["runs"][0]
        self.assertEqual((saved["run_id"], saved["attempt"], saved["source_sha"]),
                         (41033358115, 1, historical_sha))
        self.assertEqual(saved["workflow_path"], ".github/workflows/bazel-vs-unprivileged.yml")
        self.assertEqual(saved["started_at"], "2026-10-30T09:25:46Z")
        self.assertEqual(saved["pr_numbers"], [76])
        self.assertEqual((saved["status"], saved["conclusion"]), ("completed", "cancelled"))
        self.assertEqual([item["kind"] for item in saved["jobs"]], ["other", "bazel-vs"])
        for item in saved["jobs"]:
            self.assertEqual((item["status"], item["conclusion"]), ("completed", "cancelled"))
            self.assertIsNone(item["image_build_seconds"])
            self.assertEqual(item["steps"], [])
        self.assertEqual(saved["jobs"][1]["url"],
                         f"https://github.com/{REPO}/actions/runs/41033358115/job/124010400469")

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
