"""Exercise the host wrapper with a disposable process representing builder init."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import trace_builder_exit


class BuilderExitTraceTest(unittest.TestCase):
    def test_docker_status_and_measured_process_exit_survive_tracing(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            root = Path(tmp)
            fake = root / "docker"
            fake.write_text("""#!/usr/bin/env python3
from pathlib import Path
import subprocess, sys, time
args = sys.argv[1:]
cid = "f" * 64
events = Path("events")
def event(name):
    with events.open("a") as out:
        print(time.time_ns(), name, cid, file=out)
if args[0] == "info":
    print("29.1.3 overlay2")
elif args[0] == "events":
    assert "--filter" in args and "type=container" in args and "--since" in args
    assert "{{.TimeNano}} {{.Action}} {{.Actor.ID}}" in args
    events.touch()
    with events.open() as f:
        while True:
            line = f.readline()
            if line: print(line, end="", flush=True)
            else: time.sleep(.01)
elif args[0] == "inspect":
    print(Path("pid").read_text())
    Path("inspected").touch()
elif args[0] == "run":
    assert "--rm=true" in args and "--init" in args
    assert args[args.index("--label")+1].startswith("io.sonic-buildimage.builder-exit=")
    child = subprocess.Popen(["sleep", "5"])
    Path("pid").write_text(str(child.pid))
    event("create")
    event("start")
    for i in range(200):
        if Path("inspected").exists(): break
        time.sleep(.01)
    assert Path("inspected").exists()
    marker = args[args.index("--mount")+1].split("src=",1)[1].split(",dst=")[0]
    Path(marker).write_text(str(time.time_ns()))
    time.sleep(.15)
    child.terminate()
    child.wait()
    time.sleep(.15)
    event("die")
    time.sleep(.02)
    event("destroy")
    sys.exit(17)
else:
    sys.exit(2)
""")
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, PATH=tmp + os.pathsep + os.defpath), \
                    mock.patch.object(trace_builder_exit, "STORAGE_HELPER", root / "unused.py"):
                rc = trace_builder_exit.main(["--rm=true", "--init", "--env", "EXAMPLE=keep-private", "image"])
            self.assertEqual(rc, 17)
            report = json.loads(trace_builder_exit.REPORT.read_text())
            self.assertEqual(17, report["docker_run_exit_code"])
            self.assertNotIn("keep-private", str(report))
            events = report["events_epoch_ns"]
            ordered = [events[key] for key in (
                "docker_create", "docker_start", "collector_finished", "init_exited_observed",
                "docker_die", "docker_destroy", "docker_run_returned")]
            self.assertEqual(ordered, sorted(ordered))
            self.assertGreater(report["seconds"]["init_exited_observed_to_docker_die"], .05)

    def test_event_fields_are_allowlisted_and_missing_observations_stay_missing(self):
        from threading import Event

        raw = io.StringIO("123 exec_start private-content\n456 die invalid-id\n")
        events = {}
        trace_builder_exit.read_events(raw, events, [], Event())
        self.assertEqual(events, {})
        self.assertEqual(trace_builder_exit.intervals({"collector_finished": 1, "docker_die": 1000000001}),
                         {"collector_finished_to_docker_die": 1.0})
