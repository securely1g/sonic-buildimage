"""Read core SONiC service health inside an already booted single-ASIC VS guest."""

import json
from pathlib import Path
import subprocess

SERVICES = ("database", "swss", "syncd", "bgp")
MARKER = "SONIC_GUEST_VALIDATION_JSON:"


def command(arguments):
    try:
        result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10)
        return {"returncode": result.returncode, "output": result.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"returncode": -1, "output": str(error)}


def critical_status(declarations, supervisor):
    """Use sonic-mgmt's program/group declarations, rejecting missing entries."""
    expected = []
    for line in declarations.splitlines():
        if not line.strip():
            continue
        kind, separator, name = line.strip().partition(":")
        if separator != ":" or kind not in ("program", "group") or not name or ":" in name:
            raise ValueError("Invalid critical-process declaration: " + line)
        expected.append((kind, name))
    if not expected:
        raise ValueError("No critical processes declared")
    actual = {}
    for line in supervisor.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] not in ("RUNNING", "EXITED", "STOPPED", "FATAL", "BACKOFF", "STARTING", "UNKNOWN"):
            raise ValueError("Invalid supervisor status: " + line)
        if fields[0] in actual:
            raise ValueError("Duplicate supervisor process: " + fields[0])
        actual[fields[0]] = fields[1]
    selected = {}
    for kind, name in expected:
        matches = [process for process in actual if process == name] if kind == "program" else [process for process in actual if process.startswith(name + ":")]
        if not matches:
            raise ValueError("Missing critical " + kind + ": " + name)
        selected.update({process: actual[process] for process in matches})
    return selected


def probe(run=command):
    result = {"schema_version": 1, "services": {}, "status": "passed"}
    for service in SERVICES:
        checks = {
            "systemd": run(["systemctl", "is-active", service + ".service"]),
            "container": run(["docker", "inspect", "--format", "{{.State.Running}}", service]),
            "declarations": run(["docker", "exec", service, "cat", "/etc/supervisor/critical_processes"]),
            "supervisor": run(["docker", "exec", service, "supervisorctl", "status"]),
        }
        errors = []
        for name in ("systemd", "container", "declarations"):
            if checks[name]["returncode"] != 0:
                errors.append(name + " command failed")
        # supervisorctl may return nonzero for an unrelated one-shot process;
        # the declared critical processes must all exist and be RUNNING.
        if checks["systemd"]["output"] != "active":
            errors.append("systemd unit is not active")
        if checks["container"]["output"] != "true":
            errors.append("container is not running")
        try:
            processes = critical_status(checks["declarations"]["output"], checks["supervisor"]["output"])
            errors.extend(name + " is " + state for name, state in processes.items() if state != "RUNNING")
        except ValueError as error:
            processes = {}
            errors.append(str(error))
        result["services"][service] = {"checks": checks, "critical_processes": processes, "errors": errors}
        if errors:
            result["status"] = "failed"
    result["database_ping"] = run(["docker", "exec", "database", "redis-cli", "ping"])
    if result["database_ping"] != {"returncode": 0, "output": "PONG"}:
        result["status"] = "failed"
    version = Path("/etc/sonic/sonic_version.yml")
    result["sonic_version"] = version.read_text() if version.is_file() else None
    return result


if __name__ == "__main__":
    print(MARKER + json.dumps(probe(), separators=(",", ":")), flush=True)
