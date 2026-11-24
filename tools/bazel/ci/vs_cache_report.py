"""Publish safe spawn-level evidence for two fresh VS remote-cache builds."""

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import cache_reuse, public_artifacts, summarize_bep
from tools.bazel.ci.artifact_validation import require


def dependency_component(target):
    if target is None:
        return "bookkeeping"
    if target.startswith("@"):
        return target.split("//", 1)[0].lstrip("@").rstrip("+").replace("_", "-")
    return "sonic-buildimage"


def action_output(name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_./+~:@=-]{1,1024}", name)
            and not name.startswith("/") and
            not any(part in (".", "..") for part in name.split("/")), "invalid output")
    public_artifacts.safe_text(name)
    return name


def capture_container_dependencies(workspace, directory, bazel, startup, options, roots, *, budget=60):
    """Query configured dependency labels with a bounded, non-building diagnostic budget."""
    started = time.monotonic()
    containers = {}
    expression = ('("@@" + target.label.workspace_name if target.label.workspace_name else "") '
                  '+ "//" + target.label.package + ":" + target.label.name')
    for archive, targets in sorted(roots.items()):
        public_artifacts.relative_name(archive)
        for target in targets:
            public_artifacts.label(target)
        entry = {"available": False, "dependencies": [], "reason": "query-budget-exhausted"}
        containers[archive.removesuffix(".gz")] = entry
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            continue
        entry["reason"] = "query-unavailable"
        try:
            output = subprocess.check_output(
                [bazel, *startup, "cquery", *options, "--output=starlark",
                 "--starlark:expr=" + expression, "deps(set(" + " ".join(targets) + "))"],
                cwd=workspace, stderr=subprocess.DEVNULL, text=True, timeout=remaining)
            dependencies = sorted({public_artifacts.label(line.strip())
                                   for line in output.splitlines() if line.strip()})
            require(bool(dependencies), "empty container dependency query")
            entry.update(available=True, dependencies=dependencies, reason=None)
        except subprocess.TimeoutExpired:
            entry["reason"] = "query-timeout"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    result = {"containers": containers, "elapsed_seconds": time.monotonic() - started}
    public_artifacts.safe_json(result)
    try:
        (directory / "container-dependencies.json").write_text(json.dumps(result, sort_keys=True) + "\n")
    except OSError:
        print("Container dependency attribution unavailable.", file=sys.stderr)


class MakeTargets:
    """Pair observed Make markers; never infer a cache result from absent markers."""

    def __init__(self):
        self.active = {}
        self.targets = []

    def observe(self, line, when):
        from tools.ci.build_dashboard.breakdown import ANSI, TARGET
        match = TARGET.search(ANSI.sub("", line).strip())
        if match is None:
            return
        outcome, target = match.groups()
        if (not re.fullmatch(r"target/[A-Za-z0-9_./+~:@-]{1,240}", target)
                or any(part in (".", "..") for part in target.split("/"))):
            return
        try:
            public_artifacts.safe_text(target)
        except ValueError:
            return
        if outcome == "building":
            self.active[target] = when if target not in self.active else None
            return
        start = self.active.pop(target, None)
        self.targets.append({"target": target, "outcome": outcome,
                             "duration_seconds": when - start if start is not None else None})

    def finish(self):
        for target in self.active:
            self.targets.append({"target": target, "outcome": "unfinished", "duration_seconds": None})
        return {"available": True, "targets": self.targets}


def record_make(stream, output, destination):
    started = time.monotonic()
    recorder = MakeTargets()
    for line in stream:
        recorder.observe(line, time.monotonic() - started)
        output.write(line)
        output.flush()
    result = recorder.finish()
    public_artifacts.safe_json(result)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, sort_keys=True) + "\n")


def action_key(action):
    return (action.get("owner"), action["target"], action["mnemonic"],
            tuple(action["outputs"]), action["digest"])


def capture(execution_log, build_events, destination):
    """Discard private fields and fail closed without changing the build result."""
    result = {"available": False, "complete": False, "actions": [], "reason": None}
    stage = "execution-log-unavailable"
    try:
        if execution_log.stat().st_size > 512 * 1024 * 1024:
            stage = "oversized-execution-log"
            raise ValueError(stage)
        groups = Counter()
        for record in cache_reuse.json_records(execution_log):
            stage = "invalid-spawn-identity"
            require(isinstance(record, dict), "invalid spawn")
            target = record.get("targetLabel") or None
            if target is not None:
                public_artifacts.label(target)
            mnemonic, runner = record.get("mnemonic"), record.get("runner")
            require(isinstance(mnemonic, str) and
                    re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", mnemonic), "invalid mnemonic")
            require(isinstance(runner, str) and
                    re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", runner), "invalid runner")
            stage = "invalid-spawn-cache-result"
            hit = record.get("cacheHit", False)
            require(type(hit) is bool and
                    hit == (runner in ("remote cache hit", "disk cache hit")), "invalid cache result")
            flags = [record.get(name) for name in ("cacheable", "remoteCacheable")]
            require(all(value is None or type(value) is bool for value in flags), "invalid eligibility")
            eligible = False if False in flags else True if all(flags) else None
            exit_code, status = record.get("exitCode", 0), record.get("status", "")
            require(type(exit_code) is int and isinstance(status, str), "invalid spawn status")
            stage = "invalid-spawn-outputs"
            outputs = record.get("listedOutputs", [])
            require(isinstance(outputs, list), "invalid outputs")
            outputs = tuple(sorted(action_output(name) for name in outputs))
            stage = "invalid-spawn-digest"
            digest_record = record.get("digest") or {}
            require(isinstance(digest_record, dict), "invalid digest")
            digest = digest_record.get("hash") or None
            if digest is not None:
                public_artifacts.digest(digest)
            groups[(target, mnemonic, runner, hit, eligible, bool(exit_code or status),
                    outputs, digest)] += 1
            stage = "execution-log-unavailable"
        actions = [
            dict(zip(("target", "mnemonic", "runner", "cache_hit", "remote_eligible",
                      "failed", "outputs", "digest"), key), outputs=list(key[6]), count=count)
            for key, count in groups.items()
        ]
        stage = "unsafe-spawn-metadata"
        public_artifacts.safe_json(actions)
        result.update(available=True, actions=actions)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        result["reason"] = {
            "execution-log record exceeds 64 MiB": "oversized-spawn-record",
            "truncated or malformed execution log": "invalid-execution-log",
            "duplicate execution-log field": "duplicate-execution-field",
        }.get(str(error), stage)
    result["bep"] = {"available": False, "complete": False}
    try:
        summary = summarize_bep.analyze(build_events)
        finished = summary["stream_complete"] and summary["command"] == "build"
        result["complete"] = result["available"] and finished
        runners = summary["actions"]["runners"]
        if "total" in runners:
            remote, disk = (runners.get(name, 0) for name in ("remote cache hit", "disk cache hit"))
            executed = sum(count for name, count in runners.items()
                           if name not in ("total", "internal", "remote cache hit", "disk cache hit"))
            require(runners["total"] == remote + disk + executed + runners.get("internal", 0),
                    "inconsistent runner counts")
            result["bep"] = {
                "available": True, "complete": finished,
                "counts": {"logged_spawns": remote + disk + executed, "remote_hits": remote,
                           "disk_hits": disk, "executed": executed,
                           "explicitly_ineligible": None, "failed": None},
            }
            if result["available"] and any(
                    counts(result["actions"])[key] != result["bep"]["counts"][key]
                    for key in ("remote_hits", "disk_hits")):
                result.update(available=False, complete=False, actions=[],
                              reason="spawn-metrics-mismatch")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        pass
    if not result["complete"] and result["reason"] is None:
        result["reason"] = "incomplete-build-events"
    result["usable_complete"] = result["complete"] or result["bep"]["complete"]
    if not result["available"]:
        print(f'Spawn detail unavailable ({result["reason"]}); '
              f'BEP runner metrics {"available" if result["bep"]["complete"] else "unavailable"}.',
              file=sys.stderr)
    destination.write_text(json.dumps(result, sort_keys=True) + "\n")


def counts(actions):
    return {
        "logged_spawns": sum(action["count"] for action in actions),
        "remote_hits": sum(action["count"] for action in actions if action["runner"] == "remote cache hit"),
        "disk_hits": sum(action["count"] for action in actions if action["runner"] == "disk cache hit"),
        "executed": sum(action["count"] for action in actions if not action["cache_hit"]),
        "explicitly_ineligible": sum(action["count"] for action in actions
                                     if not action["cache_hit"] and action["remote_eligible"] is False),
        "failed": sum(action["count"] for action in actions if action["failed"]),
    }


def summarize(root, revision, pass_number, status, duration, build_log=None, make_records=None):
    require(public_artifacts.REVISION.fullmatch(revision), "invalid revision")
    actions, phases, captures, incomplete = [], [], 0, 0
    attribution = {"available_queries": 0, "unavailable_queries": 0, "missing_maps": 0,
                   "elapsed_seconds": 0, "reasons": {}}
    paths = sorted(root.glob("*/recipe-*/build.cache.json"))
    for path in paths:
        data = public_artifacts.load_json(path.read_text())
        captures += 1
        incomplete += not data.get("usable_complete", data["available"] and data["complete"])
        owner = path.relative_to(root).parts[0]
        source, observed = "unavailable", None
        if data["available"] and data["complete"]:
            source, observed = "spawn log", counts(data["actions"])
        elif data.get("bep", {}).get("available") and data["bep"]["complete"]:
            source, observed = "BEP runner metrics", data["bep"]["counts"]
        phases.append({"owner": owner, "source": source, "counts": observed,
                       "complete": data.get("usable_complete", data["complete"]),
                       "reason": data.get("reason")})
        dependencies = {}
        mapping = path.with_name("container-dependencies.json")
        if mapping.is_file():
            graph = public_artifacts.load_json(mapping.read_text())
            attribution["elapsed_seconds"] += graph["elapsed_seconds"]
            for container, entry in graph["containers"].items():
                attribution["available_queries" if entry["available"] else "unavailable_queries"] += 1
                if entry.get("reason"):
                    reason = entry["reason"]
                    attribution["reasons"][reason] = attribution["reasons"].get(reason, 0) + 1
                if entry["available"]:
                    dependencies[container] = set(entry["dependencies"])
        elif owner == "vs-container-images":
            attribution["missing_maps"] += 1
        for action in data["actions"]:
            containers = sorted(container for container, labels in dependencies.items() if action["target"] in labels)
            if not dependencies and owner.endswith(".gz"):
                containers = [owner.removesuffix(".gz")]
            actions.append({**action, "owner": owner, "component": dependency_component(action["target"]),
                            "containers": containers})
    missing = sum(not path.with_name("build.cache.json").is_file()
                  for path in root.glob("*/recipe-*/build.timing.json"))
    warnings = Counter()
    if build_log is not None and build_log.is_file():
        with build_log.open(errors="replace") as stream:
            for line in stream:
                for name in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "lost inputs"):
                    if name in line:
                        warnings[name] += 1
    version = root.resolve().parents[2] / ".bazelversion"
    totals = {
        key: (sum(phase["counts"][key] for phase in phases)
              if phases and all(phase["counts"] is not None and phase["counts"][key] is not None
                                for phase in phases) else None)
        for key in counts([])
    }
    runner = os.environ.get("RUNNER_NAME")
    if runner is not None:
        require(re.fullmatch(r"[A-Za-z0-9_. -]{1,160}", runner), "invalid runner name")
    result = {
        "schema": 1, "revision": revision, "pass_number": pass_number, "status": status,
        "configuration": {
            "image_version": os.environ.get("SONIC_IMAGE_VERSION"),
            "debug_layer": os.environ.get("SONIC_ENABLE_DEBUG_LAYER"),
            "source_date_epoch": os.environ.get("SOURCE_DATE_EPOCH"),
            "bazel_version": version.read_text().strip() if version.is_file() else None,
        },
        "duration_seconds": duration, "captures": captures,
        "incomplete_captures": incomplete, "missing_captures": missing,
        "complete": status == "success" and captures > 0 and incomplete == 0 and missing == 0,
        "counts": totals, "warning_lines": dict(warnings), "actions": actions,
        "phases": phases, "runner": runner,
        "spawn_details_complete": captures > 0 and all(phase["source"] == "spawn log" and phase["complete"]
                                                       for phase in phases)
                                  and missing == 0,
        "container_attribution": attribution,
        "make": (public_artifacts.load_json(make_records.read_text())
                 if make_records is not None and make_records.is_file()
                 else {"available": False, "targets": []}),
    }
    public_artifacts.safe_json(result)
    return result


def compare(first, second):
    passes = [value for value in (first, second) if value is not None]
    comparable = (len(passes) == 2 and first["pass_number"] == 1 and second["pass_number"] == 2
                  and first["revision"] == second["revision"] and
                  first["configuration"] == second["configuration"] and
                  all(value["complete"] for value in passes))
    prior = {action_key(action) for action in (first or {}).get("actions", []) if not action["cache_hit"]}
    executed = [{**action, "same_group_executed_in_first":
                 True if action_key(action) in prior else
                 False if first and first.get("spawn_details_complete", first["complete"]) else None}
                for action in (second or {}).get("actions", []) if not action["cache_hit"]]
    components = {}
    make_targets = {}
    for index, value in enumerate((first, second)):
        if value is None:
            continue
        grouped = defaultdict(list)
        for action in value["actions"]:
            for container in action.get("containers") or [action["owner"]]:
                grouped[(container, dependency_component(action["target"]))].append(action)
        for (container, component), actions in grouped.items():
            row = components.setdefault((container, component), {
                "container_or_phase": container, "component": component, "first": None, "second": None})
            row[("first", "second")[index]] = counts(actions)
        grouped_make = defaultdict(list)
        for target in value.get("make", {}).get("targets", []):
            grouped_make[target["target"]].append(target)
        for target, records in grouped_make.items():
            row = make_targets.setdefault(target, {"target": target, "first": None, "second": None})
            durations = [record["duration_seconds"] for record in records]
            row[("first", "second")[index]] = {
                "outcomes": dict(Counter(record["outcome"] for record in records)),
                "duration_seconds": sum(durations) if all(value is not None for value in durations) else None}
    reasons = []
    for number, value in enumerate((first, second), 1):
        if value is None:
            reasons.append(f"Pass {number} report is missing.")
        elif value["status"] != "success":
            reasons.append(f"Pass {number} build did not succeed.")
        elif not value["complete"]:
            reasons.append(f"Pass {number} cache telemetry is incomplete.")
    if len(passes) == 2:
        if first["revision"] != second["revision"]:
            reasons.append("Build revisions differ.")
        if first["configuration"] != second["configuration"]:
            reasons.append("Build configurations differ.")
        if first["pass_number"] != 1 or second["pass_number"] != 2:
            reasons.append("Reports have incorrect pass numbers.")
    return {"schema": 1, "comparable": comparable, "passes": passes, "reasons": reasons,
            "second_executed_groups": executed,
            "components": [components[key] for key in sorted(components)],
            "make_targets": [make_targets[key] for key in sorted(make_targets)]}


def cache_cell(value):
    return "not observed" if value is None else f'{value["remote_hits"]} / {value["executed"]}'


def make_cell(value):
    if value is None:
        return "not observed"
    return ", ".join(("unfinished (no ending marker)" if outcome == "unfinished" else f"`[ {outcome} ]`")
                     + (f" x{count}" if count > 1 else "")
                     for outcome, count in sorted(value["outcomes"].items()))


def duration_cell(value):
    return "unknown" if value is None or value["duration_seconds"] is None else f'{value["duration_seconds"]:.1f}'


def elapsed(seconds):
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes}m {seconds:02d}s"


def observed_counts(value):
    if not value.get("phases") and not value["complete"]:
        return {key: None for key in counts([])}
    return value["counts"]


def number(value):
    return "n/a" if value is None else f"{value:,}"


def markdown(report):
    lines = [
        "## VS remote-cache probe",
        "",
        ("**Cache telemetry available.**" if report["comparable"] and len(report["passes"]) == 1 else
         "**Cache comparison available.**" if report["comparable"] else
         "**Cache comparison unavailable: Incomplete or non-comparable evidence.**"),
    ]
    for reason in report.get("reasons", []):
        lines.append(f"- {reason}")
    lines += ["", "Two separate build workspaces at a pinned revision; shared Bazel/NFS caches. "
              "First pass reads/writes; second pass reads only. The shared cache is not cleared.",
              "", "| Pass | Installer build | Build time | Remote hits | Executed | Remote reuse |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for value in report["passes"]:
        count = observed_counts(value)
        total = (count["remote_hits"] + count["disk_hits"] + count["executed"]
                 if all(count[key] is not None for key in ("remote_hits", "disk_hits", "executed")) else 0)
        reuse = f'{count["remote_hits"] / total:.1%}' if total else "n/a"
        lines.append(f'| {value["pass_number"]} | **{value["status"]}** | '
                     f'{elapsed(value["duration_seconds"])} | {number(count["remote_hits"])} | '
                     f'{number(count["executed"])} | {reuse} |')
    if len(report["passes"]) == 2:
        first, second = report["passes"]
        if (all(value["status"] == "success" for value in (first, second)) and
                first["revision"] == second["revision"] and first["configuration"] == second["configuration"]
                and first["duration_seconds"] > 0):
            delta = second["duration_seconds"] - first["duration_seconds"]
            lines += ["", f'Second build: **{elapsed(abs(delta))} '
                      f'({"slower" if delta > 0 else "faster"} by '
                      f'{abs(delta) / first["duration_seconds"]:.1%})**. '
                      "Observed build time only; runner differences and reporting overhead can affect it."]
    lines += ["", "Remote reuse is remote hits / (remote hits + disk hits + executed processes) "
              "in instrumented Bazel build phases, **not** a server request hit rate or whole-installer coverage. "
              "**Executed does not prove a remote miss**: ineligible actions, fallback and changed keys "
              "can execute. Local action-cache/BEP counters are not remote-cache hit counters. "
              "Missing counts are **n/a**, never zero."]
    for value in report["passes"]:
        sources = ", ".join(sorted({phase["source"] for phase in value.get("phases", [])})) or "unavailable"
        lines += ["", f'Pass {value["pass_number"]}: revision `{value["revision"][:12]}`; '
                  f'runner `{value.get("runner") or "not recorded"}`; counts: **{sources}**.']
    if any(not value.get("spawn_details_complete", value["complete"]) for value in report["passes"]):
        lines += ["", "**Per-action detail is incomplete.** Complete BEP runner metrics may still establish "
                  "aggregate remote reuse; they cannot identify every dependency/output that executed."]

    rows = report.get("components", [])
    if rows:
        lines += ["", "### Bazel dependency components by container", "",
                  "Cells show remote hits / executed spawns from available spawn logs. "
                  "Shared dependencies appear for every consuming container; rows are **not additive**. "
                  "Attribution uses configured dependency-label membership; unattributed work retains its phase.",
                  "", "| Container or phase | Dependency component | First | Second |",
                  "| --- | --- | ---: | ---: |"]
        for row in sorted(rows, key=lambda row: (-(row["second"] or {}).get("executed", 0),
                                                 row["container_or_phase"], row["component"]))[:15]:
            lines.append(f'| {row["container_or_phase"]} | {row["component"]} | '
                         f'{cache_cell(row["first"])} | {cache_cell(row["second"])} |')
    candidates = [action for action in report["second_executed_groups"]
                  if action["remote_eligible"] is not False and action["target"] is not None]
    if candidates:
        lines += ["", "<details>", "<summary>Second-pass executed groups (possible misses; top 25)</summary>", "",
                  "| Container or phase | Dependency target | Action | Declared outputs | Count | Also executed in first |",
                  "| --- | --- | --- | --- | ---: | --- |"]
    for action in sorted(candidates, key=lambda item: (-item["count"], str(action_key(item))))[:25]:
        consumers = ", ".join(action.get("containers") or [action["owner"]])
        outputs = ", ".join(f"`{name}`" for name in action["outputs"][:3]) or "not recorded"
        if len(action["outputs"]) > 3:
            outputs += f' (+{len(action["outputs"]) - 3} more in JSON)'
        repeated = action["same_group_executed_in_first"]
        repeated = "unknown" if repeated is None else "yes" if repeated else "no"
        lines.append(f'| {consumers} | `{action["target"]}` | {action["mnemonic"]} | '
                     f'{outputs} | {action["count"]} | {repeated} |')
    if candidates:
        lines += ["", "</details>"]
    lines += ["", "### Make package/target reuse", "",
              "`[ cached ]` is a Make-level cache hit; `[ finished ]` means the recipe completed, "
              "possibly using Bazel remote hits. Neither finished nor missing markers prove a cache miss.",
              "", "| Pass | Cached records | Finished records | Unfinished records |",
              "| --- | ---: | ---: | ---: |"]
    for value in report["passes"]:
        make = value.get("make", {})
        outcomes = Counter(target["outcome"] for target in make.get("targets", []))
        cells = [number(outcomes[key]) if make.get("available") else "n/a"
                 for key in ("cached", "finished", "unfinished")]
        lines.append(f'| {value["pass_number"]} | {" | ".join(cells)} |')
    make_rows = [row for row in report.get("make_targets", []) if not row["target"].endswith("-install")]
    if make_rows:
        lines += ["", "Slowest second-pass targets (top 12; install-helper rows omitted). "
                  "Paired marker times can overlap and are **not additive**; missing timing pairs are unknown.",
                  "", "| Make target | First outcome | Second outcome | First seconds | Second seconds |",
                  "| --- | --- | --- | ---: | ---: |"]
    for row in sorted(make_rows,
                      key=lambda row: (-((row["second"] or {}).get("duration_seconds") or 0), row["target"]))[:12]:
        lines.append(f'| `{row["target"]}` | {make_cell(row["first"])} | {make_cell(row["second"])} | '
                     f'{duration_cell(row["first"])} | {duration_cell(row["second"])} |')
    lines += ["", "<details>", "<summary>Telemetry diagnostics</summary>", ""]
    for value in report["passes"]:
        attribution = value.get("container_attribution", {})
        lines += [f'**Pass {value["pass_number"]}:** {value["captures"]} captures; '
                  f'{value["incomplete_captures"]} incomplete, {value["missing_captures"]} missing. '
                  f'Container queries: {attribution.get("available_queries", 0)} available, '
                  f'{attribution.get("unavailable_queries", 0)} unavailable; '
                  f'{attribution.get("elapsed_seconds", 0):.1f}s overhead included in build time.',
                  f'Query reasons: `{json.dumps(attribution.get("reasons", {}), sort_keys=True)}`. '
                  f'Warning-line counts: `{json.dumps(value["warning_lines"], sort_keys=True)}`.', ""]
        for phase in value.get("phases", []):
            lines.append(f'- `{phase["owner"]}`: {phase["source"]}; '
                         f'{phase["reason"] or ("complete" if phase["complete"] else "incomplete-build-events")}.')
    lines += ["", "</details>", "",
              "The comparison JSON artifact contains all recorded groups, declared outputs and Make targets. "
              "No cache-hit percentage threshold is enforced."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    single = commands.add_parser("summarize")
    single.add_argument("--root", type=Path, required=True)
    single.add_argument("--revision", required=True)
    single.add_argument("--pass-number", type=int, choices=(1, 2), required=True)
    single.add_argument("--status", choices=("success", "failure", "cancelled", "skipped"), required=True)
    single.add_argument("--duration", type=float, default=0)
    single.add_argument("--build-log", type=Path)
    single.add_argument("--make-records", type=Path)
    recorder = commands.add_parser("record-make")
    recorder.add_argument("--output", type=Path, required=True)
    pair = commands.add_parser("compare")
    pair.add_argument("--first", type=Path, required=True)
    pair.add_argument("--second", type=Path, required=True)
    for command in (single, pair):
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "record-make":
        sys.stdin.reconfigure(errors="replace")
        record_make(sys.stdin, sys.stdout, args.output)
        return 0
    if args.command == "summarize":
        result = summarize(args.root, args.revision, args.pass_number, args.status,
                           args.duration, args.build_log, args.make_records)
        view = compare(result, None) if args.pass_number == 1 else compare(None, result)
        view["comparable"] = result["complete"]
        view["reasons"] = ([] if result["complete"] else
                           [f'Pass {args.pass_number} build or cache telemetry is incomplete.'])
    else:
        values = [public_artifacts.load_json(path.read_text()) if path.is_file() else None
                  for path in (args.first, args.second)]
        result = view = compare(*values)
    public_artifacts.safe_json(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    args.summary.write_text(markdown(view))
    if args.command == "compare" and not result["comparable"]:
        print("Cache comparison unavailable: " + " ".join(result["reasons"]), file=sys.stderr)
    return int(args.command == "compare" and not result["comparable"])


if __name__ == "__main__":
    sys.exit(main())
