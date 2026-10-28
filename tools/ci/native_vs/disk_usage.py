"""Sample allocated filesystem usage during the native VS build.

Measure workspace and Docker storage once per filesystem, not directory sizes
or Docker's logical layer sizes. Results include other activity on those host
filesystems and are sampled maxima, not an instantaneous or build-only peak.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import threading
import time


def sample(paths):
    filesystems = {}
    errors = []
    for path in paths:
        try:
            path = Path(path).resolve(strict=True)
            device = str(path.stat().st_dev)
            if device in filesystems:
                filesystems[device]["paths"].append(str(path))
                continue
            usage = os.statvfs(path)
            mount = path
            while not mount.is_mount():
                mount = mount.parent
            filesystems[device] = {
                "device": device, "paths": [str(path)], "mount": str(mount),
                "total_bytes": usage.f_blocks * usage.f_frsize,
                "used_bytes": (usage.f_blocks - usage.f_bfree) * usage.f_frsize,
                "available_bytes": usage.f_bavail * usage.f_frsize,
            }
        except OSError as error:
            errors.append({"path": str(path), "error": str(error)})
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "filesystems": list(filesystems.values()), "errors": errors,
    }


def summarize(samples, finished, reason):
    baseline = {item["device"]: item for item in samples[0]["filesystems"]} if samples else {}
    complete = [row for row in samples if not row["errors"] and baseline
                and {item["device"] for item in row["filesystems"]} == set(baseline)]
    result = {
        "schema": 1,
        "measurement": "allocated host filesystem usage; shared filesystems counted once",
        "limitations": "Sampled maxima include unrelated host activity and exclude any usage between samples.",
        "finished": finished, "stop_reason": reason,
        "sample_count": len(samples), "complete_sample_count": len(complete),
        "started_at": samples[0]["timestamp"] if samples else None,
        "last_sample_at": samples[-1]["timestamp"] if samples else None,
        "filesystems": [],
    }
    if not complete or (samples and samples[0]["errors"]):
        return result
    initial_used = sum(item["used_bytes"] for item in baseline.values())
    peak = max(complete, key=lambda row: sum(item["used_bytes"] for item in row["filesystems"]))
    peak_used = sum(item["used_bytes"] for item in peak["filesystems"])
    result.update({
        "initial_used_bytes": initial_used,
        "maximum_sampled_used_bytes": peak_used,
        "maximum_sampled_growth_bytes": max(0, peak_used - initial_used),
        "maximum_sampled_at": peak["timestamp"],
        "minimum_sampled_available_bytes": min(sum(item["available_bytes"] for item in row["filesystems"]) for row in complete),
    })
    for device, first in baseline.items():
        values = [item for row in complete for item in row["filesystems"] if item["device"] == device]
        result["filesystems"].append({
            "device": device, "paths": first["paths"], "mount": first["mount"],
            "total_bytes": first["total_bytes"], "initial_used_bytes": first["used_bytes"],
            "maximum_sampled_used_bytes": max(item["used_bytes"] for item in values),
            "maximum_sampled_growth_bytes": max(0, max(item["used_bytes"] for item in values) - first["used_bytes"]),
            "minimum_available_bytes": min(item["available_bytes"] for item in values),
        })
    return result


def save_summary(output, finished, reason):
    samples = []
    path = output / "samples.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                finished = False  # A killed writer may leave a partial last line.
    result = summarize(samples, finished, reason)
    temporary = output / "summary.json.tmp"
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(output / "summary.json")
    return result


def record(args):
    args.output.mkdir(parents=True, exist_ok=True)
    interrupted = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: interrupted.set())
    deadline = time.monotonic() + args.max_duration
    reason = "recorder error"
    stream = (args.output / "samples.jsonl").open("x")
    try:
        with stream:
            while True:
                row = sample(args.path)
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                if row["errors"] and not (args.output / "ready").exists():
                    raise RuntimeError("Cannot measure all requested filesystems: " + str(row["errors"]))
                (args.output / "ready").touch()
                if (args.output / "stop").exists():
                    reason = "requested stop"
                    break
                if interrupted.is_set() or time.monotonic() >= deadline:
                    reason = "signal" if interrupted.is_set() else "duration limit"
                    break
                next_sample = min(deadline, time.monotonic() + args.interval)
                while not interrupted.is_set() and time.monotonic() < next_sample:
                    if (args.output / "stop").exists():
                        break
                    interrupted.wait(min(1, max(0, next_sample - time.monotonic())))
    finally:
        save_summary(args.output, reason == "requested stop", reason)
        (args.output / "done").write_text(reason + "\n")


def finish(args):
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "stop").touch()
    deadline = time.monotonic() + args.wait
    while not (args.output / "done").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not (args.output / "done").exists():
        save_summary(args.output, False, "recorder did not finish before timeout")
        raise SystemExit("Disk recorder did not finish; partial samples retained")
    result = json.loads((args.output / "summary.json").read_text())
    print(json.dumps(result, indent=2))
    if not result["finished"] or result["complete_sample_count"] != result["sample_count"]:
        raise SystemExit("Disk measurement is incomplete; inspect samples and summary")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recorder = commands.add_parser("record")
    recorder.add_argument("--output", type=Path, required=True)
    recorder.add_argument("--path", action="append", required=True)
    recorder.add_argument("--interval", type=float, default=30)
    recorder.add_argument("--max-duration", type=float, default=21600)
    stopper = commands.add_parser("finish")
    stopper.add_argument("--output", type=Path, required=True)
    stopper.add_argument("--wait", type=float, default=30)
    args = parser.parse_args()
    if args.command == "record":
        if args.interval <= 0 or args.max_duration <= 0:
            parser.error("interval and max-duration must be positive")
        record(args)
    else:
        finish(args)


if __name__ == "__main__":
    main()
