"""Reduce BEP and spawn logs to bounded, public cache and timing measurements."""

import gzip
import json
import math
import re


MAX_RECORD = 16 * 1024 * 1024
MAX_INPUT = 512 * 1024 * 1024
MAX_ACTIONS = 3
MAX_NUMBER = 2 ** 53 - 1
MNEMONIC = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
TARGET = re.compile(
    r"(?:(?:@{1,2}[A-Za-z0-9_.+~-]+)?//[A-Za-z0-9_+./~-]*:[A-Za-z0-9_+./~-]+)\Z")
COUNTS = ("action_cache_hits", "action_cache_misses", "bep_actions_executed",
          "spawns", "remote_hits", "disk_hits", "uncached", "other_cached", "failed_spawns")
TIMES = ("wall_seconds", "analysis_seconds", "execution_seconds")


def number(value):
    if type(value) is str and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) not in (int, float) or not 0 <= value <= MAX_NUMBER or not math.isfinite(value):
        raise ValueError("Invalid telemetry number")
    return value


def integer(value):
    value = number(value)
    if type(value) is not int:
        raise ValueError("Invalid telemetry count")
    return value


def duration(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,9})?s", value):
        raise ValueError("Invalid protobuf duration")
    return number(float(value[:-1]))


def records(path):
    """Read concatenated JSON objects, including Bazel's pretty-printed spawns."""
    opener = gzip.open if path.suffix == ".gz" else open
    decoder = json.JSONDecoder()
    with opener(path, "rt", encoding="utf-8") as stream:
        buffer, total, chunk, eof = "", 0, 65536, False
        while True:
            buffer = buffer.lstrip()
            if buffer:
                try:
                    value, end = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    if eof:
                        raise
                else:
                    if not isinstance(value, dict):
                        raise ValueError("Expected a JSON object")
                    yield value
                    buffer, chunk = buffer[end:], 65536
                    continue
            elif eof:
                return
            block = stream.read(chunk)
            total += len(block)
            buffer += block
            if total > MAX_INPUT or len(buffer) > MAX_RECORD:
                raise ValueError("Telemetry input exceeds reporting limit")
            eof = not block
            chunk = min(chunk * 2, 1024 * 1024)


def logfile(directory, phase, kind):
    path = directory / f"{phase}.{kind}.json.gz"
    if not path.exists():
        path = directory / f"{phase}.{kind}.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Telemetry log unavailable")
    return path


def bep_metrics(path):
    metrics, finished, last = None, False, False
    for event in records(path):
        if "buildMetrics" in event:
            if metrics is not None or not isinstance(event["buildMetrics"], dict):
                raise ValueError("Invalid BEP metrics")
            metrics = event["buildMetrics"]
        finished |= "finished" in event
        last |= event.get("lastMessage") is True
    if not finished or not last:
        raise ValueError("Incomplete BEP stream")
    result = {}
    metrics = metrics or {}
    timing = metrics.get("timingMetrics")
    if isinstance(timing, dict):
        result.update(zip(TIMES, (number(timing.get(key, 0)) / 1000 for key in
                                 ("wallTimeInMs", "analysisPhaseTimeInMs", "executionPhaseTimeInMs"))))
    actions = metrics.get("actionSummary", {})
    cache = actions.get("actionCacheStatistics") if isinstance(actions, dict) else None
    if isinstance(cache, dict):
        result.update(action_cache_hits=integer(cache.get("hits", 0)),
                      action_cache_misses=integer(cache.get("misses", 0)),
                      bep_actions_executed=integer(actions.get("actionsExecuted", 0)))
    return result


def spawn_metrics(path):
    result = dict.fromkeys(("spawns", "remote_hits", "disk_hits", "uncached",
                           "other_cached", "failed_spawns"), 0)
    longest = []
    for spawn in records(path):
        if not isinstance(spawn.get("runner"), str) or type(spawn.get("cacheHit")) is not bool:
            raise ValueError("Invalid spawn record")
        hit, runner = spawn["cacheHit"], spawn["runner"]
        result["spawns"] += 1
        category = ("remote_hits" if hit and runner == "remote cache hit" else
                    "disk_hits" if hit and runner == "disk cache hit" else
                    "other_cached" if hit else "uncached")
        result[category] += 1
        exit_code = spawn.get("exitCode", 0)
        if type(exit_code) is not int or not -(2 ** 31) <= exit_code < 2 ** 31:
            raise ValueError("Invalid spawn exit code")
        if exit_code or spawn.get("status", ""):
            result["failed_spawns"] += 1
        mnemonic = spawn.get("mnemonic")
        elapsed = spawn.get("metrics", {}).get("totalTime")
        if not isinstance(mnemonic, str) or not MNEMONIC.fullmatch(mnemonic) or elapsed is None:
            continue
        target = spawn.get("targetLabel")
        if (not isinstance(target, str) or len(target) > 240 or not TARGET.fullmatch(target)
                or any(part in {".", ".."} for part in re.split(r"[/:]", target))):
            target = None
        public_runner = {"remote_hits": "remote-cache", "disk_hits": "disk-cache",
                         "other_cached": "other-cache"}.get(category)
        if public_runner is None:
            public_runner = ("remote" if runner == "remote" else "local" if runner in {
                "local", "linux-sandbox", "darwin-sandbox", "processwrapper-sandbox", "worker",
            } else "other")
        longest.append({"mnemonic": mnemonic, "target": target, "runner": public_runner,
                        "cache_hit": hit, "seconds": duration(elapsed)})
        longest = sorted(longest, key=lambda action: action["seconds"], reverse=True)[:MAX_ACTIONS]
    return result, longest


def summarize(plans):
    expected = [(directory, phase) for directory, plan in plans for phase in plan.get("telemetry", [])]
    if not expected:
        return None
    result = dict.fromkeys(COUNTS + TIMES)
    result.update(expected=len(expected), bep=0, execution=0, timing_count=0,
                  action_cache_count=0, top_actions=[])
    for directory, phase in expected:
        try:
            bep = bep_metrics(logfile(directory, phase, "bep"))
        except (OSError, EOFError, ValueError, TypeError, AttributeError, RecursionError):
            continue
        result["bep"] += 1
        for count, keys in (("timing_count", TIMES), ("action_cache_count", COUNTS[:3])):
            if all(key in bep for key in keys):
                result[count] += 1
                for key in keys:
                    result[key] = (result[key] or 0) + bep[key]
        try:
            spawns, longest = spawn_metrics(logfile(directory, phase, "execution"))
        except (OSError, EOFError, ValueError, TypeError, AttributeError, RecursionError):
            continue
        result["execution"] += 1
        for key, value in spawns.items():
            result[key] = (result[key] or 0) + value
        result["top_actions"] = sorted(result["top_actions"] + longest,
                                       key=lambda action: action["seconds"], reverse=True)[:MAX_ACTIONS]
    return result
