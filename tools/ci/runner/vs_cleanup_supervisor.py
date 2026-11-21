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
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.cleanup_runner_bazel_cache import cleanup as cleanup_caches
from tools.bazel.ci.cleanup_vs_workspace import cleanup as cleanup_checkout
from tools.bazel.ci.cleanup_vs_workspace import checkout_path


LABEL = "vs-cleanup-canary"
LIMIT = 65536
FINAL_RESULTS = {"success", "failure", "cancelled", "timed_out", "action_required",
                 "neutral", "skipped", "stale"}


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
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Host GitHub API request failed (HTTP {error.code})") from None
        except Exception:
            # Do not include headers, credentials or HTTP response bodies in logs.
            raise RuntimeError("Host GitHub API request failed") from None

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
        self.mutex = threading.Lock()
        self.github = github or GitHub(config)

    def save(self):
        atomic_json(self.journal, self.state)

    def handoff(self, message, uid):
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
                            self.handoff(json.loads(line), uid)
                            result = {"ok": True, "defer": self.config.get("defer_cleanup", False)}
                        except (OSError, ValueError, KeyError, TypeError):
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
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "HOME": self.account.pw_dir if user else "/root", "LANG": "C.UTF-8"}
        if user:
            arguments = ["runuser", "-u", self.account.pw_name, "--", *arguments]
        return subprocess.run(arguments, check=True, text=True, env=env,
                              capture_output=capture, timeout=900)

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

    def configure(self):
        settings = self.runner_dir / ".runner"
        if not settings.exists():
            token = self.github.registration()
            owner = self.config["repository"].split("/")[0]
            target = owner if self.config.get("registration_scope", "repo") == "org" else self.config["repository"]
            arguments = ["runuser", "-u", self.account.pw_name, "--",
                         str(self.runner_dir / "config.sh"), "--unattended",
                         "--url", "https://github.com/" + target, "--token", token,
                         "--name", self.config["runner_name"], "--work", str(self.work_root),
                         "--ephemeral", "--no-default-labels", "--labels", LABEL]
            # Registration credentials and private config output never reach the job environment.
            result = subprocess.run(arguments, cwd=self.runner_dir, capture_output=True, timeout=120)
            if result.returncode:
                raise RuntimeError("Ephemeral registration failed")
        value = json.loads(settings.read_text())
        folder = Path(value["workFolder"])
        if not folder.is_absolute():
            folder = self.runner_dir / folder
        if not value.get("ephemeral") or folder.resolve(strict=True) != self.work_root:
            raise ValueError("Registration is not ephemeral or has the wrong work root")
        runner = self.github.runner(value["agentId"])
        if (runner["name"] != self.config["runner_name"] or runner["busy"]
                or runner["status"] != "offline"):
            raise ValueError("Registration identity is unexpected or already active")
        if {label["name"] for label in runner["labels"]} != {LABEL}:
            raise ValueError("Registration has production/default labels")
        return value["agentId"]

    def finish(self):
        self.check_idle()
        self.check_storage()
        record = self.state.get("record")
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

    def cycle(self):
        if self.state and self.state["phase"] != "idle":
            self.finish()
        self.admission()
        runner_id = self.configure()
        identity = self.work_root.stat()
        self.state = {"phase": "listening", "session": uuid.uuid4().hex,
                      "runner_id": runner_id, "started": time.time(),
                      "work_device": identity.st_dev, "work_inode": identity.st_ino}
        self.save()
        print(f"Starting canary session {self.state['session']}", flush=True)
        environment = {"HOME": self.account.pw_dir, "LANG": "C.UTF-8",
                       "PATH": "/usr/local/bin:/usr/bin:/bin"}
        with subprocess.Popen(
                ["runuser", "-u", self.account.pw_name, "--", str(self.runner_dir / "run.sh")],
                cwd=self.runner_dir, env=environment, start_new_session=True) as listener:
            try:
                status = listener.wait()
            finally:
                if listener.poll() is None:
                    listener.terminate()
                    listener.wait()
        if status:
            raise ValueError("Listener exited unsuccessfully; cleanup requires recovery")
        self.state["listener_exited"] = time.time()
        self.save()
        self.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/sonic-vs-cleanup/config.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--recover-only", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "The host supervisor must run as root\n")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    try:
        trusted(ROOT, directory=True)
        trusted(Path(__file__).resolve())
        config = json.loads(trusted(args.config).read_text())
        state_dir = trusted(Path("/var/lib/sonic-vs-cleanup"), directory=True)
        runtime = trusted(Path("/run/sonic-vs-cleanup"), directory=True)
        with (state_dir / "supervisor.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            supervisor = Supervisor(config, state_dir)
            if args.recover_only:
                if supervisor.state and supervisor.state["phase"] != "idle":
                    supervisor.finish()
            else:
                with supervisor.server(runtime / "control.sock"):
                    while True:
                        supervisor.cycle()
                        if args.once:
                            break
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, RuntimeError) as error:
        detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        parser.exit(1, f"VS supervisor stopped: {detail}; retain private journal before retry\n")


if __name__ == "__main__":
    main()
