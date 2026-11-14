"""Test collection of VS image recipe receipts from their producing Actions step."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_collect import collector, FakeAPI, REPO, START, END, empty, job, run


def payload():
    return {"version": 1, "images": [
        {"owner": owner, "bazel_seconds": 100, "lock_seconds": 13.5, "compile_seconds": 37,
         "foreign_seconds": 0, "fetch_seconds": 10, "export_seconds": 2,
         "bazel_phases": 3, "profiles": 3, "exports": 2, "failed": False}
        for owner in sorted(collector.breakdown.RECIPE_OWNERS)]}


def marker(value):
    return "SONIC_VS_IMAGE_TIMINGS_V1 " + json.dumps(value, separators=(",", ":"))


def log(lines):
    # The Actions API records whole seconds. Neighbouring steps, the runner's
    # ANSI-coloured command echo and a genuine receipt can all share that second.
    events = [
        ("001", "##[group]Run previous step"), ("002", "##[endgroup]"),
        ("003", marker(payload())), ("010", "##[group]Run set -euo pipefail"),
        ("011", '\x1b[36;1m  python3 -B tools/bazel/ci/summarize_vs_image_timings.py "$timings"\x1b[0m'),
        ("012", marker(payload())), ("013", "##[endgroup]"),
        *(("020", line) for line in lines),
        ("030", "##[group]Run actions/upload-artifact"), ("031", "##[endgroup]"),
        ("032", marker(payload())),
    ]
    return "".join(f"2026-10-10T00:10:00.{ms}0000Z {line}\n" for ms, line in events).encode()


def host_job(build_conclusion="success"):
    raw = dict(job(), name="VS installer in SONiC slave (AMD64)")
    build = dict(raw["steps"][0], name=collector.HOST_VS_BUILD, conclusion=build_conclusion)
    summary = dict(build, name=collector.VS_TIMING_SUMMARY, number=2, started_at=END, conclusion="success")
    return dict(raw, steps=[build, summary], conclusion=build_conclusion)


def api_with(raw, log_bytes):
    api = FakeAPI(latest=[run(path=".github/workflows/bazel-oci.yml")])
    api.jobs[f"repos/{REPO}/actions/runs/10/attempts/1/jobs"] = [raw]
    api.logs[99] = log_bytes
    return api


class RecipeTimingsTest(unittest.TestCase):
    def test_fences_require_command_header_and_standalone_receipt(self):
        step = host_job()["steps"][1]
        parse = collector.breakdown.parse_recipe_log
        receipt = payload()
        # A measured zero is preserved. Missing observations remain null.
        row = receipt["images"][0]
        row.update(exports=0, export_seconds=None, failed=True)
        self.assertEqual(parse(log([marker(receipt)]), step), dict(receipt, status="available"))
        self.assertEqual(parse(log([]), step)["reason"], "not-recorded")
        self.assertEqual(parse(log([marker(receipt)] * 2), step)["reason"], "invalid-timing")
        self.assertEqual(parse(log([marker(receipt)]).replace(b"summarize_vs_image_timings.py",
                                                                b"some_other_script.py"), step)["reason"],
                         "invalid-timing")
        wrong_step = dict(step, started_at=START)
        self.assertEqual(parse(log([marker(receipt)]), wrong_step)["status"], "unavailable")

    def test_invalid_receipts_cannot_publish_unknown_or_unbounded_data(self):
        step = host_job()["steps"][1]
        invalid = [dict(failed=1), dict(owner="private-runner"), dict(owner=[]), dict(profiles=True),
                   dict(exports=3), dict(exports=0), dict(fetch_seconds=None), dict(compile_seconds=-1),
                   dict(lock_seconds=float("inf")), dict(secret="never publish this")]
        for edits in invalid:
            receipt = payload()
            receipt["images"][0].update(edits)
            with self.subTest(edits=edits):
                self.assertEqual(collector.breakdown.parse_recipe_log(log([marker(receipt)]), step),
                                 collector.breakdown.recipe_unavailable("invalid-timing"))
        receipt = payload()
        receipt["images"][0] = receipt["images"][1]
        for text in (marker(receipt), marker(payload()).replace('"owner"', '"owner":"hidden","owner"', 1),
                     marker(payload()) + " " * 4096):
            self.assertEqual(collector.breakdown.parse_recipe_log(log([text]), step)["reason"], "invalid-timing")

    def test_collection_shares_log_budget_and_retries_transient_failures(self):
        raw = host_job()
        api = api_with(raw, collector.LogReadError("logs-temporarily-unavailable"))
        old = collector.collect(api, REPO, empty(), log_jobs=0)
        self.assertEqual(api.log_calls, [])
        self.assertEqual(old["runs"][0]["jobs"][0]["image_recipe_timings"]["reason"], "pending-collection")
        old = collector.collect(api, REPO, old)
        self.assertEqual(api.log_calls, [(REPO, 99)])
        self.assertEqual(old["runs"][0]["jobs"][0]["image_recipe_timings"]["reason"], "logs-temporarily-unavailable")
        retry = api_with(raw, log([marker(payload())]))
        new = collector.collect(retry, REPO, old)
        self.assertEqual(retry.log_calls, [(REPO, 99)])
        result = new["runs"][0]["jobs"][0]
        self.assertEqual(result["image_recipe_timings"]["status"], "available")
        self.assertEqual(result["image_build_seconds"], 600)
        self.assertEqual(result["image_build_step_numbers"], [1])
        # Re-reading identical metadata preserves the receipt without another download.
        saved = collector.collect(retry, REPO, new, backfill=True)
        self.assertEqual(retry.log_calls, [(REPO, 99)])
        self.assertEqual(saved["runs"][0]["jobs"], new["runs"][0]["jobs"])

    def test_failed_build_can_publish_partial_timings_and_old_jobs_stay_readable(self):
        receipt = payload()
        receipt["images"][0].update(profiles=0, lock_seconds=None, compile_seconds=None,
                                    foreign_seconds=None, fetch_seconds=None, failed=True)
        api = api_with(host_job("failure"), log([marker(receipt)]))
        detail = collector.collect(api, REPO, empty())["runs"][0]["jobs"][0]
        self.assertIsNone(detail["image_build_seconds"])
        self.assertEqual(detail["image_recipe_timings"], dict(receipt, status="available"))
        raw = host_job()
        raw["steps"].pop()
        detail = collector.collect(api_with(raw, b""), REPO, empty(), log_jobs=0)["runs"][0]["jobs"][0]
        self.assertNotIn("image_recipe_timings", detail)

    def test_history_rejects_unbound_receipts_and_does_not_reuse_changed_jobs(self):
        raw = host_job()
        saved = collector.collect(api_with(raw, log([marker(payload())])), REPO, empty())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            collector.write_atomic(path, saved)
            self.assertEqual(collector.load_history(path, REPO), saved)
            for change in ("step", "value", "workflow"):
                bad = copy.deepcopy(saved)
                item = bad["runs"][0]["jobs"][0]
                if change == "step":
                    item["steps"].pop()
                elif change == "value":
                    item["image_recipe_timings"]["images"][0]["profiles"] = 0
                else:
                    bad["runs"][0]["workflow_path"] = ".github/workflows/bazel.yml"
                collector.write_atomic(path, bad)
                with self.subTest(change=change), self.assertRaises(collector.CollectionError):
                    collector.load_history(path, REPO)
        raw["steps"][1]["number"] = 3
        api = api_with(raw, collector.LogReadError("logs-expired"))
        updated = collector.collect(api, REPO, saved, backfill=True)["runs"][0]["jobs"][0]
        self.assertEqual(api.log_calls, [(REPO, 99)])
        self.assertEqual(updated["image_recipe_timings"]["reason"], "logs-expired")
