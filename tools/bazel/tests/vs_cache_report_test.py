"""Exercise fresh-build cache evidence without building an installer."""

import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import yaml

from tools.bazel.ci import time_vs_image, vs_cache_report


ROOT = Path(__file__).resolve().parents[3]


class VsCacheReportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "execution.json"
        self.events = self.root / "events.json"
        self.directory = time_vs_image.start_recipe(self.root, "kernel", ["build"], ["build"], [])
        self.destination = self.directory / "build.cache.json"
        self.record = {
            "targetLabel": "@@sonic-linux-kernel+//:kernel", "mnemonic": "CppCompile",
            "runner": "linux-sandbox", "cacheHit": False, "cacheable": True,
            "remoteCacheable": True, "listedOutputs": ["bazel-out/bin/kernel.o"],
            "digest": {"hash": "a" * 64}, "commandArgs": ["private-command"],
            "environmentVariables": [{"name": "SECRET", "value": "private-environment"}],
            "inputs": [{"path": "/home/private/input"}],
        }
        self.events.write_text(
            '{"id":{"started":{}},"started":{"command":"build","buildToolVersion":"8.5.1"}}\n'
            '{"id":{"buildFinished":{}},"finished":{"overallSuccess":true,"exitCode":{"code":0}}}\n'
            '{"id":{"buildMetrics":{}},"lastMessage":true}\n')

    def capture(self, records):
        self.log.write_text("\n".join(json.dumps(record) for record in records))
        vs_cache_report.capture(self.log, self.events, self.destination)
        return json.loads(self.destination.read_text())

    def report(self, number=1, status="success"):
        return vs_cache_report.summarize(self.root / "artifacts/vs/image-timings",
                                         "b" * 40, number, status, 15)

    def runner_metrics(self):
        events = self.events.read_text().splitlines()
        events[-1] = json.dumps({
            "id": {"buildMetrics": {}}, "lastMessage": True,
            "buildMetrics": {"actionSummary": {
                "actionsExecuted": "439",
                "actionCacheStatistics": {"hits": 9999, "misses": 1},
                "runnerCount": [{"name": "total", "count": 439},
                                {"name": "remote cache hit", "count": 412},
                                {"name": "linux-sandbox", "count": 1},
                                {"name": "internal", "count": 26}],
            }},
        })
        self.events.write_text("\n".join(events) + "\n")

    def test_rejected_spawn_details_keep_verified_runner_counts_separate_from_local_cache_hits(self):
        self.runner_metrics()
        data = self.capture([{**self.record, "listedOutputs": ["/home/private/output"]}])
        self.assertFalse(data["available"])
        self.assertTrue(data["usable_complete"])
        self.assertEqual(data["reason"], "invalid-spawn-outputs")
        first, second = self.report(1), self.report(2)
        self.assertTrue(second["complete"])
        self.assertFalse(second["spawn_details_complete"])
        self.assertEqual(second["counts"]["remote_hits"], 412)
        self.assertEqual(second["counts"]["executed"], 1)
        self.assertIsNone(second["counts"]["explicitly_ineligible"])
        self.assertEqual(second["phases"][0]["source"], "BEP runner metrics")
        comparison = vs_cache_report.compare(first, second)
        self.assertTrue(comparison["comparable"])
        text = vs_cache_report.markdown(comparison)
        self.assertIn("Per-action detail is incomplete", text)
        self.assertIn("412", text)
        self.assertNotIn("9999", text)
        self.assertNotIn("Dependency target |", text)
        self.assertNotIn("private", json.dumps(comparison))

    def test_empty_spawn_log_cannot_override_nonzero_structured_remote_hits(self):
        self.runner_metrics()
        data = self.capture([])
        self.assertEqual(data["reason"], "spawn-metrics-mismatch")
        self.assertFalse(data["available"])
        self.assertTrue(self.report()["complete"])
        self.assertEqual(self.report()["counts"]["remote_hits"], 412)

    def test_large_private_spawn_log_uses_bounded_structured_metrics(self):
        self.runner_metrics()
        with self.log.open("wb") as stream:
            stream.truncate(513 * 1024 * 1024)
        vs_cache_report.capture(self.log, self.events, self.destination)
        data = json.loads(self.destination.read_text())
        self.assertEqual(data["reason"], "oversized-execution-log")
        self.assertTrue(data["usable_complete"])
        self.assertEqual(self.report()["counts"]["remote_hits"], 412)

    def test_incomplete_events_do_not_authorize_aggregate_comparison(self):
        self.runner_metrics()
        self.events.write_text(self.events.read_text().replace('"lastMessage": true', '"lastMessage": false'))
        self.log.write_text("{")
        vs_cache_report.capture(self.log, self.events, self.destination)
        self.assertFalse(self.report()["complete"])
        self.assertIsNone(self.report()["counts"]["remote_hits"])
        self.assertFalse(vs_cache_report.compare(self.report(1), self.report(2))["comparable"])

    def test_runner_names_are_reported_without_changing_build_comparability(self):
        self.capture([self.record])
        with mock.patch.dict(os.environ, {"RUNNER_NAME": "GitHub Actions 1000062452"}):
            first = self.report(1)
        with mock.patch.dict(os.environ, {"RUNNER_NAME": "sonic-vs-runner-2"}):
            second = self.report(2)
        comparison = vs_cache_report.compare(first, second)
        self.assertTrue(comparison["comparable"])
        text = vs_cache_report.markdown(comparison)
        self.assertIn("GitHub Actions 1000062452", text)
        self.assertIn("sonic-vs-runner-2", text)

    def test_legacy_failed_capture_summary_shows_successful_builds_but_unknown_cache_counts(self):
        self.capture([])
        first, second = self.report(1), self.report(2)
        for value, seconds in ((first, 3196), (second, 2801)):
            value["complete"] = False
            value["duration_seconds"] = seconds
            value.pop("phases")
            value.pop("spawn_details_complete")
        text = vs_cache_report.markdown(vs_cache_report.compare(first, second))
        self.assertIn("| 1 | **success** | 53m 16s | n/a | n/a | n/a |", text)
        self.assertIn("| 2 | **success** | 46m 41s | n/a | n/a | n/a |", text)
        self.assertIn("6m 35s (faster by 12.4%)", text)
        self.assertIn("Pass 1 cache telemetry is incomplete", text)
        self.assertNotIn("Container or phase | Dependency component", text)

    def test_reports_remote_disk_executed_and_ineligible_without_private_fields(self):
        self.capture([
            self.record, self.record,
            {**self.record, "runner": "remote cache hit", "cacheHit": True},
            {**self.record, "runner": "disk cache hit", "cacheHit": True},
            {**self.record, "remoteCacheable": False},
        ])
        result = self.report()
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"], {
            "logged_spawns": 5, "remote_hits": 1, "disk_hits": 1, "executed": 3,
            "explicitly_ineligible": 1, "failed": 0,
        })
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(vs_cache_report.make_cell({"outcomes": {"unfinished": 1}}),
                         "unfinished (no ending marker)")
        self.assertEqual(result["actions"][0]["outputs"], ["bazel-out/bin/kernel.o"])

    def test_missing_eligibility_is_unknown_and_failed_spawns_remain_safe(self):
        record = {key: value for key, value in self.record.items()
                  if key not in ("cacheable", "remoteCacheable")}
        data = self.capture([{**record, "exitCode": 1, "status": "private-error"}])
        self.assertTrue(data["actions"][0]["failed"])
        self.assertIsNone(data["actions"][0]["remote_eligible"])
        self.assertNotIn("private", json.dumps(data))
        self.assertFalse(self.report(status="failure")["complete"])

    def test_malformed_unsafe_or_truncated_logs_are_not_zero_misses(self):
        for text in (
                "{", json.dumps(self.record) + "\n{",
                json.dumps({**self.record, "runner": "private\ncommand"}),
                json.dumps({**self.record, "listedOutputs": ["/home/private/output"]}),
                json.dumps({**self.record, "cacheHit": True}),
                '{"mnemonic":"CppCompile","mnemonic":"CppLink"}'):
            with self.subTest(text=text):
                self.log.write_text(text)
                vs_cache_report.capture(self.log, self.events, self.destination)
                data = json.loads(self.destination.read_text())
                self.assertFalse(data["available"])
                self.assertEqual(data["actions"], [])
                self.assertFalse(self.report()["complete"])

    def test_empty_successful_spawn_log_is_distinct_from_missing_or_incomplete_evidence(self):
        self.assertTrue(self.capture([])["complete"])
        self.assertTrue(self.report()["complete"])
        self.events.unlink()
        self.assertFalse(self.capture([])["complete"])
        self.assertFalse(self.report()["complete"])
        self.destination.unlink()
        (self.directory / "build.timing.json").write_text("{}")
        report = self.report()
        self.assertEqual(report["missing_captures"], 1)
        self.assertFalse(report["complete"])

    def test_compares_only_same_revision_complete_builds_and_keeps_second_executions(self):
        self.capture([self.record])
        first = self.report(1)
        second = self.report(2)
        pair = vs_cache_report.compare(first, second)
        self.assertTrue(pair["comparable"])
        self.assertTrue(pair["second_executed_groups"][0]["same_group_executed_in_first"])
        self.assertIn("Executed does not prove a remote miss", vs_cache_report.markdown(pair))
        self.capture([{**self.record, "runner": "remote cache hit", "cacheHit": True}])
        warm = vs_cache_report.compare(first, self.report(2))
        self.assertEqual(warm["second_executed_groups"], [])
        for value in (None, {**second, "revision": "c" * 40},
                      {**second, "complete": False}, {**second, "pass_number": 1},
                      {**second, "configuration": {"image_version": "changed"}}):
            self.assertFalse(vs_cache_report.compare(first, value)["comparable"])
        self.assertIsNone(vs_cache_report.compare(None, second)["second_executed_groups"][0]
                          ["same_group_executed_in_first"])

    def test_cli_writes_comparison_artifact_and_fails_on_missing_pass(self):
        self.capture([self.record])
        first, second = self.root / "first.json", self.root / "second.json"
        first.write_text(json.dumps(self.report(1)))
        second.write_text(json.dumps(self.report(2)))
        output, summary = self.root / "comparison.json", self.root / "summary.txt"
        command = [sys.executable, "-B", str(ROOT / "tools/bazel/ci/vs_cache_report.py"),
                   "compare", "--first", str(first), "--second", str(second),
                   "--output", str(output), "--summary", str(summary)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(output.read_text())["comparable"])
        self.assertIn("CppCompile", summary.read_text())
        second.unlink()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(output.read_text())["comparable"])
        self.assertIn("Incomplete", summary.read_text())

    def test_warning_lines_do_not_publish_errors_or_claim_request_counts(self):
        self.capture([self.record])
        self.log.write_text("UNAVAILABLE private-endpoint\nlost inputs private-digest\n")
        report = vs_cache_report.summarize(self.root / "artifacts/vs/image-timings",
                                          "b" * 40, 1, "failure", 15, self.log)
        self.assertEqual(report["warning_lines"], {"UNAVAILABLE": 1, "lost inputs": 1})
        self.assertNotIn("private", json.dumps(report))

    def test_configured_dependency_mapping_marks_shared_components_without_double_counting_global_spawns(self):
        self.directory = time_vs_image.start_recipe(self.root, "vs-container-images", ["build"], ["build"], [])
        self.destination = self.directory / "build.cache.json"
        common = {**self.record, "targetLabel": "@@sonic_swss_common+//common:common"}
        self.capture([common, common])
        graph = {"elapsed_seconds": 2, "containers": {
            "docker-orchagent": {"available": True, "dependencies": [common["targetLabel"]]},
            "docker-syncd-vs": {"available": True, "dependencies": [common["targetLabel"]]},
        }}
        (self.directory / "container-dependencies.json").write_text(json.dumps(graph))
        first, second = self.report(1), self.report(2)
        self.assertEqual(second["counts"]["executed"], 2)
        self.assertEqual(second["actions"][0]["containers"], ["docker-orchagent", "docker-syncd-vs"])
        report = vs_cache_report.compare(first, second)
        self.assertEqual(len(report["components"]), 2)
        self.assertTrue(all(row["component"] == "sonic-swss-common" and row["second"]["executed"] == 2
                            for row in report["components"]))
        text = vs_cache_report.markdown(report)
        self.assertIn("not additive", text)
        self.assertIn("bazel-out/bin/kernel.o", text)
        (self.directory / "container-dependencies.json").unlink()
        missing = self.report(2)
        self.assertEqual(missing["actions"][0]["containers"], [])
        self.assertEqual(missing["container_attribution"]["missing_maps"], 1)

    def test_dependency_queries_preserve_configuration_and_fail_closed_with_a_total_budget(self):
        roots = {"a.gz": ["//dockers:a"], "b.gz": ["//dockers:b"]}
        with mock.patch.object(vs_cache_report.subprocess, "check_output", return_value="//dockers:a\n"), \
                mock.patch.object(vs_cache_report.time, "monotonic", side_effect=[0, 0, 70, 70]):
            vs_cache_report.capture_container_dependencies(self.root, self.directory, "bazel",
                                                           ["--output_base=private"], ["--config=syncd-dash"], roots)
        data = json.loads((self.directory / "container-dependencies.json").read_text())
        self.assertTrue(data["containers"]["a"]["available"])
        self.assertFalse(data["containers"]["b"]["available"])
        for response in ("", "/home/private/input\n"):
            with mock.patch.object(vs_cache_report.subprocess, "check_output", return_value=response) as execute:
                vs_cache_report.capture_container_dependencies(
                    self.root, self.directory, "bazel", ["--output_base=private"], ["--config=syncd-dash"],
                    {"a.gz": ["//dockers:a"]})
            command = execute.call_args.args[0]
            self.assertEqual(command[:4], ["bazel", "--output_base=private", "cquery", "--config=syncd-dash"])
            self.assertIn("deps(set(//dockers:a))", command)
            self.assertIn("target.label.workspace_name", " ".join(command))
            data = json.loads((self.directory / "container-dependencies.json").read_text())
            self.assertFalse(data["containers"]["a"]["available"])
            self.assertNotIn("private", json.dumps(data))

    def test_make_markers_distinguish_cached_finished_unfinished_and_missing_timing_pairs(self):
        recorder = vs_cache_report.MakeTargets()
        recorder.observe("[ building ] [ target/sonic-vs.bin ]", 0)
        recorder.observe("[ building ] [ target/docker-orchagent.gz ]", 1)
        recorder.observe("\x1b[32m[ cached ] [ target/docker-orchagent.gz ]\x1b[0m", 3)
        recorder.observe("[ finished ] [ target/sonic-vs.bin ]", 10)
        recorder.observe("[ cached ] [ target/debs/trixie/libfoo_1:2~rc1_amd64.deb ]", 11)
        recorder.observe("[ building ] [ target/unfinished.deb ]", 12)
        recorder.observe("[ cached ] [ /home/private/target/secret.deb ]", 13)
        result = recorder.finish()
        self.assertEqual([row["outcome"] for row in result["targets"]], ["cached", "finished", "cached", "unfinished"])
        self.assertEqual([row["duration_seconds"] for row in result["targets"]], [2, 10, None, None])
        self.assertNotIn("private", json.dumps(result))
        duplicate = vs_cache_report.MakeTargets()
        for when in (0, 1):
            duplicate.observe("[ building ] [ target/foo.deb ]", when)
        duplicate.observe("[ finished ] [ target/foo.deb ]", 2)
        self.assertIsNone(duplicate.finish()["targets"][0]["duration_seconds"])

    def test_make_recorder_preserves_stdout_and_comparison_keeps_make_and_bazel_outcomes_separate(self):
        raw = "private compiler output\n[ building ] [ target/foo.deb ]\n[ finished ] [ target/foo.deb ]\n"
        destination, output = self.root / "make.json", io.StringIO()
        vs_cache_report.record_make(io.StringIO(raw), output, destination)
        self.assertEqual(output.getvalue(), raw)
        self.assertNotIn("private", destination.read_text())
        self.capture([{**self.record, "runner": "remote cache hit", "cacheHit": True}])
        first = vs_cache_report.summarize(self.root / "artifacts/vs/image-timings", "b" * 40, 1,
                                          "success", 15, make_records=destination)
        destination.write_text(json.dumps({"available": True, "targets": [
            {"target": "target/foo.deb", "outcome": "cached", "duration_seconds": 1},
            {"target": "target/bar.deb", "outcome": "finished", "duration_seconds": 2},
        ]}))
        second = vs_cache_report.summarize(self.root / "artifacts/vs/image-timings", "b" * 40, 2,
                                           "success", 15, make_records=destination)
        report = vs_cache_report.compare(first, second)
        foo = next(row for row in report["make_targets"] if row["target"] == "target/foo.deb")
        self.assertEqual(foo["first"]["outcomes"], {"finished": 1})
        self.assertEqual(foo["second"]["outcomes"], {"cached": 1})
        self.assertEqual(second["counts"]["remote_hits"], 1)
        self.assertEqual(second["counts"]["executed"], 0)
        self.assertIn("[ cached ]", vs_cache_report.markdown(report))
        self.assertIn("not observed", vs_cache_report.markdown(report))

    def test_record_make_cli_handles_non_utf8_output_without_losing_cache_markers(self):
        destination = self.root / "make.json"
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools/bazel/ci/vs_cache_report.py"),
             "record-make", "--output", str(destination)],
            input=b"private \xff\n[ cached ] [ target/foo.deb ]\n", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"[ cached ] [ target/foo.deb ]", result.stdout)
        self.assertEqual(json.loads(destination.read_text())["targets"][0]["outcome"], "cached")
        self.assertNotIn("private", destination.read_text())

    def test_declared_debian_output_names_support_epochs_and_prerelease_versions(self):
        data = self.capture([{**self.record, "listedOutputs": ["bazel-out/bin/libfoo_1:2~rc1_amd64.deb"]}])
        self.assertTrue(data["available"])
        self.assertEqual(data["actions"][0]["outputs"], ["bazel-out/bin/libfoo_1:2~rc1_amd64.deb"])

    @unittest.skipUnless(os.environ.get("SONIC_CACHE_REPORT_QUERY_TEST") == "1",
                         "opt-in configured-query smoke check; no installer build")
    def test_real_fresh_builds_capture_remote_hits_without_publishing_private_fields(self):
        stored = {}

        class CacheHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_PUT(self):
                stored[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                data = stored.get(self.path)
                self.send_response(404 if data is None else 200)
                self.send_header("Content-Length", str(len(data or b"")))
                self.end_headers()
                if data is not None:
                    self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), CacheHandler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        workspace = self.root / "spawn"
        workspace.mkdir()
        (workspace / ".bazelversion").write_text("8.5.1\n")
        (workspace / "WORKSPACE").write_text('workspace(name = "cache_probe")\n')
        (workspace / "BUILD.bazel").write_text(
            'load(":probe.bzl", "probe")\nplatform(name = "host")\nprobe(name = "example")\n')
        (workspace / "probe.bzl").write_text(
            'def _impl(ctx):\n'
            '    out = ctx.actions.declare_file(ctx.label.name + ".txt")\n'
            '    ctx.actions.run(executable = "/bin/sh", '
            'arguments = ["-c", \'printf cache-probe > "$1"\', "probe", out.path], '
            'outputs = [out], mnemonic = "CacheProbe")\n'
            '    return [DefaultInfo(files = depset([out]))]\n'
            'probe = rule(implementation = _impl)\n')
        for number in (1, 2):
            directory = time_vs_image.start_recipe(self.root, "kernel", ["build"], ["build"], [])
            command = [
                shutil.which("bazel"), "--batch", "--ignore_all_rc_files",
                "--output_user_root=" + str(self.root / f"spawn-state-{number}"), "build",
                "--enable_bzlmod=false", "--enable_workspace=true", "--incompatible_autoload_externally=",
                "--platforms=//:host", "--host_platform=//:host",
                f"--remote_cache=http://127.0.0.1:{server.server_port}",
                "--remote_timeout=10", f"--remote_upload_local_results={'true' if number == 1 else 'false'}",
                "//:example",
            ]
            with mock.patch.dict(os.environ, {"SONIC_VS_CACHE_REPORT": "1"}):
                time_vs_image.run_phase(directory, "build", command, cwd=workspace)
            destination = directory / "build.cache.json"
            capture = json.loads(destination.read_text())
            self.assertTrue(capture["complete"], capture)
            self.assertEqual(vs_cache_report.counts(capture["actions"])[
                "executed" if number == 1 else "remote_hits"], 1)
            self.assertNotIn("commandArgs", destination.read_text())

    @unittest.skipUnless(os.environ.get("SONIC_CACHE_REPORT_QUERY_TEST") == "1",
                         "opt-in configured-query smoke check; no installer build")
    def test_real_configured_dependency_query_without_building_actions(self):
        workspace = self.root / "query"
        workspace.mkdir()
        (workspace / "MODULE.bazel").write_text('module(name = "cache_probe")\n')
        (workspace / ".bazelversion").write_text("8.5.1\n")
        (workspace / "WORKSPACE").write_text("")
        (workspace / "BUILD.bazel").write_text(
            'platform(name = "host")\nfilegroup(name = "leaf", srcs = [])\n'
            'filegroup(name = "top", srcs = [":leaf"])\n')
        startup = ["--batch", "--ignore_all_rc_files",
                   "--output_user_root=" + str(self.root / "bazel-state")]
        options = ["--enable_bzlmod=false", "--enable_workspace=true", "--incompatible_autoload_externally=",
                   "--platforms=//:host", "--host_platform=//:host"]
        bazel = shutil.which("bazel")
        self.assertIsNotNone(bazel)
        vs_cache_report.capture_container_dependencies(
            workspace, self.directory, bazel, startup, options, {"container.gz": ["//:top"]})
        graph = json.loads((self.directory / "container-dependencies.json").read_text())
        self.assertTrue(graph["containers"]["container"]["available"], graph)
        self.assertTrue({"//:top", "//:leaf"}.issubset(graph["containers"]["container"]["dependencies"]))

    def test_capture_is_opt_in_per_invocation_and_removes_private_logs_on_failure(self):
        private = []

        def execute(command, **kwargs):
            execution = Path(next(arg.split("=", 1)[1] for arg in command
                                  if arg.startswith("--execution_log_json_file=")))
            events = Path(next(arg.split("=", 1)[1] for arg in command
                               if arg.startswith("--build_event_json_file=")))
            self.assertIn("--execution_log_sort=false", command)
            self.assertEqual(execution.stat().st_mode & 0o777, 0o600)
            execution.write_text(json.dumps(self.record))
            events.write_text(self.events.read_text())
            private.append(execution)
            raise subprocess.CalledProcessError(37, command)

        with mock.patch.dict("os.environ", {"SONIC_VS_CACHE_REPORT": "1"}), \
                mock.patch.object(time_vs_image.subprocess, "run", side_effect=execute):
            for _ in range(2):
                directory = time_vs_image.start_recipe(self.root, "kernel", ["build"], ["build"], [])
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    time_vs_image.run_phase(directory, "build", ["bazel", "build", "//:kernel"])
                self.assertEqual(raised.exception.returncode, 37)
                self.assertTrue((directory / "build.cache.json").is_file())
        self.assertNotEqual(private[0], private[1])
        self.assertTrue(all(not path.exists() for path in private))
        with mock.patch.dict("os.environ", {"SONIC_VS_CACHE_REPORT": "0"}), \
                mock.patch.object(time_vs_image.subprocess, "run") as execute:
            time_vs_image.run_phase(self.directory, "build", ["bazel", "build", "//:kernel"])
            self.assertFalse(any(arg.startswith("--execution_log") for arg in execute.call_args.args[0]))

    def test_existing_private_execution_log_is_not_overridden_or_deleted(self):
        for flags in (["--execution_log_json_file=execution.json"],
                      ["--execution_log_json_file", "execution.json"]):
            with self.subTest(flags=flags):
                self.log.write_text(json.dumps(self.record))
                with mock.patch.dict("os.environ", {"SONIC_VS_CACHE_REPORT": "1"}), \
                        mock.patch.object(time_vs_image.subprocess, "run") as execute:
                    time_vs_image.run_phase(self.directory, "build", ["bazel", "build", *flags, "//:kernel"],
                                            cwd=self.root)
                self.assertEqual(sum(arg.startswith("--execution_log_json_file")
                                     for arg in execute.call_args.args[0]), 1)
                self.assertTrue(self.log.exists())
                self.assertTrue(json.loads(self.destination.read_text())["available"])

    def test_daily_workflow_runs_sequential_pinned_passes_and_uploads_only_safe_reports(self):
        daily = yaml.safe_load((ROOT / ".github/workflows/bazel-vs-cache-daily.yml").read_text())
        trigger = daily.get("on", daily.get(True))
        self.assertEqual(trigger["schedule"], [{"cron": "0 6 * * *"}])
        self.assertIn("workflow_dispatch", trigger)
        self.assertEqual(trigger["push"]["branches"], ["ci/vs-daily-cache-report"])
        self.assertNotIn("pull_request", trigger)
        self.assertNotIn("pull_request_target", trigger)
        first, second = daily["jobs"]["first"], daily["jobs"]["second"]
        self.assertIn("first", second["needs"])
        self.assertIn("!cancelled()", second["if"])
        self.assertEqual(first["with"]["revision"], second["with"]["revision"])
        self.assertTrue(first["with"]["upload_cache"])
        self.assertFalse(second["with"]["upload_cache"])
        worker = yaml.safe_load((ROOT / ".github/workflows/bazel-vs-cache-pass.yml").read_text())["jobs"]["build"]
        self.assertEqual(worker["env"]["SONIC_ENABLE_DEBUG_LAYER"], "n")
        self.assertIn("--disk_cache=", worker["env"]["BAZEL_CONTAINER_ARGS"])
        self.assertEqual(worker["env"]["SONIC_VS_KERNEL_REMOTE_REUSE"], "0")
        self.assertIn("pass_number", worker["defaults"]["run"]["working-directory"])
        scripts = "\n".join(step.get("run", "") for step in worker["steps"])
        self.assertIn("bash tools/bazel/ci/build_vs.sh", scripts)
        self.assertNotIn("docker image prune", scripts)
        self.assertNotIn("docker builder prune", scripts)
        uploads = [step["with"]["path"] for step in worker["steps"]
                   if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(uploads), 1)
        self.assertTrue(uploads[0].endswith(".json"))
        self.assertNotIn("execution", uploads[0])
        self.assertIn("/.vs-cache-probe/", (ROOT / ".gitignore").read_text().splitlines())
        self.assertIn(".vs-cache-probe", (ROOT / ".bazelignore").read_text().splitlines())

    def test_cache_write_policy_allows_both_caches_only_on_trusted_branches_and_events(self):
        worker = yaml.safe_load((ROOT / ".github/workflows/bazel-vs-cache-pass.yml").read_text())["jobs"]["build"]
        policy = next(step for step in worker["steps"]
                      if step.get("name") == "Configure trusted diagnostic cache access")
        cases = (
            ("refs/heads/master", "schedule", "1", True),
            ("refs/heads/master", "workflow_dispatch", "1", True),
            ("refs/heads/ci/vs-daily-cache-report", "push", "1", True),
            ("refs/heads/ci/vs-daily-cache-report", "workflow_dispatch", "1", True),
            ("refs/heads/other-branch", "workflow_dispatch", "1", False),
            ("refs/pull/123/merge", "pull_request", "1", False),
            ("refs/heads/master", "pull_request_target", "1", False),
            ("refs/tags/ci/vs-daily-cache-report", "push", "1", False),
            ("refs/heads/master", "schedule", "0", False),
            ("refs/heads/ci/vs-daily-cache-report", "push", "0", False),
        )
        for ref, event, enabled, writable in cases:
            with self.subTest(ref=ref, event=event, enabled=enabled):
                output = self.root / "github-env"
                output.unlink(missing_ok=True)
                environment = dict(os.environ, GITHUB_REF=ref, GITHUB_EVENT_NAME=event,
                                   DEFAULT_BRANCH="master", PROBE_WRITE_CACHE=enabled, GITHUB_ENV=str(output))
                result = subprocess.run(["bash", "-c", policy["run"]], env=environment,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(),
                                 f'SONIC_BAZEL_REMOTE_UPLOAD={"y" if writable else "n"}\n'
                                 f'SONIC_DPKG_CACHE_METHOD={"rwcache" if writable else "rcache"}\n')


if __name__ == "__main__":
    unittest.main()
