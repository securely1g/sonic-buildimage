"""Check attribution of Bazel's overlapping VS recipe profile events."""

import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.summarize_vs_image_timings import owner_row, summarize_profile


def event(category, name, start, duration, tid=1, mnemonic=None):
    record = {"ph": "X", "pid": 1, "tid": tid, "cat": category, "name": name,
              "ts": start * 1_000_000, "dur": duration * 1_000_000}
    if mnemonic:
        record["args"] = {"mnemonic": mnemonic}
    return record


class ImageTimingSummaryTest(unittest.TestCase):
    def test_compilation_counts_running_processes_and_unions_parallel_workers(self):
        events = [
            event("action processing", "Compiling a.cc", 0, 10, mnemonic="CppCompile"),
            event("general information", "subprocess.run", 2, 5),
            event("action processing", "Cached compile", 0, 18, tid=2, mnemonic="CppCompile"),
            event("Local execution process wall time", "CppCompile", 4, 6, tid=3),
            event("action processing", "Building lib", 7, 7, tid=4, mnemonic="CcCmakeMakeRule"),
            event("general information", "subprocess.run", 9, 4, tid=4),
            event("action processing", "Linking", 11, 3, mnemonic="CppLink"),
            event("general information", "subprocess.run", 12, 1),
        ]
        self.assertEqual(summarize_profile(list(reversed(events))),
                         {"compile": 8, "foreign": 4, "lock": 0, "fetch": 0})

    def test_fetch_excludes_output_reuse_uploads_and_cache_lookups(self):
        events = [
            event("build phase marker", "Blocking on busy Bazel server (in client)", -4, 4),
            event("build phase marker", "Blocking on busy Bazel server (in server)", 0, 2),
            event("remote output download", "download outputs", 2, 5),
            event("remote output download", "Download outputs", 3, 1, tid=2),
            event("remote output download", "stage remote inputs", 6, 3, tid=3),
            event("remote output download", "reuse outputs", 9, 2),
            event("remote output download", "await output reuse", 11, 2),
            event("remote output download", "download server logs", 13, 3),
            event("remote action cache check", "check cache hit", 14, 2),
            event("Remote execution upload time", "upload outputs", 15, 2),
        ]
        self.assertEqual(summarize_profile(events),
                         {"compile": 0, "foreign": 0, "lock": 6, "fetch": 7})

    def test_recipe_uses_independent_profiles_and_handles_failed_or_missing_phases(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "docker-teamd.gz"
            row, issues = owner_row(directory)
            self.assertEqual(row[1:], ["unavailable"] * 6)
            self.assertTrue(issues)
            directory.mkdir()
            for phase in ("build", "cquery-docker-teamd.gz", "cquery-docker-teamd-dbg.gz",
                          "export-docker-teamd.gz", "export-docker-teamd-dbg.gz"):
                (directory / f"{phase}.timing.json").write_text(json.dumps(
                    {"phase": phase, "elapsed_seconds": 2, "exit_code": 0}))
                if not phase.startswith("export"):
                    with gzip.open(directory / f"{phase}.profile.json.gz", "wt") as stream:
                        json.dump({"traceEvents": [event(
                            "build phase marker", "Blocking on busy Bazel server (in server)", 0, 1)
                        ]}, stream)
            row, issues = owner_row(directory)
            self.assertEqual(row[1:], ["6.00", "3.00", "0.00", "0.00", "0.00", "4.00"])
            self.assertEqual(issues, [])
            (directory / "build.profile.json.gz").write_bytes(b"truncated profile")
            (directory / "export-docker-teamd.gz.timing.json").unlink()
            (directory / "build.timing.json").write_text(json.dumps(
                {"phase": "build", "elapsed_seconds": 2, "exit_code": 1}))
            row, issues = owner_row(directory)
            self.assertEqual(row[2], "2.00 (partial)")
            self.assertEqual(row[-1], "2.00 (partial)")
            self.assertIn("build exited 1", issues)
            self.assertIn("build profile missing or unreadable", issues)


if __name__ == "__main__":
    unittest.main()
