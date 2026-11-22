#!/usr/bin/env python3
"""Run one ephemeral VS listener, then recover cleanup before admitting another."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from time import sleep
from urllib.error import HTTPError, URLError
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.cleanup_runner_bazel_cache import cleanup as cleanup_caches
from tools.bazel.ci.cleanup_vs_workspace import cleanup as cleanup_checkout
from tools.bazel.ci.cleanup_vs_workspace import checkout_path
from tools.ci.runner.vs_cleanup_client import request


LABEL = "vs-cleanup-canary"
HOST_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LIMIT = 65536
FINAL_RESULTS = {"success", "failure", "cancelled", "timed_out", "action_required",
                 "neutral", "skipped", "stale"}
IDLE_STOP_VERSION = "2.338.2"
IDLE_STOP_PROOF = {"runner_version": IDLE_STOP_VERSION, "output_complete": True,
                   "no_job_started": True, "orderly_exit": True}


def positive(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("Expected a positive numeric identifier")
    return value


def trusted(path, directory=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Trusted paths must be absolute and not redirected")
    for entry in (path, *path.parents):
        metadata = entry.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError(f"Trusted path is not administrator-owned: {entry}")
    if directory != path.is_dir():
        raise ValueError("Unexpected trusted path type")
    return path


def atomic_json(path, value):
    temporary = path.with_suffix(".new")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ListenerOutput:
    """Observe the listener's pre-worker job announcement, not workflow output."""

    def __init__(self):
        self.mutex = threading.Lock()
        self.version = None
        self.versions = 0
        self.listening = 0
        self.exiting = 0
        self.success = 0
        self.job_started = False
        self.error = False
        self.complete = False

    def read(self, stream):
        try:
            while True:
                raw = stream.readline(LIMIT + 1)
                if not raw:
                    with self.mutex:
                        self.complete = True
                    break
                if not isinstance(raw, bytes):
                    raise ValueError("Unexpected listener output")
                try:
                    line = raw.decode("utf-8")
                except UnicodeError:
                    with self.mutex:
                        self.error = True
                    line = raw.decode("utf-8", errors="replace")
                with self.mutex:
                    if len(raw) > LIMIT or not raw.endswith(b"\n"):
                        self.error = True
                    version = re.fullmatch(r"Current runner version: '([^']+)'\n", line)
                    if version:
                        self.version = version[1]
                        self.versions += 1
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z: Listening for Jobs\n", line):
                        self.listening += 1
                    if "Running job:" in line:
                        self.job_started = True
                    if line == "Exiting...\n":
                        self.exiting += 1
                    if line == "Runner listener exit with 0 return code, stop the service, no retry needed.\n":
                        self.success += 1
                    if line == "Restarting runner...\n":
                        self.error = True
                print(line, end="", flush=True)
        except (OSError, ValueError):
            with self.mutex:
                self.error = True

    def ready(self):
        with self.mutex:
            return (self.version == IDLE_STOP_VERSION and self.versions == 1
                    and self.listening == 1 and not self.job_started and not self.error)

    def proof(self):
        with self.mutex:
            return (self.version == IDLE_STOP_VERSION and self.versions == 1
                    and self.listening == 1 and not self.job_started and not self.error
                    and self.complete and self.exiting == 1 and self.success == 1)


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Host GitHub API request failed (HTTP {status_code})")


class GitHub:
    def __init__(self, config):
        self.repository = config["repository"]
        self.registration_scope = config.get("registration_scope", "repo")
        self.token_file = trusted(config["token_file"])
        if self.token_file.stat().st_mode & 0o077:
            raise ValueError("GitHub credential must be private to root")

    def api(self, path, post=False):
        token = self.token_file.read_text().strip()
        if not token or "\n" in token:
            raise ValueError("Invalid host credential")
        request = urllib.request.Request(
            "https://api.github.com/" + path,
            data=b"" if post else None,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            method="POST" if post else "GET")
        delays = (5, 15)
        for attempt in range(len(delays) + 1):
            failure = RuntimeError("Host GitHub API request failed")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as error:
                failure = GitHubAPIError(error.code)
                retry = error.code in (500, 502, 503, 504)
                error.close()
            except (URLError, TimeoutError, ConnectionError) as error:
                reason = error.reason if isinstance(error, URLError) else error
                retry = (isinstance(reason, (TimeoutError, ConnectionError))
                         or (isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN))
            except Exception:
                # Do not include headers, credentials or HTTP response bodies in logs.
                raise failure from None
            if post or not retry or attempt == len(delays):
                raise failure from None
            print(f"Transient host GitHub API read failure; retrying attempt "
                  f"{attempt + 2}/3 in {delays[attempt]}s", flush=True)
            sleep(delays[attempt])

    def registration(self):
        if self.registration_scope == "org":
            scope = "orgs/" + self.repository.split("/")[0]
        else:
            scope = "repos/" + self.repository
        token = self.api(scope + "/actions/runners/registration-token", post=True)["token"]
        if not isinstance(token, str) or not token or "\n" in token:
            raise ValueError("Invalid registration token response")
        return token

    def runner(self, runner_id):
        if self.registration_scope == "org":
            scope = "orgs/" + self.repository.split("/")[0]
        else:
            scope = "repos/" + self.repository
        return self.api(f"{scope}/actions/runners/{runner_id}")

    def conclusion(self, record, runner_id, job_name):
        matches = []
        for page in range(1, 101):
            path = (f"repos/{self.repository}/actions/runs/{record['run_id']}"
                    f"/attempts/{record['run_attempt']}/jobs?per_page=100&page={page}")
            jobs = self.api(path)["jobs"]
            matches.extend(job for job in jobs
                           if job["runner_id"] == runner_id and job["name"] == job_name)
            if len(jobs) < 100:
                break
        else:
            raise ValueError("Job lookup exceeded the bounded page limit")
        if len(matches) != 1:
            raise ValueError("Cannot uniquely identify the enrolled job")
        job = matches[0]
        if job["status"] != "completed":
            return None
        if job["conclusion"] not in FINAL_RESULTS:
            raise ValueError("Unknown final job result")
        return job["conclusion"]

    def verify_artifacts(self, record):
        for artifact_id in record["artifact_ids"]:
            artifact = self.api(f"repos/{self.repository}/actions/artifacts/{artifact_id}")
            if artifact["expired"] or str(artifact["workflow_run"]["id"]) != record["run_id"]:
                raise ValueError("Published artifact does not belong to the enrolled run")


class Supervisor:
    def __init__(self, config, state_dir, github=None):
        self.config = config
        self.account = pwd.getpwnam(config["runner_user"])
        if self.account.pw_uid == 0:
            raise ValueError("The listener must not run as root")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", config["repository"]):
            raise ValueError("Invalid repository")
        if config.get("registration_scope", "repo") not in ("repo", "org"):
            raise ValueError("Invalid registration scope")
        self.runner_dir = Path(config["runner_dir"])
        self.work_root = Path(config["work_root"])
        for path in (self.runner_dir, self.work_root):
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise ValueError("Runner and work roots must be existing canonical directories")
        if self.runner_dir == self.work_root or self.runner_dir.is_relative_to(self.work_root):
            raise ValueError("Runner installation must be outside the job work root")
        repository = config["repository"].split("/")[1]
        self.workspace = self.work_root / repository / repository
        self.state_dir = Path(state_dir)
        self.journal = self.state_dir / "pending.json"
        self.state = json.loads(self.journal.read_text()) if self.journal.exists() else None
        self.mutex = threading.RLock()
        self.github = github or GitHub(config)
        self.stop_requested = threading.Event()
        self.listener_output = ListenerOutput()

    def save(self):
        with self.mutex:
            atomic_json(self.journal, self.state)

    def handoff(self, message, uid):
        if message == {"action": "stop-idle"}:
            return self.stop_idle(uid)
        if uid != self.account.pw_uid:
            raise ValueError("Unexpected peer account")
        if set(message) != {"action", "run_id", "run_attempt", "artifact_id"}:
            raise ValueError("Invalid handoff fields")
        run_id, attempt = positive(message["run_id"]), positive(message["run_attempt"])
        artifacts = message["artifact_id"]
        if not isinstance(artifacts, list) or len(artifacts) > 3:
            raise ValueError("Invalid artifact identifiers")
        for artifact in artifacts:
            positive(artifact)
        with self.mutex:
            if not self.state or self.state["phase"] != "listening":
                raise ValueError("No active listener session")
            record = self.state.get("record")
            if message["action"] == "enroll" and not artifacts:
                self.check_workspace()
                checkout = checkout_path(self.workspace, run_id, attempt)
                identity = checkout.stat()
                candidate = {"run_id": run_id, "run_attempt": attempt,
                             "device": identity.st_dev, "inode": identity.st_ino,
                             "artifact_ids": []}
                if record and record != candidate:
                    raise ValueError("Another job has already enrolled")
                self.state["record"] = candidate
            elif message["action"] == "authorize" and len(artifacts) in (2, 3):
                if not record or (run_id, attempt) != (record["run_id"], record["run_attempt"]):
                    raise ValueError("Job did not enroll")
                record["artifact_ids"] = artifacts
            else:
                raise ValueError("Invalid handoff action")
            self.save()

    def idle_runner(self, runner_id, status):
        runner = self.github.runner(runner_id)
        if (not isinstance(runner, dict) or not isinstance(runner.get("labels"), list)
                or any(not isinstance(label, dict) for label in runner["labels"])):
            raise ValueError("Invalid canary identity response during idle stop")
        if (runner.get("id") != runner_id or runner.get("name") != self.config["runner_name"]
                or runner.get("status") not in ("online", "offline")
                or (status is not None and runner["status"] != status)
                or runner.get("busy") is not False
                or {label["name"] for label in runner["labels"]} != {LABEL}):
            raise ValueError("Idle stop requires the exact canary runner to be unoccupied")
        return runner

    def stop_idle(self, uid):
        if uid != 0:
            raise ValueError("Idle stop requires root")
        with self.mutex:
            if (not self.state or self.state["phase"] != "listening"
                    or "record" in self.state or not self.listener_output.ready()):
                raise ValueError("Idle stop requires a verified idle listener")
            session, runner_id = self.state["session"], self.state["runner_id"]
        self.idle_runner(runner_id, "online")
        with self.mutex:
            if (self.state["session"] != session or self.state["phase"] != "listening"
                    or "record" in self.state or not self.listener_output.ready()):
                raise ValueError("Listener changed while requesting idle stop")
            self.state["idle_stop_requested"] = True
            self.save()
            self.stop_requested.set()

    @contextmanager
    def server(self, path):
        stopped = threading.Event()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            if path.exists() or path.is_symlink():
                path.unlink()
            listener.bind(str(path))
            os.chown(path, self.account.pw_uid, self.account.pw_gid)
            os.chmod(path, 0o600)
            listener.listen(8)
            listener.settimeout(0.5)

            def serve():
                while not stopped.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(5)
                        try:
                            _, uid, _ = struct.unpack(
                                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                            with connection.makefile("rb") as stream:
                                line = stream.readline(LIMIT + 1)
                            if len(line) > LIMIT or not line.endswith(b"\n"):
                                raise ValueError("Invalid handoff size")
                            message = json.loads(line)
                            self.handoff(message, uid)
                            result = {"ok": True, "defer": (
                                message["action"] != "stop-idle"
                                and self.config.get("defer_cleanup", False))}
                        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                            result = {"ok": False}
                        try:
                            connection.sendall(json.dumps(result).encode() + b"\n")
                        except OSError:
                            pass

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                yield
            finally:
                stopped.set()
                thread.join()
                path.unlink(missing_ok=True)

    def command(self, arguments, *, user=False, capture=False):
        env = {"PATH": HOST_PATH,
               "HOME": self.account.pw_dir if user else "/root", "LANG": "C.UTF-8"}
        if user:
            arguments = [self.launcher(), "-u", self.account.pw_name, "--", *arguments]
        return subprocess.run(arguments, check=True, text=True, env=env,
                              capture_output=capture, timeout=900)

    def launcher(self):
        executable = shutil.which("runuser", path=HOST_PATH)
        if not executable:
            raise ValueError("Host runuser executable is unavailable")
        return executable

    def check_idle(self):
        service = self.config["original_service"]
        if not re.fullmatch(r"actions\.runner\.[\w.-]+\.service", service):
            raise ValueError("Invalid original runner service")
        for property_name, expected in (("ActiveState", "inactive"), ("UnitFileState", "disabled")):
            value = self.command(
                ["systemctl", "show", service, "--value", "--property", property_name],
                capture=True).stdout.strip()
            if value != expected:
                raise ValueError("Original runner service is not inactive and disabled")
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                name = (process / "comm").read_text().strip()
            except FileNotFoundError:
                continue
            if name in ("Runner.Listener", "Runner.Worker", "Agent.Listener", "Agent.Worker"):
                raise ValueError("A competing runner or worker remains")

    def validate_nfs(self):
        source = self.config["nfs_package_cache"]
        if not Path(source).is_absolute():
            raise ValueError("NFS cache must be absolute")
        filesystem = self.command(
            ["stat", "--file-system", "--format=%T", "--", source], capture=True).stdout.strip()
        if filesystem not in ("nfs", "nfs4"):
            raise ValueError("Package cache is not accessible on NFS")

    def no_containers(self):
        if self.command(["docker", "ps", "--all", "--quiet"], capture=True).stdout.strip():
            raise ValueError("Dedicated Docker daemon still has containers")

    def check_workspace(self):
        if self.workspace.resolve(strict=True) != self.workspace:
            raise ValueError("Runner workspace has been redirected")

    def check_storage(self):
        mount = Path(self.config["build_mount"])
        if not mount.is_absolute() or mount.resolve(strict=True) != mount or not mount.is_mount():
            raise ValueError("Dedicated build filesystem is not mounted")
        if (self.work_root.resolve(strict=True) != self.work_root
                or not self.work_root.is_relative_to(mount)):
            raise ValueError("Canary work root must be on the dedicated build filesystem")
        if self.state and self.state["phase"] != "idle":
            identity = self.work_root.stat()
            if (identity.st_dev, identity.st_ino) != (
                    self.state["work_device"], self.state["work_inode"]):
                raise ValueError("Enrolled build storage identity changed")

    def admission(self):
        self.check_idle()
        self.no_containers()
        self.validate_nfs()
        self.check_storage()
        filesystem = os.statvfs(self.work_root)
        if filesystem.f_bavail * filesystem.f_frsize < self.config["min_free_bytes"]:
            raise ValueError("Insufficient free build disk")

    def configure(self, *, replace_stale=True):
        settings = self.runner_dir / ".runner"
        if not settings.exists():
            token = self.github.registration()
            owner = self.config["repository"].split("/")[0]
            target = owner if self.config.get("registration_scope", "repo") == "org" else self.config["repository"]
            arguments = [self.launcher(), "-u", self.account.pw_name, "--",
                         str(self.runner_dir / "config.sh"), "--unattended",
                         "--url", "https://github.com/" + target, "--token", token,
                         "--name", self.config["runner_name"], "--work", str(self.work_root),
                         "--ephemeral", "--no-default-labels", "--labels", LABEL]
            # Registration credentials and private config output never reach the job environment.
            result = subprocess.run(arguments, cwd=self.runner_dir, capture_output=True, timeout=120)
            if result.returncode:
                raise RuntimeError("Ephemeral registration failed")
        value = json.loads(settings.read_text(encoding="utf-8-sig"))
        folder = Path(value["workFolder"])
        if not folder.is_absolute():
            folder = self.runner_dir / folder
        if not value.get("ephemeral") or folder.resolve(strict=True) != self.work_root:
            raise ValueError("Registration is not ephemeral or has the wrong work root")
        try:
            runner = self.github.runner(value["agentId"])
        except GitHubAPIError as error:
            if error.status_code != 404 or not replace_stale:
                raise
            if self.state != {"phase": "idle"}:
                raise ValueError("Stale registration requires completed recovery and an idle journal") from None
            self.check_idle()
            result = subprocess.run(
                [self.launcher(), "-u", self.account.pw_name, "--",
                 str(self.runner_dir / "config.sh"), "remove", "--local"],
                cwd=self.runner_dir, capture_output=True, timeout=120)
            if result.returncode or settings.exists():
                raise RuntimeError("Stale local ephemeral registration could not be removed")
            print("Retired ephemeral registration removed locally; registering a successor", flush=True)
            return self.configure(replace_stale=False)
        if (runner["name"] != self.config["runner_name"] or runner["busy"]
                or runner["status"] != "offline"):
            raise ValueError("Registration identity is unexpected or already active")
        if {label["name"] for label in runner["labels"]} != {LABEL}:
            raise ValueError("Registration has production/default labels")
        return value["agentId"]

    def finish(self):
        self.check_idle()
        self.check_storage()
        with self.mutex:
            self.state["phase"] = "awaiting_result"
            record = self.state.get("record")
            self.save()
        if not record:
            raise ValueError("Interrupted/unenrolled session requires operator inspection")
        deadline = time.monotonic() + self.config.get("completion_timeout", 300)
        while True:
            conclusion = self.github.conclusion(
                record, self.state["runner_id"], self.config["job_name"])
            if conclusion:
                break
            if time.monotonic() >= deadline:
                raise ValueError("Final job result is not yet known; retaining pending cleanup")
            time.sleep(5)
        self.state["phase"] = "cleanup"
        self.state["conclusion"] = conclusion
        self.state.setdefault("cleanup_started", time.time())
        self.save()
        print(f"Cleaning canary session {self.state['session']}", flush=True)
        label = (f"io.sonic-buildimage.ci-job={self.config['repository']}"
                 f":{record['run_id']}:{record['run_attempt']}:vs")
        containers = self.command(
            ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", "label=" + label],
            capture=True).stdout.split()
        for container in containers:
            if not re.fullmatch(r"[0-9a-f]{64}", container):
                raise ValueError("Unexpected container identity")
            self.command(["docker", "rm", "--force", container])
        self.no_containers()
        self.check_workspace()
        checkout = checkout_path(self.workspace, record["run_id"], record["run_attempt"])
        if conclusion == "success" and record["artifact_ids"]:
            self.github.verify_artifacts(record)
            if checkout.exists():
                identity = checkout.stat()
                if (identity.st_dev, identity.st_ino) != (record["device"], record["inode"]):
                    raise ValueError("Enrolled checkout identity changed")
                cleanup_checkout(self.workspace, record["run_id"], record["run_attempt"])
        self.validate_nfs()
        if self.config.get("reclaim_caches", False):
            cleanup_caches(self.work_root / "_temp", self.workspace.parent,
                           remove_package_cache=True)
            self.command(["docker", "image", "prune", "--all", "--force"])
            self.command(["docker", "builder", "prune", "--all", "--force"])
        completed = dict(self.state, phase="complete", cleanup_finished=time.time())
        atomic_json(self.state_dir / (self.state["session"] + ".json"), completed)
        self.state = {"phase": "idle"}
        self.save()

    def recover(self):
        if self.state and self.state["phase"] != "idle":
            if self.state["phase"] == "idle_stopped":
                if ("record" in self.state or self.state.get("idle_stop_proof") != IDLE_STOP_PROOF
                        or self.state.get("idle_stop_requested") is not True):
                    raise ValueError("Unverified idle stop requires operator inspection")
                self.check_idle()
                atomic_json(self.state_dir / (self.state["session"] + ".json"), self.state)
                self.state = {"phase": "idle"}
                self.save()
            elif self.state["phase"] == "not_started" and not self.state.get("record"):
                self.check_idle()
                atomic_json(self.state_dir / (self.state["session"] + ".json"), self.state)
                self.state = {"phase": "idle"}
                self.save()
            else:
                self.finish()

    def interrupt_listener(self, process):
        def descendant(pid):
            seen = set()
            while pid > 1 and pid not in seen:
                if pid == process.pid:
                    return True
                seen.add(pid)
                fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
                pid = int(fields[1])
            return False

        def owned(entry):
            return ((entry / "comm").read_text().strip() == "Runner.Listener"
                    and entry.stat().st_uid == self.account.pw_uid
                    and descendant(int(entry.name)))

        candidates = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if owned(entry):
                    candidates.append(entry)
            except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
                continue
        if len(candidates) != 1:
            raise ValueError("Cannot uniquely identify the owned listener for idle stop")
        entry = candidates[0]
        descriptor = os.pidfd_open(int(entry.name))
        try:
            if not owned(entry):
                raise ValueError("Owned listener identity changed before idle stop")
            # runuser forwards termination and escalates after two seconds. Signal
            # the actual listener instead, allowing its dispatcher to reap workers.
            signal.pidfd_send_signal(descriptor, signal.SIGINT)
        finally:
            os.close(descriptor)

    def finish_idle_stop(self, status):
        if status or not self.listener_output.proof():
            raise ValueError("Idle stop could not prove that no job started; retaining journal")
        self.check_idle()
        self.no_containers()
        deadline = time.monotonic() + self.config.get("completion_timeout", 300)
        while self.idle_runner(self.state["runner_id"], None)["status"] != "offline":
            if time.monotonic() >= deadline:
                raise ValueError("Stopped runner is not yet offline; retaining journal")
            time.sleep(1)
        with self.mutex:
            self.state["phase"] = "idle_stopped"
            self.state["idle_stop_proof"] = dict(IDLE_STOP_PROOF)
            self.state["stop_finished"] = time.time()
            self.save()
        self.recover()
        print("Canary stopped idle; session preserved and no job data removed", flush=True)

    def cycle(self):
        self.recover()
        self.admission()
        executable = self.launcher()
        runner_id = self.configure()
        identity = self.work_root.stat()
        with self.mutex:
            self.state = {"phase": "starting", "session": uuid.uuid4().hex,
                          "runner_id": runner_id, "started": time.time(),
                          "work_device": identity.st_dev, "work_inode": identity.st_ino}
            self.save()
        print(f"Starting canary session {self.state['session']}", flush=True)
        environment = {"HOME": self.account.pw_dir, "LANG": "C.UTF-8",
                       "PATH": HOST_PATH}
        self.listener_output = ListenerOutput()
        try:
            process = subprocess.Popen(
                [executable, "-u", self.account.pw_name, "--", str(self.runner_dir / "run.sh")],
                cwd=self.runner_dir, env=environment, start_new_session=True, umask=0o022,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        except OSError as error:
            # Failed exec proves no listener started; interrupted listeners remain ambiguous.
            with self.mutex:
                self.state["phase"] = "not_started"
                self.state["spawn_error"] = type(error).__name__
                self.save()
            raise RuntimeError(f"Canary listener could not start ({type(error).__name__})") from None
        with process as listener:
            with self.mutex:
                self.state["phase"] = "listening"
                self.save()
            reader = threading.Thread(target=self.listener_output.read,
                                      args=(listener.stdout,), daemon=True)
            reader.start()
            try:
                interrupted = False
                stop_deadline = None
                while True:
                    try:
                        status = listener.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        if self.stop_requested.is_set() and not interrupted:
                            self.interrupt_listener(listener)
                            interrupted = True
                            stop_deadline = time.monotonic() + 120
                        if stop_deadline is not None and time.monotonic() >= stop_deadline:
                            raise ValueError("Idle stop timed out; retaining pending journal")
            finally:
                if listener.poll() is None:
                    listener.terminate()
                    listener.wait()
                reader.join(timeout=5)
                listener.stdout.close()
        if reader.is_alive():
            raise ValueError("Listener output did not close; retaining pending journal")
        if status and not self.stop_requested.is_set():
            raise ValueError("Listener exited unsuccessfully; cleanup requires recovery")
        with self.mutex:
            idle_stop = self.stop_requested.is_set() and "record" not in self.state
            self.state["phase"] = "verifying_idle_stop" if idle_stop else "awaiting_result"
            self.state["listener_exited"] = time.time()
            self.save()
        if idle_stop:
            self.finish_idle_stop(status)
        else:
            self.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/sonic-vs-cleanup/config.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--stop-idle", action="store_true")
    args = parser.parse_args()
    if args.stop_idle and (args.once or args.recover_only):
        parser.error("--stop-idle cannot be combined with --once or --recover-only")
    if os.geteuid() != 0:
        parser.exit(1, "The host supervisor must run as root\n")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    try:
        trusted(ROOT, directory=True)
        trusted(Path(__file__).resolve())
        config = json.loads(trusted(args.config).read_text())
        state_dir = trusted(Path("/var/lib/sonic-vs-cleanup"), directory=True)
        runtime = trusted(Path("/run/sonic-vs-cleanup"), directory=True)
        if args.stop_idle:
            request({"action": "stop-idle"}, runtime / "control.sock", timeout=120)
            print("Idle stop requested; wait for the service to become inactive", flush=True)
            return
        with (state_dir / "supervisor.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            supervisor = Supervisor(config, state_dir)
            if args.recover_only:
                supervisor.recover()
            else:
                with supervisor.server(runtime / "control.sock"):
                    while True:
                        supervisor.cycle()
                        if args.once or supervisor.stop_requested.is_set():
                            break
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, RuntimeError) as error:
        detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        parser.exit(1, f"VS supervisor stopped: {detail}; retain private journal before retry\n")


if __name__ == "__main__":
    main()
