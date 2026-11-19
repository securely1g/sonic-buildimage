"""Check capture, bounded reduction and honest cache-coverage reporting."""

import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import bazel_telemetry as telemetry, time_vs_image
from tools.bazel.ci.summarize_vs_image_timings import report


def bep_events():
    return [
        {"finished": {"exitCode": {"code": 0}}},
        {"lastMessage": True, "buildMetrics": {
            "timingMetrics": {"wallTimeInMs": "10000", "analysisPhaseTimeInMs": "7000",
                              "executionPhaseTimeInMs": "5000"},
            "actionSummary": {"actionsExecuted": "40", "actionCacheStatistics": {"hits": 100, "misses": 40}},
        }},
    ]


def spawn(runner="remote cache hit", cached=True, elapsed="2.500s", **changes):
    return dict({"runner": runner, "cacheHit": cached, "mnemonic": "CppCompile",
                 "targetLabel": "//example:library", "metrics": {"totalTime": elapsed},
                 "exitCode": 0}, **changes)


class BazelTelemetryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = {"bazel": ["build"], "profiles": ["build"], "exports": [],
                     "telemetry": ["build"]}
        self.write("bep", bep_events())

    def write(self, kind, events, compressed=False):
        path = self.root / ("build." + kind + ".json" + (".gz" if compressed else ""))
        opener = gzip.open if compressed else open
        with opener(path, "wt") as stream:
            for value in events:
                stream.write(json.dumps(value, indent=2) + "\n")
        return path

    def summary(self):
        return telemetry.summarize([(self.root, self.plan)])

    def test_separates_cache_layers_preserves_zero_and_bounds_public_actions(self):
        secret = "private-command-and-credentials"
        self.write("execution", [
            spawn(commandArgs=[secret], environmentVariables=[{"name": "TOKEN", "value": secret}]),
            spawn("disk cache hit", True, "0.100s"),
            spawn("linux-sandbox", False, "4s", exitCode=1, status=secret),
            spawn("custom-" + secret, False, "3s", targetLabel="/home/" + secret),
            spawn("other-cache-" + secret, True, "1s"),
        ], compressed=True)
        value = self.summary()
        self.assertEqual((value["bep"], value["execution"], value["timing_count"]), (1, 1, 1))
        self.assertEqual((value["wall_seconds"], value["analysis_seconds"], value["execution_seconds"]),
                         (10, 7, 5))
        self.assertEqual((value["action_cache_hits"], value["action_cache_misses"],
                          value["bep_actions_executed"]), (100, 40, 40))
        self.assertEqual([value[key] for key in ("spawns", "remote_hits", "disk_hits", "uncached",
                                                "other_cached", "failed_spawns")], [5, 1, 1, 2, 1, 1])
        self.assertEqual([item["seconds"] for item in value["top_actions"]], [4, 3, 2.5])
        self.assertIsNone(value["top_actions"][1]["target"])
        self.assertEqual(value["top_actions"][1]["runner"], "other")
        self.assertNotIn(secret, json.dumps(value))

    def test_missing_truncated_and_empty_logs_do_not_become_false_cache_misses(self):
        value = self.summary()
        self.assertEqual((value["bep"], value["execution"]), (1, 0))
        self.assertIsNone(value["remote_hits"])
        self.write("execution", [])
        self.assertEqual((self.summary()["execution"], self.summary()["spawns"]), (1, 0))
        (self.root / "build.execution.json").write_text(json.dumps(spawn()) + '\n{"runner":')
        self.assertEqual(self.summary()["execution"], 0)
        self.assertIsNone(self.summary()["spawns"])
        self.write("execution", [spawn()])
        self.write("bep", bep_events()[:-1])
        value = self.summary()
        self.assertEqual((value["bep"], value["execution"]), (0, 0))
        self.assertIsNone(value["wall_seconds"])
        self.assertIsNone(value["remote_hits"])

    def test_multiple_invocations_sum_only_observed_sources_with_explicit_coverage(self):
        self.write("execution", [spawn()])
        other = self.root / "missing"
        other.mkdir()
        value = telemetry.summarize([(self.root, self.plan), (other, self.plan)])
        self.assertEqual((value["expected"], value["bep"], value["execution"]), (2, 1, 1))
        self.assertEqual(value["remote_hits"], 1)
        self.assertIsNone(telemetry.summarize([(self.root, dict(self.plan, telemetry=[]))]))

    def test_streams_large_records_and_rejects_malformed_or_oversized_inputs(self):
        path = self.write("execution", [spawn(commandArgs=["x" * 150000]), spawn()])
        self.assertEqual(telemetry.spawn_metrics(path)[0]["spawns"], 2)
        with patch.object(telemetry, "MAX_RECORD", 32):
            with self.assertRaises(ValueError):
                list(telemetry.records(path))
        self.write("execution", [spawn(cacheHit="true")])
        self.assertEqual(self.summary()["execution"], 0)
        self.write("execution", [spawn(elapsed="-1s")])
        self.assertEqual(self.summary()["execution"], 0)
        events = bep_events()
        events[1]["buildMetrics"]["actionSummary"]["actionsExecuted"] = str(10 ** 400)
        self.write("bep", events)
        self.assertEqual(self.summary()["bep"], 0)

    def test_complete_failed_build_retains_observed_spawns_and_missing_groups_stay_null(self):
        self.write("bep", [{"finished": {"exitCode": {"code": 1}}}, {"lastMessage": True}])
        self.write("execution", [spawn("linux-sandbox", False, exitCode=-1)])
        value = self.summary()
        self.assertEqual((value["bep"], value["execution"], value["failed_spawns"]), (1, 1, 1))
        self.assertEqual(value["timing_count"], 0)
        self.assertIsNone(value["analysis_seconds"])
        self.assertIsNone(value["action_cache_hits"])

    def test_compression_failure_preserves_raw_log_and_removes_partial_output(self):
        self.write("execution", [spawn()])
        with patch.object(time_vs_image.shutil, "copyfileobj", side_effect=OSError("disk full")):
            time_vs_image.compress_telemetry(self.root, "build")
        self.assertTrue((self.root / "build.execution.json").is_file())
        self.assertFalse((self.root / "build.execution.json.gz.tmp").exists())

    def test_summary_is_bounded_and_exposes_layers_coverage_and_no_repository_hit_claim(self):
        directory = self.root / "timings/docker-example.gz/recipe-one"
        directory.mkdir(parents=True)
        (directory / "recipe.json").write_text(json.dumps(self.plan))
        (directory / "build.bep.json").write_bytes((self.root / "build.bep.json").read_bytes())
        self.write("execution", [spawn()])
        (directory / "build.execution.json").write_bytes((self.root / "build.execution.json").read_bytes())
        receipt, markdown = report(directory.parents[1])
        self.assertEqual(receipt["recipes"][0]["telemetry"]["remote_hits"], 1)
        self.assertIn("Local persistent action-cache hits / misses", markdown)
        self.assertIn("includes remote-cache hits", markdown)
        self.assertIn("different denominators", markdown)
        self.assertIn("do not measure repository download/prepared-cache hit rates", markdown)
        self.assertIn("Analysis span | 7s", markdown)
        header = dict(receipt["recipes"][0])
        header["details"] = dict(header["details"], repositories=[], critical_path=[], phases=[])
        header["owner"] = "x" * 192
        header["telemetry"]["spawns"] = header["telemetry"]["remote_hits"] = 3
        header["telemetry"]["top_actions"] = [{
            "mnemonic": "M" * 64, "target": "//example:".ljust(240, "p"),
            "runner": "remote-cache", "cache_hit": True, "seconds": telemetry.MAX_NUMBER,
        }] * 3
        self.assertLess(len(json.dumps({"recipe": header}).encode()) + 26, 4096)

    def test_wrapper_captures_only_build_test_preserves_stdout_exit_and_compresses_on_failure(self):
        fake = self.root / "bazel.py"
        fake.write_text("""
import json, pathlib, sys
args = sys.argv[1:]
pathlib.Path('args.json').write_text(json.dumps(args))
for flag in ('--build_event_json_file=', '--execution_log_json_file='):
    for arg in args:
        if arg.startswith(flag):
            pathlib.Path(arg[len(flag):]).write_text('{}\\\\n')
print('only-command-stdout')
sys.exit(17 if '--fail' in args else 0)
""")
        for phase, fail in (("build", False), ("build", True), ("test", False), ("cquery-example", False)):
            with self.subTest(phase=phase, fail=fail):
                directory = time_vs_image.start_recipe(self.root, "example", [phase], [phase], [])
                command = [sys.executable, "-B", str(Path(time_vs_image.__file__)), str(directory), phase,
                           sys.executable, str(fake), "--batch", phase.split("-")[0]]
                if fail:
                    command.append("--fail")
                result = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
                self.assertEqual(result.returncode, 17 if fail else 0, result.stderr)
                self.assertEqual(result.stdout, "only-command-stdout\n")
                timing = json.loads((directory / f"{phase}.timing.json").read_text())
                self.assertEqual(timing["exit_code"], result.returncode)
                args = json.loads((self.root / "args.json").read_text())
                for kind, flag in (("bep", "--build_event_json_file="), ("execution", "--execution_log_json_file=")):
                    self.assertEqual(sum(arg.startswith(flag) for arg in args), int(phase in ("build", "test")))
                    if phase in ("build", "test"):
                        self.assertFalse((directory / f"{phase}.{kind}.json").exists())
                        with gzip.open(directory / f"{phase}.{kind}.json.gz", "rt") as stream:
                            self.assertEqual(stream.read(), "{}\\n")


if __name__ == "__main__":
    unittest.main()
