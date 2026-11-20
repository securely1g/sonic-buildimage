"""Run the selected VS builder normally and time the outer Docker lifecycle."""

import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid


# Direct script entry also works when the host sets PYTHONSAFEPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_vs_workspace import checkout_path


REPORT = Path("artifacts/vs/builder-exit.json")
STORAGE_HELPER = Path(__file__).with_name("trace_builder_storage.py")
MARKER_IN_BUILDER = "/run/sonic-vs-builder-exit-marker"
OUTPUT_MOUNT = "/sonic-vs-bazel-outputs"
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


def prepare_host_outputs():
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    checkout = checkout_path(workspace, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    if Path.cwd() != checkout:
        raise ValueError("Host Bazel outputs require the run-specific VS checkout")
    parent = checkout / ".vs-bazel-outputs"
    parent.mkdir(mode=0o700, exist_ok=True)
    item = parent.lstat()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid() or stat.S_IMODE(item.st_mode) != 0o700:
        raise ValueError("Host Bazel output parent must be a private directory owned by the runner")
    # Each invocation starts empty, including a retry within this checkout.
    source = Path(tempfile.mkdtemp(prefix="builder-", dir=parent))
    for component in ("bazel", "p4rt"):
        (source / component).mkdir(mode=0o700)
    return source


def check_output_mount(container, source):
    mount = docker_field("inspect", "--format",
                         '{{range .Mounts}}{{if eq .Destination "' + OUTPUT_MOUNT +
                         '"}}{{.Type}} {{.RW}} {{.Source}}{{end}}{{end}}', container)
    return mount == "bind true " + str(source)


def output_evidence(source):
    # Only inspect well-known shallow paths, never walk the measured execroots.
    device = source.stat().st_dev
    return {
        "relative_source": str(source.relative_to(Path.cwd())),
        "filesystem_device": f"{os.major(device)}:{os.minor(device)}",
        "bazel_execroot_present": any(p.is_dir() for p in (source / "bazel").glob("_bazel_*/*/execroot")),
        "p4rt_execroot_present": (source / "p4rt/execroot").is_dir(),
    }


def observe_init(work, events, started, stopped, status, output_source):
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
        if output_source is not None:
            status["host_bazel_outputs"]["docker_bind_verified"] = check_output_mount(container, output_source)
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

    output_source = None
    output_args = []
    if os.environ.get("SONIC_VS_HOST_BAZEL_OUTPUTS") == "1":
        output_source = prepare_host_outputs()
        output_args = ["--mount", f"type=bind,src={output_source},dst={OUTPUT_MOUNT}",
                       "--env", f"P4RT_BAZEL_OUTPUT_BASE={OUTPUT_MOUNT}/p4rt"]
        status["host_bazel_outputs"] = {"docker_bind_verified": False}

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
                                    args=(work, events, started, stopped, status, output_source), daemon=True)
        reader.start()
        observer.start()
        try:
            # Keep the CLI and its normal auto-removal in the experiment. Inherit
            # stdin/out/err so slow log processing cannot cause a new exit delay.
            result = subprocess.run([
                "docker", "run", "--label", tag, "--mount",
                f"type=bind,src={marker},dst={MARKER_IN_BUILDER}",
                "--env", f"SONIC_VS_BUILDER_EXIT_MARKER={MARKER_IN_BUILDER}", *output_args, *argv])
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
            if output_source is not None:
                status["host_bazel_outputs"].update(output_evidence(output_source))
            status["seconds"] = intervals(events)
            REPORT.write_text(json.dumps(status, indent=2) + "\n")
            print("[ vs timing ] [ builder-exit trace ]", json.dumps(status["seconds"]), flush=True)
    if result.returncode == 0 and output_source is not None:
        proof = status["host_bazel_outputs"]
        if not all(proof[key] for key in ("docker_bind_verified", "bazel_execroot_present", "p4rt_execroot_present")):
            raise RuntimeError("Host Bazel output benchmark lacks mounted execroot evidence")
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
