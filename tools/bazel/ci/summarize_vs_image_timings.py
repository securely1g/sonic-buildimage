"""Summarize the timed Make image recipes using Bazel 8 JSON trace profiles."""

import argparse
from bisect import bisect_right
from collections import defaultdict
import gzip
import json
import math
from operator import itemgetter
from pathlib import Path


OWNERS = ("docker-fpm-frr.gz", "docker-mux.gz", "docker-teamd.gz", "docker-syncd-vs.gz")
COMPILERS = {"CppCompile", "CppModuleCompile", "Rustc", "GoCompilePkg", "GoCompile"}
CATEGORIES = ("lock", "compile", "foreign", "fetch")
LOCK_EVENTS = {
    "Blocking on busy Bazel server (in client)",
    "Blocking on busy Bazel server (in server)",
}


def union_seconds(intervals):
    """Return the observed wall time, counting active threads only once."""
    total = 0
    previous_end = -math.inf
    for start, end in sorted(intervals):
        total += max(0, end - max(previous_end, start))
        previous_end = max(previous_end, end)
    return total / 1_000_000  # Bazel trace timestamps are in microseconds.


def summarize_profile(events):
    spans = {key: [] for key in CATEGORIES}
    actions = defaultdict(list)
    sandbox_processes = defaultdict(list)
    for event in events:
        if event.get("ph") != "X":
            continue
        start = event["ts"]
        interval = (start, start + event["dur"])
        name = event["name"]
        category = event.get("cat")
        thread = (event["pid"], event["tid"])
        if category == "action processing":
            actions[thread].append((*interval, event.get("args", {}).get("mnemonic", "")))
        elif category == "general information" and name == "subprocess.run":
            sandbox_processes[thread].append(interval)
        elif category == "Local execution process wall time":
            add_process(spans, name, interval)
        elif category == "build phase marker" and name in LOCK_EVENTS:
            spans["lock"].append(interval)
        elif category == "remote output download" and name.lower() in {
            "download outputs", "stage remote inputs",
        }:
            spans["fetch"].append(interval)

    # Sandboxed subprocess.run events carry no mnemonic. Their enclosing action on
    # the same thread supplies it. A cache hit has no compiler subprocess.
    for thread, processes in sandbox_processes.items():
        parents = sorted(actions[thread], key=itemgetter(0))
        for start, end in processes:
            index = bisect_right(parents, start, key=itemgetter(0)) - 1
            if index >= 0 and end <= parents[index][1]:
                add_process(spans, parents[index][2], (start, end))
    return {key: union_seconds(intervals) for key, intervals in spans.items()}


def add_process(spans, mnemonic, interval):
    if mnemonic in COMPILERS:
        spans["compile"].append(interval)
    # rules_foreign_cc constructs Cc<configure_name.capitalize()>MakeRule.
    elif mnemonic.startswith("Cc") and mnemonic.endswith("MakeRule"):
        spans["foreign"].append(interval)


def measured(values, expected):
    if not values:
        return "unavailable"
    suffix = "" if len(values) == expected else " (partial)"
    seconds = sum(values)
    display = "<0.01" if 0 < seconds < 0.01 else f"{seconds:.2f}"
    return display + suffix


def owner_row(directory):
    owner = directory.name
    if not directory.is_dir():
        return [owner] + ["unavailable"] * 6, ["recipe timing directory missing"]
    archives = (owner, owner.removesuffix(".gz") + "-dbg.gz")
    queries = ["cquery-" + archive for archive in archives]
    bazel = ["build", *queries]
    exports = ["export-" + archive for archive in archives]
    elapsed = {}
    profiles = []
    problems = []
    for phase in bazel + exports:
        try:
            timing = json.loads((directory / f"{phase}.timing.json").read_text())
            duration = timing["elapsed_seconds"]
            if timing["phase"] != phase or not math.isfinite(duration) or duration < 0:
                raise ValueError("invalid phase or duration")
            code = timing["exit_code"]
            elapsed[phase] = duration
            if code != 0:
                problems.append(f"{phase} exited {code}")
        except (OSError, ValueError, KeyError, TypeError):
            problems.append(f"{phase} timing missing or unreadable")
    for phase in bazel:
        try:
            with gzip.open(directory / f"{phase}.profile.json.gz", "rt") as stream:
                events = json.load(stream)["traceEvents"]
            if not isinstance(events, list) or not events:
                raise ValueError("empty trace")
            profiles.append(summarize_profile(events))
        except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError):
            problems.append(f"{phase} profile missing or unreadable")

    return [
        owner,
        measured([elapsed[p] for p in bazel if p in elapsed], len(bazel)),
        *(measured([profile[k] for profile in profiles], len(bazel)) for k in CATEGORIES),
        measured([elapsed[p] for p in exports if p in elapsed], len(exports)),
    ], problems


def report(root):
    print("### VS image recipe timings (wall seconds)\n")
    print("| Image pair | Bazel build + queries | Bazel lock wait | Native compiler "
          "subprocesses | Foreign build subprocesses | Cache input/output fetch | Archive export |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    notes = []
    for owner in OWNERS:
        row, problems = owner_row(root / owner)
        print("| " + " | ".join(row) + " |")
        if problems:
            notes.append(f"- `{owner}` — " + ", ".join(problems))
    print("\nBazel wall time includes the build and two output queries. Lock wait covers "
          "both client and server contention. Compiler time covers identified C/C++, Rust "
          "and Go compiler subprocesses. Foreign build time covers the whole rules_foreign_cc "
          "subprocess, including configure, build and install.")
    print("\nCache fetch covers recorded input staging and output materialization from disk "
          "or remote cache. Network-only transfer time is unavailable. Export covers both "
          "Make archive comparisons or copies. Each profile counts concurrent spans within a "
          "column once. Columns and image pairs can overlap. Bazel also spends time on analysis, "
          "linking, packaging and other work. Zero means no matching span was recorded. "
          "Short operations can fall below Bazel’s profiling thresholds.")
    if notes:
        print("\nIncomplete measurements\n")
        print("\n".join(notes))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    report(parser.parse_args().directory)
