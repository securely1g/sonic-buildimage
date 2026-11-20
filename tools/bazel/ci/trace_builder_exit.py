"""Run the selected VS builder normally and time the outer Docker lifecycle."""

import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
import uuid


REPORT = Path("artifacts/vs/builder-exit.json")
STORAGE_HELPER = Path(__file__).with_name("trace_builder_storage.py")
MARKER_IN_BUILDER = "/run/sonic-vs-builder-exit-marker"
ACTIONS = ("create", "start", "die", "destroy")


def read_events(stream, events, started, destroyed):
    # Request only lifecycle fields. Docker's full event JSON includes user labels.
    for line in stream:
        fields = line.split()
        if len(fields) != 3:
            continue
        timestamp, action, container = fields
        if (action in ACTIONS and timestamp.isdigit() and
                re.fullmatch(r"[0-9a-f]{64}", container)):
            events.setdefault("docker_" + action, int(timestamp))
            if action == "start":
                started.append(container)
            if action == "destroy":
                destroyed.set()


def docker_field(*args):
    proc = subprocess.run(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, timeout=5, check=True)
    return proc.stdout.strip()


def observe_init(work, events, started, stopped, status):
    # The start event supplies the exact ID even when unrelated builders share the daemon.
    while not started:
        if stopped.wait(0.1):
            return
    container = started[0]
    pidfd = None
    helper = None
    try:
        pid = int(docker_field("inspect", "--format", "{{.State.Pid}}", container))
        if pid <= 0:
            raise ValueError("no live container init PID")
        status["container_id"] = container
        status["init_host_pid"] = pid
        if STORAGE_HELPER.is_file():
            path = REPORT.with_name("builder-exit-storage.json").absolute()
            path.write_text("")
            try:
                helper = subprocess.Popen([
                    "sudo", "-n", "/usr/bin/python3", "-B", str(STORAGE_HELPER.resolve()),
                    "--pid", str(pid), "--container-id", container, "--marker", str(work / "marker"),
                    "--output", str(path), "--stop", str(work / "stop")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                status["storage_error"] = "could not start storage observer"
        pidfd = os.pidfd_open(pid)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        while not stopped.is_set():
            if poller.poll(200):
                events["init_exited_observed"] = time.time_ns()
                break
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
        status["init_error"] = type(exc).__name__
    finally:
        if pidfd is not None:
            os.close(pidfd)
        if helper is not None:
            # The helper records removal after PID exit; let the build continue meanwhile.
            stopped.wait()
            try:
                status["storage_observer_exit_code"] = helper.wait(timeout=15)
            except subprocess.TimeoutExpired:
                helper.terminate()
                status["storage_error"] = "storage observer did not finish after Docker returned"


def intervals(events):
    # Report signed deltas. CLI return and destroy can race in some daemon versions.
    stages = ["collector_finished", "init_exited_observed", "docker_die",
              "docker_destroy", "docker_run_returned"]
    pairs = [*zip(stages, stages[1:]), ("collector_finished", "docker_die"),
             ("collector_finished", "docker_run_returned")]
    return {a + "_to_" + b: (events[b] - events[a]) / 1e9
            for a, b in pairs if a in events and b in events}


def main(argv):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tag = "io.sonic-buildimage.builder-exit=" + uuid.uuid4().hex
    events = {}
    status = {"schema_version": 1, "mode": os.environ.get("DOCKER_LOAD_ON_BOOT", "n"),
              "runner": os.environ.get("RUNNER_NAME"), "events_epoch_ns": events}
    try:
        status["host_boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        status["engine_version_and_driver"] = docker_field(
            "info", "--format", "{{.ServerVersion}} {{.Driver}}")
    except (OSError, subprocess.SubprocessError) as exc:
        status["host_metadata_error"] = type(exc).__name__

    with tempfile.TemporaryDirectory(prefix="sonic-vs-builder-exit-") as tmp:
        work = Path(tmp)
        marker = work / "marker"
        marker.touch()
        # The private parent directory is mode 0700 on the runner. Inside the
        # builder, the collector writes the single mounted file as its build UID.
        marker.chmod(0o666)
        watch = ["docker", "events", "--since", str(int(time.time())), "--filter", "type=container",
                 "--filter", "label=" + tag, "--format", "{{.TimeNano}} {{.Action}} {{.Actor.ID}}"]
        for action in ACTIONS:
            watch += ["--filter", "event=" + action]
        stopped = threading.Event()
        destroyed = threading.Event()
        started = []
        subscription = subprocess.Popen(watch, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True)
        reader = threading.Thread(target=read_events,
                                  args=(subscription.stdout, events, started, destroyed), daemon=True)
        observer = threading.Thread(target=observe_init,
                                    args=(work, events, started, stopped, status), daemon=True)
        reader.start()
        observer.start()
        try:
            # Keep the CLI and its normal auto-removal in the experiment. Inherit
            # stdin/out/err so slow log processing cannot cause a new exit delay.
            result = subprocess.run([
                "docker", "run", "--label", tag, "--mount",
                f"type=bind,src={marker},dst={MARKER_IN_BUILDER}",
                "--env", f"SONIC_VS_BUILDER_EXIT_MARKER={MARKER_IN_BUILDER}", *argv])
            events["docker_run_returned"] = time.time_ns()
            status["docker_run_exit_code"] = result.returncode
        finally:
            (work / "stop").touch()
            stopped.set()
            # Drain the last daemon event even if it arrives after the CLI response.
            destroyed.wait(timeout=3)
            subscription.terminate()
            try:
                subscription.wait(timeout=2)
            except subprocess.TimeoutExpired:
                subscription.kill()
                subscription.wait()
            reader.join()
            subscription.stdout.close()
            observer.join()
            raw_marker = marker.read_text().strip()
            if raw_marker.isdigit():
                events["collector_finished"] = int(raw_marker)
            status["seconds"] = intervals(events)
            REPORT.write_text(json.dumps(status, indent=2) + "\n")
            print("[ vs timing ] [ builder-exit trace ]", json.dumps(status["seconds"]), flush=True)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
